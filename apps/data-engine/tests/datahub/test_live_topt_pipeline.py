"""`LiveToptCapture._real_financial` branch coverage — no network, no DB.

Regression target: the live pipeline silently excluded every financial-branch
issuer (JPM, the sole TOPT bank) from `large_model_value_v0` because it set
`gross_profit=None` for financial issuers instead of the pre-provision-profit
proxy the golden fixture documents (`large_model_value_v0_strategy.v1.json`'s
top-level `notes` + JPM's own `grounding`/`derivation` fields: "gross_profit is
the parser's financial_issuer_split pre-provision-profit proxy"). Confirmed by
reading `seed_strategy_inputs_from_capture`: it seeds `staging.strategy_backtest_inputs`
straight from this dict's `gross_profit` key, so a `None` here becomes a missing
input the evaluator rejects with `missing_gross_profit_fact` on every scheduled
tick — never exercised until now because `_real_financial` had no test at all.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from data_engine.datahub import live_topt_pipeline
from data_engine.datahub.live_topt_pipeline import LiveToptCapture

CUTOFF = datetime(2026, 3, 31, tzinfo=UTC)

# Matches _pre_provision_profit's actual concepts (RevenuesNetOfInterestExpense minus
# NoninterestExpense) -- NOT the golden fixture's documented grounding text, which cites
# InterestIncomeExpenseNet + NoninterestIncome - NoninterestExpense instead. That's a
# separate, pre-existing doc/implementation drift in the pre-provision-profit XBRL concept
# choice, orthogonal to the gross_profit=None exclusion bug this test targets.
_JPM_FACTS: dict[str, Any] = {
    "facts": {
        "us-gaap": {
            "RevenuesNetOfInterestExpense": {
                "units": {
                    "USD": [{"filed": "2026-02-13", "start": "2025-01-01", "end": "2025-12-31", "val": 130000000000}]
                }
            },
            "NoninterestExpense": {
                "units": {
                    "USD": [{"filed": "2026-02-13", "start": "2025-01-01", "end": "2025-12-31", "val": 95640000000}]
                }
            },
        }
    }
}

# A non-financial issuer with reported GrossProfit — the branch this bug never affected.
_AAPL_FACTS: dict[str, Any] = {
    "facts": {
        "us-gaap": {
            "GrossProfit": {
                "units": {
                    "USD": [{"filed": "2026-02-01", "start": "2025-01-01", "end": "2025-12-31", "val": 180000000000}]
                }
            }
        }
    }
}


def _capture(monkeypatch: pytest.MonkeyPatch, ticker: str, facts: dict[str, Any]) -> dict[str, str | None]:
    monkeypatch.setattr(live_topt_pipeline.sec, "ticker_to_cik", lambda _t: 1)
    monkeypatch.setattr(live_topt_pipeline.sec, "fetch_company_facts", lambda _cik: facts)
    capture = LiveToptCapture(cutoff=CUTOFF, version="test")
    return capture._real_financial(ticker)


def test_financial_branch_issuer_gets_gross_profit_from_pre_provision_profit_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _capture(monkeypatch, "JPM", _JPM_FACTS)

    assert result["operating_branch"] == "financial"
    assert result["pre_provision_profit"] == "34360000000"
    assert result["gross_profit"] == "34360000000", (
        "financial-branch gross_profit must equal the pre-provision-profit proxy, "
        "or the evaluator drops the issuer as missing_gross_profit_fact"
    )


def test_non_financial_issuer_still_uses_reported_gross_profit(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _capture(monkeypatch, "AAPL", _AAPL_FACTS)

    assert result["operating_branch"] == "non_financial"
    assert result["gross_profit"] == "180000000000"
    assert result["pre_provision_profit"] is None


def test_financial_branch_issuer_with_no_sec_facts_stays_null_not_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_cik(_ticker: str) -> int:
        raise RuntimeError("no CIK mapping")

    monkeypatch.setattr(live_topt_pipeline.sec, "ticker_to_cik", _raise_cik)
    capture = LiveToptCapture(cutoff=CUTOFF, version="test")

    result = capture._real_financial("JPM")

    assert result["gross_profit"] is None
    assert result["pre_provision_profit"] is None
