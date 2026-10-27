"""data-engine test harness: the external call ledger writes to memory, never to a
warehouse (#729).

`sources.gateway` records every vendor request through a module-level writer whose
default is an autocommit INSERT into `staging.api_call_ledger`. A unit test that
exercises an adapter with a fake transport must not open a database connection as a
side effect — and must never land fake rows in a real ledger when a developer's `.env`
points at one. So every test gets a `MemoryLedger`; tests that want to assert on the
rows request the `call_ledger` fixture by name.
"""

import os

import pytest
from data_engine.sources import gateway
from data_engine.sources.landed_keys import MemoryLandedKeys


@pytest.fixture(autouse=True)
def call_ledger(monkeypatch) -> gateway.MemoryLedger:
    ledger = gateway.MemoryLedger()
    monkeypatch.setattr(gateway, "_writer", ledger)
    return ledger


@pytest.fixture(autouse=True)
def landed_keys(monkeypatch) -> MemoryLandedKeys:
    """The index of landed fetch keys (#1411 K3) lives in memory, never in a warehouse: a unit test
    must not open a database connection or read an object store as a side effect. A test that wants
    the deployed index builds `PostgresLandedKeys` over its own isolated database."""
    index = MemoryLandedKeys()
    monkeypatch.setattr(gateway, "_landed_keys", index)
    return index


@pytest.fixture(autouse=True)
def isolate_ambient_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """When DATABASE_URL is unset, data-engine tests must not connect to ambient localhost:5432 (#1197)."""
    if "DATABASE_URL" not in os.environ:
        from data_engine.config import settings

        monkeypatch.setattr(settings, "database_url", "postgresql://127.0.0.1:1/unreachable")


@pytest.fixture
def idle_moomoo_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The moomoo gate reads an empty ledger and the pacing window is empty (#1521).

    The gate reads `staging.api_call_ledger`; a unit test that runs the real `_call` over a fake
    OpenD must not open a database connection. The rows still go through `emit` into the
    `call_ledger` fixture. A test of the quota itself uses `moomoo_ledger_db` instead."""
    from data_engine.sources import moomoo_ledger

    monkeypatch.setattr(moomoo_ledger, "calls_today", lambda: 0)
    monkeypatch.setattr(moomoo_ledger, "kline_distinct_stocks_30d", lambda: set())
    moomoo_ledger._recent_calls.clear()


@pytest.fixture(scope="session")
def _moomoo_database():
    """One isolated, migrated database per test process: the replay of the migration chain costs
    seconds, so the tests that need it share it and `moomoo_ledger_db` empties the ledger for each."""
    from truealpha_runtime.testing import isolated_test_database

    with isolated_test_database("moomoo") as url:
        yield url


@pytest.fixture
def moomoo_ledger_db(_moomoo_database: str, monkeypatch: pytest.MonkeyPatch):
    """The one ledger writer and the moomoo gate over a real, empty `staging.api_call_ledger` (#1521).

    `emit` writes the warehouse and the gate reads the rows it wrote, so a quota test fails when the
    writer, the SQL or the `request_uri` form drifts. The ledger is empty at the start of each test,
    so a quota counts only this test's calls; the fixture skips where no Postgres runs and fails in CI."""
    from data_engine.config import settings
    from data_engine.sources import moomoo_ledger

    monkeypatch.setattr(settings, "database_url", _moomoo_database)
    monkeypatch.setattr(settings, "external_call_ledger", "postgres")
    monkeypatch.setattr(gateway, "_writer", gateway.postgres_writer)
    gateway._pg_reset()
    gateway._pg_execute("delete from staging.api_call_ledger")
    moomoo_ledger._recent_calls.clear()
    try:
        yield _moomoo_database
    finally:
        gateway._pg_reset()
