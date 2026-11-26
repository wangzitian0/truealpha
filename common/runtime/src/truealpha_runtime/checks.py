from __future__ import annotations

import time
from collections.abc import Callable

import psycopg
from infra2_sdk.service import DependencyStatus, ProbeResult
from infra2_sdk.storage import PostgresSettings, probe_postgres, probe_s3, redact_postgres_error

from truealpha_runtime.config import RuntimeSettings
from truealpha_runtime.storage import S3RawObjectStore


def _pg_settings(database_url: str, connect_timeout_seconds: int = 5) -> PostgresSettings:
    return PostgresSettings(url=database_url, connect_timeout_seconds=connect_timeout_seconds)


def redact_postgres_exception(exc: Exception, *, database_url: str, connect_timeout_seconds: int = 5) -> str:
    """Scrub credentials from a PostgreSQL exception using SDK settings."""
    try:
        postgres_settings = _pg_settings(database_url, connect_timeout_seconds)
    except Exception:  # noqa: BLE001
        return type(exc).__name__
    return redact_postgres_error(exc, postgres_settings.url)


def _run_probe(name: str, fn: Callable[[], ProbeResult], on_error: Callable[[Exception], str]) -> ProbeResult:
    started = time.perf_counter()
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(
            name=name,
            status=DependencyStatus.ABSENT,
            detail=on_error(exc),
            duration_ms=(time.perf_counter() - started) * 1000,
        )


class DatabaseCheck:
    name = "database"

    def __init__(self, settings: RuntimeSettings) -> None:
        self.settings = settings

    def probe(self) -> ProbeResult:
        def _check() -> ProbeResult:
            pg = _pg_settings(self.settings.database_url, self.settings.database_connect_timeout_seconds)
            return probe_postgres(pg.url, connect_timeout=pg.connect_timeout_seconds, name=self.name)

        return _run_probe(
            self.name,
            _check,
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
                    name=self.name,
                    status=DependencyStatus.ABSENT,
                    detail="Postgres entity graph tables are missing",
                    duration_ms=elapsed,
                )
            return ProbeResult(
                name=self.name,
                status=DependencyStatus.PRESENT,
                detail="Postgres entity graph tables present",
                duration_ms=elapsed,
            )

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
            return probe_s3(store.s3_settings, client=store.client, name=self.name)

        return _run_probe(self.name, _check, str)


__all__ = [
    "DatabaseCheck",
    "DependencyStatus",
    "GraphStoreCheck",
    "ObjectStorageCheck",
    "ProbeResult",
    "redact_postgres_exception",
]
