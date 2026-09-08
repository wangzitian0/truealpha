"""#770: staging.strategy_backtest_inputs.input_key is registry-backed, not CHECK-enumerated.

Migration 0032 enforced `input_key` with `CHECK (input_key in (...))`; adding a metric
therefore meant a migration before the writer could land it, which is exactly what
init.md rule 22 forbids ("adding a metric is a registry edit, not a migration"). Migration
20260908T1027 drops that CHECK, and `seed_strategy_inputs_from_capture` now validates
`input_key` against `truealpha_contracts.metrics` before every insert instead
(`is_registered_input_key`).

Skips without a local Postgres; ci-python/ci-db run it migrated.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import psycopg
import pytest
from data_engine.config import settings
from truealpha_contracts.metrics import METRICS, MetricSpec, UnitFamily
from truealpha_contracts.models import DataSource


@pytest.fixture
def connection():
    try:
        active = psycopg.connect(settings.database_url, connect_timeout=3, autocommit=False)
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    try:
        yield active
    finally:
        active.rollback()
        active.close()


def test_a_never_enumerated_metric_lands_with_no_migration(connection) -> None:
    """The direct proof of #770's acceptance criterion. Before this PR, landing a new
    metric meant widening the enumerated CHECK in a migration -- 0032 enumerated the
    original six, and 0039 already had to add `net_income`/`earnings_cagr_3y` to it the
    same way. A synthetic key that has never been enumerated by ANY migration must insert
    exactly as cleanly as those, with no migration accompanying this test."""
    cutoff = datetime(2026, 1, 1, tzinfo=UTC)
    connection.execute(
        """
        insert into staging.strategy_backtest_inputs
            (issuer_id, cutoff_at, input_key, value, confidence, knowable_at)
        values (%s, %s, %s, %s, %s, %s)
        """,
        ("issuer:test:770", cutoff, "synthetic_test_metric_770", "1.23", "0.9", cutoff),
    )
    row = connection.execute(
        "select value from staging.strategy_backtest_inputs where issuer_id = %s and input_key = %s",
        ("issuer:test:770", "synthetic_test_metric_770"),
    ).fetchone()
    assert row is not None and str(row[0]) == "1.23"


class _FakeCursor:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple]:
        return self._rows


class _FakeConnection:
    """A minimal stand-in for `psycopg.Connection` that hands `seed_strategy_inputs_from_capture`
    one fabricated financial-fact observation and records every INSERT it is asked to run --
    no real Postgres needed for this unit-level guard."""

    def __init__(self, select_rows: list[tuple]) -> None:
        self._select_rows = select_rows
        self.inserts: list[tuple] = []

    def execute(self, sql: str, params: tuple = ()) -> _FakeCursor:
        if sql.strip().lower().startswith("select"):
            return _FakeCursor(self._select_rows)
        assert sql.strip().lower().startswith("insert"), sql
        self.inserts.append(params)
        return _FakeCursor([])


def test_seed_strategy_inputs_rejects_an_unregistered_input_key(monkeypatch) -> None:
    """The writer's registry-backed validation is what replaces the dropped CHECK
    (#770 finding 1) -- it must refuse an input key nothing in
    `truealpha_contracts.metrics.METRICS` recognizes, the same way the CHECK used to,
    before it ever reaches the database."""
    import data_engine.datahub.strategy_bridge as strategy_bridge

    cutoff = datetime(2026, 1, 1, tzinfo=UTC)
    payload = {
        "issuer_id": "issuer:test:770c",
        "listing_id": "listing:test:770c",
        "not_a_registered_metric": "1.0",
    }
    fake = _FakeConnection([("financial-fact", "0.9", payload, cutoff)])
    monkeypatch.setattr(strategy_bridge, "_STRATEGY_FINANCIAL_KEYS", ("not_a_registered_metric",))
    monkeypatch.setattr(strategy_bridge, "_STRATEGY_PERIODIC_KEYS", {})

    with pytest.raises(ValueError, match="not_a_registered_metric"):
        strategy_bridge.seed_strategy_inputs_from_capture(fake, run_id="run:does-not-exist", cutoff=cutoff)
    assert fake.inserts == [], "an unregistered input key must be rejected before any insert"


def test_a_newly_registered_metric_needs_no_migration_to_be_admitted(connection) -> None:
    """The registry side of the same #770 acceptance criterion: registering a brand-new
    metric is enough for `is_registered_input_key` -- the writer's guard -- to admit it,
    proven against the real, migrated schema rather than a mock."""
    from truealpha_contracts.metrics import is_registered_input_key

    synthetic_name = "synthetic_test_metric_770_registry"
    assert synthetic_name not in METRICS
    assert not is_registered_input_key(synthetic_name)

    METRICS[synthetic_name] = MetricSpec(
        name=synthetic_name,
        unit_family=UnitFamily.RATIO,
        source_priority=(DataSource.SEC,),
        description="A metric registered only for this test.",
    )
    try:
        assert is_registered_input_key(synthetic_name)
        cutoff = datetime(2026, 1, 1, tzinfo=UTC)
        connection.execute(
            """
            insert into staging.strategy_backtest_inputs
                (issuer_id, cutoff_at, input_key, value, confidence, knowable_at)
            values (%s, %s, %s, %s, %s, %s)
            """,
            ("issuer:test:770b", cutoff, synthetic_name, "4.56", "0.9", cutoff),
        )
    finally:
        del METRICS[synthetic_name]
