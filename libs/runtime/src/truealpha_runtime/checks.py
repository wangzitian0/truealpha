from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum

import psycopg

from truealpha_runtime.config import RuntimeSettings
from truealpha_runtime.storage import S3RawObjectStore


class DependencyStatus(StrEnum):
    PRESENT = "present"
    ABSENT = "absent"


@dataclass(frozen=True)
class ProbeResult:
    name: str
    status: DependencyStatus
    detail: str
    duration_ms: float

    @property
    def present(self) -> bool:
        return self.status is DependencyStatus.PRESENT


class DatabaseCheck:
    name = "database"

    def __init__(self, settings: RuntimeSettings) -> None:
        self.settings = settings

    def probe(self) -> ProbeResult:
        started = time.perf_counter()
        try:
            with psycopg.connect(
                self.settings.database_url,
                connect_timeout=self.settings.database_connect_timeout_seconds,
            ) as connection:
                connection.execute("select 1").fetchone()
            return ProbeResult(self.name, DependencyStatus.PRESENT, "SELECT 1 succeeded", _elapsed(started))
        except Exception as exc:  # noqa: BLE001 - a probe reports absence instead of raising
            return ProbeResult(self.name, DependencyStatus.ABSENT, str(exc), _elapsed(started))


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
                    self.name, DependencyStatus.ABSENT, "Postgres KG tables are missing", _elapsed(started)
                )
            return ProbeResult(self.name, DependencyStatus.PRESENT, "Postgres KG tables present", _elapsed(started))
        except Exception as exc:  # noqa: BLE001
            return ProbeResult(self.name, DependencyStatus.ABSENT, str(exc), _elapsed(started))


class ObjectStorageCheck:
    name = "object_storage"

    def __init__(self, settings: RuntimeSettings, *, store: S3RawObjectStore | None = None) -> None:
        self.store = store or S3RawObjectStore(settings)

    def probe(self) -> ProbeResult:
        started = time.perf_counter()
        try:
            self.store.ensure_bucket(create=False)
            return ProbeResult(self.name, DependencyStatus.PRESENT, "bucket accessible", _elapsed(started))
        except Exception as exc:  # noqa: BLE001
            return ProbeResult(self.name, DependencyStatus.ABSENT, str(exc), _elapsed(started))


def run_dependency_checks(settings: RuntimeSettings) -> tuple[ProbeResult, ...]:
    return (
        DatabaseCheck(settings).probe(),
        GraphStoreCheck(settings).probe(),
        ObjectStorageCheck(settings).probe(),
    )


def _elapsed(started: float) -> float:
    return (time.perf_counter() - started) * 1000
