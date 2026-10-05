"""Harness helpers for the suites and the repository tools that reach real infrastructure.

`skip_or_fail` is the gate for integration suites that need the live runtime: CI
provisions real Postgres + MinIO precisely so those tests RUN there — a silently-skipped
suite reads as green while covering nothing — so CI sets TRUEALPHA_REQUIRE_RUNTIME=1,
turning an unreachable runtime into a hard failure; locally (no env var) the same call
skips cleanly.

`load_tool` and `apply_migration_chain` reach repository paths (`tools/`, `db/`) rather
than installed packages, which is why they live here and not behind the runtime
boundary in `truealpha_runtime.__init__`.
"""

import importlib.util
import os
import subprocess
import sys
import threading
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any

REQUIRE_RUNTIME_ENV = "TRUEALPHA_REQUIRE_RUNTIME"
REPO_ROOT = Path(__file__).resolve().parents[4]
TOOLS = REPO_ROOT / "tools"
DB_DIR = REPO_ROOT / "db"


def load_tool(name: str) -> ModuleType:
    """Import a `tools/<name>.py` script as a module.

    The scripts are executables, not a package, so reaching them needs
    `spec_from_file_location`. Nine test files each carried their own six-line
    copy of that bootstrap — four of them added during the session about
    deleting duplication. One copy, here, where the other test-only helper
    already lives.
    """
    path = TOOLS / f"{name}.py"
    if not path.exists():
        raise FileNotFoundError(f"no tool named {name!r} in {TOOLS}")
    spec = importlib.util.spec_from_file_location(f"truealpha_tool_{name}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    # Registered before exec so dataclasses in the tool can resolve their own
    # module during class creation — omitting this raises a confusing
    # AttributeError from dataclasses._is_type.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def skip_or_fail(reason: str) -> None:
    import pytest

    if os.environ.get(REQUIRE_RUNTIME_ENV, "").strip().lower() in {"1", "true", "yes", "on"}:
        pytest.fail(f"{REQUIRE_RUNTIME_ENV} is set but: {reason}", pytrace=False)
    pytest.skip(reason)


def apply_migration_chain(
    database_url: str,
    *,
    db_dir: Path = DB_DIR,
    timeout: float = 300,
    check: bool = True,
    psql_command: str | None = None,
) -> str:
    """Bring `database_url` to the declared schema, through THE applier (#984).

    `db/apply_migrations.sh` is the single implementation of "apply db/migrations/*.sql
    in glob order, then db/roles.sql, stopping on the first error". A test that needs a
    migrated database of its own calls this instead of writing the loop again. Four of
    them used to carry their own copy, and the copies had already diverged: three ran
    one `psql -f` per file, and the fourth pushed each file through psycopg and never
    applied roles.sql at all -- so a fixture whose docstring said "migrated from
    scratch" produced a schema no other environment has.

    Raises AssertionError carrying psql's own output, which is what a caller wants to
    read when a migration fails: the file, the line and the SQLSTATE. `check=False`
    returns that output instead of raising, for the one caller that replays the chain
    over a database it has deliberately broken and is asking what replay does to it.
    """
    runner = db_dir / "apply_migrations.sh"
    environment = dict(os.environ)
    # The caller named a target; an inherited admin DSN or a psql-runs-elsewhere command
    # from the surrounding shell must not redirect it somewhere else.
    environment.pop("MIGRATIONS_DATABASE_URL", None)
    environment.pop("TRUEALPHA_PSQL", None)
    environment |= {"DATABASE_URL": database_url, "TRUEALPHA_DB_DIR": str(db_dir)}
    # `psql_command` selects the applier's other transport: psql running somewhere the
    # repository path does not exist (a container), so each file arrives on stdin.
    if psql_command is not None:
        environment["TRUEALPHA_PSQL"] = psql_command
    completed = subprocess.run(
        ["sh", str(runner)],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if check and completed.returncode != 0:
        raise AssertionError(
            f"{runner} exited {completed.returncode}:\n{(completed.stdout + completed.stderr)[-8000:]}"
        )
    return completed.stdout + completed.stderr


class OtlpCollectorStub:
    """A localhost OTLP/HTTP receiver that records what a service really exports (#1034).

    Faking the exporter proves the settings, not the wiring: a service entrypoint that never
    calls `init_telemetry`, or calls it and exports under the wrong identity, would still pass.
    This stub is the other end of the wire. A subprocess that imports the real entrypoint is
    pointed at `endpoint`, does some work, flushes, and the test reads back the decoded
    resource identity of every span, metric point and log record the stub received.

    Binds `127.0.0.1` on an ephemeral port, so it never collides with an ambient collector.
    """

    SIGNALS = {"traces": "/v1/traces", "metrics": "/v1/metrics", "logs": "/v1/logs"}

    def __init__(self) -> None:
        self.requests: list[tuple[str, bytes]] = []
        stub = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - the http.server hook name
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                stub.requests.append((self.path, body))
                self.send_response(200)
                self.send_header("Content-Type", "application/x-protobuf")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return None

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def __enter__(self) -> "OtlpCollectorStub":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()

    def _decoded(self, signal: str) -> list[Any]:
        from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
        from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

        messages: dict[str, Any] = {
            "traces": ExportTraceServiceRequest,
            "metrics": ExportMetricsServiceRequest,
            "logs": ExportLogsServiceRequest,
        }
        message = messages[signal]
        return [message.FromString(body) for path, body in self.requests if path == self.SIGNALS[signal]]

    def resources(self, signal: str) -> list[dict[str, str]]:
        """The resource attributes of every resource batch received for `signal`."""
        batches = []
        for request in self._decoded(signal):
            for resource_group in getattr(request, f"resource_{'spans' if signal == 'traces' else signal}"):
                batches.append({kv.key: kv.value.string_value for kv in resource_group.resource.attributes})
        return batches

    def span_names(self) -> list[str]:
        return [
            span.name
            for request in self._decoded("traces")
            for resource_spans in request.resource_spans
            for scope_spans in resource_spans.scope_spans
            for span in scope_spans.spans
        ]

    def log_bodies(self) -> list[str]:
        return [
            record.body.string_value
            for request in self._decoded("logs")
            for resource_logs in request.resource_logs
            for scope_logs in resource_logs.scope_logs
            for record in scope_logs.log_records
        ]


def run_python_probe(
    code: str, environ: Mapping[str, str], *, timeout: float = 120
) -> subprocess.CompletedProcess[str]:
    """Run `code` in a fresh interpreter whose environment is exactly the ambient one without any
    OpenTelemetry/service-identity variable, plus `environ`.

    A fresh process is the only honest way to test an entrypoint that installs process-global
    providers and a root-logger handler on import, and the scrub keeps a developer's shell (or a
    CI job that sets `OTEL_*`) from deciding the outcome. The working directory is the repository
    root, where the services' relative manifest paths resolve.
    """
    scrubbed = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("OTEL_") and name not in {"SERVICE_NAME", "SERVICE_VERSION", "ENVIRONMENT", "ENV"}
    }
    return subprocess.run(
        [sys.executable, "-c", code],
        env={**scrubbed, **environ},
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
