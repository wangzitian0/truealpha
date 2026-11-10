# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from __future__ import annotations; import time; from typing import Any; import psycopg; from infra2_sdk.runtime.health import DependencyKind, DependencyStatus, ProbeResult, probe_orchestrator; from infra2_sdk.runtime.postgres import PostgresSettings, probe_postgres, redact_postgres_error; from infra2_sdk.runtime.s3 import probe_s3; from truealpha_runtime.config import RuntimeSettings; from truealpha_runtime.storage import S3RawObjectStore
def _redacted_detail(exc: Exception, settings: RuntimeSettings) -> str:
    try: pg = PostgresSettings(dsn=settings.database_url, connect_timeout_seconds=settings.database_connect_timeout_seconds)
    except Exception: return type(exc).__name__
    return f"{type(exc).__name__}: {redact_postgres_error(str(exc), pg)}"
class DatabaseCheck:
    name = "database"
    def __init__(self, settings: RuntimeSettings) -> None: self.settings = settings
    def probe(self) -> ProbeResult:
        started = time.perf_counter()
        try: return probe_postgres(PostgresSettings(dsn=self.settings.database_url, connect_timeout_seconds=self.settings.database_connect_timeout_seconds))
        except Exception as exc: return ProbeResult(self.name, DependencyStatus.ABSENT, _redacted_detail(exc, self.settings), (time.perf_counter() - started) * 1000)
class GraphStoreCheck:
    name = "graph_store"
    def __init__(self, settings: RuntimeSettings) -> None: self.settings = settings
    def probe(self) -> ProbeResult:
        started = time.perf_counter()
        try:
            with psycopg.connect(self.settings.database_url, connect_timeout=self.settings.database_connect_timeout_seconds) as conn: row = conn.execute("select to_regclass('staging.kg_edges'), to_regclass('staging.kg_identifiers')").fetchone()
            ok = bool(row and all(t is not None for t in row)); return ProbeResult(self.name, DependencyStatus.PRESENT if ok else DependencyStatus.ABSENT, "Postgres KG tables present" if ok else "Postgres KG tables are missing", (time.perf_counter() - started) * 1000)
        except Exception as exc: return ProbeResult(self.name, DependencyStatus.ABSENT, _redacted_detail(exc, self.settings), (time.perf_counter() - started) * 1000)
class ObjectStorageCheck:
    name = "object_storage"
    def __init__(self, settings: RuntimeSettings, *, store: S3RawObjectStore | None = None) -> None: self.settings = settings; self._store = store
    def probe(self) -> ProbeResult:
        started = time.perf_counter()
        try:
            store = self._store or S3RawObjectStore(self.settings)
            if not store._has_injected_client: store._require_endpoint()
            return probe_s3(store.s3_settings, client=store.client)
        except Exception as exc: return ProbeResult(self.name, DependencyStatus.ABSENT, str(exc), (time.perf_counter() - started) * 1000)
class OrchestratorCheck:
    name = "orchestrator"; kind = DependencyKind.ORCHESTRATOR
    def __init__(self, url: str = "http://127.0.0.1:3001/server_info", *, client: Any | None = None, settings: Any | None = None) -> None: self.url = url; self._client = client; self._settings = settings
    async def probe(self) -> ProbeResult: return await probe_orchestrator(self.url, name=self.name, client=self._client, settings=self._settings)
__all__ = ["DatabaseCheck", "DependencyStatus", "GraphStoreCheck", "ObjectStorageCheck", "OrchestratorCheck", "ProbeResult"]
