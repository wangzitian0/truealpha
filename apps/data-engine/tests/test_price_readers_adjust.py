"""#1131: every reader of the price tables filters on `adjust`.

`staging.market_prices_daily` holds two series for one (symbol, date) since #1131: the
split-adjusted bar (`adjust = 'splits'`) and the unadjusted bar (`adjust = 'none'`).
A reader that ignores `adjust` flips between the two series from one run to the next.
Each test below writes both series with different closes. Each reader must return the
series it asked for and never the other one.
"""

from __future__ import annotations

import inspect
import os
import sys
from datetime import date
from pathlib import Path

import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub.market_prices import PriceBarRecord, insert_market_prices_daily, insert_market_prices_monthly
from data_engine.datahub.universe_mask import UniverseMaskReason, compute_and_persist_universe_mask_from_db
from data_engine.lanes.market_data import _distinct_trading_dates

REPO_ROOT = Path(__file__).resolve().parents[3]
SPLIT_CLOSE = 50
UNADJUSTED_CLOSE = 500
SERIES = {"splits": SPLIT_CLOSE, "none": UNADJUSTED_CLOSE}


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


def _bar(symbol: str, d: date, close: int, resolution: str = "1D") -> PriceBarRecord:
    return PriceBarRecord(
        symbol=symbol,
        date=d,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1000,
        source="twelvedata",
        resolution=resolution,
    )


def _load_prices_reader():
    scripts_dir = Path(__file__).resolve().parents[1] / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    from run_vectorbt_backtest import _load_prices

    return _load_prices


def _write_both_series(connection, symbol: str, trading_date: date, *, unadjusted_last: bool) -> None:
    order = ["splits", "none"] if unadjusted_last else ["none", "splits"]
    for adjust in order:
        written = insert_market_prices_daily(connection, [_bar(symbol, trading_date, SERIES[adjust])], adjust=adjust)
        assert written == 1, f"setup did not write the {adjust} bar"


@pytest.mark.parametrize("unadjusted_last", [True, False], ids=["none-written-last", "splits-written-last"])
@pytest.mark.parametrize("adjust", ["splits", "none"])
def test_load_prices_returns_only_the_series_it_asked_for(connection, adjust: str, unadjusted_last: bool) -> None:
    """The backtest script reads one close per (symbol, date). Insertion order must not pick the series."""
    symbol = f"T1131LOAD{adjust.upper()}{int(unadjusted_last)}"
    trading_date = date(2026, 9, 21)
    _write_both_series(connection, symbol, trading_date, unadjusted_last=unadjusted_last)

    prices = _load_prices_reader()(connection, "staging.market_prices_daily", [symbol], adjust=adjust)

    assert prices.to_dicts() == [{"symbol": symbol, "date": trading_date, "close": float(SERIES[adjust])}]


def test_load_prices_reads_the_monthly_table_by_adjust(connection) -> None:
    symbol = "T1131LOADMONTHLY"
    month_end = date(2026, 8, 31)
    insert_market_prices_monthly(connection, [_bar(symbol, month_end, 60, "1M")], adjust="splits")
    insert_market_prices_monthly(connection, [_bar(symbol, month_end, 600, "1M")], adjust="none")

    split = _load_prices_reader()(connection, "staging.market_prices_monthly", [symbol], adjust="splits")
    unadjusted = _load_prices_reader()(connection, "staging.market_prices_monthly", [symbol], adjust="none")

    assert split["close"].to_list() == [60.0]
    assert unadjusted["close"].to_list() == [600.0]


def test_distinct_trading_dates_lists_only_the_dates_of_the_asked_series(connection) -> None:
    """A date that only the unadjusted series holds is not a split-adjusted cutoff."""
    symbol = "T1131DATES"
    shared, only_unadjusted = date(2026, 9, 21), date(2026, 9, 22)
    _write_both_series(connection, symbol, shared, unadjusted_last=True)
    insert_market_prices_daily(connection, [_bar(symbol, only_unadjusted, UNADJUSTED_CLOSE)], adjust="none")

    split_dates = _distinct_trading_dates(connection, "staging.market_prices_daily", [symbol], adjust="splits")
    unadjusted_dates = _distinct_trading_dates(connection, "staging.market_prices_daily", [symbol], adjust="none")

    assert split_dates == [shared]
    assert unadjusted_dates == [shared, only_unadjusted]


def test_universe_mask_reads_only_the_series_it_asked_for(connection) -> None:
    """A symbol with unadjusted bars and no split-adjusted bars has no split-adjusted history."""
    symbol = "T1131MASK"
    dates = [date(2026, 9, d) for d in (14, 15, 16, 17, 18)]
    insert_market_prices_daily(connection, [_bar(symbol, d, UNADJUSTED_CLOSE) for d in dates], adjust="none")
    cutoff = dates[-1]

    def mask_for(adjust: str):
        (record,) = compute_and_persist_universe_mask_from_db(
            connection,
            symbols=[symbol],
            cutoff_dates=[cutoff],
            source_table="staging.market_prices_daily",
            adjust=adjust,
            resolution="1D",
            min_periods=1,
        )
        return record.eligible, record.reason_code

    assert mask_for("none") == (True, UniverseMaskReason.OK)
    assert mask_for("splits") == (False, UniverseMaskReason.UNLISTED)


READER_FACTORIES = {
    "lane-cutoff-dates": lambda: _distinct_trading_dates,
    "universe-mask": lambda: compute_and_persist_universe_mask_from_db,
    "backtest-script": _load_prices_reader,
}


@pytest.mark.parametrize("name", sorted(READER_FACTORIES))
def test_a_reader_cannot_omit_the_adjust_filter(name: str) -> None:
    """`adjust` has no default. A call that leaves it out fails instead of mixing the series."""
    reader = READER_FACTORIES[name]()
    parameter = inspect.signature(reader).parameters.get("adjust")
    assert parameter is not None, f"{name} takes no `adjust` argument"
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty, f"{name} defaults `adjust`"


# --- no unreviewed reader ----------------------------------------------------------------

#: Every file that names a price table, with what it does there. A new file in this scan
#: needs a row here AND a test above that fails when its query drops the `adjust` filter.
REVIEWED_PRICE_TABLE_FILES = {
    "apps/data-engine/scripts/run_vectorbt_backtest.py": "reader, tested by test_load_prices_*",
    "apps/data-engine/src/data_engine/datahub/market_prices.py": "writer and latest-vintage lookup",
    "apps/data-engine/src/data_engine/datahub/universe_mask.py": "reader, tested by test_universe_mask_*",
    "apps/data-engine/src/data_engine/lanes/market_data.py": "reader, tested by test_distinct_trading_dates_*",
    "tools/schema_drift.py": "prose in a docstring, no query",
}
SCANNED_ROOTS = ("apps", "libs", "tools")
SCANNED_SUFFIXES = {".py", ".ts", ".tsx", ".sql", ".sh"}
SKIPPED_DIRECTORIES = {"tests", "node_modules", ".venv", "__pycache__", ".next"}
PRICE_TABLE_NAMES = ("market_prices_daily", "market_prices_monthly")


def _files_naming_a_price_table(root: Path) -> set[str]:
    found: set[str] = set()
    for scanned in SCANNED_ROOTS:
        for directory, subdirectories, files in os.walk(root / scanned):
            subdirectories[:] = [name for name in subdirectories if name not in SKIPPED_DIRECTORIES]
            for name in files:
                path = Path(directory) / name
                if path.suffix not in SCANNED_SUFFIXES:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
                if any(table in text for table in PRICE_TABLE_NAMES):
                    found.add(path.relative_to(root).as_posix())
    return found


def test_the_scan_finds_a_new_file_that_names_a_price_table(tmp_path: Path) -> None:
    """The scan itself must be able to fail: a planted reader appears, a test file does not."""
    planted = tmp_path / "apps" / "app-web" / "lib" / "prices.ts"
    planted.parent.mkdir(parents=True)
    planted.write_text("select close from staging.market_prices_daily", encoding="utf-8")
    ignored = tmp_path / "apps" / "app-web" / "tests" / "prices.test.ts"
    ignored.parent.mkdir(parents=True)
    ignored.write_text("staging.market_prices_monthly", encoding="utf-8")
    (tmp_path / "libs").mkdir()
    (tmp_path / "tools").mkdir()

    assert _files_naming_a_price_table(tmp_path) == {"apps/app-web/lib/prices.ts"}


def test_no_unreviewed_file_reads_a_price_table() -> None:
    found = _files_naming_a_price_table(REPO_ROOT)
    assert found == set(REVIEWED_PRICE_TABLE_FILES), (
        f"unreviewed: {sorted(found - set(REVIEWED_PRICE_TABLE_FILES))}; "
        f"no longer naming a price table: {sorted(set(REVIEWED_PRICE_TABLE_FILES) - found)}"
    )
