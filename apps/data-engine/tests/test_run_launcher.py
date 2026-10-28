"""The run launcher of the deployed instance fails a run whose worker is gone (#1442).

A container restart kills the code server and every run process under it. The new code server
does not know these runs, so no process writes a failure. Before #1442 such a run stayed
`STARTED` until the instance-wide limit (3 hours) ended it. Staging showed three such runs on
2026-10-20 after the code server restarted at 08:26 UTC.

The tests use the `run_launcher` and `run_monitoring` blocks of the real `dagster.yaml`, so a
change of either block reaches them. The storage is a temporary SQLite instance, and the code
server is a real `dagster api grpc` process that the test kills.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from dagster import DagsterEventType, DagsterInstance, DagsterRunStatus
from dagster._core.events import DagsterEvent
from dagster._core.events.log import EventLogEntry
from dagster._core.launcher import WorkerStatus
from dagster._core.storage.tags import GRPC_INFO_TAG, MAX_RUNTIME_SECONDS_TAG
from dagster._core.test_utils import create_run_for_test, freeze_time, instance_for_test
from dagster._core.workspace.context import WorkspaceProcessContext
from dagster._core.workspace.load_target import EmptyWorkspaceTarget, GrpcServerTarget
from dagster._daemon.monitoring.run_monitoring import execute_run_monitoring_iteration
from dagster._grpc.client import DagsterGrpcClient
from data_engine.run_launcher import (
    CONTRACT_BOUND_TAG,
    MAX_RUNTIME_ALIAS_TAG,
    RUN_WORKER_LOST,
    MonitoredRunLauncher,
    bound_tags_to_write,
    max_runtime_tags,
)

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
from run_seed import seed_success_run  # noqa: E402

DAGSTER_YAML = Path(__file__).resolve().parents[1] / "dagster.yaml"
LOCATION_FILE = Path(__file__).resolve().parent / "support" / "run_launcher_location.py"
LOCATION_NAME = "run_launcher_test"
WAITING_JOB_NAME = "run_launcher_waiting_job"
#: Seconds that one wait for the code server or for a run status may take.
STARTUP_SECONDS = 90

logger = logging.getLogger(__name__)


def _deployed_monitoring_overrides() -> dict[str, Any]:
    """The `run_launcher` and `run_monitoring` blocks of the real `dagster.yaml`.

    A block that the file omits stays at the Dagster default, as it does in the deployed instance.
    """
    config = yaml.safe_load(DAGSTER_YAML.read_text(encoding="utf-8"))
    return {key: config[key] for key in ("run_launcher", "run_monitoring") if key in config}


@pytest.fixture
def instance() -> Iterator[DagsterInstance]:
    with instance_for_test(overrides=_deployed_monitoring_overrides()) as test_instance:
        yield test_instance


def _wait_until(condition: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + STARTUP_SECONDS
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"{what} did not happen in {STARTUP_SECONDS} seconds")
        time.sleep(0.2)


class CodeServer:
    """A `dagster api grpc` process on a Unix socket, in its own process group.

    The run processes of the server are in the same group. `kill` stops all of them with
    SIGKILL, as a container restart does.
    """

    def __init__(self, socket_path: str) -> None:
        self.socket_path = socket_path
        self.process: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "dagster",
                "api",
                "grpc",
                "--python-file",
                str(LOCATION_FILE),
                "--socket",
                self.socket_path,
                "--location-name",
                LOCATION_NAME,
            ],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        client = DagsterGrpcClient(socket=self.socket_path, host="localhost")
        _wait_until(lambda: self._answers(client), "the code server start")

    def _answers(self, client: DagsterGrpcClient) -> bool:
        assert self.process is not None
        if self.process.poll() is not None:
            raise AssertionError(f"the code server stopped with exit code {self.process.returncode}")
        try:
            client.ping("ready")
        except Exception:
            return False
        return True

    def kill(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.process.wait(timeout=STARTUP_SECONDS)


@pytest.fixture
def socket_path() -> Iterator[str]:
    # A Unix socket path must stay short (104 bytes on macOS), so the directory is under /tmp.
    directory = tempfile.mkdtemp(prefix="ta1442-", dir="/tmp")
    yield os.path.join(directory, "code-server.sock")
    shutil.rmtree(directory, ignore_errors=True)


def _launch(instance: DagsterInstance, workspace_context: WorkspaceProcessContext, tags: dict[str, str]) -> str:
    workspace = workspace_context.create_request_context()
    location = workspace.get_code_location(LOCATION_NAME)
    remote_job = location.get_repository("__repository__").get_full_job(WAITING_JOB_NAME)
    run = instance.create_run_for_job(
        job_def=_waiting_job_definition(),
        remote_job_origin=remote_job.get_remote_origin(),
        job_code_origin=remote_job.get_python_origin(),
        tags=tags,
    )
    instance.launch_run(run.run_id, workspace)
    return run.run_id


def _waiting_job_definition() -> Any:
    sys.path.insert(0, str(LOCATION_FILE.parent))
    try:
        from run_launcher_location import run_launcher_waiting_job
    finally:
        sys.path.remove(str(LOCATION_FILE.parent))
    return run_launcher_waiting_job


def _status(instance: DagsterInstance, run_id: str) -> DagsterRunStatus:
    run = instance.get_run_by_id(run_id)
    assert run is not None
    return run.status


def _tags(instance: DagsterInstance, run_id: str) -> dict[str, str]:
    run = instance.get_run_by_id(run_id)
    assert run is not None
    return dict(run.tags)


def _failure_messages(instance: DagsterInstance, run_id: str) -> list[str]:
    return [entry.message for entry in instance.all_logs(run_id, of_type=DagsterEventType.RUN_FAILURE)]


def _monitor_once(workspace_context: WorkspaceProcessContext) -> None:
    """One poll of the run monitor, as the daemon does it."""
    list(execute_run_monitoring_iteration(workspace_context, logger))


def test_the_deployed_instance_selects_the_monitored_launcher(instance: DagsterInstance) -> None:
    assert isinstance(instance.run_launcher, MonitoredRunLauncher)
    assert instance.run_launcher.supports_check_run_worker_health is True
    assert instance.run_monitoring_enabled is True
    # 0 resume attempts: a lost run fails. The launcher cannot resume a run.
    assert instance.run_monitoring_max_resume_run_attempts == 0


def test_a_run_whose_code_server_restarted_fails_at_the_next_monitor_poll(
    instance: DagsterInstance, socket_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kill the code server and its run processes, start a new one, and poll the monitor once."""
    seed_success_run(instance, WAITING_JOB_NAME, 1800.0)
    expected_bound = 3600

    server = CodeServer(socket_path)
    server.start()
    try:
        target = GrpcServerTarget(host="localhost", socket=socket_path, port=None, location_name=LOCATION_NAME)
        with WorkspaceProcessContext(instance, target) as workspace_context:
            bounded = _launch(instance, workspace_context, tags={})
            operator_set = _launch(instance, workspace_context, tags={MAX_RUNTIME_SECONDS_TAG: "86400"})
            for run_id in (bounded, operator_set):
                _wait_until(lambda run_id=run_id: _status(instance, run_id) == DagsterRunStatus.STARTED, "run start")

            # A live code server lists both runs: the monitor leaves them running.
            _monitor_once(workspace_context)
            assert _status(instance, bounded) == DagsterRunStatus.STARTED
            assert _status(instance, operator_set) == DagsterRunStatus.STARTED

            server.kill()
            server.start()
            _monitor_once(workspace_context)

            for run_id in (bounded, operator_set):
                assert _status(instance, run_id) == DagsterRunStatus.FAILURE
                messages = _failure_messages(instance, run_id)
                assert any(RUN_WORKER_LOST in message for message in messages), messages
                assert any(socket_path in message for message in messages), messages

            # The launch wrote the contract bound, and kept the bound that the operator set.
            assert _tags(instance, bounded).get(MAX_RUNTIME_SECONDS_TAG) == str(expected_bound)
            assert _tags(instance, bounded).get(CONTRACT_BOUND_TAG) == str(expected_bound)
            assert _tags(instance, operator_set).get(MAX_RUNTIME_SECONDS_TAG) == "86400"
            assert CONTRACT_BOUND_TAG not in _tags(instance, operator_set)
    finally:
        server.kill()


def _started_run(instance: DagsterInstance, tags: dict[str, str], started_at: datetime) -> str:
    """A run in `STARTED` whose RUN_START event has the time `started_at`."""
    run = create_run_for_test(instance, job_name=WAITING_JOB_NAME, status=DagsterRunStatus.STARTING, tags=tags)
    with freeze_time(started_at):
        instance.handle_new_event(
            EventLogEntry(
                error_info=None,
                level=logging.DEBUG,
                user_message="",
                run_id=run.run_id,
                timestamp=started_at.timestamp(),
                dagster_event=DagsterEvent(DagsterEventType.RUN_START.value, job_name=WAITING_JOB_NAME),
            )
        )
    assert _status(instance, run.run_id) == DagsterRunStatus.STARTED
    return run.run_id


def test_a_code_server_that_does_not_answer_leaves_the_run_to_its_maximum_runtime(
    instance: DagsterInstance,
) -> None:
    """No answer proves nothing. The run stays STARTED until it passes its bound, then it fails."""
    absent = {"host": "localhost", "socket": "/tmp/ta1442-no-such-dir/code-server.sock"}
    now = datetime.now(UTC)
    tags = {GRPC_INFO_TAG: json.dumps(absent), MAX_RUNTIME_SECONDS_TAG: "600"}
    young = _started_run(instance, tags, started_at=now - timedelta(seconds=60))
    old = _started_run(instance, tags, started_at=now - timedelta(seconds=601))

    run = instance.get_run_by_id(young)
    assert run is not None
    health = instance.run_launcher.check_run_worker_health(run)
    assert health.status == WorkerStatus.RUNNING
    assert "did not answer" in (health.msg or "")

    with WorkspaceProcessContext(instance, EmptyWorkspaceTarget()) as workspace_context:
        _monitor_once(workspace_context)

    assert _status(instance, young) == DagsterRunStatus.STARTED
    assert _status(instance, old) == DagsterRunStatus.FAILURE
    assert _failure_messages(instance, old), "the run failed without a RUN_FAILURE event"


def test_a_run_without_a_code_server_address_is_left_to_its_maximum_runtime(instance: DagsterInstance) -> None:
    run_id = _started_run(instance, {}, started_at=datetime.now(UTC))
    run = instance.get_run_by_id(run_id)
    assert run is not None

    assert instance.run_launcher.check_run_worker_health(run).status == WorkerStatus.RUNNING


def test_a_run_with_an_unreadable_code_server_address_is_left_to_its_maximum_runtime(
    instance: DagsterInstance,
) -> None:
    """A health check that raises would skip the runtime check at every poll. It must not raise."""
    run_id = _started_run(instance, {GRPC_INFO_TAG: "not json"}, started_at=datetime.now(UTC))
    run = instance.get_run_by_id(run_id)
    assert run is not None

    assert instance.run_launcher.check_run_worker_health(run).status == WorkerStatus.RUNNING


#: A job of the deployed catalog with a measured bound.
BOUNDED_JOB = "head_reports_pipeline"


def _contract_tags(instance: DagsterInstance) -> dict[str, str]:
    bound = max_runtime_tags(BOUNDED_JOB, instance=instance)[MAX_RUNTIME_SECONDS_TAG]
    return {MAX_RUNTIME_SECONDS_TAG: str(bound), CONTRACT_BOUND_TAG: str(bound)}


@pytest.mark.parametrize(
    ("run_tags", "written"),
    [
        pytest.param({}, "contract", id="new run"),
        pytest.param({MAX_RUNTIME_SECONDS_TAG: "86400"}, "nothing", id="operator bound"),
        pytest.param({MAX_RUNTIME_ALIAS_TAG: "86400"}, "nothing", id="operator bound through the alias"),
        pytest.param(
            {MAX_RUNTIME_SECONDS_TAG: "999", CONTRACT_BOUND_TAG: "999"},
            "contract",
            id="re-execution copies an old contract bound",
        ),
        pytest.param(
            {MAX_RUNTIME_SECONDS_TAG: "86400", CONTRACT_BOUND_TAG: "999"},
            "nothing",
            id="operator bound on a re-execution",
        ),
    ],
)
def test_the_launch_keeps_an_operator_bound_and_renews_a_copied_contract_bound(
    instance: DagsterInstance, run_tags: dict[str, str], written: str
) -> None:
    """A re-execution copies its parent's tags (#1442 review). Only an operator's bound may stay."""
    seed_success_run(instance, BOUNDED_JOB, 1800.0)
    expected = _contract_tags(instance) if written == "contract" else {}

    assert bound_tags_to_write(BOUNDED_JOB, run_tags, instance=instance) == expected
