"""#758, #1257: the deployed Dagster op backtests large_model_value_v0 on a real database.

The op runs unchanged against a migrated database: real `staging.strategy_backtest_inputs`
rows at three quarter ends, real unadjusted and split-adjusted daily bars (one held symbol
splits 10-for-1), the real strategy definition, `evaluate_cutoff`, the simulation and the mart
writes. The issuer-to-ticker lineage is real too (#1257): each issuer is minted as an entity,
and a financial-fact observation exists under each of its two id generations, the legacy
`issuer:lei:` id and the canonical entity UUID. The inputs hold both generations at the same
cutoffs, as staging does. Nothing in the resolution path is stubbed.

The op's connection is the test's connection with `commit` disabled, so every row rolls back.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import dagster as dg
import psycopg
import pytest
from data_engine.datahub import backtest as datahub_backtest
from data_engine.datahub.market_prices import PriceBarRecord, insert_market_prices_daily, is_xnys_trading_day
from data_engine.datahub.price_series import load_daily_series
from data_engine.datahub.strategy_bridge import load_strategy_definition
from data_engine.lanes import backtest as lane
from data_engine.quality.nightly_verdicts import TICK_TAG

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
from financial_lineage_seed import (  # noqa: E402
    Listed,
    MemoryStore,
    Observed,
    SeededIssuer,
    mint_issuer,
    seed_financial_lineage,
    seed_listing_identity,
)

Q1 = datetime(2024, 3, 31, 23, 59, 59, tzinfo=UTC)
Q2 = datetime(2024, 6, 30, 23, 59, 59, tzinfo=UTC)
Q3 = datetime(2024, 9, 30, 23, 59, 59, tzinfo=UTC)
SPLIT_DAY = date(2024, 6, 10)
FIRST_SESSION, LAST_SESSION = date(2024, 3, 25), date(2024, 10, 11)
TICK = "2024-10-14T21:30:00+00:00"
#: The entities exist before every cutoff and before the tick.
MINTED_AT = datetime(2024, 1, 1, tzinfo=UTC)
#: Issuer f holds no seeded inputs: only the writer in the issuer-list test lands one.
NAMES = ("a", "b", "c", "d", "e", "f")
#: The last close each issuer reports at each cutoff: 1 or 2 is cheap (selected), 1000 is above its band.
LAST_CLOSE = {
    # Issuer e holds inputs at Q1 only, and its ticker T758E never has a bar.
    Q1: {"a": "1", "b": "2", "c": "1000", "e": "1000"},
    # Issuer d holds inputs from Q2 only: it is in the strategy's universe, not in the benchmark's.
    Q2: {"a": "1", "b": "2", "c": "1000", "d": "1000"},
    Q3: {"a": "1000", "b": "2", "c": "1", "d": "1000"},
}
#: The legacy generation reports other closes at the same cutoffs. A reader that mixes legacy
#: rows into the canonical inputs changes the decisions, so every decision assertion catches it.
LEGACY_LAST_CLOSE = {
    cutoff: {name: "1000" if close != "1000" else "1" for name, close in closes.items()}
    for cutoff, closes in LAST_CLOSE.items()
}
#: T758C has no bar on the first fill session: the benchmark buys it at its next open.
NO_BAR = {("T758C", date(2024, 4, 1))}


def _lei(name: str) -> str:
    return f"T758{name.upper()}".ljust(18, "X") + "00"


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
    base = {"T758A": Decimal(1000), "T758B": Decimal(50), "T758C": Decimal(20), "T758D": Decimal(30)}[symbol]
    price = base * (1 + Decimal("0.002") * index)
    return price / 10 if symbol == "T758A" and day >= SPLIT_DAY else price


def _seed_prices(connection: psycopg.Connection, *, shock: Decimal = Decimal(1)) -> None:
    for symbol in ("T758A", "T758B", "T758C", "T758D"):
        unadjusted, adjusted = [], []
        for index, day in enumerate(_sessions()):
            if (symbol, day) in NO_BAR:
                continue
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


def _seed_inputs(
    connection: psycopg.Connection,
    issuers: dict[str, SeededIssuer],
    last_close: dict[datetime, dict[str, str]],
    *,
    legacy: bool = False,
) -> None:
    for cutoff, closes in last_close.items():
        for name, close in closes.items():
            issuer_id = issuers[name].legacy_issuer_id if legacy else issuers[name].canonical_issuer_id
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
                    (issuer_id, cutoff, key, value, cutoff - timedelta(days=2)),
                )


def _mint_and_seed_lineage(connection: psycopg.Connection) -> dict[str, SeededIssuer]:
    """Each issuer as an entity, with one financial-fact observation per id generation.

    The legacy observation is older; the canonical one landed after the identity unification.
    """
    issuers = {
        name: mint_issuer(
            connection,
            lei=_lei(name),
            mic="XNAS",
            ticker=f"T758{name.upper()}",
            cik=758_000 + ord(name),
            known_at=MINTED_AT,
        )
        for name in NAMES
    }
    observed = []
    for issuer in issuers.values():
        observed.append(
            Observed(
                issuer.legacy_issuer_id,
                issuer.legacy_listing_id,
                issuer.cik,
                "non_financial",
                datetime(2024, 1, 5, tzinfo=UTC),
            )
        )
        observed.append(
            Observed(
                issuer.canonical_issuer_id,
                issuer.canonical_listing_id,
                issuer.cik,
                "non_financial",
                datetime(2024, 2, 1, tzinfo=UTC),
            )
        )
    seed_financial_lineage(
        connection,
        observed,
        company_facts={issuer.cik: {"facts": {}} for issuer in issuers.values()},
        store=MemoryStore(),
        cutoff=datetime(2024, 2, 1, tzinfo=UTC),
    )
    return issuers


@pytest.fixture
def seeded(connection, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    connection.execute("delete from staging.strategy_backtest_inputs")
    issuers = _mint_and_seed_lineage(connection)
    _seed_inputs(connection, issuers, LAST_CLOSE)
    # The old generation at the same cutoffs, as staging holds it (#1257).
    _seed_inputs(connection, issuers, LEGACY_LAST_CLOSE, legacy=True)
    # A monthly and a live cutoff: the backtest evaluates schedule cutoffs only.
    _seed_inputs(connection, issuers, {datetime(2024, 4, 1, tzinfo=UTC): LAST_CLOSE[Q1]})
    _seed_prices(connection)
    monkeypatch.setattr(connection, "commit", lambda: None)
    monkeypatch.setattr(lane.psycopg, "connect", lambda *_args, **_kwargs: _Borrowed(connection))
    verdicts: list[tuple[bool | None, str]] = []
    monkeypatch.setattr(
        "data_engine.quality.nightly_verdicts.record",
        lambda name, *, ran_at, ok, summary, run_id: verdicts.append((ok, summary)),
    )
    return {"connection": connection, "verdicts": verdicts, "issuers": issuers}


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
    assert run["benchmark_rule"] == "equal_weight_buy_and_hold_first_cutoff_issuers"
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
        # A full-spend buy carries the last-digit Decimal residual in its fee (cash ends at zero).
        assert abs(fee - value * Decimal("0.0005")) <= value * Decimal("1e-20")


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

    _seed_inputs(connection, seeded["issuers"], {Q3: {"b": "1000"}})
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


def test_the_benchmark_holds_the_first_cutoff_issuers_and_records_a_late_fill(seeded) -> None:
    """Audit finding 2: issuer d enters the inputs at Q2, so it is not in the benchmark. T758C has
    no bar on 2024-04-01, so the benchmark buys it at its next open and records the delay."""
    connection = seeded["connection"]
    payload = _run_row(connection, _run_op()["run_id"])["metrics_payload"]

    benchmark = payload["benchmark"]
    assert benchmark["universe"] == ["T758A", "T758B", "T758C", "T758E"]
    assert benchmark["deferred_fills"] == [
        {"symbol": "T758C", "ordered_session": "2024-04-01", "filled_session": "2024-04-02"}
    ]
    assert benchmark["never_filled"] == ["T758E"]
    assert [(item["symbol"], item["ex_date"], item["factor"]) for item in benchmark["splits_applied"]] == [
        ("T758A", "2024-06-10", "10/1")
    ], "the benchmark holds T758A through its split and records the event"
    assert payload["binding"]["benchmark_universe"] == ["T758A", "T758B", "T758C", "T758E"]


def test_the_run_id_binds_the_deployed_code_identity(seeded, monkeypatch: pytest.MonkeyPatch) -> None:
    """Audit finding 3: the same data under another release is another run."""
    from data_engine.config import settings

    monkeypatch.setattr(settings, "git_commit_sha", "v0.9.1")
    first = _run_op()
    monkeypatch.setattr(settings, "git_commit_sha", "v0.9.2")
    second = _run_op()

    assert first["run_id"] != second["run_id"]
    binding = _run_row(seeded["connection"], second["run_id"])["metrics_payload"]["binding"]
    assert binding["code_git_commit_sha"] == "v0.9.2"
    assert binding["engine_id"].startswith("factors.backtest.engine:")


def test_the_input_snapshot_describes_the_rows_the_decisions_read(seeded, monkeypatch: pytest.MonkeyPatch) -> None:
    """Audit finding 10: a writer that lands a Q3 row after the decisions must not change run_id.

    Red when the snapshot is read after the decisions: it then hashes a row no decision used.
    """
    from data_engine.strategy_backtest_gateway import (
        StrategyBacktestGateway,
        combine_snapshot_ids,
        snapshot_id_from_rows,
    )

    connection = seeded["connection"]
    gateway = StrategyBacktestGateway(connection)
    canonical = {issuer.canonical_issuer_id for issuer in seeded["issuers"].values()}

    def canonical_snapshot() -> str:
        return combine_snapshot_ids(
            [snapshot_id_from_rows([row for row in gateway.rows_at(c) if row[0] in canonical]) for c in (Q1, Q2, Q3)]
        )

    seen: dict[str, str] = {}
    real_decide = datahub_backtest.decide

    def decide_then_a_writer_lands(*args, **kwargs):
        decisions = real_decide(*args, **kwargs)
        seen["used"] = canonical_snapshot()
        _seed_inputs(connection, seeded["issuers"], {Q3: {"b": "3"}})
        return decisions

    monkeypatch.setattr(datahub_backtest, "decide", decide_then_a_writer_lands)
    binding = _run_row(connection, _run_op()["run_id"])["metrics_payload"]["binding"]

    assert binding["input_snapshot_id"] == seen["used"]
    assert binding["input_snapshot_id"] != canonical_snapshot(), (
        "the writer must change the stored inputs, or this test proves nothing"
    )


def test_the_run_stores_its_limitations(seeded) -> None:
    """Audit finding 4: the disclosed limitations travel with the run, including vintage and session rules."""
    from data_engine.datahub.backtest import LIMITATIONS

    payload = _run_row(seeded["connection"], _run_op()["run_id"])["metrics_payload"]

    assert payload["limitations"][: len(LIMITATIONS)] == list(LIMITATIONS)
    (extra,) = payload["limitations"][len(LIMITATIONS) :]
    assert "T758E" in extra and "stayed cash for the whole run" in extra, (
        "round-2 audit item 3: a benchmark symbol that never opens must be a stated limitation"
    )
    assert any("vintage" in item for item in payload["limitations"])
    assert any("exchange calendar" in item for item in payload["limitations"])


@pytest.mark.parametrize("app_env", ["staging", "production", "prod"])
@pytest.mark.parametrize("sha", ["unknown", ""])
def test_a_deployed_environment_without_a_code_identity_refuses_the_run(
    seeded, monkeypatch: pytest.MonkeyPatch, app_env: str, sha: str
) -> None:
    """Round-2 audit item 1: GIT_COMMIT_SHA defaults to "unknown". Two code versions would then
    share one run_id in staging or production, so the run fails and persists nothing."""
    from data_engine.config import settings

    monkeypatch.setattr(settings, "app_env", app_env)
    monkeypatch.setattr(settings, "git_commit_sha", sha)
    connection = seeded["connection"]
    before = connection.execute("select count(*) from mart.backtest_runs").fetchone()[0]

    with pytest.raises(RuntimeError, match="GIT_COMMIT_SHA"):
        _run_op()

    assert connection.execute("select count(*) from mart.backtest_runs").fetchone()[0] == before
    assert seeded["verdicts"][-1][0] is False


@pytest.mark.parametrize("app_env", ["dev", "test"])
def test_a_local_environment_without_a_code_identity_still_runs(
    seeded, monkeypatch: pytest.MonkeyPatch, app_env: str
) -> None:
    from data_engine.config import settings

    monkeypatch.setattr(settings, "app_env", app_env)
    monkeypatch.setattr(settings, "git_commit_sha", "unknown")

    metadata = _run_op()

    binding = _run_row(seeded["connection"], metadata["run_id"])["metrics_payload"]["binding"]
    assert binding["code_git_commit_sha"] == "unknown"


def test_the_issuer_list_comes_from_the_same_read_as_the_inputs(seeded, monkeypatch: pytest.MonkeyPatch) -> None:
    """Round-2 audit item 2: a writer that lands a new issuer f at Q1 just before the input read.

    Red when the issuer list is read in a separate, earlier query: f is then in the inputs but not
    in the issuer list, so lineage is never asked for it.
    """
    connection = seeded["connection"]
    issuers = seeded["issuers"]
    gateway_type = datahub_backtest.StrategyBacktestGateway
    real_read = gateway_type.rows_at
    landed: list[bool] = []

    def writer_lands_first(self, cutoff_at):
        if not landed:
            _seed_inputs(connection, issuers, {Q1: {"f": "1000"}})
            landed.append(True)
        return real_read(self, cutoff_at)

    asked: list[str] = []
    real_lineage = datahub_backtest.canonical_history_issuers

    def lineage_spy(connection_, ids, **kwargs):
        asked.extend(ids)
        return real_lineage(connection_, ids, **kwargs)

    monkeypatch.setattr(gateway_type, "rows_at", writer_lands_first)
    monkeypatch.setattr(datahub_backtest, "canonical_history_issuers", lineage_spy)
    payload = _run_row(connection, _run_op()["run_id"])["metrics_payload"]

    assert issuers["f"].canonical_issuer_id in asked
    assert "T758F" in payload["benchmark"]["universe"], "f holds inputs at Q1 in the read the run used"


# -- #1257: two id generations, resolved through the real lineage ---------------------------


def test_both_id_generations_at_the_same_cutoffs_run_on_the_canonical_issuers(seeded) -> None:
    """#1257 (a): staging holds every issuer under `issuer:lei:` and under its entity UUID, at the
    same cutoffs. Red on the code before the fix: both generations resolve to one ticker and the
    run stops with "two issuers resolve to one ticker". The run must read the canonical rows only.
    """
    connection = seeded["connection"]
    issuers = seeded["issuers"]

    payload = _run_row(connection, _run_op()["run_id"])["metrics_payload"]

    canonical = {issuer.canonical_issuer_id for issuer in issuers.values()}
    assert {row[0] for row in payload["binding"]["issuers"]} <= canonical
    assert {row[1] for row in payload["binding"]["issuers"]} == {"T758A", "T758B", "T758C", "T758D", "T758E"}
    first = payload["cutoffs"][0]
    # The canonical inputs select a and b at Q1. The legacy rows (closes swapped) would select c and e.
    assert first["weights"] == {"T758A": "0.500000", "T758B": "0.500000"}
    assert sum(first["outcomes"].values()) == 4, "Q1 evaluates the 4 canonical issuers, never their legacy twins"
    assert sorted(payload["ignored_legacy_issuer_ids"]) == sorted(
        issuer.legacy_issuer_id for name, issuer in issuers.items() if name != "f"
    )


def test_two_canonical_issuers_on_one_ticker_still_stop_the_run(seeded) -> None:
    """The guard stays after canonicalization: two distinct entities that resolve to one ticker
    are a lineage defect, and the run refuses them."""
    connection = seeded["connection"]
    twin = mint_issuer(connection, lei=_lei("z"), mic="XNYS", ticker="T758A", cik=758_999, known_at=MINTED_AT)
    seed_financial_lineage(
        connection,
        [
            Observed(
                twin.canonical_issuer_id,
                twin.canonical_listing_id,
                twin.cik,
                "non_financial",
                datetime(2024, 2, 2, tzinfo=UTC),
            )
        ],
        company_facts={twin.cik: {"facts": {}}},
        store=MemoryStore(),
        cutoff=datetime(2024, 2, 2, tzinfo=UTC),
    )
    _seed_inputs(connection, {"z": twin}, {Q1: {"z": "1000"}})

    with pytest.raises(RuntimeError, match="two issuers resolve to one ticker"):
        _run_op()


# -- #1270: a share count from before a split -----------------------------------------------------

#: The instant an NVDA-shaped share count became knowable: filed on 2024-05-29, before the 10-for-1
#: split of T758A on 2024-06-10. The last close of Q2 is knowable on 2024-06-28, after the split.
FILED_BEFORE_THE_SPLIT = datetime(2024, 5, 29, 23, 59, 59, tzinfo=UTC)


def _run_backtest(connection: psycopg.Connection):
    return datahub_backtest.run_core_strategy_backtest(connection, tick=datetime.fromisoformat(TICK))


def _date_the_share_count_before_the_split(connection: psycopg.Connection, issuer_id: str) -> None:
    """The winning share count of the issuer at Q2 becomes an unadjusted row filed before the split."""
    updated = connection.execute(
        "update staging.strategy_backtest_inputs set knowable_at = %s "
        "where issuer_id = %s and cutoff_at = %s and input_key = 'shares_outstanding'",
        (FILED_BEFORE_THE_SPLIT, issuer_id, Q2),
    ).rowcount
    assert updated == 1, "the fixture holds one share count of this issuer at Q2"


def _append_the_adjusted_share_count(connection: psycopg.Connection, issuer_id: str) -> None:
    """What the projector appends after the split: the count times 10, knowable at the close."""
    connection.execute(
        """
        insert into staging.strategy_backtest_inputs
            (issuer_id, cutoff_at, input_key, value, confidence, knowable_at, recorded_at)
        values (%s, %s, 'shares_outstanding', %s, '0.90', %s, clock_timestamp())
        """,
        (issuer_id, Q2, Decimal("1000000000") * 10, Q2 - timedelta(days=2)),
    )


def test_a_share_count_from_before_a_split_stops_the_run_before_it_writes(seeded) -> None:
    """Staging, 2024-06-30: NVDA held a count filed before its split beside a post-split close.
    The run succeeded with wrong picks. Red when the run does not raise. Red when the message omits
    the symbol, the cutoff, the split with its factor or the job that corrects the row."""
    connection = seeded["connection"]
    _date_the_share_count_before_the_split(connection, seeded["issuers"]["a"].canonical_issuer_id)
    runs_before = connection.execute("select count(*) from mart.backtest_runs").fetchone()[0]

    with pytest.raises(datahub_backtest.ShareBasisError) as raised:
        _run_backtest(connection)

    message = str(raised.value)
    assert "T758A at 2024-06-30T23:59:59+00:00" in message
    assert "10/1 split on 2024-06-10" in message
    assert "strategy_history_projection_pipeline" in message
    assert "T758B" not in message, "only the stale issuer is named"
    assert connection.execute("select count(*) from mart.backtest_runs").fetchone()[0] == runs_before


def test_the_nightly_op_goes_red_on_a_share_count_from_before_a_split(seeded) -> None:
    """The error is not caught: a failed Dagster run and a red verdict are the visible signal. A pending
    or a green verdict would hide it."""
    _date_the_share_count_before_the_split(seeded["connection"], seeded["issuers"]["a"].canonical_issuer_id)

    with pytest.raises(datahub_backtest.ShareBasisError):
        _run_op()

    assert seeded["verdicts"][-1][0] is False


def test_the_adjusted_share_row_that_the_projector_appends_lets_the_run_through(seeded) -> None:
    """The table is append-only: the stale row stays and loses by `knowable_at` to the adjusted row."""
    connection = seeded["connection"]
    issuer_id = seeded["issuers"]["a"].canonical_issuer_id
    _date_the_share_count_before_the_split(connection, issuer_id)
    with pytest.raises(datahub_backtest.ShareBasisError):
        _run_backtest(connection)

    _append_the_adjusted_share_count(connection, issuer_id)
    outcome = _run_backtest(connection)

    assert outcome.inserted is True
    held = [
        row[2]
        for row in datahub_backtest.StrategyBacktestGateway(connection).rows_at(Q2)
        if row[0] == issuer_id and row[1] == "shares_outstanding"
    ]
    assert held == [Decimal("10000000000")], "the decisions read the adjusted count"
    stored = connection.execute(
        "select count(*) from staging.strategy_backtest_inputs "
        "where issuer_id = %s and cutoff_at = %s and input_key = 'shares_outstanding'",
        (issuer_id, Q2),
    ).fetchone()[0]
    assert stored == 2, "the stale row is still in the table"


def test_an_old_share_count_with_no_split_after_it_does_not_stop_the_run(seeded) -> None:
    """The check keys on a split, not on the age of the count: T758B never splits."""
    connection = seeded["connection"]
    _date_the_share_count_before_the_split(connection, seeded["issuers"]["b"].canonical_issuer_id)

    assert _run_backtest(connection).inserted is True


def test_a_stale_row_under_a_legacy_id_does_not_stop_the_run(seeded) -> None:
    """The projector writes canonical ids only, so nothing can ever correct a legacy row. The check
    reads canonical ids only; otherwise every later run would stop for good."""
    connection = seeded["connection"]
    _date_the_share_count_before_the_split(connection, seeded["issuers"]["a"].legacy_issuer_id)

    assert _run_backtest(connection).inserted is True


def test_a_projector_commit_between_the_two_reads_stops_the_run_instead_of_passing_unchecked(
    seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run reads the dates before the values. Suppose the projector commits its correction between
    the two reads. The decisions then read the corrected count, but the dates are the stale ones. The
    run stops, and the next run passes. Red when the values are read first. The run then checks dates
    that are newer than the values the decisions used. It passes on a stale count.
    """
    connection = seeded["connection"]
    issuer_id = seeded["issuers"]["a"].canonical_issuer_id
    _date_the_share_count_before_the_split(connection, issuer_id)
    gateway_type = datahub_backtest.StrategyBacktestGateway
    reads: list[str] = []
    landed: list[bool] = []
    racing = [True]

    def landing_before_the_second_kind(kind: str, real_read):
        def read(self, cutoff_at):
            if racing[0]:
                reads.append(kind)
                if len(set(reads)) == 2 and not landed:
                    _append_the_adjusted_share_count(connection, issuer_id)
                    landed.append(True)
            return real_read(self, cutoff_at)

        return read

    for kind, name in (("dates", "market_value_vintages_at"), ("values", "rows_at")):
        monkeypatch.setattr(gateway_type, name, landing_before_the_second_kind(kind, getattr(gateway_type, name)))

    with pytest.raises(datahub_backtest.ShareBasisError):
        _run_backtest(connection)
    assert landed == [True], "the correction must land between the two reads, or this test proves nothing"
    assert reads[0] == "dates", "the dates are read first"

    racing[0] = False
    assert _run_backtest(connection).inserted is True


# -- #1265: one price symbol per issuer ---------------------------------------------------------

#: The financial lineage of a #1265 issuer lands after the fixture's lineage, in its own capture run.
LISTED_LINEAGE_AT = datetime(2024, 2, 3, tzinfo=UTC)


def _seed_listed_issuer(
    connection: psycopg.Connection, name: str, *, lineage: str, listings: tuple[str, ...], close: str
) -> SeededIssuer:
    """One more issuer: its lineage ticker is `lineage`, and its listing-identity cells name `listings`.

    It holds inputs at Q1, Q2 and Q3 with the close `close`. At 0.5 it is the cheapest issuer, so
    each cutoff selects it.
    """
    issuer = mint_issuer(
        connection, lei=_lei(name), mic="XNAS", ticker=lineage, cik=758_000 + ord(name), known_at=MINTED_AT
    )
    store = MemoryStore()
    seed_financial_lineage(
        connection,
        [
            Observed(
                issuer.canonical_issuer_id, issuer.canonical_listing_id, issuer.cik, "non_financial", LISTED_LINEAGE_AT
            )
        ],
        company_facts={issuer.cik: {"facts": {}}},
        store=store,
        cutoff=LISTED_LINEAGE_AT,
    )
    seed_listing_identity(
        connection,
        [
            Listed(issuer.canonical_issuer_id, f"listing:xnas:{ticker.lower()}", ticker, MINTED_AT)
            for ticker in listings
        ],
        store=store,
        cutoff=MINTED_AT,
    )
    _seed_inputs(connection, {name: issuer}, {cutoff: {name: close} for cutoff in (Q1, Q2, Q3)})
    return issuer


def _store_both_series(connection: psycopg.Connection, symbol: str, *, base: Decimal) -> None:
    """Both daily series of one symbol on every session of the fixture. The series show no split."""
    bars = []
    for index, day in enumerate(_sessions()):
        close = base * (1 + Decimal("0.001") * index)
        open_ = close * Decimal("0.99")
        bars.append(PriceBarRecord(symbol, day, open_, close, open_, close, Decimal(1000)))
    insert_market_prices_daily(connection, bars, adjust="none")
    insert_market_prices_daily(connection, bars, adjust="splits")


def _strategy_line(symbol: str, cutoffs: str) -> str:
    return (
        f"The strategy selected {symbol} at {cutoffs}, but no open existed on the fill session, "
        "so the weight it could not buy stayed cash."
    )


def test_an_issuer_without_series_of_its_own_is_priced_by_its_other_listing(seeded) -> None:
    """#1265 (a). Staging, v0.1.11: Alphabet is GOOG in lineage, and the store holds GOOGL bars only.
    The run kept GOOG's weight in cash from 2025-04 to 2025-09. Red on the code before the fix: each
    cutoff records T1265G as unfilled, and half of the portfolio stays cash."""
    connection = seeded["connection"]
    _seed_listed_issuer(connection, "g", lineage="T1265G", listings=("T1265G", "T1265L"), close="0.5")
    _store_both_series(connection, "T1265L", base=Decimal(77))

    run_id = _run_op()["run_id"]

    payload = _run_row(connection, run_id)["metrics_payload"]
    selected = [entry for entry in payload["cutoffs"] if "T1265G" in entry["weights"]]
    assert len(selected) == 3, "each cutoff must select g, or this test proves nothing"
    assert [entry["unfilled_symbols"] for entry in selected] == [[], [], []]
    cash = connection.execute(
        "select max(cash_weight) from mart.backtest_valuations where run_id = %s and resolution = '1D'", (run_id,)
    ).fetchone()[0]
    assert cash < Decimal("0.01"), f"the weight of g must be invested, not cash: {cash}"
    open_ = connection.execute(
        "select open from staging.market_prices_daily where symbol = 'T1265L' and adjust = 'none' "
        "and trading_date = '2024-04-01'"
    ).fetchone()[0]
    buys = [row for row in _trades(connection, run_id) if row[1] == "T1265G" and row[2] == "BUY"]
    assert buys[0][0] == date(2024, 4, 1) and buys[0][4] == open_, "the T1265L open fills the buy, under T1265G"
    assert not [row for row in _trades(connection, run_id) if row[1] == "T1265L"], "every output names the lineage"
    assert payload["binding"]["price_symbols"] == {"T1265G": "T1265L"}, "the run id binds the mapping"
    assert payload["price_symbols"] == {"T1265G": "T1265L"}
    assert "T1265G is priced by the bars of its T1265L listing." in payload["limitations"]
    assert "T1265G" in payload["benchmark"]["universe"] and payload["benchmark"]["never_filled"] == ["T758E"]
    # The price snapshot hashes the stored series that priced the run: T1265L, under its own symbol.
    used = sorted({row[1] for row in payload["binding"]["issuers"]} - {"T1265G"} | {"T1265L"})
    assert payload["binding"]["price_snapshot_sha256"] == datahub_backtest.price_snapshot_sha256(
        load_daily_series(connection, used, adjust="none"), load_daily_series(connection, used, adjust="splits")
    )


def test_an_issuer_without_series_on_any_listing_is_named_with_its_cutoffs(seeded) -> None:
    """#1265 (b). No listing of g holds a stored series, so g has no price symbol. The run still
    succeeds. Each cutoff that selects g records it as unfilled, and the limitations name g with
    those cutoffs. Red on the code before the fix: the limitations name the benchmark side only."""
    connection = seeded["connection"]
    _seed_listed_issuer(connection, "g", lineage="T1265N", listings=("T1265M", "T1265N"), close="0.5")

    metadata = _run_op()

    assert metadata["inserted"] is True and seeded["verdicts"][-1][0] is True, "the run must not fail for it"
    payload = _run_row(connection, metadata["run_id"])["metrics_payload"]
    assert _strategy_line("T1265N", "2024-03-31, 2024-06-30, 2024-09-30") in payload["limitations"]
    assert [entry["unfilled_symbols"] for entry in payload["cutoffs"]] == [["T1265N"], ["T1265N"], ["T1265N"]]
    assert payload["binding"]["price_symbols"] == {}, "a listing without both series prices nothing"
    assert not [item for item in payload["limitations"] if "is priced by the bars of" in item]


def test_two_issuers_on_one_price_symbol_stop_the_run(seeded) -> None:
    """#1265 (d). Issuer g is T1265G in lineage, and its other listing T1265L holds both series. A
    second entity k is T1265L in lineage. Both read the T1265L series, so the run would trade one
    series for two issuers. Red on the code before the fix: the run succeeds and g stays cash."""
    connection = seeded["connection"]
    _seed_listed_issuer(connection, "g", lineage="T1265G", listings=("T1265G", "T1265L"), close="0.5")
    _seed_listed_issuer(connection, "k", lineage="T1265L", listings=("T1265L",), close="1000")
    _store_both_series(connection, "T1265L", base=Decimal(77))
    runs_before = connection.execute("select count(*) from mart.backtest_runs").fetchone()[0]

    with pytest.raises(RuntimeError, match="two issuers resolve to one price symbol: T1265L prices T1265G, T1265L"):
        _run_backtest(connection)

    assert connection.execute("select count(*) from mart.backtest_runs").fetchone()[0] == runs_before
