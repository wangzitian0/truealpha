from __future__ import annotations

from unittest.mock import MagicMock

import psycopg
import pytest
from botocore.exceptions import ClientError
from infra2_sdk.runtime.probes import DependencyStatus, ProbeResult
from truealpha_runtime.checks import (
    DatabaseCheck,
    GraphStoreCheck,
    ObjectStorageCheck,
    run_dependency_checks,
)
from truealpha_runtime.config import RuntimeSettings


class FakeConnection:
    def __init__(self, query_result: tuple | None = None) -> None:
        self.query_result = query_result
        self.executed_queries: list[str] = []

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        pass

    def execute(self, query: str) -> FakeConnection:
        self.executed_queries.append(query)
        return self

    def fetchone(self) -> tuple | None:
        return self.query_result


class FakeS3Client:
    def __init__(self, *, bucket_exists: bool = True) -> None:
        self.bucket_exists = bucket_exists

    def head_bucket(self, *, Bucket: str) -> dict:
        if not self.bucket_exists:
            raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadBucket")
        return {}


def test_database_check_reports_present_when_select_1_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_conn = FakeConnection(query_result=(1,))
    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: fake_conn)

    settings = RuntimeSettings(
        database_url="postgresql://user:pass@localhost:5432/testdb",
        _env_file=None,
    )
    result = DatabaseCheck(settings).probe()

    assert isinstance(result, ProbeResult)
    assert result.status is DependencyStatus.PRESENT
    assert result.present is True
    assert result.name == "database"
    assert "SELECT 1 succeeded" in result.detail
    assert result.duration_ms >= 0


def test_database_check_redacts_credentials_on_connection_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_password = "supersecretpassword123"

    def raise_connect(*args, **kwargs):
        raise psycopg.OperationalError(f"connection to server at 'localhost', password '{secret_password}' failed")

    monkeypatch.setattr(psycopg, "connect", raise_connect)

    settings = RuntimeSettings(
        database_url=f"postgresql://user:{secret_password}@localhost:5432/testdb",
        _env_file=None,
    )
    result = DatabaseCheck(settings).probe()

    assert result.status is DependencyStatus.ABSENT
    assert result.present is False
    assert result.name == "database"
    assert secret_password not in result.detail
    assert "<redacted>" in result.detail


def test_graph_store_check_reports_present_when_tables_exist(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_conn = FakeConnection(query_result=("staging.kg_edges", "staging.kg_identifiers"))
    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: fake_conn)

    settings = RuntimeSettings(
        database_url="postgresql://user:pass@localhost:5432/testdb",
        _env_file=None,
    )
    result = GraphStoreCheck(settings).probe()

    assert result.status is DependencyStatus.PRESENT
    assert result.present is True
    assert result.name == "graph_store"
    assert "present" in result.detail


def test_graph_store_check_reports_absent_when_tables_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_conn = FakeConnection(query_result=(None, "staging.kg_identifiers"))
    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: fake_conn)

    settings = RuntimeSettings(
        database_url="postgresql://user:pass@localhost:5432/testdb",
        _env_file=None,
    )
    result = GraphStoreCheck(settings).probe()

    assert result.status is DependencyStatus.ABSENT
    assert result.present is False
    assert result.name == "graph_store"
    assert "missing" in result.detail


def test_object_storage_check_reports_present_when_bucket_accessible() -> None:
    fake_client = FakeS3Client(bucket_exists=True)
    store = MagicMock()
    store.client = fake_client
    store.s3_settings = MagicMock()
    store.s3_settings.bucket = "test-bucket"

    settings = RuntimeSettings(_env_file=None)
    result = ObjectStorageCheck(settings, store=store).probe()

    assert result.status is DependencyStatus.PRESENT
    assert result.present is True
    assert result.name == "object_storage"


def test_object_storage_check_reports_absent_on_error() -> None:
    fake_client = FakeS3Client(bucket_exists=False)
    store = MagicMock()
    store.client = fake_client
    store.s3_settings = MagicMock()
    store.s3_settings.bucket = "test-bucket"

    settings = RuntimeSettings(_env_file=None)
    result = ObjectStorageCheck(settings, store=store).probe()

    assert result.status is DependencyStatus.ABSENT
    assert result.present is False
    assert result.name == "object_storage"


def test_run_dependency_checks_aggregates_all_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_conn = FakeConnection(query_result=(1,))
    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: fake_conn)

    settings = RuntimeSettings(
        database_url="postgresql://user:pass@localhost:5432/testdb",
        _env_file=None,
    )
    results = run_dependency_checks(settings)

    assert len(results) == 3
    names = {r.name for r in results}
    assert names == {"database", "graph_store", "object_storage"}
