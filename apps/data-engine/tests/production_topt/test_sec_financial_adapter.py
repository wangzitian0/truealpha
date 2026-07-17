from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from data_engine.datahub.production_topt.executor import FetchFailure, FetchSuccess
from data_engine.datahub.production_topt.sec_financial_adapter import (
    FinancialFactsBundle,
    SecFinancialFactAdapter,
    SecTarget,
    SourceUnavailableError,
    build_bundle,
    pit_concept_value,
)
from truealpha_contracts import ObligationReasonCode
from truealpha_contracts.datahub import CaptureWorkItem

_CUTOFF = date(2026, 3, 31)


def _work_item(digest: str) -> CaptureWorkItem:
    return CaptureWorkItem(
        campaign_id="capture-campaign:" + "1" * 64,
        source_request_id="source-request:" + digest,
        schedule_policy_id="schedule-policy:" + "2" * 64,
    )


def _facts() -> dict:
    return {
        "facts": {
            "us-gaap": {
                "GrossProfit": {
                    "units": {
                        "USD": [
                            {"end": "2024-12-31", "val": 100, "filed": "2025-02-01"},
                            {"end": "2025-12-31", "val": 120, "filed": "2026-02-01"},
                            # Filed after the cutoff — must be excluded (not knowable yet).
                            {"end": "2026-03-31", "val": 999, "filed": "2026-04-20"},
                        ]
                    }
                },
                "Assets": {"units": {"USD": [{"end": "2025-12-31", "val": 500, "filed": "2026-02-01"}]}},
            },
            "dei": {
                "EntityCommonStockSharesOutstanding": {
                    "units": {"shares": [{"end": "2026-01-31", "val": 10, "filed": "2026-02-10"}]}
                }
            },
        }
    }


def test_pit_excludes_future_filings_and_picks_latest() -> None:
    hit = pit_concept_value(_facts(), "us-gaap", "GrossProfit", "USD", _CUTOFF)
    assert hit is not None
    value, filed = hit
    assert value == Decimal("120")  # the 999 filed 2026-04-20 is excluded
    assert filed == date(2026, 2, 1)


def test_build_bundle_extracts_all_three() -> None:
    bundle = build_bundle(_facts(), _CUTOFF)
    assert bundle is not None
    assert bundle.gross_profit == Decimal("120")
    assert bundle.total_assets == Decimal("500")
    assert bundle.shares_outstanding == Decimal("10")
    assert bundle.knowable_at.date() <= _CUTOFF


def test_build_bundle_none_when_all_future() -> None:
    facts = {
        "facts": {
            "us-gaap": {
                "GrossProfit": {
                    "units": {
                        "USD": [
                            {"end": "2026-03-31", "val": 1, "filed": "2026-06-01"},
                        ]
                    }
                }
            }
        }
    }
    assert build_bundle(facts, _CUTOFF) is None


def test_adapter_success() -> None:
    item = _work_item("3" * 64)
    bundle = FinancialFactsBundle(
        gross_profit=Decimal("120"),
        total_assets=Decimal("500"),
        shares_outstanding=Decimal("10"),
        raw_bytes=b"{}",
        knowable_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    adapter = SecFinancialFactAdapter({item.work_item_id: SecTarget(320193, _CUTOFF)}, lambda cik, c: bundle)
    result = adapter.fetch(item)
    assert isinstance(result, FetchSuccess)
    assert result.confidence == Decimal("0.95")


def test_adapter_all_missing_is_unavailable() -> None:
    item = _work_item("4" * 64)
    adapter = SecFinancialFactAdapter({item.work_item_id: SecTarget(1, _CUTOFF)}, lambda cik, c: None)
    result = adapter.fetch(item)
    assert isinstance(result, FetchFailure)
    assert result.reason_code is ObligationReasonCode.FIELD_UNAVAILABLE


def test_adapter_transient_and_timeout() -> None:
    item = _work_item("5" * 64)

    def _boom(cik, cutoff):
        raise SourceUnavailableError("503")

    def _timeout(cik, cutoff):
        raise TimeoutError

    a1 = SecFinancialFactAdapter({item.work_item_id: SecTarget(1, _CUTOFF)}, _boom)
    a2 = SecFinancialFactAdapter({item.work_item_id: SecTarget(1, _CUTOFF)}, _timeout)
    assert a1.fetch(item).reason_code is ObligationReasonCode.TRANSIENT_NETWORK
    assert a2.fetch(item).reason_code is ObligationReasonCode.TIMEOUT


def test_unknown_work_item_is_contract_violation() -> None:
    item = _work_item("6" * 64)
    other = _work_item("7" * 64)
    adapter = SecFinancialFactAdapter({item.work_item_id: SecTarget(1, _CUTOFF)}, lambda cik, c: None)
    assert adapter.fetch(other).reason_code is ObligationReasonCode.CONTRACT_VIOLATION
