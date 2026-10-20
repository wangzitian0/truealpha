"""The demand side of the code server memory: the run queue and the executor (#1467).

The gRPC server and every run process share the memory cgroup of one container. On 2026-10-20 the
ceiling of 768 MiB was exceeded by three processes per run, and the kernel killed the container with
every run in it. These tests hold the demand under the declared capacity:

1. `dagster.yaml` sets the run limit that `code_server_capacity` derives, and the limit fits inside
   the utilization limit of the ceiling.
2. Every deployed job uses the in-process executor, so a run is one process (`processes_per_run`).
3. A job that waits for other runs carries the tag that the queue caps below the run limit, so a run
   that it waits for always finds a slot.
4. A fresh import of the composition root fits the declared size of one process.

No test opens a database. Each test fails under its mutation: a queue limit one too high, a job on the
default executor, a waiting job without the tag, a process size below the measured import.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import dagster as dg
import pytest
import yaml
from dagster._core.errors import DagsterInvalidConfigError
from dagster._core.instance.config import dagster_instance_config
from dagster._core.instance.ref import InstanceRef
from data_engine.dagster_defs import build_definitions, defs
from data_engine.lanes.quality import WAITS_FOR_RUNS_TAG
from truealpha_contracts.environment_contract import CodeServerCapacity, load_environment_contract

DAGSTER_YAML = Path(__file__).resolve().parents[1] / "dagster.yaml"
CAPACITY: CodeServerCapacity = load_environment_contract().code_server_capacity
#: The environments that run the deployed definitions.
DEPLOYED_ENVIRONMENTS = ("production", "staging")

#: The code a fresh interpreter runs: import the composition root, print the peak resident memory in bytes.
#: `ru_maxrss` is in kilobytes on Linux and in bytes on macOS.
_PEAK_RSS_PROGRAM = (
    "import resource, sys\n"
    "import data_engine.dagster_defs\n"
    "peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss\n"
    "print(peak if sys.platform == 'darwin' else peak * 1024)\n"
)


def _queue(directory: Path = DAGSTER_YAML.parent) -> Any:
    """The run queue config of the instance that the daemon builds from the file in `directory`."""
    return InstanceRef.from_dir(str(directory)).run_coordinator.get_run_queue_config()


def _held_mib(runs: int) -> int:
    """The memory in MiB of the server processes and `runs` runs, each run at `processes_per_run` processes."""
    return (CAPACITY.server_processes + runs * CAPACITY.processes_per_run) * CAPACITY.process_mib


def _deployed_jobs() -> dict[str, list[dg.JobDefinition]]:
    return {
        "root": list(defs.resolve_all_job_defs()),
        **{env: list(build_definitions(env).resolve_all_job_defs()) for env in DEPLOYED_ENVIRONMENTS},
    }


# --- the run queue ---------------------------------------------------------------------


def test_the_queue_limit_is_the_value_that_the_declared_capacity_derives() -> None:
    assert _queue().max_concurrent_runs == CAPACITY.max_concurrent_runs


def test_the_demand_fits_inside_the_utilization_limit_of_the_ceiling() -> None:
    """The runs that the queue admits, beside the server, hold at most 70% of the ceiling (#1418)."""
    runs = _queue().max_concurrent_runs

    assert _held_mib(runs) <= CAPACITY.utilization_limit * CAPACITY.ceiling_mib, (
        f"{runs} concurrent runs hold {_held_mib(runs)} MiB of {CAPACITY.ceiling_mib} MiB:"
        f" more than {CAPACITY.utilization_limit:.0%}"
    )


def test_one_more_run_than_the_limit_would_pass_the_utilization_limit() -> None:
    """The limit is the largest that fits. A lower one would leave the capacity unused."""
    assert _held_mib(_queue().max_concurrent_runs + 1) > CAPACITY.utilization_limit * CAPACITY.ceiling_mib


def test_the_default_queue_limit_of_dagster_does_not_fit_the_ceiling(tmp_path: Path) -> None:
    """Without a `run_queue` block Dagster admits its own default. That demand exceeded the 768 MiB ceiling."""
    config = yaml.safe_load(DAGSTER_YAML.read_text(encoding="utf-8"))
    del config["run_queue"]
    (tmp_path / "dagster.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")

    dagster_default = _queue(tmp_path).max_concurrent_runs

    assert dagster_default > CAPACITY.max_concurrent_runs
    assert _held_mib(dagster_default) > CAPACITY.ceiling_mib


def test_the_run_queue_leaves_room_for_the_run_that_a_waiting_run_waits_for() -> None:
    config = _queue()
    limits = [limit for limit in config.tag_concurrency_limits if limit["key"] == WAITS_FOR_RUNS_TAG]

    assert len(limits) == 1, "exactly one limit caps the runs that wait for other runs"
    assert limits[0]["value"] == "true"
    assert limits[0]["limit"] == CAPACITY.max_waiting_runs
    assert limits[0]["limit"] < config.max_concurrent_runs


# --- the executor ----------------------------------------------------------------------


def test_every_deployed_job_runs_in_one_process() -> None:
    """The default executor starts a process for every step: the 768 MiB container held three for one job."""
    assert CAPACITY.processes_per_run == 1
    deployed = _deployed_jobs()
    assert all(len(jobs) >= 15 for jobs in deployed.values()), "the scan found too few jobs to prove anything"

    offenders = {
        scope: sorted(job.name for job in jobs if job.executor_def.name != "in_process")
        for scope, jobs in deployed.items()
    }

    assert offenders == {scope: [] for scope in deployed}


def test_the_root_default_executor_is_the_in_process_executor() -> None:
    """A job without its own executor takes the default of the root Definitions (a job added later)."""
    repository = defs.get_repository_def()

    assert repository.get_all_jobs(), "no job to inspect"
    assert {job.executor_def.name for job in repository.get_all_jobs()} == {"in_process"}


# --- the jobs that wait for other runs -------------------------------------------------


def _jobs_that_wait() -> set[str]:
    """The jobs with an op whose function calls `_await_settled`, found by the names its code refers to."""
    return {
        job.name
        for job in defs.resolve_all_job_defs()
        for node in job.graph.nodes
        if "_await_settled" in node.definition.compute_fn.decorated_fn.__code__.co_names
    }


def _jobs_with_the_waiting_tag() -> set[str]:
    return {job.name for job in defs.resolve_all_job_defs() if job.tags.get(WAITS_FOR_RUNS_TAG) == "true"}


def test_the_scan_for_waiting_jobs_finds_the_two_jobs_that_wait_today() -> None:
    """Green while empty: a scan that finds no job would pass the test below for any job set."""
    assert _jobs_that_wait() == {"output_invariants_check", "datahub_confidence_report"}


def test_a_job_that_waits_for_other_runs_has_the_waiting_tag_and_no_other_job_has_it() -> None:
    assert _jobs_with_the_waiting_tag() == _jobs_that_wait()


# --- one process and the declared size -------------------------------------------------


def test_the_declared_process_size_covers_a_fresh_import_of_the_composition_root() -> None:
    """Each run process imports the composition root. The size grows with the image (768 MiB held it in July)."""
    completed = subprocess.run(  # noqa: S603 - the interpreter of this test run, a fixed program
        [sys.executable, "-I", "-c", _PEAK_RSS_PROGRAM], capture_output=True, text=True, check=True, timeout=120
    )
    peak_mib = int(completed.stdout.strip().splitlines()[-1]) / (1024 * 1024)

    assert peak_mib <= CAPACITY.process_mib, (
        f"a process that imports data_engine.dagster_defs holds {peak_mib:.0f} MiB, above the declared"
        f" {CAPACITY.process_mib} MiB; measure the code server again and raise process_mib in"
        " environment_contract.v1.json (the queue limit follows)"
    )


# --- the file is read strictly ---------------------------------------------------------


def _rename(mapping: dict[str, Any], old: str, new: str) -> None:
    items = [(new if key == old else key, value) for key, value in mapping.items()]
    mapping.clear()
    mapping.update(items)


def _misspell_max_concurrent_runs(config: dict[str, Any]) -> None:
    _rename(config["run_queue"], "max_concurrent_runs", "max_concurrent_run")


def _misspell_tag_concurrency_limits(config: dict[str, Any]) -> None:
    _rename(config["run_queue"], "tag_concurrency_limits", "tag_concurrency_limit")


def _misspell_limit(config: dict[str, Any]) -> None:
    _rename(config["run_queue"]["tag_concurrency_limits"][0], "limit", "limits")


@pytest.mark.parametrize(
    "mutate",
    [_misspell_max_concurrent_runs, _misspell_tag_concurrency_limits, _misspell_limit],
    ids=lambda mutate: mutate.__name__,
)
def test_an_unknown_run_queue_key_fails_the_instance_config_load(
    tmp_path: Path, mutate: Callable[[dict[str, Any]], None]
) -> None:
    """A misspelled key must raise, or the queue could fall back to Dagster's limit of 10 without a sound."""
    config = yaml.safe_load(DAGSTER_YAML.read_text(encoding="utf-8"))
    assert "run_queue" in config, "dagster.yaml has no run_queue block (#1467)"
    mutate(config)
    (tmp_path / "dagster.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")

    with pytest.raises(DagsterInvalidConfigError):
        dagster_instance_config(str(tmp_path))
