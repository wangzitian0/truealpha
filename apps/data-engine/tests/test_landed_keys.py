"""The index of landed fetch keys outlives its process (#1411 K3).

`PostgresLandedKeys` is the deployed index. These tests run it over an isolated database and an
in-memory object store, with one connection per simulated process. A key that one process landed
is served to the next with the same bytes, a rolled-back caller transaction does not take the key
with it, and bytes that the store lost read as a miss instead of an error.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime

import psycopg
import pytest
from data_engine.datahub.fetch_key import FetchKey
from data_engine.sources import gateway
from data_engine.sources.landed_keys import PostgresLandedKeys
from truealpha_runtime.testing import InMemoryRawObjectStore, isolated_test_database

BODY = b'{"cik":320193}'
FETCHED_AT = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def database() -> Iterator[str]:
    """One isolated database for the module; each test lands its own key, so no test sees another's."""
    with isolated_test_database("landedkeys") as url:
        yield url


def _key(entity: str) -> FetchKey:
    return FetchKey("sec", "companyfacts", f"issuer:cik:{entity}", "2026-10-21")


def _process(url: str, store: InMemoryRawObjectStore) -> PostgresLandedKeys:
    """One simulated process: its own autocommit connection, like the gateway's ledger connection."""
    connection = psycopg.connect(url, autocommit=True)
    return PostgresLandedKeys(lambda: connection, connection.close, store=store)


def _land(index: PostgresLandedKeys, key: FetchKey, body: bytes = BODY) -> None:
    index.land(key, body, content_type="application/json", fetched_at=FETCHED_AT, run_key="dagster:run-1")


def test_a_key_landed_by_one_process_is_served_to_the_next_with_the_same_bytes(database: str) -> None:
    key, store = _key("0000000001"), InMemoryRawObjectStore()
    _land(_process(database, store), key)

    restarted = _process(database, store)
    assert restarted.lookup(key) == BODY
    assert restarted.lookup(key) == BODY

    with psycopg.connect(database) as reader:
        reuse, sha, run_key, fetched = reader.execute(
            """
            select k.reuse_count, k.payload_sha256, k.landed_run_key, f.byte_length
            from staging.landed_fetch_keys k join raw.fetches f on f.id = k.raw_fetch_id
            where k.canonical_entity_id = %s
            """,
            (key.canonical_entity_id,),
        ).fetchone()
    assert (reuse, run_key, fetched) == (2, "dagster:run-1", len(BODY))
    assert len(sha) == 64


def test_a_key_that_never_landed_is_a_miss_and_counts_no_reuse(database: str) -> None:
    key = _key("0000000002")
    assert _process(database, InMemoryRawObjectStore()).lookup(key) is None
    with psycopg.connect(database) as reader:
        count = reader.execute(
            "select count(*) from staging.landed_fetch_keys where canonical_entity_id = %s", (key.canonical_entity_id,)
        ).fetchone()[0]
    assert count == 0


def test_a_rolled_back_caller_transaction_does_not_take_the_landed_key_with_it(database: str) -> None:
    """The tick holds one transaction for its whole run. A failed tick rolls back; its vendor calls stay landed."""
    key, store = _key("0000000003"), InMemoryRawObjectStore()
    with psycopg.connect(database) as caller:
        caller.execute("select 1")
        _land(_process(database, store), key)
        caller.rollback()
    assert _process(database, store).lookup(key) == BODY


def test_bytes_that_the_store_lost_read_as_a_miss_and_a_new_landing_replaces_the_pointer(
    database: str, caplog: pytest.LogCaptureFixture
) -> None:
    key = _key("0000000004")
    _land(_process(database, InMemoryRawObjectStore()), key)

    empty_store = InMemoryRawObjectStore()
    with caplog.at_level(logging.ERROR, logger="data_engine.sources.landed_keys"):
        assert _process(database, empty_store).lookup(key) is None
    assert key.ledger_key in caplog.text

    replacement = _process(database, empty_store)
    _land(replacement, key, body=b'{"cik":320193,"again":true}')
    assert replacement.lookup(key) == b'{"cik":320193,"again":true}'
    with psycopg.connect(database) as reader:
        count = reader.execute(
            "select count(*) from staging.landed_fetch_keys where canonical_entity_id = %s", (key.canonical_entity_id,)
        ).fetchone()[0]
    assert count == 1


def test_the_gateway_logs_and_keeps_going_when_a_seat_has_no_raw_source(
    database: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A seat that `DataSource` does not name cannot land bytes. The vendor answer still reaches the caller."""
    monkeypatch.setattr(gateway, "_landed_keys", _process(database, InMemoryRawObjectStore()))
    key = FetchKey("no-such-seat", "x", "e", "2026-10-21")

    with caplog.at_level(logging.ERROR, logger="data_engine.sources.gateway"):
        assert gateway.fetch_once(key, lambda: b"answer") == b"answer"
    assert "landing no-such-seat|x|e|2026-10-21 failed" in caplog.text
    with psycopg.connect(database) as reader:
        count = reader.execute("select count(*) from staging.landed_fetch_keys where seat = 'no-such-seat'").fetchone()[
            0
        ]
    assert count == 0


def test_the_deployed_wiring_keeps_a_landing_through_a_rollback_and_a_dropped_connection(
    database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gateway's own autocommit connection lands the key, so the caller's transaction cannot take it.

    The connection then drops, as after a database restart; the next read opens a new one."""
    from data_engine import raw_store
    from data_engine.config import settings

    key, store = _key("0000000005"), InMemoryRawObjectStore()
    monkeypatch.setattr(settings, "database_url", database)
    monkeypatch.setattr(raw_store, "object_store", lambda: store)
    monkeypatch.setattr(gateway, "_landed_keys", PostgresLandedKeys(gateway._pg, gateway._pg_reset))
    gateway._pg_reset()
    try:
        with psycopg.connect(database) as caller:
            caller.execute("select 1")
            gateway.land(key, BODY)
            caller.rollback()
        gateway._pg().close()
        assert gateway.landed(key) == BODY
    finally:
        gateway._pg_reset()


def test_a_ledger_without_the_fetch_key_column_still_records_a_keyed_call(
    database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A code server that starts before the migration replay keeps its audit rows (deploy skew).

    The writer on the gateway's connection falls back to the unkeyed row. The writer on a caller's
    connection does it in a savepoint, so the caller's transaction is not aborted."""
    from data_engine.config import settings

    monkeypatch.setattr(settings, "database_url", database)
    record = gateway.CallRecord(
        source="sec", endpoint="e", caller="skew-test", called_at=FETCHED_AT, ok=True, fetch_key="sec|e|x|d"
    )
    gateway._pg_reset()
    with psycopg.connect(database, autocommit=True) as admin:
        admin.execute("alter table staging.api_call_ledger drop column fetch_key")
        try:
            gateway.postgres_writer(record)
            with psycopg.connect(database) as caller:
                gateway.SourceGateway(caller, caller="skew-test")._write(record)
                assert caller.execute("select 1").fetchone() == (1,), "the caller's transaction was aborted"
                caller.commit()
            rows = admin.execute("select count(*) from staging.api_call_ledger where caller = 'skew-test'").fetchone()
        finally:
            gateway._pg_reset()
            admin.execute("alter table staging.api_call_ledger add column fetch_key text")
    assert rows == (2,)
