"""moomoo OpenD as the third price origin and the second financial-fact origin (#579 A3, #771).

Three guarantees live here, none of which needs OpenD:

1. **Quantity.** The K-line origin asserts the settled regular-session close at/before the
   price cutoff and refuses an in-progress bar, an intraday stamp or a bar after the
   partition (#535/#637). The financials origin asserts ANNUAL figures on the SEC
   `period_end` axis — moomoo's UTC-rendered period stamp lands the day before the
   calendar period end, and a Q4 row is a quarter, never a year.
2. **Calibration.** The statement field ids are numeric and undocumented; the mapping is
   measured here against the SEC XBRL facts for the four captured issuers (byte-pinned
   samples from the 2026-07-10 reconnaissance capture) and the declared 1% tolerance is
   the one those measurements clear. A changed vendor definition turns this red.
3. **Gate and ledger.** Every OpenD call goes through `sources.moomoo._call`: it is gated
   by the monthly backstop, paced, and recorded, and a refused call leaves the cell
   honestly single-origin.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from data_engine.contracts.datahub import CaptureWorkItem
from data_engine.datahub.capture_core import moomoo_origin as origin_module
from data_engine.datahub.capture_core.concept_mapping import DEFAULT_RULESET
from data_engine.datahub.capture_core.corroboration_audit import corroboration_tally
from data_engine.datahub.capture_core.executor import FetchSuccess
from data_engine.datahub.capture_core.market_price_adapter import (
    CorroboratingOrigin,
    MarketPriceAdapter,
    MarketPriceQuote,
    MarketPriceTarget,
)
from data_engine.datahub.capture_core.moomoo_origin import (
    BALANCE_SHEET,
    BALANCE_SHEET_FIELDS,
    CONSENSUS_GROWTH_KEY,
    CONSENSUS_LOOKBACK_DAYS,
    INCOME_FIELDS,
    INCOME_STATEMENT,
    MoomooFinancialsFetcher,
    MoomooKlineFetcher,
    NotASessionCloseError,
    OpenDClient,
    annual_values_by_period_end,
    canonical_bytes,
    moomoo_code,
    parse_annual_financials,
    parse_consensus_growth,
    parse_settled_close,
    period_end_of,
    valuation_detail_key,
)
from data_engine.datahub.capture_core.parser_identity import PARSER_VERSION
from data_engine.datahub.capture_core.sec_financial_adapter import (
    FinancialFactCorroboratingOrigin,
    SecFinancialFactAdapter,
    SecTarget,
    SnapshotValue,
    build_bundle,
    resolve_field,
)
from data_engine.datahub.capture_core.sec_financial_adapter import (
    annual_values_by_period_end as sec_annual_values_by_period_end,
)
from data_engine.datahub.fetch_key import FetchKey
from data_engine.datahub.quality_report import (
    _SOURCE_BY_PARSER,
    classify_financial_fact_entry,
    reconcile_financial_fact_entries,
)
from data_engine.sources import gateway
from data_engine.sources import moomoo as mm
from data_engine.sources import moomoo_ledger as ledger
from factors.snapshot_inputs import OperatingBranch
from truealpha_contracts.models import DataSource
from truealpha_runtime.reconciliation import ReconciliationOutcome
from truealpha_runtime.reconciliation_policies import (
    FINANCIAL_FACT_RECONCILIATION_POLICY,
    RECONCILIATION_POLICY,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "support"))
from moomoo_valuation_sample import XOM_2026_11_20, XOM_2026_11_21  # noqa: E402

_SAMPLES = Path(__file__).resolve().parents[2] / "samples"
# The 2026-07-10 reconnaissance capture, byte-pinned: a hand-edited fixture turns this red.
_MOOMOO_CASSETTES = {
    "DDOG": "0971fe838f5d2dbac2242054ff42e176fbca804bf84901b87f66f814ccb9b0b4",
    "DUOL": "eed7cd4264aca71b442ab7abb4affe1bebbc8f4d44a8dcfa8fb592929d95a813",
    "NICE": "999d396bfbd517953cdce2c48af26178994e91b497636740dfbe50deacfdab66",
    "SHOP": "b2d3e0aa464abc5d8a7504b811a333edc0b80744b6557234d9931a607fd4b55e",
}
_SEC_CASSETTES = {
    "DDOG": "DDOG_CIK0001561550.json",
    "DUOL": "DUOL_CIK0001562088.json",
    "NICE": "NICE_CIK0001003935.json",
    "SHOP": "SHOP_CIK0001594805.json",
}
# Both sample sets were captured 2026-07-09/10: FY2025 filings are on file for all four.
_CUTOFF = date(2026, 7, 10)
_PARTITION = date(2026, 7, 29)


def _moomoo_sample(ticker: str) -> dict[str, Any]:
    body = (_SAMPLES / "moomoo" / f"{ticker}.json").read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    assert digest == _MOOMOO_CASSETTES[ticker], f"samples/moomoo/{ticker}.json changed: {digest}"
    return json.loads(body)


def _statements(ticker: str) -> tuple[dict[str, Any], dict[str, Any]]:
    sample = _moomoo_sample(ticker)
    return sample["financials_income"]["data"], sample["financials_balance_sheet"]["data"]


def _sec_facts(ticker: str) -> dict[str, Any]:
    return json.loads((_SAMPLES / "sec" / _SEC_CASSETTES[ticker]).read_bytes())


class _CassetteClient:
    """`MoomooClient` over captured statements and scripted bars; records every call."""

    def __init__(
        self,
        *,
        bars: dict[str, list[dict[str, Any]]] | None = None,
        statements: dict[str, tuple[dict[str, Any], dict[str, Any]]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.bars = bars or {}
        self.statements = statements or {}
        self.error = error
        self.calls: list[tuple[str, ...]] = []

    def history_kline(self, code: str, *, start: date, end: date, identity: FetchKey) -> list[dict[str, Any]]:
        self.calls.append(("history_kline", code, start.isoformat(), end.isoformat()))
        if self.error is not None:
            raise self.error
        return self.bars[code]

    def financial_statements(self, code: str, *, statement_type: int, identity: FetchKey) -> dict[str, Any]:
        self.calls.append(("financial_statements", code, str(statement_type)))
        if self.error is not None:
            raise self.error
        income, balance = self.statements[code]
        return {INCOME_STATEMENT: income, BALANCE_SHEET: balance}[statement_type]


def _bar(day: str, close: float, *, stamp: str = " 00:00:00") -> dict[str, Any]:
    """One daily bar in the shape `request_history_kline` decodes (KL_FIELD.ALL)."""
    return {
        "code": "US.AAPL",
        "name": "Apple",
        "time_key": f"{day}{stamp}",
        "open": close - 1.5,
        "close": close,
        "high": close + 2.0,
        "low": close - 2.5,
        "pe_ratio": 32.1,
        "turnover_rate": 0.4,
        "volume": 41_000_000,
        "turnover": 41_000_000 * close,
        "change_rate": 0.3,
        "last_close": close - 1.0,
    }


_SETTLED_WINDOW = [_bar("2026-07-27", 334.55), _bar("2026-07-28", 337.10), _bar("2026-07-29", 338.19)]


# -- market price: the accepted and the refused quantities ------------------------------


def test_the_settled_close_at_the_partition_is_accepted() -> None:
    raw = canonical_bytes(_SETTLED_WINDOW)
    quote = parse_settled_close(raw, partition=_PARTITION)
    assert quote is not None
    assert quote.close == Decimal("338.19") and isinstance(quote.close, Decimal)
    assert quote.as_of == _PARTITION
    assert quote.knowable_at.date() <= _PARTITION, "a corroboration knowable after the cutoff is look-ahead"
    assert quote.raw_bytes == raw, "the landed bytes are what was parsed"


def test_the_settled_bar_carries_open_high_low_and_volume() -> None:
    """The K-line bar is the regular session's (unadjusted, no extended hours), so every
    field of it is an independent assertion for the fused bar, not only the close."""
    quote = parse_settled_close(canonical_bytes(_SETTLED_WINDOW), partition=_PARTITION)
    assert quote is not None
    assert (quote.open, quote.high, quote.low, quote.volume) == (
        Decimal("336.69"),
        Decimal("340.19"),
        Decimal("335.69"),
        Decimal("41000000"),
    )
    assert all(isinstance(v, Decimal) for v in (quote.open, quote.high, quote.low, quote.volume))


def test_a_bar_missing_a_field_asserts_nothing_for_it() -> None:
    bar = {k: v for k, v in _bar("2026-07-29", 338.19).items() if k not in ("volume", "open")}
    quote = parse_settled_close(canonical_bytes([bar]), partition=_PARTITION)
    assert quote is not None and quote.close == Decimal("338.19")
    assert quote.open is None and quote.volume is None
    assert quote.high == Decimal("340.19")


def test_a_bar_after_the_partition_is_refused() -> None:
    """The in-progress bar (#535): the cutoff is the last SETTLED session, so any later
    bar is the running one — or look-ahead on a replay. Neither may corroborate."""
    with pytest.raises(NotASessionCloseError, match="after the 2026-07-29 partition"):
        parse_settled_close(canonical_bytes([*_SETTLED_WINDOW, _bar("2026-07-30", 340.08)]), partition=_PARTITION)


def test_an_intraday_stamp_is_refused() -> None:
    with pytest.raises(NotASessionCloseError, match="instant, not a session date"):
        parse_settled_close(canonical_bytes([_bar("2026-07-29", 340.08, stamp=" 15:59:00")]), partition=_PARTITION)


def test_a_holiday_partition_falls_back_to_the_last_settled_session() -> None:
    """The primary's `<=` max-pick on a market holiday resolves the prior session; the
    third origin must resolve the same one, not report nothing."""
    quote = parse_settled_close(canonical_bytes(_SETTLED_WINDOW[:2]), partition=_PARTITION)
    assert quote is not None and (quote.as_of, quote.close) == (date(2026, 7, 28), Decimal("337.10"))


def test_no_bars_means_absent() -> None:
    assert parse_settled_close(canonical_bytes([]), partition=_PARTITION) is None


def test_the_fetcher_asks_for_a_bounded_window_ending_on_the_cutoff() -> None:
    client = _CassetteClient(bars={"US.AAPL": _SETTLED_WINDOW})
    quote = MoomooKlineFetcher(client)("AAPL", _PARTITION)
    assert quote is not None and quote.close == Decimal("338.19")
    assert client.calls == [("history_kline", "US.AAPL", "2026-07-15", "2026-07-29")], (
        "the window must end ON the settled cutoff so the running session is never inside it"
    )


def test_a_failing_client_leaves_the_cell_absent_and_is_not_retried_into_a_number(caplog) -> None:
    client = _CassetteClient(error=RuntimeError("OpenD not reachable"))
    fetcher = MoomooKlineFetcher(client)
    with caplog.at_level(logging.WARNING), corroboration_tally() as tally:
        assert fetcher("AAPL", _PARTITION) is None
        assert fetcher("AAPL", _PARTITION) is None
    assert len(client.calls) == 1, "a cached outcome must not re-hit OpenD"
    # Absent, but not silent (#885): one warning naming the origin and the exception
    # type, one count — the cached second answer is not a second loss.
    [warning] = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert "moomoo-kline" in warning.getMessage() and "RuntimeError" in warning.getMessage()
    assert warning.exc_info is not None
    assert tally.summary() == "corroborations refused 1 (moomoo-kline fetch 1)"


def test_the_market_price_adapter_attaches_the_third_origin_under_moomoos_own_source() -> None:
    item = CaptureWorkItem(
        campaign_id="capture-campaign:" + "1" * 64,
        source_request_id="source-request:" + "3" * 64,
        schedule_policy_id="schedule-policy:" + "2" * 64,
    )
    target = MarketPriceTarget("AAPL", _PARTITION, "issuer:cik:320193", "security:cusip:037833100", "listing:xnas:aapl")

    def primary(symbol: str, cutoff: date) -> MarketPriceQuote:
        return MarketPriceQuote(b"yahoo", Decimal("338.19"), cutoff, datetime.combine(cutoff, datetime.min.time(), UTC))

    third = CorroboratingOrigin(
        origin=origin_module.KLINE_ORIGIN,
        parser_version=origin_module.KLINE_PARSER_VERSION,
        mapping_version=origin_module.KLINE_MAPPING_VERSION,
        value_key=origin_module.KLINE_VALUE_KEY,
        fetch=MoomooKlineFetcher(_CassetteClient(bars={"US.AAPL": _SETTLED_WINDOW})),
        raw_source=DataSource.MOOMOO,
    )
    result = MarketPriceAdapter({item.work_item_id: target}, primary, corroborating_origins=(third,)).fetch(item)
    assert isinstance(result, FetchSuccess)
    (corroboration,) = result.corroborations
    assert corroboration.origin == "moomoo-kline"
    assert corroboration.record.payload["close"] == "338.19"
    assert corroboration.record.parser_version == origin_module.KLINE_PARSER_VERSION
    assert corroboration.raw.source is DataSource.MOOMOO, "moomoo bytes land under moomoo's prefix, not Yahoo's"
    assert corroboration.raw.record_id == "moomoo-kline:AAPL:2026-07-29"


# -- financial facts: the SEC period axis ------------------------------------------------


def test_a_period_end_lands_on_the_sec_axis_not_on_its_utc_rendering() -> None:
    income, _ = _statements("DDOG")
    fy2025 = next(r for r in income["report_list"] if r["period_text"] == "2025/FY")
    assert fy2025["date_time_str"] == "2025-12-30", "the vendor's own UTC rendering is the day before"
    assert period_end_of(fy2025) == date(2025, 12, 31), "the SEC period_end for DDOG's FY2025 is 2025-12-31"


def test_only_annual_rows_are_annual() -> None:
    income, _ = _statements("DDOG")
    annual = annual_values_by_period_end(income, INCOME_FIELDS)
    fy_rows = [r for r in income["report_list"] if r["financial_type"] == 7]
    assert len(annual) == len(fy_rows) >= 5
    assert annual[date(2025, 12, 31)]["revenue"] == Decimal("3427158000")
    q4_revenue = Decimal("953194000")
    assert all(values["revenue"] != q4_revenue for values in annual.values()), "a Q4 row is a quarter, never a year"


def test_the_assertion_carries_the_newest_period_at_or_before_the_cutoff() -> None:
    income, balance = _statements("DDOG")
    raw = canonical_bytes({"income": income, "balance_sheet": balance})
    assertion = parse_annual_financials(raw, cutoff=_CUTOFF)
    assert assertion is not None
    assert assertion.period_end == date(2025, 12, 31)
    assert assertion.values["revenue"] == Decimal("3427158000")
    assert assertion.values["gross_profit"] == Decimal("2740201000")
    assert assertion.values["net_income"] == Decimal("107741000")
    assert assertion.values["eps_diluted"] == Decimal("0.31")
    assert assertion.values["total_assets"] == Decimal("6643844000")
    assert assertion.currency == "USD" and assertion.accounting_standards == "US_GAAP"
    from data_engine.datahub.capture_core.source_registrations import AsOfAxis, knowable_instant

    assert assertion.knowable_at == knowable_instant(AsOfAxis.SETTLED_SESSION, date(2026, 7, 10))
    # A cutoff before FY2025's period end must not see FY2025 at all.
    earlier = parse_annual_financials(raw, cutoff=date(2025, 6, 30))
    assert earlier is not None and earlier.period_end == date(2024, 12, 31)
    assert date(2025, 12, 31) not in earlier.by_period_end


def _sec_series(facts: dict[str, Any], field: str) -> dict[date, Decimal]:
    if field in ("eps_basic", "eps_diluted"):
        concept = "EarningsPerShareBasic" if field == "eps_basic" else "EarningsPerShareDiluted"
        series = sec_annual_values_by_period_end(facts, "us-gaap", concept, "USD/shares", _CUTOFF)
    else:
        series = resolve_field(facts, DEFAULT_RULESET, field, _CUTOFF)
    return {end: datum.value for end, datum in series.items()}


@pytest.mark.parametrize("ticker", sorted(_MOOMOO_CASSETTES))
def test_moomoo_statements_equal_the_sec_facts_within_the_declared_tolerance(ticker: str) -> None:
    """The calibration the field-id map and the 1% tolerance rest on, measured on the
    captured bytes: revenue, gross profit, total assets and EPS are byte-equal to the XBRL
    facts at the same period end; net income differs only where the definitions do
    (NICE: ProfitLoss vs NetIncomeLoss, 0.70%), inside the policy's tolerance."""
    income, balance = _statements(ticker)
    moomoo = parse_annual_financials(canonical_bytes({"income": income, "balance_sheet": balance}), cutoff=_CUTOFF)
    assert moomoo is not None
    facts = _sec_facts(ticker)
    tolerance = FINANCIAL_FACT_RECONCILIATION_POLICY.relative_tolerance
    for field in (*INCOME_FIELDS, *BALANCE_SHEET_FIELDS):
        sec = _sec_series(facts, field)
        shared = sorted(set(sec) & set(moomoo.by_period_end))[-3:]
        assert len(shared) >= 2, f"{ticker}.{field}: too few shared periods to calibrate on"
        for end in shared:
            ours, theirs = moomoo.by_period_end[end][field], sec[end]
            assert ours is not None, f"{ticker}.{field}@{end}: moomoo carries nothing"
            if field == "net_income":
                field_tolerance = Decimal("0.01") if ticker == "NICE" else tolerance
                assert abs(ours - theirs) <= field_tolerance * max(abs(ours), abs(theirs)), (
                    f"{ticker}.net_income@{end}: {ours} vs {theirs} is outside the declared tolerance"
                )
            else:
                assert ours == theirs, f"{ticker}.{field}@{end}: moomoo {ours} != SEC {theirs}"


# -- financial facts: through the adapter and into the fusion engine ----------------------


def _work_item(digest: str) -> CaptureWorkItem:
    return CaptureWorkItem(
        campaign_id="capture-campaign:" + "1" * 64,
        source_request_id="source-request:" + digest,
        schedule_policy_id="schedule-policy:" + "2" * 64,
    )


def _sec_target(ticker: str, cutoff: date = _CUTOFF) -> SecTarget:
    return SecTarget(
        cik=1,
        cutoff=cutoff,
        issuer_id="issuer:cik:1",
        instrument_id="security:cusip:Y",
        listing_id=f"listing:xnas:{ticker.lower()}",
        operating_branch=OperatingBranch.NON_FINANCIAL,
        ticker=ticker,
    )


def _moomoo_financials_origin(client: _CassetteClient) -> FinancialFactCorroboratingOrigin:
    return FinancialFactCorroboratingOrigin(
        origin=origin_module.FINANCIALS_ORIGIN,
        parser_version=origin_module.FINANCIALS_PARSER_VERSION,
        mapping_version=origin_module.FINANCIALS_MAPPING_VERSION,
        fetch=MoomooFinancialsFetcher(client),
    )


def _capture_with_second_origin(ticker: str, *, cutoff: date = _CUTOFF, client: _CassetteClient | None = None):
    client = client or _CassetteClient(statements={moomoo_code(ticker): _statements(ticker)})
    facts = _sec_facts(ticker)
    item = _work_item("4" * 64)
    adapter = SecFinancialFactAdapter(
        {item.work_item_id: _sec_target(ticker, cutoff)},
        lambda cik, cutoff, branch: build_bundle(facts, cutoff, branch),
        corroborating_origins=(_moomoo_financials_origin(client),),
    )
    result = adapter.fetch(item)
    assert isinstance(result, FetchSuccess)
    return result, client


def _entries(result: FetchSuccess, *, corroboration_payload: dict[str, Any] | None = None):
    assert result.record is not None
    primary = classify_financial_fact_entry(
        PARSER_VERSION,
        result.transaction_time,
        result.normalized_sha256,
        "normalized-observation:" + "a" * 64,
        "source-vintage:" + "b" * 64,
        "raw-object:" + "c" * 64,
        result.record.payload,
    )
    assert primary is not None and primary.is_primary
    entries = [primary]
    for index, corroboration in enumerate(result.corroborations):
        payload = corroboration_payload if corroboration_payload is not None else corroboration.record.payload
        second = classify_financial_fact_entry(
            corroboration.record.parser_version,
            corroboration.transaction_time,
            hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
            f"normalized-observation:{index:064x}",
            "source-vintage:" + "e" * 64,
            "raw-object:" + "f" * 64,
            payload,
        )
        assert second is not None and not second.is_primary
        entries.append(second)
    return entries


def test_the_adapter_attaches_the_second_origin_as_its_own_vendors_bytes() -> None:
    result, client = _capture_with_second_origin("NICE")
    assert [call[0] for call in client.calls] == ["financial_statements", "financial_statements"], (
        "one income statement and one balance sheet per issuer per run"
    )
    (corroboration,) = result.corroborations
    assert corroboration.origin == "moomoo-financials"
    assert corroboration.raw.source is DataSource.MOOMOO
    assert corroboration.raw.record_id == "moomoo-financials:NICE:2025-12-31"
    payload = corroboration.record.payload
    assert payload["period_end"] == "2025-12-31"
    assert payload["revenue"] == "2945399000" and payload["total_assets"] == "5106030000"
    assert payload["eps_diluted"] == "9.67", "EPS travels even though the primary has no EPS to reconcile yet"
    assert list(payload["by_period_end"]) == sorted(payload["by_period_end"]), "hash-stable order"
    assert corroboration.transaction_time.date() <= _CUTOFF
    # Repeated fetches for the same issuer come from the cache, never a third call.
    assert result.record is not None and result.record.payload["revenue"] == "2945399000"


def test_the_fusion_agrees_on_every_field_the_samples_share() -> None:
    """NICE is the hardest of the four: net income differs by definition (0.70%) and the
    cell must still reconcile under the declared tolerance — that is what the tolerance
    is FOR. A 3% shift on one field abstains that field and the subject."""
    result, _ = _capture_with_second_origin("NICE")
    cutoff = datetime(2026, 7, 10, 22, 15, tzinfo=UTC)
    cell = reconcile_financial_fact_entries("listing:xnas:nice", _entries(result), cutoff=cutoff)
    assert cell["outcome"] == ReconciliationOutcome.AGREED.value, cell
    assert set(cell["fields"]) == {"revenue", "gross_profit", "net_income", "total_assets"}
    for field_name, graded in cell["fields"].items():
        assert graded["outcome"] == ReconciliationOutcome.AGREED.value, (field_name, graded)
        assert graded["origin_groups"] == 2 and graded["period_end"] == "2025-12-31"
        assert graded["selected_source"] == "sec-company-facts:v1", "the primary selects; the second corroborates"
    assert cell["fields"]["net_income"]["selected_value"] == "612101000"

    (corroboration,) = result.corroborations
    shifted = json.loads(json.dumps(corroboration.record.payload))
    shifted["by_period_end"]["2025-12-31"]["revenue"] = str(Decimal("2945399000") * Decimal("1.03"))
    abstained = reconcile_financial_fact_entries(
        "listing:xnas:nice", _entries(result, corroboration_payload=shifted), cutoff=cutoff
    )
    assert abstained["fields"]["revenue"]["outcome"] == ReconciliationOutcome.CONFLICT_PRIORITY_SERVED.value
    assert abstained["fields"]["net_income"]["outcome"] == ReconciliationOutcome.AGREED.value
    assert abstained["outcome"] == ReconciliationOutcome.CONFLICT_PRIORITY_SERVED.value, (
        "one conflicting field marks cell conflict"
    )


def test_a_period_the_primary_has_not_filed_is_never_compared() -> None:
    """Alignment is on the primary's filed period. With a cutoff before the FY2025 10-K,
    the primary asserts FY2024 and the fusion compares FY2024 — moomoo's newer periods,
    whether an early release or a restatement, corroborate nothing the primary lacks."""
    cutoff = date(2025, 6, 30)
    result, _ = _capture_with_second_origin("DDOG", cutoff=cutoff)
    assert result.record is not None and result.record.payload["revenue_period_end"] == "2024-12-31"
    cell = reconcile_financial_fact_entries(
        "listing:xnas:ddog", _entries(result), cutoff=datetime(2025, 6, 30, 22, 15, tzinfo=UTC)
    )
    assert cell["outcome"] == ReconciliationOutcome.AGREED.value
    fields = cell["fields"]
    assert {fields[flow]["period_end"] for flow in ("revenue", "gross_profit", "net_income")} == {"2024-12-31"}
    # The primary's newest total-assets instant is the Q1 2025 10-Q's; moomoo's quarterly
    # balance sheet corroborates that same instant, never a fiscal year the primary lacks.
    assert fields["total_assets"]["period_end"] == "2025-03-31"
    assert "2025-12-31" not in {graded["period_end"] for graded in fields.values()}


def test_a_second_origin_without_the_primarys_period_is_absent_not_conflicting() -> None:
    """Alignment is per field: dropping moomoo's FY2025 leaves the three flows single-origin
    (never a conflict), while `total_assets` — an instant the primary dates at SHOP's
    newest 10-Q — still corroborates at ITS period. The subject is not corroborated."""
    result, _ = _capture_with_second_origin("SHOP")
    (corroboration,) = result.corroborations
    payload = json.loads(json.dumps(corroboration.record.payload))
    payload["by_period_end"].pop("2025-12-31")
    cell = reconcile_financial_fact_entries(
        "listing:xnas:shop",
        _entries(result, corroboration_payload=payload),
        cutoff=datetime(2026, 7, 10, 22, 15, tzinfo=UTC),
    )
    assert cell["outcome"] == ReconciliationOutcome.INSUFFICIENT_INDEPENDENT_ORIGINS.value
    fields = cell["fields"]
    for flow in ("revenue", "gross_profit", "net_income"):
        assert fields[flow]["outcome"] == ReconciliationOutcome.INSUFFICIENT_INDEPENDENT_ORIGINS.value, flow
        assert fields[flow]["period_end"] == "2025-12-31"
    assert fields["total_assets"]["outcome"] == ReconciliationOutcome.AGREED.value
    assert fields["total_assets"]["period_end"] > "2025-12-31", "the primary's newest instant is a 10-Q's"


def test_a_failing_second_origin_leaves_the_financial_cell_single_origin(caplog) -> None:
    with caplog.at_level(logging.WARNING), corroboration_tally() as tally:
        result, _ = _capture_with_second_origin("DUOL", client=_CassetteClient(error=ConnectionError("OpenD down")))
    assert result.corroborations == ()
    assert result.record is not None and result.record.payload["revenue"] == "1037589000", "the primary is untouched"
    [warning] = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert "moomoo-financials" in warning.getMessage() and "ConnectionError" in warning.getMessage()
    assert "DUOL" in warning.getMessage()
    assert tally.summary() == "corroborations refused 1 (moomoo-financials fetch 1)"


def test_a_target_without_a_ticker_asks_no_symbol_keyed_origin() -> None:
    client = _CassetteClient(statements={"US.DUOL": _statements("DUOL")})
    item = _work_item("5" * 64)
    target = SecTarget(
        cik=1,
        cutoff=_CUTOFF,
        issuer_id="i",
        instrument_id="n",
        listing_id="l",
        operating_branch=OperatingBranch.NON_FINANCIAL,
    )
    adapter = SecFinancialFactAdapter(
        {item.work_item_id: target},
        lambda cik, cutoff, branch: build_bundle(_sec_facts("DUOL"), cutoff, branch),
        corroborating_origins=(_moomoo_financials_origin(client),),
    )
    result = adapter.fetch(item)
    assert isinstance(result, FetchSuccess) and result.corroborations == () and client.calls == []


# -- gate and ledger: the deployed client goes through `_call` ---------------------------


class _FakeQuoteContext:
    """What `OpenQuoteContext` answers, in the SDK's return shapes, with the arguments kept."""

    def __init__(self, income: dict[str, Any], balance: dict[str, Any]) -> None:
        self.kline_kwargs: list[dict[str, Any]] = []
        self.statement_types: list[int] = []
        self._statements = {INCOME_STATEMENT: income, BALANCE_SHEET: balance}

    def request_history_kline(self, code: str, **kwargs: Any):
        self.kline_kwargs.append({"code": code, **kwargs})
        return mm.moomoo.RET_OK, pd.DataFrame(_SETTLED_WINDOW), None

    def get_financials_statements(self, code: str, *, statement_type: int, **_: Any):
        self.statement_types.append(statement_type)
        return mm.moomoo.RET_OK, self._statements[statement_type]


@pytest.fixture
def opend(monkeypatch, moomoo_ledger_db):
    """A fake OpenD behind the REAL `sources.moomoo` path, over an isolated ledger database.

    `moomoo_ledger_db` is a throwaway copy, so a developer .env cannot land fake rows in a real ledger."""
    monkeypatch.setattr(mm.settings, "moomoo_opend_host", "opend.test")
    monkeypatch.setattr(mm.settings, "moomoo_opend_port", 11111)
    income, balance = _statements("DDOG")
    ctx = _FakeQuoteContext(income, balance)

    @contextmanager
    def fake_connect():
        yield ctx

    monkeypatch.setattr(mm, "connect", fake_connect)
    return ctx


def test_a_kline_request_is_gated_recorded_and_unadjusted(opend) -> None:
    key = FetchKey("moomoo", "history_kline", "symbol:AAPL", _PARTITION.isoformat())
    bars = OpenDClient().history_kline("US.AAPL", start=_PARTITION - timedelta(days=14), end=_PARTITION, identity=key)
    assert [bar["close"] for bar in bars] == [334.55, 337.10, 338.19]
    assert ledger.calls_this_month() == 1, "every OpenD call is a ledger row"
    expected_raw = canonical_bytes(bars)
    (recorded,) = gateway._pg_execute(
        "select endpoint, caller, ok, payload_sha256, byte_length from staging.api_call_ledger where source = 'moomoo'"
    ).fetchall()
    assert recorded[:3] == ("request_history_kline", "moomoo_origin", True)
    assert recorded[3] == hashlib.sha256(expected_raw).hexdigest(), (
        "ledger payload_sha256 must match the exact canonical_bytes landed in raw.fetches (#1358)"
    )
    assert recorded[4] == len(expected_raw)
    (kwargs,) = opend.kline_kwargs
    assert kwargs["autype"] == mm.moomoo.AuType.NONE, (
        "unadjusted: the primary's raw close, not a forward-adjusted series"
    )
    assert kwargs["extended_time"] is False, "regular session only — a post-market last trade is the #535 quantity"
    assert kwargs["ktype"] == mm.moomoo.KLType.K_DAY
    assert (kwargs["start"], kwargs["end"]) == ("2026-07-15", "2026-07-29")


def test_the_financials_request_is_three_gated_calls_and_a_failed_valuation_detail_keeps_the_statements(
    opend,
) -> None:
    client = OpenDClient()
    assertion = MoomooFinancialsFetcher(client)("DDOG", _CUTOFF)
    assert assertion is not None and assertion.values["revenue"] == Decimal("3427158000")
    growth = assertion.snapshots[CONSENSUS_GROWTH_KEY]
    assert growth.value is None and str(growth.reason).startswith("valuation_fetch_error:")
    assert opend.statement_types == [INCOME_STATEMENT, BALANCE_SHEET]
    assert ledger.calls_this_month() == 3, "income, balance sheet and valuation detail are each a ledger row"


def test_the_7d_kline_quota_refuses_the_call_and_the_cell_stays_single_origin(opend, monkeypatch) -> None:
    monkeypatch.setattr(ledger, "distinct_stock_quota", lambda: 1)
    fetcher = MoomooKlineFetcher(OpenDClient())
    assert fetcher("AAPL", _PARTITION) is not None, "the first stock fits the 7-day quota"
    assert fetcher("MSFT", _PARTITION) is None, "the second distinct stock is refused BEFORE it is spent"
    assert ledger.calls_this_month() == 1
    assert len(opend.kline_kwargs) == 1, "a refused call never reached OpenD"


# -- the origin factories and the identities fusion reads back ----------------------------


def test_no_flag_means_no_origin(monkeypatch) -> None:
    monkeypatch.setattr(origin_module.settings, "moomoo_kline_origin_enabled", False)
    monkeypatch.setattr(origin_module.settings, "moomoo_financials_origin_enabled", False)
    assert origin_module.moomoo_kline_origin() is None
    assert origin_module.moomoo_financials_origin() is None


def test_an_enabled_origin_without_opend_coordinates_refuses_at_route_build(monkeypatch) -> None:
    monkeypatch.setattr(origin_module.settings, "moomoo_kline_origin_enabled", True)
    monkeypatch.setattr(origin_module.settings, "moomoo_financials_origin_enabled", True)
    monkeypatch.setattr(origin_module.settings, "moomoo_opend_host", "")
    monkeypatch.setattr(origin_module.settings, "moomoo_opend_port", 0)
    with pytest.raises(ValueError, match="MOOMOO_OPEND_HOST"):
        origin_module.moomoo_kline_origin()
    with pytest.raises(ValueError, match="MOOMOO_OPEND_HOST"):
        origin_module.moomoo_financials_origin()


def test_the_origins_vintages_are_the_ones_fusion_reads(monkeypatch) -> None:
    """A value-key or priority drift drops an origin out of fusion with no error anywhere
    (the #535 shape): the registry, the policies and the origins are asserted to agree."""
    monkeypatch.setattr(origin_module.settings, "moomoo_kline_origin_enabled", True)
    monkeypatch.setattr(origin_module.settings, "moomoo_financials_origin_enabled", True)
    monkeypatch.setattr(origin_module.settings, "moomoo_opend_host", "opend.test")
    monkeypatch.setattr(origin_module.settings, "moomoo_opend_port", 11111)
    kline = origin_module.moomoo_kline_origin()
    financials = origin_module.moomoo_financials_origin()
    assert kline is not None and financials is not None
    source_id, origin_group, value_key = _SOURCE_BY_PARSER[kline.parser_version]
    assert value_key == kline.value_key == "close"
    assert source_id in RECONCILIATION_POLICY.source_priority, "an unregistered source is silently excluded"
    assert RECONCILIATION_POLICY.source_priority.index(source_id) == 2, "the third origin ranks last"
    assert origin_group == "origin:moomoo-kline:v1"
    source_id, origin_group, _ = _SOURCE_BY_PARSER[financials.parser_version]
    assert source_id == "moomoo-financials:v1" and source_id in FINANCIAL_FACT_RECONCILIATION_POLICY.source_priority
    assert FINANCIAL_FACT_RECONCILIATION_POLICY.source_priority[0] == "sec-company-facts:v1"
    assert kline.raw_source is DataSource.MOOMOO and financials.raw_source is DataSource.MOOMOO


def test_the_price_route_wires_the_third_origin_only_when_enabled(monkeypatch) -> None:
    from data_engine.datahub.capture_core import twelve_data_origin as twelve
    from data_engine.datahub.capture_core.market_price_adapter import build_route
    from data_engine.datahub.capture_core.source_registrations import RouteCell, RouteContext

    monkeypatch.setattr(twelve.settings, "twelve_data_api_key", "")
    context = RouteContext(
        cutoff=datetime(2026, 7, 29, 22, 15, tzinfo=UTC),
        cutoff_date=_PARTITION,
        price_cutoff_date=_PARTITION,
        partition_start=datetime(2026, 6, 30, tzinfo=UTC),
        universe_published_at=None,
        coordinates={},
        connection=None,
    )
    cells = [RouteCell("wi-1", "market-price", "issuer:cik:1", "instrument:1", "listing:xnas:aapl", "AAPL")]
    monkeypatch.setattr(origin_module.settings, "moomoo_kline_origin_enabled", False)
    assert [o.origin for o in build_route(context, cells)._corroborating_origins] == []
    monkeypatch.setattr(origin_module.settings, "moomoo_kline_origin_enabled", True)
    monkeypatch.setattr(origin_module.settings, "moomoo_opend_host", "opend.test")
    monkeypatch.setattr(origin_module.settings, "moomoo_opend_port", 11111)
    assert [o.origin for o in build_route(context, cells)._corroborating_origins] == ["moomoo-kline"]


# -- consensus earnings growth: the real valuation answer and its vendor instant (#1791) ---------


def test_the_real_valuation_answer_gives_the_consensus_growth_in_percent_and_the_vendor_instant() -> None:
    """XOM, stored 2026-11-21: trailing P/E 16.459 and forward P/E 10.861. The price cancels, so the forward
    consensus earnings are 16.459 / 10.861 = 1.515422 times the trailing earnings: growth 51.5422 percent."""
    answer = XOM_2026_11_21.answer()
    growth = parse_consensus_growth(answer)
    assert growth.reason is None
    assert growth.value is not None and growth.value.quantize(Decimal("0.0001")) == Decimal("51.5422")
    assert growth.knowable_at == datetime(2026, 11, 21, 3, 3, 35, tzinfo=UTC), "the epoch field, not the Eastern string"
    assert answer["last_update_time_str"] == "2026-11-20 22:03:35"
    rows = answer["profit_growth_rate"]["profit_data"]
    assert rows and not any("profit_growth_rate" in row for row in rows), "profit_data holds indexed multiples"


def _without(mapping: dict[str, Any], key: str) -> dict[str, Any]:
    return {name: value for name, value in mapping.items() if name != key}


@pytest.mark.parametrize(
    ("edit", "reason"),
    [
        (lambda a: {**a, "trend": {**a["trend"], "current_value": -7.723}}, "non_positive_trailing_pe"),
        (lambda a: {**a, "trend": {**a["trend"], "forward_value": 0.0}}, "non_positive_forward_pe"),
        (lambda a: {**a, "trend": {**a["trend"], "forward_value": -7.723}}, "non_positive_forward_pe"),
        (lambda a: {**a, "trend": _without(a["trend"], "forward_value")}, "missing_forward_pe"),
        (lambda a: {**a, "trend": _without(a["trend"], "current_value")}, "missing_trailing_pe"),
        (lambda a: {**a, "valuation_type": 2}, "not_a_pe_valuation"),
    ],
)
def test_a_non_positive_or_missing_pe_gives_no_growth_and_names_the_reason(edit, reason) -> None:
    """-7.723 is a real vendor P/E: US.SLNG in the `plate_distribution` of the same answer."""
    answer = XOM_2026_11_21.answer()
    assert -7.723 in [item["value"] for item in answer["plate_distribution"]["stock_items"]]
    growth = parse_consensus_growth(edit(answer))
    assert growth == SnapshotValue(None, XOM_2026_11_21.knowable_at, reason)


def test_parse_annual_financials_attaches_the_consensus_growth_only_when_knowable_at_the_cutoff() -> None:
    """The bound is the settled session close of the cutoff: 2026-11-20 21:00 UTC is before the vendor stamp."""
    income, balance = _statements("DDOG")
    raw = canonical_bytes({"income": income, "balance_sheet": balance, "valuation_detail": XOM_2026_11_21.answer()})
    known = parse_annual_financials(raw, cutoff=date(2026, 11, 21))
    assert known is not None
    growth = known.snapshots[CONSENSUS_GROWTH_KEY]
    assert growth.value is not None and growth.value.quantize(Decimal("0.0001")) == Decimal("51.5422")
    assert growth.knowable_at == XOM_2026_11_21.knowable_at
    assert CONSENSUS_GROWTH_KEY not in known.values, "a live snapshot is not an annual figure of the period end"
    early = parse_annual_financials(raw, cutoff=date(2026, 11, 20))
    assert early is not None
    assert early.snapshots[CONSENSUS_GROWTH_KEY] == SnapshotValue(
        None, XOM_2026_11_21.knowable_at, "consensus_after_cutoff"
    )


class _ValuationClient(_CassetteClient):
    """`_CassetteClient` that also answers `valuation_detail` with one real stored answer."""

    def __init__(self, answer: dict[str, Any]) -> None:
        # The DDOG statements only give the assertion a period end; these tests read the valuation answer.
        super().__init__(statements={"US.XOM": _statements("DDOG")})
        self.answer = answer

    def valuation_detail(self, code: str, *, identity: FetchKey) -> dict[str, Any]:
        self.calls.append(("valuation_detail", code, identity.as_of))
        return self.answer


@pytest.fixture
def after_the_session_of_2026_11_20(monkeypatch) -> datetime:
    """The real fetch instant of the 2026-11-21 answer: 03:03:34 UTC, after the 21:00 UTC close of 2026-11-20."""
    instant = datetime(2026, 11, 21, 3, 3, 34, tzinfo=UTC)
    monkeypatch.setattr(gateway, "_utcnow", lambda: instant)
    return instant


@pytest.mark.usefixtures("after_the_session_of_2026_11_20")
def test_the_fetcher_reads_the_stored_answer_of_yesterday_when_todays_answer_is_after_the_cutoff(landed_keys) -> None:
    """The real staging state: the answer of today is stamped after the close of the cutoff session. The answer
    stored under 2026-11-20 is stamped 03:52:54 UTC that day, before the close. The run reads it with no vendor call."""
    landed_keys.bodies[valuation_detail_key("XOM", date(2026, 11, 20))] = XOM_2026_11_20.raw_bytes()
    client = _ValuationClient(XOM_2026_11_21.answer())
    assertion = MoomooFinancialsFetcher(client)("XOM", date(2026, 11, 20))
    assert assertion is not None
    growth = assertion.snapshots[CONSENSUS_GROWTH_KEY]
    assert growth.knowable_at == XOM_2026_11_20.knowable_at
    assert growth.value is not None and growth.value.quantize(Decimal("0.0001")) == Decimal("51.5432")
    vendor_calls = [call for call in client.calls if call[0] == "valuation_detail"]
    assert vendor_calls == [("valuation_detail", "US.XOM", "2026-11-21")], "only the key of today calls the vendor"
    assert json.loads(assertion.raw_bytes)["valuation_detail"] == XOM_2026_11_20.answer(), (
        "the bytes hold what was read"
    )


@pytest.mark.usefixtures("after_the_session_of_2026_11_20")
def test_the_search_checks_the_vendor_stamp_of_each_stored_answer_not_its_key_day(landed_keys) -> None:
    """A stored answer stamped after the cutoff is passed over, and the next older answer that is knowable is read."""
    landed_keys.bodies[valuation_detail_key("XOM", date(2026, 11, 20))] = XOM_2026_11_21.raw_bytes()
    landed_keys.bodies[valuation_detail_key("XOM", date(2026, 11, 19))] = XOM_2026_11_20.raw_bytes()
    assertion = MoomooFinancialsFetcher(_ValuationClient(XOM_2026_11_21.answer()))("XOM", date(2026, 11, 20))
    assert assertion is not None
    assert assertion.snapshots[CONSENSUS_GROWTH_KEY].knowable_at == XOM_2026_11_20.knowable_at


@pytest.mark.usefixtures("after_the_session_of_2026_11_20")
def test_the_fetcher_gives_no_growth_when_no_answer_is_knowable_at_the_cutoff(landed_keys) -> None:
    """The close of 2026-11-19 is before every stored stamp, so no answer qualifies."""
    landed_keys.bodies[valuation_detail_key("XOM", date(2026, 11, 20))] = XOM_2026_11_20.raw_bytes()
    assertion = MoomooFinancialsFetcher(_ValuationClient(XOM_2026_11_21.answer()))("XOM", date(2026, 11, 19))
    assert assertion is not None
    growth = assertion.snapshots[CONSENSUS_GROWTH_KEY]
    assert growth.value is None and growth.reason == "consensus_after_cutoff"


@pytest.mark.usefixtures("after_the_session_of_2026_11_20")
def test_the_search_reads_no_answer_older_than_the_lookback(landed_keys) -> None:
    """The answer qualifies by its stamp. One day past the window of the look-back, the run does not read it."""
    oldest_read = date(2026, 11, 20) - timedelta(days=CONSENSUS_LOOKBACK_DAYS - 1)
    landed_keys.bodies[valuation_detail_key("XOM", oldest_read - timedelta(days=1))] = XOM_2026_11_20.raw_bytes()
    outside = MoomooFinancialsFetcher(_ValuationClient(XOM_2026_11_21.answer()))("XOM", date(2026, 11, 20))
    assert outside is not None and outside.snapshots[CONSENSUS_GROWTH_KEY].reason == "consensus_after_cutoff"
    landed_keys.bodies[valuation_detail_key("XOM", oldest_read)] = XOM_2026_11_20.raw_bytes()
    inside = MoomooFinancialsFetcher(_ValuationClient(XOM_2026_11_21.answer()))("XOM", date(2026, 11, 20))
    assert inside is not None and inside.snapshots[CONSENSUS_GROWTH_KEY].knowable_at == XOM_2026_11_20.knowable_at


def test_the_corroboration_payload_carries_the_vendor_instant_or_the_reason() -> None:
    """The adapter writes the instant under `vintage`, the key that the strategy seed reads (`vintage_knowable_at`)."""
    from data_engine.datahub.capture_core.sec_financial_adapter import _snapshot_payload
    from data_engine.datahub.input_cells import vintage_knowable_at

    growth = parse_consensus_growth(XOM_2026_11_21.answer())
    payload = _snapshot_payload({CONSENSUS_GROWTH_KEY: growth})
    assert payload[CONSENSUS_GROWTH_KEY] == str(growth.value)
    assert vintage_knowable_at(payload)(CONSENSUS_GROWTH_KEY, None) == XOM_2026_11_21.knowable_at
    refused = _snapshot_payload({CONSENSUS_GROWTH_KEY: SnapshotValue(None, None, "consensus_after_cutoff")})
    assert refused == {"consensus_profit_growth_reason": "consensus_after_cutoff"}


# -- fetch keys (#1411 K3): the second reader of a landed key never asks OpenD ----------


def test_a_second_kline_fetcher_of_the_same_symbol_and_session_makes_no_openD_call() -> None:
    """A new fetcher (the next tick, the next universe) reads the landed bars of the key it shares."""
    first, second = _CassetteClient(bars={"US.AAPL": _SETTLED_WINDOW}), _CassetteClient(bars={"US.AAPL": []})
    quote_one = MoomooKlineFetcher(first)("AAPL", _PARTITION)
    quote_two = MoomooKlineFetcher(second)("AAPL", _PARTITION)
    assert quote_one is not None and quote_two == quote_one
    assert len(first.calls) == 1 and second.calls == [], "the second fetcher reached OpenD for a landed key"


def test_the_financials_fetcher_lands_each_statement_under_its_own_key() -> None:
    """Income statement and balance sheet are two OpenD requests, so they are two keys and two ledger rows."""
    first, second = _CassetteClient(statements={"US.DDOG": _statements("DDOG")}), _CassetteClient(statements={})
    one = MoomooFinancialsFetcher(first)("DDOG", _CUTOFF)
    two = MoomooFinancialsFetcher(second)("DDOG", _CUTOFF)
    assert one is not None and two == one
    assert [call[0] for call in first.calls] == ["financial_statements"] * 2
    assert second.calls == [], "the second fetcher reached OpenD for landed statements"


@pytest.mark.usefixtures("idle_moomoo_gate")
def test_the_moomoo_ledger_row_names_the_key_of_the_fetch_that_made_the_call(call_ledger) -> None:
    """`sources.moomoo._call` records through `record_call`; the verdict counts the row only if it names the key."""

    def kline(ctx):
        return mm.moomoo.RET_OK, []

    key = FetchKey("moomoo", "history_kline", "symbol:AAPL", "2026-07-29")
    mm._call(object(), "request_history_kline", "test", kline, request_uri="US.AAPL", identity=key)
    assert [row.fetch_key for row in call_ledger] == [key.ledger_key]
