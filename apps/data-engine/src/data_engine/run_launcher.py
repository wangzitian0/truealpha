"""The run launcher of the deployed Dagster instance (#1442).

`apps/data-engine/dagster.yaml` selects this class. The daemon, the webserver and the code server
load it from the same image.

Dagster's `DefaultRunLauncher` starts each run as a child process of the code server. A container
restart (a deploy, a crash) kills that child. The new code server does not know the run, so no
process writes a failure, and the run stays `STARTED`. The run monitor ends such a run only through
a worker health check, and `DefaultRunLauncher` has none. Before #1442 only the instance-wide
`max_runtime_seconds` (3 hours, #1271) ended these runs.

`MonitoredRunLauncher` adds two things:

1. `launch_run` writes the job's maximum runtime to the run tag `dagster/max_runtime`. The value
   comes from successful runs of the job in `dagster.runs` over the policy window (#1522). The run
   monitor fails a run that exceeds it. A job with no successful run in the window gets no tag, and
   the instance limit (3 hours) applies. An operator can set `dagster/max_runtime` (or Dagster's
   alias `dagster/max_runtime_seconds`) at launch to run a job longer than its bound by intent.
   The launcher keeps that value. A re-execution copies every tag of its parent run, so the
   launcher also writes `truealpha/contract_max_runtime`. A copied bound that equals this record
   came from the contract, and the launcher replaces it with the bound derived from history.
2. `check_run_worker_health` asks the code server at the run's address whether it still executes
   the run (`GetCurrentRuns`). A code server that answers and does not list the run proves that the
   run worker is gone: the run monitor marks the run failed at its next poll. A code server that
   does not answer proves nothing. The run then stays `STARTED`, and its maximum runtime bounds it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

import dagster as dg
from dagster._core.launcher import CheckRunHealthResult, LaunchRunContext, WorkerStatus
from dagster._core.launcher.default_run_launcher import DefaultRunLauncher
from dagster._core.storage.dagster_run import DagsterRun
from dagster._core.storage.tags import GRPC_INFO_TAG, MAX_RUNTIME_SECONDS_TAG
from dagster._grpc.client import DagsterGrpcClient
from dagster._grpc.types import GetCurrentRunsResult
from dagster._serdes import deserialize_value
from truealpha_contracts.environment_contract import load_environment_contract

logger = logging.getLogger(__name__)

#: The start of the failure message of a run whose code server lost it. Tests and log searches match it.
RUN_WORKER_LOST = "run worker lost"
#: The bound that this launcher wrote from the contract. It tells a copied bound from an operator's.
CONTRACT_BOUND_TAG = "truealpha/contract_max_runtime"
#: Dagster reads this alias when `dagster/max_runtime` is absent. This launcher never writes it.
MAX_RUNTIME_ALIAS_TAG = "dagster/max_runtime_seconds"


def max_runtime_tags(job_name: str, instance: dg.DagsterInstance | None = None) -> dict[str, str]:
    """Return the run tags that bound a run of `job_name`, or no tag when no successful run is in the window (#1442, #1522)."""
    if instance is None:
        return {}
    try:
        contract = load_environment_contract()
        cutoff = datetime.now(UTC) - timedelta(days=contract.job_runtime_bounds.window_days)
        records = instance.get_run_records(
            filters=dg.RunsFilter(
                job_name=job_name,
                statuses=[dg.DagsterRunStatus.SUCCESS],
                created_after=cutoff,
            )
        )
        durations = [
            record.end_time - record.start_time
            for record in records
            if record.start_time is not None and record.end_time is not None and record.end_time > record.start_time
        ]
        if not durations:
            return {}
        longest_seconds = max(durations)
        bound = contract.derive_max_runtime_seconds(longest_seconds)
        return {MAX_RUNTIME_SECONDS_TAG: str(bound), CONTRACT_BOUND_TAG: str(bound)}
    except Exception as error:
        logger.warning("could not derive max runtime for job %s: %r", job_name, error)
        return {}


def bound_tags_to_write(
    job_name: str,
    run_tags: Mapping[str, str],
    instance: dg.DagsterInstance | None = None,
) -> dict[str, str]:
    """Return the tags that `launch_run` writes. An operator's bound stays; any other bound is derived."""
    if MAX_RUNTIME_ALIAS_TAG in run_tags:
        return {}
    present = run_tags.get(MAX_RUNTIME_SECONDS_TAG)
    if present is not None and present != run_tags.get(CONTRACT_BOUND_TAG):
        return {}
    return max_runtime_tags(job_name, instance=instance)


class MonitoredRunLauncher(DefaultRunLauncher):
    """`DefaultRunLauncher` with a derived maximum runtime per job and a run worker health check."""

    def launch_run(self, context: LaunchRunContext) -> None:
        run = context.dagster_run
        tags = bound_tags_to_write(run.job_name, run.tags, instance=self._instance)
        if tags:
            self._instance.add_run_tags(run.run_id, tags)
        super().launch_run(context)

    @property
    def supports_check_run_worker_health(self) -> bool:
        return True

    def check_run_worker_health(self, run: DagsterRun) -> CheckRunHealthResult:
        grpc_info = run.tags.get(GRPC_INFO_TAG)
        if grpc_info is None:
            # No launcher of this instance started the run. Its maximum runtime bounds it.
            return CheckRunHealthResult(WorkerStatus.RUNNING, "the run has no code server address")
        where = grpc_info
        try:
            address = json.loads(grpc_info)
            where = address.get("socket") or f"{address.get('host')}:{address.get('port')}"
            client = DagsterGrpcClient(
                port=address.get("port"),
                socket=address.get("socket"),
                host=address.get("host", "localhost"),
                use_ssl=bool(address.get("use_ssl", False)),
            )
            result = deserialize_value(client.get_current_runs(), GetCurrentRunsResult)
        except Exception as error:
            # The Dagster run monitor fails a run on any status other than RUNNING or SUCCESS, and it
            # skips the runtime check when this method raises. A code server that does not answer (it
            # restarts, or it is slow) must not fail a live run, and the runtime check must still run.
            logger.warning("code server %s did not answer for run %s: %r", where, run.run_id, error)
            return CheckRunHealthResult(WorkerStatus.RUNNING, f"code server {where} did not answer")
        if result.serializable_error_info is not None:
            logger.warning("code server %s returned an error for run %s", where, run.run_id)
            return CheckRunHealthResult(WorkerStatus.RUNNING, f"code server {where} returned an error")
        if run.run_id in result.current_runs:
            return CheckRunHealthResult(WorkerStatus.RUNNING)
        return CheckRunHealthResult(
            WorkerStatus.NOT_FOUND,
            f"{RUN_WORKER_LOST}: the code server at {where} does not execute run {run.run_id}. "
            "The code server restarted, or the run process stopped without a failure event.",
        )
