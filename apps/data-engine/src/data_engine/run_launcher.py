"""Dagster run launcher with runtime bounds and gRPC worker health checks."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

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

RUN_WORKER_LOST = "run worker lost"
CONTRACT_BOUND_TAG = "truealpha/contract_max_runtime"
MAX_RUNTIME_ALIAS_TAG = "dagster/max_runtime_seconds"
#: The universe of a run. One job serves universes of 20 and of 500 issuers, so the measured
#: run times of one universe do not bound a run of another.
UNIVERSE_TAG = "truealpha/universe"


def max_runtime_tags(
    job_name: str, instance: dg.DagsterInstance | None = None, *, universe: str | None = None
) -> dict[str, str]:
    """The bound of a run: margin x the longest SUCCESS run of the same job and the same universe."""
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
                tags={UNIVERSE_TAG: universe} if universe else None,
            )
        )
        durations = [
            record.end_time - record.start_time
            for record in records
            if record.start_time is not None and record.end_time is not None and record.end_time > record.start_time
        ]
        if not durations:
            return {}
        bound = contract.derive_max_runtime_seconds(max(durations))
        return {MAX_RUNTIME_SECONDS_TAG: str(bound), CONTRACT_BOUND_TAG: str(bound)}
    except Exception as error:
        logger.warning("could not derive max runtime for job %s: %r", job_name, error)
        return {}


def config_universe(run_config: Mapping[str, Any]) -> str | None:
    """The universe that an op config of the run names, or None for a job of one universe."""
    for op in (run_config.get("ops") or {}).values():
        if universe := ((op or {}).get("config") or {}).get("universe"):
            return str(universe)
    return None


def launch_tags(
    job_name: str,
    run_tags: Mapping[str, str],
    run_config: Mapping[str, Any],
    instance: dg.DagsterInstance | None = None,
) -> dict[str, str]:
    """The tags the launcher adds: the universe of the run config, then the bound of that universe.

    The launcher, not each schedule or sensor, names the universe. A schedule that sets no tag
    (the confidence report, #1839) and a manual launch get the bound of their own universe.
    """
    universe = run_tags.get(UNIVERSE_TAG) or config_universe(run_config)
    named = {UNIVERSE_TAG: universe} if universe and UNIVERSE_TAG not in run_tags else {}
    return named | bound_tags_to_write(job_name, {**run_tags, **named}, instance=instance)


def bound_tags_to_write(
    job_name: str, run_tags: Mapping[str, str], instance: dg.DagsterInstance | None = None
) -> dict[str, str]:
    if MAX_RUNTIME_ALIAS_TAG in run_tags:
        return {}
    present = run_tags.get(MAX_RUNTIME_SECONDS_TAG)
    if present is not None and present != run_tags.get(CONTRACT_BOUND_TAG):
        return {}
    return max_runtime_tags(job_name, instance=instance, universe=run_tags.get(UNIVERSE_TAG))


class MonitoredRunLauncher(DefaultRunLauncher):
    def launch_run(self, context: LaunchRunContext) -> None:
        run = context.dagster_run
        if tags := launch_tags(run.job_name, run.tags, run.run_config or {}, instance=self._instance):
            self._instance.add_run_tags(run.run_id, tags)
        super().launch_run(context)

    @property
    def supports_check_run_worker_health(self) -> bool:
        return True

    def check_run_worker_health(self, run: DagsterRun) -> CheckRunHealthResult:
        grpc_info = run.tags.get(GRPC_INFO_TAG)
        if grpc_info is None:
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
            # Keep RUNNING on unreachable server so Dagster still enforces max_runtime.
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
