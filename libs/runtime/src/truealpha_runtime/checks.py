from __future__ import annotations

import time

import psycopg
from infra2_sdk.runtime.postgres import PostgresSettings, _redact_error, probe_postgres
from infra2_sdk.runtime.health import DependencyStatus, ProbeResult
from infra2_sdk.runtime.s3 import probe_s3

from truealpha_runtime.config import RuntimeSettings
from truealpha_runtime.storage import S3RawObjectStore


def _redacted_detail(exc: Exception, settings: RuntimeSettings) -> str:
    """Describe a database error without the DSN or the password.

    Return only the error type when the Postgres settings cannot be built.
    No settings object exists then, and the message can echo the DSN.
    """
    try:
        postgres_settings = PostgresSettings(
            dsn=settings.database_url,
            connect_timeout_seconds=settings.database_connect_timeout_seconds,
        )
    except Exception:  # noqa: BLE001
        return type(exc).__name__
    return f"{type(exc).__name__}: {_redact_error(str(exc), postgres_settings)}"


class DatabaseCheck:
    name = "database"

    def __init__(self, settings: RuntimeSettings) -> None:
        self.settings = settings

    def probe(self) -> ProbeResult:
        started = time.perf_counter()
        try:
            postgres_settings = PostgresSettings(
                dsn=self.settings.database_url,
                connect_timeout_seconds=self.settings.database_connect_timeout_seconds,
            )
            return probe_postgres(postgres_settings)
        except Exception as exc:  # noqa: BLE001
            return ProbeResult(
                self.name,
                DependencyStatus.ABSENT,
                _redacted_detail(exc, self.settings),
                (time.perf_counter() - started) * 1000,
            )


class GraphStoreCheck:
    name = "graph_store"

    def __init__(self, settings: RuntimeSettings) -> None:
        self.settings = settings

    def probe(self) -> ProbeResult:
        started = time.perf_counter()
        try:
            with psycopg.connect(
                self.settings.database_url,
                connect_timeout=self.settings.database_connect_timeout_seconds,
            ) as connection:
                row = connection.execute(
                    "select to_regclass('staging.kg_edges'), to_regclass('staging.kg_identifiers')"
                ).fetchone()
            if row is None or any(table is None for table in row):
                return ProbeResult(
                    self.name,
                    DependencyStatus.ABSENT,
                    "Postgres KG tables are missing",
                    (time.perf_counter() - started) * 1000,
                )
            return ProbeResult(
                self.name,
                DependencyStatus.PRESENT,
                "Postgres KG tables present",
                (time.perf_counter() - started) * 1000,
            )
        except Exception as exc:  # noqa: BLE001
            return ProbeResult(
                self.name,
                DependencyStatus.ABSENT,
                _redacted_detail(exc, self.settings),
                (time.perf_counter() - started) * 1000,
            )


class ObjectStorageCheck:
    name = "object_storage"

    def __init__(self, settings: RuntimeSettings, *, store: S3RawObjectStore | None = None) -> None:
        self.settings = settings
        self._store = store

    def probe(self) -> ProbeResult:
        started = time.perf_counter()
        try:
            store = self._store or S3RawObjectStore(self.settings)
            return probe_s3(store.s3_settings, client=store.client)
        except Exception as exc:  # noqa: BLE001
            return ProbeResult(
                self.name,
                DependencyStatus.ABSENT,
                str(exc),
                (time.perf_counter() - started) * 1000,
            )


def run_dependency_checks(settings: RuntimeSettings) -> tuple[ProbeResult, ...]:
    return (
        DatabaseCheck(settings).probe(),
        GraphStoreCheck(settings).probe(),
        ObjectStorageCheck(settings).probe(),
    )


__all__ = [
    "DatabaseCheck",
    "DependencyStatus",
    "GraphStoreCheck",
    "ObjectStorageCheck",
    "ProbeResult",
    "run_dependency_checks",
]
