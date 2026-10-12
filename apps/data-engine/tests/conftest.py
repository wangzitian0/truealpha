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


@pytest.fixture(autouse=True)
def call_ledger(monkeypatch) -> gateway.MemoryLedger:
    ledger = gateway.MemoryLedger()
    monkeypatch.setattr(gateway, "_writer", ledger)
    return ledger


@pytest.fixture(autouse=True)
def isolate_ambient_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """When DATABASE_URL is unset, data-engine tests must not connect to ambient localhost:5432 (#1197)."""
    if "DATABASE_URL" not in os.environ:
        from data_engine.config import settings

        monkeypatch.setattr(settings, "database_url", "postgresql://127.0.0.1:1/unreachable")
