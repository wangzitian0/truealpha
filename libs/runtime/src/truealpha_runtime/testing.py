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

import hashlib
import importlib.util
import os
import re
import subprocess
import sys
import threading
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql
from truealpha_contracts.models import RawCapture, RawIngestionEnvelope, RawObjectRef

#: One SQL literal that a seed row may hold: a quoted string, a whole number, null, true or false.
SeedValue = str | int | bool | None

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


_SEED_TOKEN = re.compile(
    r"""\s*(?:
        (?P<comment>--[^\n]*)
      | (?P<string>'(?:[^']|'')*')
      | (?P<number>-?\d+)
      | (?P<word>null|true|false)\b
      | (?P<punct>[(),])
    )""",
    re.IGNORECASE | re.VERBOSE,
)


def read_seed_rows(migration: Path, table: str) -> list[dict[str, SeedValue]]:
    """The rows one migration seeds into `table`, read from the file text without a database.

    It reads the single `insert into <table> (<columns>) values (<row>), ... on conflict`
    statement. A row holds only quoted strings, whole numbers, null, true and false. Any
    other token raises ValueError, so a seed the reader cannot parse never reads as fewer
    rows. Two statements for the same table raise too: a seed lives in one place.
    """
    text = migration.read_text(encoding="utf-8")
    statement = re.compile(
        rf"insert\s+into\s+{re.escape(table)}\s*\((?P<columns>[^)]*)\)\s*values(?P<body>.*?)\bon\s+conflict",
        re.IGNORECASE | re.DOTALL,
    )
    found = list(statement.finditer(text))
    if len(found) != 1:
        raise ValueError(f"{migration.name} holds {len(found)} seed statements for {table}; expected exactly 1")
    columns = [column.strip() for column in found[0].group("columns").split(",")]
    body = found[0].group("body")
    rows: list[dict[str, SeedValue]] = []
    current: list[SeedValue] | None = None
    position = 0
    while position < len(body) and body[position:].strip():
        token = _SEED_TOKEN.match(body, position)
        if token is None:
            raise ValueError(f"{migration.name}: cannot read the seed of {table} near {body[position:][:40]!r}")
        position = token.end()
        kind = token.lastgroup
        value = token.group(kind) if kind else ""
        if kind == "comment":
            continue
        if kind == "punct":
            if value == "(" and current is None:
                current = []
            elif value == ")" and current is not None:
                if len(current) != len(columns):
                    raise ValueError(
                        f"{migration.name}: a seed row of {table} has {len(current)} values, not {len(columns)}"
                    )
                rows.append(dict(zip(columns, current, strict=True)))
                current = None
            elif value != ",":
                raise ValueError(f"{migration.name}: unexpected {value!r} in the seed of {table}")
            continue
        if current is None:
            raise ValueError(f"{migration.name}: a value outside a row in the seed of {table}")
        if kind == "string":
            current.append(value[1:-1].replace("''", "'"))
        elif kind == "number":
            current.append(int(value))
        else:
            current.append(None if value.lower() == "null" else value.lower() == "true")
    if current is not None:
        raise ValueError(f"{migration.name}: the seed of {table} ends inside a row")
    return rows


class InMemoryRawObjectStore:
    """In-memory implementation of RawObjectStore for unit and integration tests.

    Stores payloads in an in-memory dictionary rather than transmitting to S3/MinIO.
    """

    def __init__(self, bucket: str = "truealpha-raw") -> None:
        self.bucket = bucket
        self.objects: dict[str, bytes] = {}

    def store(self, capture: RawCapture) -> RawIngestionEnvelope:
        digest = hashlib.sha256(capture.body).hexdigest()
        key = f"raw/{capture.source.value}/{digest[:2]}/{digest}"
        self.objects[key] = capture.body
        return RawIngestionEnvelope(
            source=capture.source,
            source_record_id=capture.source_record_id,
            object=RawObjectRef(
                bucket=self.bucket,
                key=key,
                sha256=digest,
                byte_length=len(capture.body),
                content_type=capture.content_type,
            ),
            fetched_at=capture.fetched_at,
            source_published_at=capture.source_published_at,
            metadata=capture.metadata,
        )

    def get(self, ref: RawObjectRef) -> bytes:
        if ref.key not in self.objects:
            raise KeyError(f"object not found in memory store: {ref.key}")
        return self.objects[ref.key]


class IsolatedDatabase(str):
    """Database URL string with `.name` attribute identifying the database."""

    name: str

    def __new__(cls, url: str, name: str) -> "IsolatedDatabase":
        instance = super().__new__(cls, url)
        instance.name = name
        return instance


def _admin_url_for(database_url: str) -> str:
    base = urlsplit(database_url)
    return urlunsplit((base.scheme, base.netloc, "/postgres", base.query, ""))


def _named_url_for(database_url: str, dbname: str) -> str:
    base = urlsplit(database_url)
    return urlunsplit((base.scheme, base.netloc, f"/{dbname}", base.query, ""))


def _dbname_for(database_url: str) -> str:
    return urlsplit(database_url).path.lstrip("/")


@contextmanager
def isolated_test_database(
    name_prefix: str = "test",
    *,
    template: str | None = "truealpha",
    database_url: str | None = None,
    connect_timeout: int = 3,
) -> Iterator[IsolatedDatabase]:
    """Provide a fresh, isolated PostgreSQL database for test execution.

    When `template` is supplied and exists with migrated relations (e.g. CI or local
    dev where `truealpha` is already initialized), the database is cloned via:
        `CREATE DATABASE <name> TEMPLATE <template>`
    which runs in <0.2s by copying data pages directly.

    If `template` does not exist or lacks migrated tables, creates a new database
    and applies `apply_migration_chain(target_url)` as a fallback.

    On context exit, all connections to the database are terminated and the
    database is dropped with FORCE.
    """
    import pytest

    base_url = database_url or os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/truealpha")
    admin_url = _admin_url_for(base_url)
    name = f"truealpha_{name_prefix}_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    target_url = _named_url_for(base_url, name)

    try:
        admin_conn = psycopg.connect(admin_url, connect_timeout=connect_timeout, autocommit=True)
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get(REQUIRE_RUNTIME_ENV):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        skip_or_fail(f"no local Postgres; CI runs the required integration coverage ({error})")
        return

    try:
        cloned = False
        if template is not None:
            template_name = _dbname_for(template) if "://" in template else template
            # Verify template database exists
            exists = admin_conn.execute("select 1 from pg_database where datname = %s", (template_name,)).fetchone()
            if exists:
                # Terminate any idle backends holding locks on template
                admin_conn.execute(
                    "select pg_terminate_backend(pid) from pg_stat_activity where datname = %s and pid != pg_backend_pid()",
                    (template_name,),
                )
                try:
                    admin_conn.execute(
                        sql.SQL("create database {} template {}").format(
                            sql.Identifier(name), sql.Identifier(template_name)
                        )
                    )
                    cloned = True
                except psycopg.Error:
                    cloned = False

        if not cloned:
            admin_conn.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
            apply_migration_chain(target_url)

        yield IsolatedDatabase(target_url, name)
    finally:
        try:
            admin_conn.execute(
                "select pg_terminate_backend(pid) from pg_stat_activity where datname = %s and pid != pg_backend_pid()",
                (name,),
            )
            admin_conn.execute(sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(name)))
        finally:
            admin_conn.close()


@contextmanager
def clone_test_database(
    source_database: str,
    name_prefix: str = "clone",
    *,
    connect_timeout: int = 3,
) -> Iterator[IsolatedDatabase]:
    """Clone an existing database as a new isolated throwaway database.

    Terminates active connections on source before cloning to prevent concurrency conflicts.
    Drops the clone on context exit.
    """
    base_url = (
        source_database
        if "://" in source_database
        else os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/truealpha")
    )
    source_name = _dbname_for(source_database) if "://" in source_database else source_database
    admin_url = _admin_url_for(base_url)
    clone_name = f"truealpha_{name_prefix}_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    clone_url = _named_url_for(base_url, clone_name)

    admin_conn = psycopg.connect(admin_url, connect_timeout=connect_timeout, autocommit=True)
    try:
        admin_conn.execute(
            "select pg_terminate_backend(pid) from pg_stat_activity where datname = %s and pid != pg_backend_pid()",
            (source_name,),
        )
        admin_conn.execute(
            sql.SQL("create database {} template {}").format(sql.Identifier(clone_name), sql.Identifier(source_name))
        )
        yield IsolatedDatabase(clone_url, clone_name)
    finally:
        try:
            admin_conn.execute(
                "select pg_terminate_backend(pid) from pg_stat_activity where datname = %s and pid != pg_backend_pid()",
                (clone_name,),
            )
            admin_conn.execute(sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(clone_name)))
        finally:
            admin_conn.close()
