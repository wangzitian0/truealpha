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


def test_seed_strategy_inputs_zeroes_confidence_on_period_mismatch() -> None:
    """AC #3 / #1108: if operating_period_end and revenue_period_end mismatch,
    seed_strategy_inputs_from_capture sets confidence to 0.00 so the evaluator
    excludes the issuer as below_confidence_floor."""
    from decimal import Decimal

    import data_engine.datahub.strategy_bridge as strategy_bridge

    cutoff = datetime(2026, 1, 1, tzinfo=UTC)
    payload = {
        "issuer_id": "issuer:test:aapl",
        "listing_id": "listing:test:aapl",
        "operating_period_end": "2018-09-29",
        "revenue_period_end": "2025-09-27",
        "gross_profit": "100000000000",
        "revenue": "390000000000",
    }
    fake = _FakeConnection([("financial-fact", "0.92", payload, cutoff)])
    written = strategy_bridge.seed_strategy_inputs_from_capture(fake, run_id="run:test", cutoff=cutoff)
    assert written == 2
    for insert_params in fake.inserts:
        # (issuer_id, cutoff, input_key, value, confidence, knowable_at, fiscal_period)
        assert insert_params[4] == Decimal("0.00"), f"Expected 0.00 confidence on mismatch, got {insert_params[4]}"


class _RunConnection(_FakeConnection):
    """`_FakeConnection`, and the run's listing-identity tickers for the ticker query of the seed."""

    def __init__(self, select_rows: list[tuple], tickers: list[tuple[str, str]]) -> None:
        super().__init__(select_rows)
        self._tickers = tickers
        self.ticker_reads = 0

    def execute(self, sql: str, params: tuple = ()) -> _FakeCursor:
        if "->> 'ticker'" in sql:
            self.ticker_reads += 1
            return _FakeCursor(self._tickers)
        return super().execute(sql, params)


def test_the_seed_takes_the_close_of_the_primary_class_whatever_the_listing_ids() -> None:
    """#1347: the live seed takes `last_close` from one listing of each issuer. Production listing ids
    are entity UUIDs that no literal names, so the old rule took the lowest listing id. Red on that
    rule: the GOOG listing holds the lower id here, and the seed wrote the GOOG close."""
    import data_engine.datahub.strategy_bridge as strategy_bridge

    cutoff = datetime(2026, 1, 1, tzinfo=UTC)
    issuer = "00000000-1347-5000-8000-000000000000"
    goog, googl = "10000000-0000-5000-8000-000000000000", "90000000-0000-5000-8000-000000000000"
    rows = [
        ("market-price", "0.9", {"issuer_id": issuer, "listing_id": goog, "close": "100.00"}, cutoff),
        ("market-price", "0.9", {"issuer_id": issuer, "listing_id": googl, "close": "101.00"}, cutoff),
    ]
    for ordered in (rows, list(reversed(rows))):
        fake = _RunConnection(ordered, [(goog, "GOOG"), (googl, "GOOGL")])

        written = strategy_bridge.seed_strategy_inputs_from_capture(fake, run_id="run:test", cutoff=cutoff)

        assert written == 1
        ((issuer_id, _cutoff, input_key, value, *_rest),) = fake.inserts
        assert (issuer_id, input_key, value) == (issuer, "last_close", "101.00"), "the GOOGL close, never GOOG's"
        assert fake.ticker_reads == 1, "the seed reads the run's tickers once"


def test_seed_strategy_inputs_maps_listing_reused_price_to_target_issuer() -> None:
    """#1333 / #1352: when a `market-price` observation is reused by `(listing, session_date)`
    from a run that keyed the issuer under another namespace (e.g., `issuer:lei:...`),
    `seed_strategy_inputs_from_capture` maps `last_close` to the target run's `issuer_id`
    (`issuer:cik:...`) via the run's `listing-identity` / `financial-fact` cells."""
    from decimal import Decimal

    import data_engine.datahub.strategy_bridge as strategy_bridge

    cutoff = datetime(2026, 1, 1, tzinfo=UTC)
    listing_payload = {
        "issuer_id": "issuer:cik:0000320193",
        "instrument_id": "instrument:cik:0000320193",
        "listing_id": "listing:xnas:aapl",
        "ticker": "AAPL",
    }
    financial_payload = {
        "issuer_id": "issuer:cik:0000320193",
        "instrument_id": "instrument:cik:0000320193",
        "listing_id": "listing:xnas:aapl",
        "revenue": "390000000000",
    }
    reused_price_payload = {
        "issuer_id": "issuer:lei:HWUPKR0MPOU8FGXBT394",
        "instrument_id": "instrument:us0378331005",
        "listing_id": "listing:xnas:aapl",
        "close": "225.50",
    }
    fake = _FakeConnection(
        [
            ("listing-identity", "1.00", listing_payload, cutoff),
            ("financial-fact", "0.95", financial_payload, cutoff),
            ("market-price", "0.90", reused_price_payload, cutoff),
        ]
    )
    written = strategy_bridge.seed_strategy_inputs_from_capture(fake, run_id="run:test-reuse", cutoff=cutoff)
    assert written == 2
    by_key = {params[2]: params for params in fake.inserts}
    assert set(by_key) == {"revenue", "last_close"}
    assert by_key["revenue"][0] == "issuer:cik:0000320193"
    assert by_key["last_close"][0] == "issuer:cik:0000320193"
    assert by_key["last_close"][3] == "225.50"
    assert by_key["last_close"][4] == Decimal("0.90")
