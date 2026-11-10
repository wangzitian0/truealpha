# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from __future__ import annotations; import copy; import hashlib; import importlib.util; import json; import os; import re; import subprocess; import sys; import threading; import uuid; import warnings; from collections.abc import Iterator, Mapping; from contextlib import contextmanager; from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer; from pathlib import Path; from types import ModuleType; from typing import Any; from urllib.parse import urlsplit, urlunsplit; import psycopg; from infra2_sdk.runtime.environment import runtime_env_contract; from psycopg import sql; from truealpha_contracts.models import RawCapture, RawIngestionEnvelope, RawObjectRef; from truealpha_contracts.strategy import LargeModelValueV0Definition, load_live_strategy_corpus; SeedValue = str | int | bool | None; REQUIRE_RUNTIME_ENV = "TRUEALPHA_REQUIRE_RUNTIME"; REPO_ROOT = Path(__file__).resolve().parents[4]; TOOLS = REPO_ROOT / "tools"; DB_DIR = REPO_ROOT / "db"
def load_tool(name: str) -> ModuleType:
    path = TOOLS / f"{name}.py"
    if not path.exists(): raise FileNotFoundError(f"no tool named {name!r} in {TOOLS}")
    impl_path = REPO_ROOT / "common" / "runtime" / "src" / "truealpha_tools" / f"{name}.py"
    if impl_path.is_file(): return importlib.import_module(f"truealpha_tools.{name}")
    spec = importlib.util.spec_from_file_location(f"truealpha_tool_{name}", path)
    if spec is None or spec.loader is None: raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec); sys.modules[spec.name] = module; spec.loader.exec_module(module); return sys.modules.get(spec.name, module)
def skip_or_fail(reason: str) -> None:
    import pytest
    if os.environ.get(REQUIRE_RUNTIME_ENV, "").strip().lower() in {"1", "true", "yes", "on"}: pytest.fail(f"{REQUIRE_RUNTIME_ENV} is set but: {reason}", pytrace=False)
    pytest.skip(reason)
GATE_MARKER = "_truealpha_db_write_gate_installed"; DB_SKIP_GATE_MARKERS: tuple[str, ...] = ( "DB-backed test skipped: set CI=true or ALLOW_CLEAR_TEST=1", "DB write tests require CI=true or ALLOW_CLEAR_TEST=1", "DATABASE_URL must be explicitly provided for DB write tests", )
def can_write_test_database(environ: Mapping[str, str] = os.environ) -> bool: return environ.get("CI", "").strip().lower() in {"1", "true", "yes", "on"} or environ.get("ALLOW_CLEAR_TEST", "").strip() == "1"
def db_writes_allowed(environ: Mapping[str, str] = os.environ) -> bool: return can_write_test_database(environ) and bool(environ.get("DATABASE_URL", "").strip())
def install_db_write_gate() -> None:
    if getattr(psycopg.connect, GATE_MARKER, False): return
    real_connect = psycopg.connect
    def gated_connect(*args: Any, **kwargs: Any) -> Any:
        if db_writes_allowed(): return real_connect(*args, **kwargs)
        reason = f"{DB_SKIP_GATE_MARKERS[0]} and DATABASE_URL to allow database access"; warnings.warn(reason, stacklevel=2); skip_or_fail(reason); raise AssertionError("unreachable: skip_or_fail always raises")
    setattr(gated_connect, GATE_MARKER, True); setattr(gated_connect, "_real_connect", real_connect)
    psycopg.connect = gated_connect  # type: ignore[assignment]
def guard_db_write(database_url: str | None = None) -> str:
    if not can_write_test_database(): reason = f"{DB_SKIP_GATE_MARKERS[1]} to protect ambient databases"; warnings.warn(reason, stacklevel=2); skip_or_fail(reason)
    url = (database_url or os.environ.get("DATABASE_URL") or "").strip()
    if not url: reason = f"{DB_SKIP_GATE_MARKERS[2]} (no default to port 5432)"; warnings.warn(reason, stacklevel=2); skip_or_fail(reason)
    return url
def apply_migration_chain(database_url: str, *, db_dir: Path = DB_DIR, timeout: float = 300, check: bool = True, psql_command: str | None = None) -> str:
    runner = db_dir / "apply_migrations.sh"; environment = dict(os.environ); environment.pop("MIGRATIONS_DATABASE_URL", None); environment.pop("TRUEALPHA_PSQL", None); environment |= {"DATABASE_URL": database_url, "TRUEALPHA_DB_DIR": str(db_dir)}
    if psql_command is not None: environment["TRUEALPHA_PSQL"] = psql_command
    completed = subprocess.run(["sh", str(runner)], env=environment, capture_output=True, text=True, check=False, timeout=timeout)
    if check and completed.returncode != 0: raise AssertionError(f"{runner} exited {completed.returncode}:\n{(completed.stdout + completed.stderr)[-8000:]}")
    return completed.stdout + completed.stderr
class OtlpCollectorStub:
    SIGNALS = {"traces": "/v1/traces", "metrics": "/v1/metrics", "logs": "/v1/logs"}
    def __init__(self) -> None:
        self.requests: list[tuple[str, bytes]] = []; stub = self
        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None: body = self.rfile.read(int(self.headers.get("Content-Length") or 0)); stub.requests.append((self.path, body)); self.send_response(200); self.send_header("Content-Type", "application/x-protobuf"); self.send_header("Content-Length", "0"); self.end_headers()
            def log_message(self, format: str, *args: Any) -> None: return None
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler); self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
    @property
    def endpoint(self) -> str: return f"http://127.0.0.1:{self._server.server_address[1]}"
    def __enter__(self) -> OtlpCollectorStub: self._thread.start(); return self
    def __exit__(self, *exc: object) -> None: self._server.shutdown(); self._server.server_close()
    def _decoded(self, signal: str) -> list[Any]: from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest; from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest; from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest; message: dict[str, Any] = {"traces": ExportTraceServiceRequest, "metrics": ExportMetricsServiceRequest, "logs": ExportLogsServiceRequest}; return [message[signal].FromString(body) for path, body in self.requests if path == self.SIGNALS[signal]]
    def resources(self, signal: str) -> list[dict[str, str]]: return [{kv.key: kv.value.string_value for kv in rg.resource.attributes} for req in self._decoded(signal) for rg in getattr(req, f"resource_{'spans' if signal == 'traces' else signal}")]
    def span_names(self) -> list[str]: return [span.name for req in self._decoded("traces") for rs in req.resource_spans for ss in rs.scope_spans for span in ss.spans]
    def log_bodies(self) -> list[str]: return [rec.body.string_value for req in self._decoded("logs") for rl in req.resource_logs for sl in rl.scope_logs for rec in sl.log_records]
def run_python_probe(code: str, environ: Mapping[str, str], *, timeout: float = 120) -> subprocess.CompletedProcess[str]: variables: Any = runtime_env_contract()["variables"]; declared = {key for item in variables for key in (item["name"], *item["aliases"])}; scrubbed = {k: v for k, v in os.environ.items() if not k.startswith("OTEL_") and k not in declared}; return subprocess.run([sys.executable, "-c", code], env={**scrubbed, **environ}, cwd=REPO_ROOT, capture_output=True, text=True, check=False, timeout=timeout)
_SEED_TOKEN = re.compile(r"\s*(?:(?P<comment>--[^\n]*)|(?P<string>'(?:[^']|'')*')|(?P<number>-?\d+)|(?P<word>null|true|false)\b|(?P<punct>[(),]))", re.IGNORECASE)
def _seed_literal(kind: str, value: str) -> SeedValue:
    if kind == "string": return value[1:-1].replace("''", "'")
    if kind == "number": return int(value)
    return None if value.lower() == "null" else value.lower() == "true"
def _read_seed_list(where: str, columns: list[str], body: str) -> tuple[list[dict[str, SeedValue]], int]:
    rows: list[dict[str, SeedValue]] = []; current: list[SeedValue] | None = None; pos = 0
    while pos < len(body) and body[pos:].strip():
        token = _SEED_TOKEN.match(body, pos)
        if token is None: raise ValueError(f"{where}: cannot read the seed near {body[pos:][:40]!r}")
        pos = token.end(); kind = token.lastgroup or ""; value = token.group(kind) if kind else ""
        if kind == "comment" or (kind == "punct" and value == ","): continue
        if kind != "punct":
            if current is None: raise ValueError(f"{where}: a value outside a row")
            current.append(_seed_literal(kind, value))
        elif value == "(" and current is None: current = []
        elif value == ")" and current is not None:
            if len(current) != len(columns): raise ValueError(f"{where}: a seed row has {len(current)} values, not {len(columns)}")
            rows.append(dict(zip(columns, current, strict=True))); current = None
        elif value == ")": break
        else: raise ValueError(f"{where}: unexpected {value!r}")
    if current is not None: raise ValueError(f"{where}: the list ends inside a row")
    return rows, pos
def read_seed_rows(migration: Path, table: str) -> list[dict[str, SeedValue]]:
    text = migration.read_text(encoding="utf-8"); statement = re.compile(r"\bwith\s+seed\s*\((?P<columns>[^)]*)\)\s*as\s*\(\s*values\b(?P<body>.*?)\bon\s+conflict", re.IGNORECASE | re.DOTALL); insert = re.compile(rf"\binsert\s+into\s+{re.escape(table)}\s*\((?P<columns>[^)]*)\)", re.IGNORECASE); found = [m for m in statement.finditer(text) if insert.search(m.group("body"))]
    if len(found) != 1: raise ValueError(f"{migration.name} holds {len(found)} seed statements for {table}; expected exactly 1")
    columns = [c.strip() for c in found[0].group("columns").split(",")]; body = found[0].group("body"); rows, pos = _read_seed_list(f"{migration.name}: the seed of {table}", columns, body); target = insert.search(body, pos)
    if target is None: raise ValueError(f"{migration.name}: the insert into {table} is not after the seed rows")
    inserted = [c.strip() for c in target.group("columns").split(",")]
    if inserted != columns: raise ValueError(f"{migration.name}: the insert into {table} names {inserted}, but the seed names {columns}")
    return rows
class InMemoryRawObjectStore:
    def __init__(self, bucket: str = "truealpha-raw") -> None: self.bucket = bucket; self.objects: dict[str, bytes] = {}
    def store(self, capture: RawCapture) -> RawIngestionEnvelope: digest = hashlib.sha256(capture.body).hexdigest(); key = f"raw/{capture.source.value}/{digest[:2]}/{digest}"; self.objects[key] = capture.body; return RawIngestionEnvelope( source=capture.source, source_record_id=capture.source_record_id, object=RawObjectRef(bucket=self.bucket, key=key, sha256=digest, byte_length=len(capture.body), content_type=capture.content_type), fetched_at=capture.fetched_at, source_published_at=capture.source_published_at, metadata=capture.metadata, )
    def get(self, ref: RawObjectRef) -> bytes:
        if ref.key not in self.objects: raise KeyError(f"object not found in memory store: {ref.key}")
        return self.objects[ref.key]
class IsolatedDatabase(str):
    name: str
    def __new__(cls, url: str, name: str) -> IsolatedDatabase: instance = super().__new__(cls, url); instance.name = name; return instance
def _admin_url_for(database_url: str) -> str: b = urlsplit(database_url); return urlunsplit((b.scheme, b.netloc, "/postgres", b.query, ""))
def _named_url_for(database_url: str, dbname: str) -> str: b = urlsplit(database_url); return urlunsplit((b.scheme, b.netloc, f"/{dbname}", b.query, ""))
def _dbname_for(database_url: str) -> str: return urlsplit(database_url).path.lstrip("/")
@contextmanager
def isolated_test_database(name_prefix: str = "test", *, template: str | None = "truealpha", database_url: str | None = None, connect_timeout: int = 3) -> Iterator[IsolatedDatabase]:
    import pytest; base_url = guard_db_write(database_url); admin_url = _admin_url_for(base_url); name = f"truealpha_{name_prefix}_{os.getpid()}_{uuid.uuid4().hex[:8]}"; target_url = _named_url_for(base_url, name)
    try: admin_conn = psycopg.connect(admin_url, connect_timeout=connect_timeout, autocommit=True)
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get(REQUIRE_RUNTIME_ENV): pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        skip_or_fail(f"no local Postgres; CI runs the required integration coverage ({error})"); return
    try:
        cloned = False
        if template is not None:
            template_name = _dbname_for(template) if "://" in template else template
            if admin_conn.execute("select 1 from pg_database where datname = %s", (template_name,)).fetchone():
                admin_conn.execute("select pg_terminate_backend(pid) from pg_stat_activity where datname = %s and pid != pg_backend_pid()", (template_name,))
                try: admin_conn.execute(sql.SQL("create database {} template {}").format(sql.Identifier(name), sql.Identifier(template_name))); cloned = True
                except psycopg.Error: cloned = False
        if not cloned: admin_conn.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
        apply_migration_chain(target_url); yield IsolatedDatabase(target_url, name)
    finally:
        try: admin_conn.execute("select pg_terminate_backend(pid) from pg_stat_activity where datname = %s and pid != pg_backend_pid()", (name,)); admin_conn.execute(sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(name)))
        finally: admin_conn.close()
@contextmanager
def clone_test_database(source_database: str, name_prefix: str = "clone", *, connect_timeout: int = 3) -> Iterator[IsolatedDatabase]:
    base_url = guard_db_write(source_database if "://" in source_database else None); source_name = _dbname_for(source_database) if "://" in source_database else source_database; admin_url = _admin_url_for(base_url); clone_name = f"truealpha_{name_prefix}_{os.getpid()}_{uuid.uuid4().hex[:8]}"; clone_url = _named_url_for(base_url, clone_name); admin_conn = psycopg.connect(admin_url, connect_timeout=connect_timeout, autocommit=True)
    try: admin_conn.execute("select pg_terminate_backend(pid) from pg_stat_activity where datname = %s and pid != pg_backend_pid()", (source_name,)); admin_conn.execute(sql.SQL("create database {} template {}").format(sql.Identifier(clone_name), sql.Identifier(source_name))); yield IsolatedDatabase(clone_url, clone_name)
    finally:
        try: admin_conn.execute("select pg_terminate_backend(pid) from pg_stat_activity where datname = %s and pid != pg_backend_pid()", (clone_name,)); admin_conn.execute(sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(clone_name)))
        finally: admin_conn.close()
SYNTHETIC_PROVISIONAL_DEFINITION_VERSION = "0.0.1-synthetic-provisional"
def synthetic_provisional_strategy_definition() -> LargeModelValueV0Definition: payload = copy.deepcopy(load_live_strategy_corpus()["strategy_definition"]); payload["definition_version"] = SYNTHETIC_PROVISIONAL_DEFINITION_VERSION; payload["provisional_semantics"] = True; payload["tier_valuation"]["provisional_thresholds"] = True; return LargeModelValueV0Definition.model_validate_json(json.dumps(payload))
