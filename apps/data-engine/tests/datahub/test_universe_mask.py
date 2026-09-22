"""Unit tests for Dynamic Point-in-Time Universe Masking (#101).

Tests:
- Dynamic PIT universe filter:
  - Unlisted: listing_date > t -> eligible=False, reason_code='unlisted'
  - Insufficient history: listing_date <= t but lookback < min_periods -> eligible=False, reason_code='unavailable:insufficient_history'
  - Suspended / missing bar: eligible=False, reason_code='suspended'
  - Eligible and active: eligible=True, reason_code='ok'
- Strictly NO future data used (adding/mutating future bars has zero effect on cutoff t)
- Multi-cutoff lifecycle progression test (unlisted -> building history -> eligible -> suspended -> resumed)
- Batch multi-symbol evaluation
- Database persistence to staging.universe_mask
"""

from __future__ import annotations

import os
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pytest
from data_engine.datahub.universe_mask import (
    ALLOWED_SOURCE_TABLES,
    UniverseMaskReason,
    UniverseMaskRecord,
    _extract_bar_date,
    compute_and_persist_universe_mask_from_db,
    compute_universe_mask,
    evaluate_symbol_pit,
    insert_universe_mask,
    persist_universe_mask,
)


def _generate_monthly_bars(
    symbol: str,
    start_date: date,
    num_months: int,
    base_price: float = 100.0,
    zero_vol_months: set[int] | None = None,
) -> list[dict[str, Any]]:
    """Generate mock monthly bars snapped to end of month."""
    from data_engine.datahub.market_prices import last_xnys_session_of_month

    bars: list[dict[str, Any]] = []
    y = start_date.year
    m = start_date.month
    zero_vols = zero_vol_months or set()

    for i in range(num_months):
        session_date = last_xnys_session_of_month(y, m)
        vol = Decimal("0") if i in zero_vols else Decimal("1000000")
        bars.append(
            {
                "symbol": symbol,
                "date": session_date,
                "open": Decimal(str(base_price + i)),
                "high": Decimal(str(base_price + i + 2)),
                "low": Decimal(str(base_price + i - 1)),
                "close": Decimal(str(base_price + i + 1)),
                "volume": vol,
            }
        )
        # Advance month
        m += 1
        if m > 12:
            m = 1
            y += 1
    return bars


# -----------------------------------------------------------------------------
# Core PIT Eligibility Tests
# -----------------------------------------------------------------------------


def test_unlisted_when_listing_date_after_cutoff() -> None:
    cutoff = date(2025, 6, 30)
    ipo_date = date(2025, 9, 15)  # IPO in the future

    bars = _generate_monthly_bars("NEWCO", date(2025, 10, 1), 5)
    rec = evaluate_symbol_pit(
        symbol="NEWCO",
        cutoff_date=cutoff,
        listing_date=ipo_date,
        bars=bars,
        min_periods=12,
    )
    assert not rec.eligible
    assert rec.reason_code == UniverseMaskReason.UNLISTED
    assert rec.symbol == "NEWCO"
    assert rec.cutoff_date == cutoff


def test_insufficient_history_when_fewer_than_min_periods() -> None:
    ipo_date = date(2025, 1, 15)
    # 6 months of bars
    bars = _generate_monthly_bars("GROWTH", date(2025, 1, 1), 6)
    cutoff = bars[-1]["date"]

    rec = evaluate_symbol_pit(
        symbol="GROWTH",
        cutoff_date=cutoff,
        listing_date=ipo_date,
        bars=bars,
        min_periods=12,
    )
    assert not rec.eligible
    assert rec.reason_code == UniverseMaskReason.INSUFFICIENT_HISTORY


def test_suspended_when_missing_bar_at_cutoff() -> None:
    # 15 months of history, but no bar for current cutoff month
    bars = _generate_monthly_bars("HALTED", date(2024, 1, 1), 15)
    # bars cover up to March 2025. Cutoff is June 2025 (3 months missing)
    from data_engine.datahub.market_prices import last_xnys_session_of_month

    cutoff = last_xnys_session_of_month(2025, 6)

    rec = evaluate_symbol_pit(
        symbol="HALTED",
        cutoff_date=cutoff,
        listing_date=date(2023, 1, 1),
        bars=bars,
        min_periods=12,
    )
    assert not rec.eligible
    assert rec.reason_code == UniverseMaskReason.SUSPENDED


def test_suspended_when_volume_is_zero_or_none() -> None:
    # 15 months of history, month 14 (at cutoff) has volume 0
    bars = _generate_monthly_bars("FROZEN", date(2024, 1, 1), 15, zero_vol_months={14})
    cutoff = bars[-1]["date"]

    rec = evaluate_symbol_pit(
        symbol="FROZEN",
        cutoff_date=cutoff,
        listing_date=date(2023, 1, 1),
        bars=bars,
        min_periods=12,
    )
    assert not rec.eligible
    assert rec.reason_code == UniverseMaskReason.SUSPENDED


def test_suspended_when_explicit_flag_set() -> None:
    bars = _generate_monthly_bars("HALT", date(2024, 1, 1), 15)
    bars[-1]["is_suspended"] = True
    cutoff = bars[-1]["date"]

    rec = evaluate_symbol_pit(
        symbol="HALT",
        cutoff_date=cutoff,
        listing_date=date(2023, 1, 1),
        bars=bars,
        min_periods=12,
    )
    assert not rec.eligible
    assert rec.reason_code == UniverseMaskReason.SUSPENDED


def test_eligible_ok_when_history_sufficient_and_active() -> None:
    bars = _generate_monthly_bars("MATURE", date(2024, 1, 1), 24)
    cutoff = bars[-1]["date"]

    rec = evaluate_symbol_pit(
        symbol="MATURE",
        cutoff_date=cutoff,
        listing_date=date(2020, 1, 1),
        bars=bars,
        min_periods=12,
    )
    assert rec.eligible
    assert rec.reason_code == UniverseMaskReason.OK


# -----------------------------------------------------------------------------
# Strictly NO Future Data Used Test
# -----------------------------------------------------------------------------


def test_strictly_no_future_data_used() -> None:
    # Create 36 months of data: Jan 2023 to Dec 2025
    all_bars = _generate_monthly_bars("ALPHA", date(2023, 1, 1), 36)

    # Cutoff at month 14 (March 2024)
    cutoff_month_idx = 14
    cutoff = all_bars[cutoff_month_idx]["date"]

    # Evaluation with ONLY historical bars
    history_only_bars = all_bars[: cutoff_month_idx + 1]
    rec_past_only = evaluate_symbol_pit(
        symbol="ALPHA",
        cutoff_date=cutoff,
        listing_date=date(2022, 1, 1),
        bars=history_only_bars,
        min_periods=12,
    )

    # Evaluation with ALL bars (including 21 months of FUTURE data)
    rec_with_future = evaluate_symbol_pit(
        symbol="ALPHA",
        cutoff_date=cutoff,
        listing_date=date(2022, 1, 1),
        bars=all_bars,
        min_periods=12,
    )

    # Both must be identical: future data must have zero impact
    assert rec_past_only.eligible is True
    assert rec_with_future.eligible is True
    assert rec_past_only.reason_code == rec_with_future.reason_code == UniverseMaskReason.OK

    # Now mutate future bars: simulate future suspension or bankruptcy at month 25
    mutated_all_bars = [dict(b) for b in all_bars]
    for b in mutated_all_bars[20:]:
        b["volume"] = Decimal("0")
        b["close"] = Decimal("0")

    rec_with_future_crash = evaluate_symbol_pit(
        symbol="ALPHA",
        cutoff_date=cutoff,
        listing_date=date(2022, 1, 1),
        bars=mutated_all_bars,
        min_periods=12,
    )
    # The evaluation at cutoff in March 2024 remains completely unaffected
    assert rec_with_future_crash.eligible is True
    assert rec_with_future_crash.reason_code == UniverseMaskReason.OK


# -----------------------------------------------------------------------------
# Multi-Cutoff Lifecycle Progression Test
# -----------------------------------------------------------------------------


def test_multi_cutoff_lifecycle_progression() -> None:
    # Stock IPOs on 2024-04-15
    ipo_date = date(2024, 4, 15)
    # Generates 24 months of bars starting 2024-04 (April 2024 to March 2026)
    # Month index 16 (August 2025) has a trading suspension (vol=0)
    bars = _generate_monthly_bars("CYCLE", date(2024, 4, 1), 24, zero_vol_months={16})

    from data_engine.datahub.market_prices import last_xnys_session_of_month

    cutoffs = [
        last_xnys_session_of_month(2024, 1),  # 1. Before IPO -> unlisted
        last_xnys_session_of_month(2024, 6),  # 2. 3 months after IPO (<12m) -> insufficient_history
        last_xnys_session_of_month(2025, 4),  # 3. Exactly 13 months after IPO -> ok
        bars[16]["date"],  # 4. August 2025: suspended
        last_xnys_session_of_month(2025, 10),  # 5. October 2025: resumed -> ok
    ]

    records = compute_universe_mask(
        symbols=["CYCLE"],
        cutoff_dates=cutoffs,
        prices=bars,
        listing_dates={"CYCLE": ipo_date},
        min_periods=12,
    )

    recs_by_cutoff = {r.cutoff_date: r for r in records}

    # 1. Before IPO
    assert not recs_by_cutoff[cutoffs[0]].eligible
    assert recs_by_cutoff[cutoffs[0]].reason_code == UniverseMaskReason.UNLISTED

    # 2. Insufficient history (3 months)
    assert not recs_by_cutoff[cutoffs[1]].eligible
    assert recs_by_cutoff[cutoffs[1]].reason_code == UniverseMaskReason.INSUFFICIENT_HISTORY

    # 3. Eligible (>= 12 months)
    assert recs_by_cutoff[cutoffs[2]].eligible
    assert recs_by_cutoff[cutoffs[2]].reason_code == UniverseMaskReason.OK

    # 4. Suspended (vol = 0)
    assert not recs_by_cutoff[cutoffs[3]].eligible
    assert recs_by_cutoff[cutoffs[3]].reason_code == UniverseMaskReason.SUSPENDED

    # 5. Resumed
    assert recs_by_cutoff[cutoffs[4]].eligible
    assert recs_by_cutoff[cutoffs[4]].reason_code == UniverseMaskReason.OK


# -----------------------------------------------------------------------------
# Batch Multi-Symbol Evaluation Tests
# -----------------------------------------------------------------------------


def test_batch_compute_universe_mask() -> None:
    from data_engine.datahub.market_prices import last_xnys_session_of_month

    cutoff = last_xnys_session_of_month(2026, 3)

    bars_aapl = _generate_monthly_bars("AAPL", date(2020, 1, 1), 75)
    bars_new = _generate_monthly_bars("NEWCO", date(2025, 10, 1), 6)
    bars_halted = _generate_monthly_bars("HALT", date(2020, 1, 1), 75, zero_vol_months={74})

    all_prices = bars_aapl + bars_new + bars_halted

    records = compute_universe_mask(
        symbols=["AAPL", "NEWCO", "HALT"],
        cutoff_dates=[cutoff],
        prices=all_prices,
        listing_dates={
            "AAPL": date(1980, 12, 12),
            "NEWCO": date(2025, 10, 1),
            "HALT": date(2010, 1, 1),
        },
        min_periods=12,
    )

    by_sym = {r.symbol: r for r in records}
    assert by_sym["AAPL"].eligible is True
    assert by_sym["AAPL"].reason_code == UniverseMaskReason.OK

    assert by_sym["NEWCO"].eligible is False
    assert by_sym["NEWCO"].reason_code == UniverseMaskReason.INSUFFICIENT_HISTORY

    assert by_sym["HALT"].eligible is False
    assert by_sym["HALT"].reason_code == UniverseMaskReason.SUSPENDED


# -----------------------------------------------------------------------------
# Database Persistence Tests
# -----------------------------------------------------------------------------


def test_persist_universe_mask_to_postgres() -> None:
    import psycopg
    from data_engine.config import settings

    try:
        conn = psycopg.connect(settings.database_url, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail("Postgres required by environment but unreachable")
        pytest.skip("Postgres unreachable")

    cutoff = date(2026, 3, 31)
    records = [
        UniverseMaskRecord("TEST.AAPL", cutoff, True, UniverseMaskReason.OK),
        UniverseMaskRecord("TEST.NEW", cutoff, False, UniverseMaskReason.INSUFFICIENT_HISTORY),
    ]

    try:
        # First persist
        inserted = persist_universe_mask(conn, records)
        assert inserted == 2

        with conn.cursor() as cur:
            cur.execute(
                "select symbol, eligible, reason_code from staging.universe_mask where cutoff_date = %s and symbol in ('TEST.AAPL', 'TEST.NEW')",
                (cutoff,),
            )
            rows = dict((r[0], (r[1], r[2])) for r in cur.fetchall())
            assert rows["TEST.AAPL"] == (True, "ok")
            assert rows["TEST.NEW"] == (False, "unavailable:insufficient_history")

        # Idempotent update
        updated = [
            UniverseMaskRecord("TEST.NEW", cutoff, False, UniverseMaskReason.SUSPENDED),
        ]
        persist_universe_mask(conn, updated)

        with conn.cursor() as cur:
            cur.execute(
                "select eligible, reason_code from staging.universe_mask where symbol = 'TEST.NEW' and cutoff_date = %s",
                (cutoff,),
            )
            row = cur.fetchone()
            assert row == (False, "suspended")

    finally:
        with conn.cursor() as cur:
            cur.execute("delete from staging.universe_mask where symbol in ('TEST.AAPL', 'TEST.NEW')")
        conn.close()


def test_extract_bar_date_normalizes_datetime_to_date() -> None:
    dt = datetime(2026, 3, 31, 15, 30, 0)
    res = _extract_bar_date({"date": dt})
    assert res == date(2026, 3, 31)
    assert type(res) is date
    assert not isinstance(res, datetime)


def test_monthly_resolution_requires_exact_snapped_session() -> None:
    from data_engine.datahub.market_prices import last_xnys_session_of_month

    cutoff = last_xnys_session_of_month(2026, 3)
    bars = _generate_monthly_bars("MIDMONTH", date(2025, 1, 1), 15)
    # Mutate the last bar's date to be mid-month rather than last XNYS session
    bars[-1]["date"] = date(2026, 3, 15)

    rec = evaluate_symbol_pit(
        symbol="MIDMONTH",
        cutoff_date=cutoff,
        listing_date=date(2024, 1, 1),
        bars=bars,
        min_periods=12,
        resolution="1M",
    )
    assert not rec.eligible
    assert rec.reason_code == UniverseMaskReason.SUSPENDED


def test_compute_and_persist_universe_mask_from_db_validates_source_table() -> None:
    assert "staging.market_prices_daily" in ALLOWED_SOURCE_TABLES
    assert "staging.market_prices_monthly" in ALLOWED_SOURCE_TABLES
    assert "staging.mvp_market_prices" in ALLOWED_SOURCE_TABLES

    with pytest.raises(ValueError, match="Invalid source_table:"):
        compute_and_persist_universe_mask_from_db(
            None,  # type: ignore[arg-type]
            ["AAPL"],
            [date(2026, 3, 31)],
            source_table="staging.malicious_table",
        )


def test_universe_mask_record_confidence_and_raw_ref_defaults() -> None:
    rec = UniverseMaskRecord(
        symbol="AAPL",
        cutoff_date=date(2026, 3, 31),
        eligible=True,
        reason_code=UniverseMaskReason.OK,
    )
    assert rec.confidence == Decimal("1.0")
    assert rec.raw_ref is None

    custom = UniverseMaskRecord(
        symbol="AAPL",
        cutoff_date=date(2026, 3, 31),
        eligible=True,
        reason_code=UniverseMaskReason.OK,
        confidence=Decimal("0.85"),
        raw_ref="raw.fetches:456",
    )
    assert custom.confidence == Decimal("0.85")
    assert custom.raw_ref == "raw.fetches:456"


def test_insert_universe_mask_alias() -> None:
    assert insert_universe_mask is persist_universe_mask
