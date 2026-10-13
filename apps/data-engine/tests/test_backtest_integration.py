"""#758: the deployed Dagster op backtests large_model_value_v0 on a real database.

The op runs unchanged against a migrated database: real `staging.strategy_backtest_inputs`
rows at three quarter ends, real unadjusted and split-adjusted daily bars (one held symbol
splits 10-for-1), the real strategy definition, `evaluate_cutoff`, the simulation and the mart
writes. One seam is replaced: `stored_history_issuers`, the issuer-to-ticker lineage, which needs
a full capture chain and has its own tests in `test_strategy_history.py`.

The op's connection is the test's connection with `commit` disabled, so every row rolls back.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import dagster as dg
import psycopg
import pytest
from data_engine.datahub import backtest as datahub_backtest
from data_engine.datahub.market_prices import PriceBarRecord, insert_market_prices_daily, is_xnys_trading_day
from data_engine.datahub.strategy_bridge import load_strategy_definition
from data_engine.datahub.strategy_history import HistoryIssuer
from data_engine.lanes import backtest as lane
from data_engine.quality.nightly_verdicts import TICK_TAG
from factors.production_topt import OperatingBranch

Q1 = datetime(2024, 3, 31, 23, 59, 59, tzinfo=UTC)
Q2 = datetime(2024, 6, 30, 23, 59, 59, tzinfo=UTC)
Q3 = datetime(2024, 9, 30, 23, 59, 59, tzinfo=UTC)
SPLIT_DAY = date(2024, 6, 10)
FIRST_SESSION, LAST_SESSION = date(2024, 3, 25), date(2024, 10, 11)
TICK = "2024-10-14T21:30:00+00:00"
ISSUERS = {
    name: HistoryIssuer(
        issuer_id=f"issuer:t758:{name}",
        instrument_id=f"security:t758:{name}",
        listing_id=f"listing:xnas:t758{name}",
        ticker=f"T758{name.upper()}",
        cik=758_000 + ord(name),
        operating_branch=OperatingBranch.NON_FINANCIAL,
    )
    for name in ("a", "b", "c")
}
#: The last close each issuer reports at each cutoff: 1 or 2 is cheap (selected), 1000 is above its band.
LAST_CLOSE = {
    Q1: {"a": "1", "b": "2", "c": "1000"},
    Q2: {"a": "1", "b": "2", "c": "1000"},
    Q3: {"a": "1000", "b": "2", "c": "1"},
}


@pytest.fixture
def connection():
    from truealpha_runtime.testing import guard_db_write

    url = guard_db_write()
    try:
        active = psycopg.connect(url, connect_timeout=3, autocommit=False)
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    try:
        yield active
    finally:
        active.rollback()
        active.close()


class _Borrowed:
    """The op's `with psycopg.connect(...)` block, over the test's own transaction."""

    def __init__(self, connection: psycopg.Connection) -> None:
        self._connection = connection

    def __enter__(self) -> psycopg.Connection:
        return self._connection

    def __exit__(self, *_exc: object) -> None:
        return None


def _sessions() -> list[date]:
    days = (FIRST_SESSION + timedelta(days=offset) for offset in range((LAST_SESSION - FIRST_SESSION).days + 1))
    return [day for day in days if is_xnys_trading_day(day)]


def _unadjusted(symbol: str, index: int, day: date) -> Decimal:
    base = {"T758A": Decimal(1000), "T758B": Decimal(50), "T758C": Decimal(20)}[symbol]
    price = base * (1 + Decimal("0.002") * index)
    return price / 10 if symbol == "T758A" and day >= SPLIT_DAY else price


def _seed_prices(connection: psycopg.Connection, *, shock: Decimal = Decimal(1)) -> None:
    for symbol in ("T758A", "T758B", "T758C"):
        unadjusted, adjusted = [], []
        for index, day in enumerate(_sessions()):
            close = _unadjusted(symbol, index, day) * (shock if day == LAST_SESSION else 1)
            open_ = close * Decimal("0.995")
            unadjusted.append(PriceBarRecord(symbol, day, open_, close, open_, close, Decimal(1000)))
            factor = Decimal(10) if symbol == "T758A" and day < SPLIT_DAY else Decimal(1)
            adjusted.append(
                PriceBarRecord(
                    symbol, day, open_ / factor, close / factor, open_ / factor, close / factor, Decimal(1000)
                )
            )
        insert_market_prices_daily(connection, unadjusted, adjust="none")
        insert_market_prices_daily(connection, adjusted, adjust="splits")


def _seed_inputs(connection: psycopg.Connection, last_close: dict[datetime, dict[str, str]]) -> None:
    for cutoff, closes in last_close.items():
        for name, close in closes.items():
            values = {
                "gross_profit": "10000000000",
                "total_assets": "10000000000",
                "headcount": "1000",
                "revenue": "20000000000",
                "shares_outstanding": "1000000000",
                "last_close": close,
            }
            for key, value in values.items():
                connection.execute(
                    """
                    insert into staging.strategy_backtest_inputs
                        (issuer_id, cutoff_at, input_key, value, confidence, knowable_at, recorded_at)
                    values (%s, %s, %s, %s, '0.90', %s, clock_timestamp())
                    """,
                    (ISSUERS[name].issuer_id, cutoff, key, value, cutoff - timedelta(days=2)),
                )


@pytest.fixture
def seeded(connection, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    connection.execute("delete from staging.strategy_backtest_inputs")
    _seed_inputs(connection, LAST_CLOSE)
    # A monthly and a live cutoff: the backtest evaluates schedule cutoffs only.
    _seed_inputs(connection, {datetime(2024, 4, 1, tzinfo=UTC): LAST_CLOSE[Q1]})
    _seed_prices(connection)
    monkeypatch.setattr(connection, "commit", lambda: None)
    monkeypatch.setattr(lane.psycopg, "connect", lambda *_args, **_kwargs: _Borrowed(connection))
    monkeypatch.setattr(
        datahub_backtest,
        "stored_history_issuers",
        lambda _connection, ids, **_kwargs: [i for i in ISSUERS.values() if i.issuer_id in ids],
    )
    verdicts: list[tuple[bool | None, str]] = []
    monkeypatch.setattr(
        "data_engine.quality.nightly_verdicts.record",
        lambda name, *, ran_at, ok, summary, run_id: verdicts.append((ok, summary)),
    )
    return {"connection": connection, "verdicts": verdicts}


def _run_op() -> dict[str, Any]:
    context = dg.build_op_context(run_tags={TICK_TAG: TICK})
    lane.run_nightly_backtest_op(context, lane.NightlyBacktestConfig(executed_at=TICK))
    return dict(context.get_output_metadata("result"))


def _run_row(connection: psycopg.Connection, run_id: str) -> dict[str, Any]:
    with connection.cursor(row_factory=psycopg.rows.dict_row) as cursor:
        cursor.execute("select * from mart.backtest_runs where run_id = %s", (run_id,))
        row = cursor.fetchone()
    assert row is not None, run_id
    return row


def _trades(connection: psycopg.Connection, run_id: str) -> list[tuple]:
    return connection.execute(
        "select trade_date, symbol, side, shares, execution_price, trade_value, fee_paid from mart.backtest_trades "
        "where run_id = %s order by trade_date, side desc, symbol",
        (run_id,),
    ).fetchall()


def test_the_deployed_op_writes_a_large_model_value_v0_run_with_basis_ceiling_and_benchmark(seeded) -> None:
    connection = seeded["connection"]
    definition = load_strategy_definition()

    metadata = _run_op()

    run = _run_row(connection, metadata["run_id"])
    assert run["strategy_key"] == "large_model_value_v0"
    assert run["strategy_version"] == definition.definition_version
    assert run["status"] == "succeeded"
    assert run["definition_content_sha256"] == definition.content_sha256
    assert run["price_basis"] == "unadjusted_open_explicit_splits"
    assert run["claim_ceiling"] == "price_return_no_dividends"
    assert run["benchmark_rule"] == "equal_weight_buy_and_hold_same_universe"
    assert run["benchmark_cagr"] is not None and run["benchmark_max_dd"] is not None
    assert (run["start_date"], run["end_date"]) == (date(2024, 4, 1), LAST_SESSION)
    payload = run["metrics_payload"]
    assert [entry["cutoff_at"] for entry in payload["cutoffs"]] == [Q1.isoformat(), Q2.isoformat(), Q3.isoformat()]
    assert payload["binding"]["definition_content_sha256"] == definition.content_sha256
    assert seeded["verdicts"][-1][0] is True

    daily = connection.execute(
        "select valuation_date, cum_nav, benchmark_cum_nav from mart.backtest_valuations "
        "where run_id = %s and resolution = '1D' order by valuation_date",
        (metadata["run_id"],),
    ).fetchall()
    assert [row[0] for row in daily] == [day for day in _sessions() if day >= date(2024, 4, 1)]
    assert all(row[2] is not None for row in daily), "each session carries the benchmark NAV"


def test_orders_fill_at_the_next_session_open_with_the_definition_cost(seeded) -> None:
    connection = seeded["connection"]
    metadata = _run_op()
    trades = _trades(connection, metadata["run_id"])
    opens = {
        (row[0], row[1]): row[2]
        for row in connection.execute(
            "select symbol, trading_date, open from staging.market_prices_daily where adjust = 'none' and symbol like 'T758%%'"
        ).fetchall()
    }

    # Q1 (Sunday 2024-03-31) fills Monday 2024-04-01; Q3 (Monday 2024-09-30) fills Tuesday 2024-10-01.
    assert {(row[0], row[1], row[2]) for row in trades if row[0] == date(2024, 4, 1)} == {
        (date(2024, 4, 1), "T758A", "BUY"),
        (date(2024, 4, 1), "T758B", "BUY"),
    }
    assert {(row[1], row[2]) for row in trades if row[0] == date(2024, 10, 1)} >= {("T758A", "SELL"), ("T758C", "BUY")}
    assert not [row for row in trades if row[0] == date(2024, 9, 30)], "the cutoff day's own session is not eligible"
    for trade_date, symbol, _side, _shares, price, value, fee in trades:
        assert price == opens[(symbol, trade_date)], "every fill uses the unadjusted open of its session"
        assert fee == value * Decimal("0.0005")


def test_the_held_split_applies_once_and_keeps_the_nav_continuous(seeded) -> None:
    connection = seeded["connection"]
    metadata = _run_op()
    run = _run_row(connection, metadata["run_id"])

    applied = run["metrics_payload"]["splits_applied"]
    assert [(item["symbol"], item["ex_date"], item["factor"]) for item in applied] == [("T758A", "2024-06-10", "10/1")]
    nav = dict(
        connection.execute(
            "select valuation_date, cum_nav from mart.backtest_valuations where run_id = %s and resolution = '1D'",
            (metadata["run_id"],),
        ).fetchall()
    )
    assert Decimal("0.95") < nav[SPLIT_DAY] / nav[date(2024, 6, 7)] < Decimal("1.05")


def test_a_rerun_on_the_same_data_writes_nothing_and_new_data_is_a_new_run(seeded) -> None:
    connection = seeded["connection"]
    first = _run_op()
    trades = len(_trades(connection, first["run_id"]))

    again = _run_op()
    assert again["run_id"] == first["run_id"] and again["inserted"] is False
    assert (
        connection.execute("select count(*) from mart.backtest_runs where run_id = %s", (first["run_id"],)).fetchone()[
            0
        ]
        == 1
    )
    assert len(_trades(connection, first["run_id"])) == trades

    _seed_prices(connection, shock=Decimal("1.5"))
    changed = _run_op()
    assert changed["run_id"] != first["run_id"], "a new price vintage is new data"
    assert changed["inserted"] is True


def test_a_later_cutoff_input_changes_no_earlier_decision(seeded) -> None:
    """No lookahead through the database: a new Q3 vintage changes Q3 only.

    Red when the input read takes any cutoff other than its own, for example
    `where cutoff_at >= %s`: the newest-knowable Q3 row then wins at Q1 and Q2.
    """
    connection = seeded["connection"]
    first = _run_row(connection, _run_op()["run_id"])["metrics_payload"]["cutoffs"]

    _seed_inputs(connection, {Q3: {"b": "1000"}})
    second = _run_row(connection, _run_op()["run_id"])["metrics_payload"]["cutoffs"]

    assert second[:2] == first[:2], "a Q3 input changed a Q1 or Q2 decision"
    assert second[2]["weights"] != first[2]["weights"], "the Q3 change must take effect"


def test_the_same_run_id_with_a_different_result_stops_the_run(seeded) -> None:
    """The mart is append-only: a rerun never updates a run. A different result for the same
    inputs means the engine changed without a new ENGINE_ID, and the run goes red."""
    connection = seeded["connection"]
    run_id = _run_op()["run_id"]
    connection.execute(
        "update mart.backtest_runs set metrics_payload = jsonb_set(metrics_payload, '{result_sha256}', '\"0\"') "
        "where run_id = %s",
        (run_id,),
    )

    with pytest.raises(RuntimeError, match="different result"):
        _run_op()
    assert seeded["verdicts"][-1][0] is False
