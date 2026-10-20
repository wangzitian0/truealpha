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
   comes from the measured run times in the environment contract (`max_runtime_seconds`). The run
   monitor fails a run that exceeds it. A run that already carries the tag keeps its value: an
   operator sets it at launch to run a job longer than its bound by intent.
2. `check_run_worker_health` asks the code server at the run's address whether it still executes
   the run (`GetCurrentRuns`). A code server that answers and does not list the run proves that the
   run worker is gone: the run monitor marks the run failed at its next poll. A code server that
   does not answer proves nothing. The run then stays `STARTED`, and its maximum runtime bounds it.
"""

from __future__ import annotations

import json
import logging

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


def max_runtime_tags(job_name: str) -> dict[str, str]:
    """Return the run tag that bounds a run of `job_name`, or no tag when the contract gives no bound."""
    bound = load_environment_contract().max_runtime_seconds(job_name)
    return {} if bound is None else {MAX_RUNTIME_SECONDS_TAG: str(bound)}


class MonitoredRunLauncher(DefaultRunLauncher):
    """`DefaultRunLauncher` with a measured maximum runtime per job and a run worker health check."""

    def launch_run(self, context: LaunchRunContext) -> None:
        run = context.dagster_run
        if MAX_RUNTIME_SECONDS_TAG not in run.tags:
            tags = max_runtime_tags(run.job_name)
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
        address = json.loads(grpc_info)
        where = address.get("socket") or f"{address.get('host')}:{address.get('port')}"
        client = DagsterGrpcClient(
            port=address.get("port"),
            socket=address.get("socket"),
            host=address.get("host", "localhost"),
            use_ssl=bool(address.get("use_ssl", False)),
        )
        try:
            result = deserialize_value(client.get_current_runs(), GetCurrentRunsResult)
        except Exception as error:
            # The Dagster run monitor fails a run on any status other than RUNNING or SUCCESS. A
            # code server that does not answer (it restarts, or it is slow) must not fail a live run.
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
