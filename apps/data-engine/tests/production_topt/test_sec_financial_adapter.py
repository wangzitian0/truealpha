from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from data_engine.datahub.production_topt.executor import FetchFailure, FetchSuccess
from data_engine.datahub.production_topt.sec_financial_adapter import (
    FinancialFactsBundle,
    HeadcountFact,
    SecFinancialFactAdapter,
    SecTarget,
    SourceUnavailableError,
    annual_values_by_period_end,
    build_bundle,
    gross_profit,
    pre_provision_profit,
)
from factors.production_topt import OperatingBranch
from truealpha_contracts import ObligationReasonCode
from truealpha_contracts.datahub import CaptureWorkItem

_CUTOFF = date(2026, 3, 31)


def _work_item(digest: str) -> CaptureWorkItem:
    return CaptureWorkItem(
        campaign_id="capture-campaign:" + "1" * 64,
        source_request_id="source-request:" + digest,
        schedule_policy_id="schedule-policy:" + "2" * 64,
    )


def _target(cik: int = 1, branch: OperatingBranch = OperatingBranch.NON_FINANCIAL) -> SecTarget:
    return SecTarget(
        cik=cik,
        cutoff=_CUTOFF,
        issuer_id="issuer:lei:X",
        instrument_id="security:cusip:Y",
        listing_id="listing:xnas:goog",
        operating_branch=branch,
    )


def _annual(end: str, start: str, val: object, filed: str) -> dict:
    return {"end": end, "start": start, "val": val, "filed": filed}


def _facts() -> dict:
    return {
        "facts": {
            "us-gaap": {
                "GrossProfit": {
                    "units": {
                        "USD": [
                            _annual("2024-12-31", "2024-01-01", 100, "2025-02-01"),
                            _annual("2025-12-31", "2025-01-01", 120, "2026-02-01"),
                            # Filed after the cutoff — must be excluded (not knowable yet).
                            _annual("2026-03-31", "2025-04-01", 999, "2026-04-20"),
                            # A quarter, not a year: must never win over the annual figure.
                            _annual("2025-12-31", "2025-10-01", 30, "2026-02-01"),
                        ]
                    }
                },
                "Assets": {"units": {"USD": [{"end": "2025-12-31", "val": 500, "filed": "2026-02-01"}]}},
                "CommonStockSharesOutstanding": {
                    "units": {"shares": [{"end": "2026-01-31", "val": 10, "filed": "2026-02-10"}]}
                },
            }
        }
    }


def test_pit_excludes_future_filings_and_quarters() -> None:
    values = annual_values_by_period_end(_facts(), "us-gaap", "GrossProfit", "USD", _CUTOFF)
    assert set(values) == {date(2024, 12, 31), date(2025, 12, 31)}  # the 2026-04-20 filing is not knowable
    assert values[date(2025, 12, 31)].value == Decimal("120")  # the 30 is a quarter, dropped
    assert values[date(2025, 12, 31)].filed == date(2026, 2, 1)


def test_build_bundle_extracts_the_core_fields() -> None:
    bundle = build_bundle(_facts(), _CUTOFF, OperatingBranch.NON_FINANCIAL)
    assert bundle.gross_profit == Decimal("120")
    assert bundle.total_assets == Decimal("500")
    assert bundle.shares_outstanding == Decimal("10")
    assert bundle.pre_provision_profit is None
    assert bundle.knowable_at is not None
    assert bundle.knowable_at.date() <= _CUTOFF


def test_gross_profit_falls_back_to_revenue_minus_cost_over_a_shared_period() -> None:
    facts = {
        "facts": {
            "us-gaap": {
                "Revenues": {"units": {"USD": [_annual("2025-12-31", "2025-01-01", 900, "2026-02-01")]}},
                "CostOfGoodsAndServicesSold": {
                    "units": {"USD": [_annual("2025-12-31", "2025-01-01", 400, "2026-02-01")]}
                },
            }
        }
    }
    datum = gross_profit(facts, _CUTOFF)
    assert datum is not None
    assert datum.value == Decimal("500")


def test_gross_profit_never_mixes_periods() -> None:
    facts = {
        "facts": {
            "us-gaap": {
                "Revenues": {"units": {"USD": [_annual("2025-12-31", "2025-01-01", 900, "2026-02-01")]}},
                "CostOfRevenue": {"units": {"USD": [_annual("2024-12-31", "2024-01-01", 400, "2025-02-01")]}},
            }
        }
    }
    assert gross_profit(facts, _CUTOFF) is None


def _bank_facts() -> dict:
    return {
        "facts": {
            "us-gaap": {
                "RevenuesNetOfInterestExpense": {
                    "units": {"USD": [_annual("2025-12-31", "2025-01-01", 180, "2026-02-01")]}
                },
                "NoninterestExpense": {"units": {"USD": [_annual("2025-12-31", "2025-01-01", 95, "2026-02-01")]}},
                "Assets": {"units": {"USD": [{"end": "2025-12-31", "val": 4900, "filed": "2026-02-01"}]}},
            }
        }
    }


def test_depository_branch_uses_pre_provision_profit_as_the_operating_numerator() -> None:
    datum = pre_provision_profit(_bank_facts(), _CUTOFF)
    assert datum is not None
    assert datum.value == Decimal("85")

    bundle = build_bundle(_bank_facts(), _CUTOFF, OperatingBranch.FINANCIAL)
    # A bank files no GrossProfit; without the branch it would be missing_gross_profit.
    assert bundle.gross_profit == Decimal("85")
    assert bundle.pre_provision_profit == Decimal("85")


def test_non_financial_branch_never_derives_a_bank_proxy() -> None:
    bundle = build_bundle(_bank_facts(), _CUTOFF, OperatingBranch.NON_FINANCIAL)
    assert bundle.gross_profit is None
    assert bundle.pre_provision_profit is None


def test_a_payload_with_no_eligible_fact_is_captured_with_nulls() -> None:
    """A source that publishes nothing is a real, captured state — not a failure.

    XOM's post-reorganization CIK serves a company-facts document with no us-gaap
    taxonomy; the cell must record honest nulls so the factor names the gap.
    """
    item = _work_item("a" * 64)
    empty = {"facts": {"ffd": {}}}
    adapter = SecFinancialFactAdapter(
        {item.work_item_id: _target()}, lambda cik, cutoff, branch: build_bundle(empty, cutoff, branch)
    )
    result = adapter.fetch(item)
    assert isinstance(result, FetchSuccess)
    assert result.record is not None
    assert result.record.payload["gross_profit"] is None
    assert result.record.payload["total_assets"] is None
    assert result.confidence == Decimal("0.50")


def _bundle(**overrides: object) -> FinancialFactsBundle:
    fields: dict = {
        "gross_profit": Decimal("120"),
        "total_assets": Decimal("500"),
        "shares_outstanding": Decimal("10"),
        "revenue": Decimal("900"),
        "pre_provision_profit": None,
        "raw_bytes": b"{}",
        "knowable_at": datetime(2026, 2, 1, tzinfo=UTC),
    }
    fields.update(overrides)
    return FinancialFactsBundle(**fields)  # type: ignore[arg-type]


def test_adapter_success_carries_the_identity_and_branch() -> None:
    item = _work_item("3" * 64)
    adapter = SecFinancialFactAdapter({item.work_item_id: _target(320193)}, lambda cik, cutoff, branch: _bundle())
    result = adapter.fetch(item)
    assert isinstance(result, FetchSuccess)
    assert result.confidence == Decimal("0.92")  # all three core fields present
    assert result.record is not None
    assert result.record.payload["issuer_id"] == "issuer:lei:X"
    assert result.record.payload["operating_branch"] == "non_financial"
    assert result.record.payload["currency"] == "USD"


def test_adapter_no_bundle_is_field_unavailable() -> None:
    item = _work_item("4" * 64)
    adapter = SecFinancialFactAdapter({item.work_item_id: _target()}, lambda cik, cutoff, branch: None)
    result = adapter.fetch(item)
    assert isinstance(result, FetchFailure)
    assert result.reason_code is ObligationReasonCode.FIELD_UNAVAILABLE


def test_adapter_transient_and_timeout() -> None:
    item = _work_item("5" * 64)

    def _boom(cik, cutoff, branch):
        raise SourceUnavailableError("503")

    def _timeout(cik, cutoff, branch):
        raise TimeoutError

    a1 = SecFinancialFactAdapter({item.work_item_id: _target()}, _boom)
    a2 = SecFinancialFactAdapter({item.work_item_id: _target()}, _timeout)
    assert a1.fetch(item).reason_code is ObligationReasonCode.TRANSIENT_NETWORK
    assert a2.fetch(item).reason_code is ObligationReasonCode.TIMEOUT


def test_unknown_work_item_is_contract_violation() -> None:
    item = _work_item("6" * 64)
    other = _work_item("7" * 64)
    adapter = SecFinancialFactAdapter({item.work_item_id: _target()}, lambda cik, cutoff, branch: None)
    assert adapter.fetch(other).reason_code is ObligationReasonCode.CONTRACT_VIOLATION


def test_headcount_extractor_changes_the_normalized_identity() -> None:
    item = _work_item("8" * 64)
    without = SecFinancialFactAdapter({item.work_item_id: _target()}, lambda c, cut, b: _bundle())
    fact = HeadcountFact(value=Decimal("1800"), knowable_at=datetime(2026, 3, 1, tzinfo=UTC))
    with_hc = SecFinancialFactAdapter(
        {item.work_item_id: _target()},
        lambda c, cut, b: _bundle(),
        headcount_extractor=lambda c, cut: fact,
    )
    r0 = without.fetch(item)
    r1 = with_hc.fetch(item)
    assert isinstance(r0, FetchSuccess) and isinstance(r1, FetchSuccess)
    # Merging headcount changes the normalized observation identity and advances knowable_at.
    assert r1.normalized_sha256 != r0.normalized_sha256
    assert r1.transaction_time == datetime(2026, 3, 1, tzinfo=UTC)
    assert r1.record is not None
    assert r1.record.payload["headcount"] == "1800"


def test_headcount_after_cutoff_is_ignored() -> None:
    item = _work_item("9" * 64)
    late = HeadcountFact(value=Decimal("1"), knowable_at=datetime(2026, 4, 20, tzinfo=UTC))
    adapter = SecFinancialFactAdapter(
        {item.work_item_id: _target()},
        lambda c, cut, b: _bundle(),
        headcount_extractor=lambda c, cut: late,
    )
    baseline = SecFinancialFactAdapter({item.work_item_id: _target()}, lambda c, cut, b: _bundle())
    # A post-cutoff headcount is excluded, so the identity equals the no-headcount case.
    assert adapter.fetch(item).normalized_sha256 == baseline.fetch(item).normalized_sha256


def test_look_ahead_bundle_is_rejected() -> None:
    item = _work_item("b" * 64)
    adapter = SecFinancialFactAdapter(
        {item.work_item_id: _target()},
        lambda c, cut, b: _bundle(knowable_at=datetime(2026, 4, 20, tzinfo=UTC)),
    )
    assert adapter.fetch(item).reason_code is ObligationReasonCode.LOOK_AHEAD_VIOLATION
