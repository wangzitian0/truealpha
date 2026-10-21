"""The demand side of the code server memory: the run queue and the executor (#1467).

The gRPC server and every run process share the memory cgroup of one container. On 2026-10-20 the
ceiling of staging was exceeded by three processes per run, and the kernel killed the container with
every run in it. These tests hold the demand under the declared capacity:

1. `dagster.yaml` sets the run limit that `code_server_capacity` derives, and the limit fits inside
   the utilization limit of the ceiling.
2. Every deployed job uses the in-process executor, so a run is one process (`processes_per_run`).
3. A job that waits for other runs carries the tag that the queue caps below the run limit, so a run
   that it waits for always finds a slot.
4. A fresh code server fits the declared size of the server.

No test opens a database. Each test fails under its mutation: a queue limit one too high, a job on the
default executor, a waiting job without the tag, a server size below a fresh code server.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import dagster as dg
import pytest
import yaml
from dagster._core.errors import DagsterInvalidConfigError
from dagster._core.instance.config import dagster_instance_config
from dagster._core.instance.ref import InstanceRef
from dagster._grpc.client import DagsterGrpcClient
from data_engine.dagster_defs import build_definitions, defs, with_in_process_executor
from data_engine.lanes.quality import WAITS_FOR_RUNS_TAG
from truealpha_contracts.environment_contract import CodeServerCapacity, load_environment_contract

DAGSTER_YAML = Path(__file__).resolve().parents[1] / "dagster.yaml"
CAPACITY: CodeServerCapacity = load_environment_contract().code_server_capacity
#: The environments that run the deployed definitions.
DEPLOYED_ENVIRONMENTS = ("production", "staging")


def _queue(directory: Path = DAGSTER_YAML.parent) -> Any:
    """The run queue config of the instance that the daemon builds from the file in `directory`."""
    return InstanceRef.from_dir(str(directory)).run_coordinator.get_run_queue_config()


def _held_mib(runs: int) -> int:
    """The memory in MiB of the server processes and `runs` runs, each run at `processes_per_run` processes."""
    return CAPACITY.server_processes * CAPACITY.server_mib + runs * CAPACITY.processes_per_run * CAPACITY.run_mib


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
    """Without a `run_queue` block Dagster admits its own default. That demand exceeded the ceiling."""
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
    """The default executor starts a process for every step: one job held three."""
    assert CAPACITY.processes_per_run == 1
    deployed = _deployed_jobs()
    assert all(len(jobs) >= 15 for jobs in deployed.values()), "the scan found too few jobs to prove anything"

    offenders = {
        scope: sorted(job.name for job in jobs if job.executor_def.name != "in_process")
        for scope, jobs in deployed.items()
    }

    assert offenders == {scope: [] for scope in deployed}


@dg.op
def _probe_op() -> None:
    return None


@dg.job
def _probe_job() -> None:
    """A job without an executor of its own, as a lane author would add one."""
    _probe_op()


def test_a_job_without_an_executor_of_its_own_takes_the_in_process_executor_from_the_root() -> None:
    """The control resolves to Dagster's default; the root helper turns it into the in-process executor."""
    control = dg.Definitions(jobs=[_probe_job]).resolve_all_job_defs()
    deployed = with_in_process_executor(dg.Definitions(jobs=[_probe_job])).resolve_all_job_defs()

    assert [job.executor_def.name for job in control] == ["multi_or_in_process_executor"]
    assert [job.executor_def.name for job in deployed] == ["in_process"]


# --- the jobs that wait for other runs -------------------------------------------------


def _reaches(function: types.FunctionType, name: str, seen: set[types.CodeType] | None = None) -> bool:
    """Whether `function` refers to `name`, itself or through functions of its own package that it names.

    The code object holds every global name the function uses, its nested functions and its lambdas."""
    seen = set() if seen is None else seen
    return _code_reaches(function.__code__, function.__globals__, function.__module__.split(".")[0], name, seen)


def _code_reaches(
    code: types.CodeType, namespace: dict[str, Any], package: str, name: str, seen: set[types.CodeType]
) -> bool:
    if code in seen:
        return False
    seen.add(code)
    if name in code.co_names:
        return True
    if any(
        isinstance(const, types.CodeType) and _code_reaches(const, namespace, package, name, seen)
        for const in code.co_consts
    ):
        return True
    return any(
        isinstance(target := namespace.get(referenced), types.FunctionType)
        and target.__module__.split(".")[0] == package
        and _code_reaches(target.__code__, target.__globals__, package, name, seen)
        for referenced in code.co_names
    )


def _jobs_that_wait() -> set[str]:
    """The jobs with an op whose function reaches `_await_settled`, directly or through a helper."""
    return {
        job.name
        for job in defs.resolve_all_job_defs()
        for node in job.graph.nodes
        if _reaches(node.definition.compute_fn.decorated_fn, "_await_settled")
    }


def _jobs_with_the_waiting_tag() -> set[str]:
    return {job.name for job in defs.resolve_all_job_defs() if job.tags.get(WAITS_FOR_RUNS_TAG) == "true"}


def _waits() -> None:
    _await_settled()


def _waits_through_a_helper() -> None:
    _helper()


def _helper() -> None:
    _await_settled()


def _waits_in_a_lambda() -> None:
    (lambda: _await_settled())()


def _does_not_wait() -> None:
    _helper_that_does_not_wait()


def _helper_that_does_not_wait() -> None:
    return None


def _await_settled() -> None:
    return None


@pytest.mark.parametrize(
    ("function", "waits"),
    [(_waits, True), (_waits_through_a_helper, True), (_waits_in_a_lambda, True), (_does_not_wait, False)],
    ids=lambda value: getattr(value, "__name__", str(value)),
)
def test_the_scan_follows_a_wait_through_a_helper_and_a_lambda(function: types.FunctionType, waits: bool) -> None:
    assert _reaches(function, "_await_settled") is waits


def test_the_scan_for_waiting_jobs_finds_the_two_jobs_that_wait_today() -> None:
    """Green while empty: a scan that finds no job would pass the test below for any job set."""
    assert _jobs_that_wait() == {"output_invariants_check", "datahub_confidence_report"}


def test_a_job_that_waits_for_other_runs_has_the_waiting_tag_and_no_other_job_has_it() -> None:
    assert _jobs_with_the_waiting_tag() == _jobs_that_wait()


# --- one process and the declared size -------------------------------------------------


def _peak_rss_mib(pid: int) -> float:
    """Peak resident memory of process `pid` in MiB: `VmHWM` of `/proc/<pid>/status` on Linux.

    `ru_maxrss` of a child is wrong here: Linux carries the high-water mark of the parent across fork and
    exec, so a child of a 387 MiB pytest process reads 387 MiB (a Linux container measured 420 MB for a
    child of a 400 MB parent whose own `VmHWM` was 166 MiB). Without `/proc` (macOS) the current RSS from
    `ps` stands in, for a local run only; CI on Linux is the authority."""
    status = Path(f"/proc/{pid}/status")
    if status.exists():
        line = next(line for line in status.read_text().splitlines() if line.startswith("VmHWM:"))
        return int(line.split()[1]) / 1024
    return (
        int(subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True, check=True).stdout)
        / 1024
    )


def _fresh_code_server_peak_mib() -> float:
    """Start `dagster api grpc -m data_engine.dagster_defs` as the deployed pid 1 starts, wait until it answers
    a ping, and return its peak resident memory. The interpreter and the packages are the ones of this venv."""
    with tempfile.TemporaryDirectory(dir="/tmp") as directory:
        socket_path = str(Path(directory) / "s.sock")
        server = subprocess.Popen(  # noqa: S603 - the interpreter of this test run, fixed arguments
            [sys.executable, "-I", "-m", "dagster", "api", "grpc", "-m", "data_engine.dagster_defs"]
            + ["--socket", socket_path, "--lazy-load-user-code", "--log-level", "warning"]
        )
        try:
            client = DagsterGrpcClient(socket=socket_path)
            deadline = time.monotonic() + 120
            while True:
                try:
                    client.ping("capacity")
                    break
                except Exception:  # noqa: BLE001 - the server is still starting
                    if server.poll() is not None or time.monotonic() > deadline:
                        raise
                    time.sleep(0.5)
            return _peak_rss_mib(server.pid)
        finally:
            server.terminate()
            server.wait(timeout=30)


def test_the_declared_process_size_covers_a_fresh_code_server() -> None:
    """The deployed pid 1 held 229.5 MiB (`VmHWM`, staging, 2026-10-20). This starts the same command in this
    venv and reads the same counter. A podman Linux container with the image layout (`uv sync --frozen --no-dev
    --no-editable`) read 211.5 MiB for it. The size grows with the image; `server_mib` must follow."""
    peak_mib = _fresh_code_server_peak_mib()

    assert peak_mib <= CAPACITY.server_mib, (
        f"a fresh code server holds {peak_mib:.0f} MiB, above the declared {CAPACITY.server_mib} MiB;"
        " measure the code server again and raise server_mib in environment_contract.v1.json"
        " (the queue limit follows)"
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
