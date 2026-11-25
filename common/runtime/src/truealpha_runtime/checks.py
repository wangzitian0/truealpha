from __future__ import annotations

import time
from collections.abc import Callable

import psycopg
from infra2_sdk.runtime.health import DependencyStatus, ProbeResult
from infra2_sdk.runtime.postgres import PostgresSettings, probe_postgres, redact_postgres_error
from infra2_sdk.runtime.s3 import probe_s3

from truealpha_runtime.config import RuntimeSettings
from truealpha_runtime.storage import S3RawObjectStore


def _pg_settings(database_url: str, connect_timeout_seconds: int = 5) -> PostgresSettings:
    return PostgresSettings(dsn=database_url, connect_timeout_seconds=connect_timeout_seconds)


def redact_postgres_exception(exc: Exception, *, database_url: str, connect_timeout_seconds: int = 5) -> str:
    """Scrub credentials from a PostgreSQL exception using SDK settings."""
    try:
        postgres_settings = _pg_settings(database_url, connect_timeout_seconds)
    except Exception:  # noqa: BLE001
        return type(exc).__name__
    return f"{type(exc).__name__}: {redact_postgres_error(str(exc), postgres_settings)}"


def _run_probe(name: str, fn: Callable[[], ProbeResult], on_error: Callable[[Exception], str]) -> ProbeResult:
    started = time.perf_counter()
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(name, DependencyStatus.ABSENT, on_error(exc), (time.perf_counter() - started) * 1000)


class DatabaseCheck:
    name = "database"

    def __init__(self, settings: RuntimeSettings) -> None:
        self.settings = settings

    def probe(self) -> ProbeResult:
        return _run_probe(
            self.name,
            lambda: probe_postgres(
                _pg_settings(self.settings.database_url, self.settings.database_connect_timeout_seconds)
            ),
            lambda exc: redact_postgres_exception(
                exc,
                database_url=self.settings.database_url,
                connect_timeout_seconds=self.settings.database_connect_timeout_seconds,
            ),
        )


class GraphStoreCheck:
    name = "graph_store"

    def __init__(self, settings: RuntimeSettings) -> None:
        self.settings = settings

    def probe(self) -> ProbeResult:
        started = time.perf_counter()

        def _check() -> ProbeResult:
            with psycopg.connect(
                self.settings.database_url, connect_timeout=self.settings.database_connect_timeout_seconds
            ) as connection:
                row = connection.execute(
                    "select to_regclass('staging.entities'), to_regclass('staging.entity_aliases'), "
                    "to_regclass('staging.entity_relations')"
                ).fetchone()
            elapsed = (time.perf_counter() - started) * 1000
            if row is None or any(table is None for table in row):
                return ProbeResult(
                    self.name, DependencyStatus.ABSENT, "Postgres entity graph tables are missing", elapsed
                )
            return ProbeResult(self.name, DependencyStatus.PRESENT, "Postgres entity graph tables present", elapsed)

        return _run_probe(
            self.name,
            _check,
            lambda exc: redact_postgres_exception(
                exc,
                database_url=self.settings.database_url,
                connect_timeout_seconds=self.settings.database_connect_timeout_seconds,
            ),
        )


class ObjectStorageCheck:
    name = "object_storage"

    def __init__(self, settings: RuntimeSettings, *, store: S3RawObjectStore | None = None) -> None:
        self.settings = settings
        self._store = store

    def probe(self) -> ProbeResult:
        def _check() -> ProbeResult:
            store = self._store or S3RawObjectStore(self.settings)
            store._require_endpoint()
            return probe_s3(store.s3_settings, client=store.client)

        return _run_probe(self.name, _check, str)


__all__ = [
    "DatabaseCheck",
    "DependencyStatus",
    "GraphStoreCheck",
    "ObjectStorageCheck",
    "ProbeResult",
    "redact_postgres_exception",
]
