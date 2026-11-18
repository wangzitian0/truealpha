"""Check recorded SEC EDGAR earnings history for AMZN and MU (#1664 Q2)."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from data_engine.datahub.capture_core.sec_financial_adapter import build_bundle
from factors.base.peg import peg
from factors.composite.strategy_evaluator import IssuerInput, evaluate_cutoff
from factors.panel import IssuerClass
from factors.snapshot_inputs import OperatingBranch
from factors.types import Fact, GrowthConvention, UnitFamily
from truealpha_contracts.strategy import load_live_strategy_definition

SAMPLES_SEC_DIR = Path(__file__).resolve().parents[1] / "samples" / "sec"
CUTOFF = datetime(2026, 11, 17, tzinfo=UTC)


def _load_company_facts(filename: str) -> dict:
    return json.loads((SAMPLES_SEC_DIR / filename).read_text(encoding="utf-8"))


def _peg_facts(entity_id: str, net_income_by_period: dict[date, Decimal]) -> list[Fact]:
    facts = [
        Fact(
            entity_id=entity_id,
            metric="price",
            value=Decimal("200"),
            unit_family=UnitFamily.PER_SHARE,
            confidence=Decimal("0.9"),
            as_of=CUTOFF,
        ),
        Fact(
            entity_id=entity_id,
            metric="shares_outstanding",
            value=Decimal("1000000000"),
            unit_family=UnitFamily.COUNT,
            confidence=Decimal("0.9"),
            as_of=CUTOFF,
        ),
    ]
    for period_end, value in sorted(net_income_by_period.items()):
        start = period_end.replace(year=period_end.year - 1) + timedelta(days=1)
        facts.append(
            Fact(
                entity_id=entity_id,
                metric="net_income",
                value=value,
                unit_family=UnitFamily.CURRENCY,
                confidence=Decimal("0.92"),
                as_of=CUTOFF,
                fiscal_period=f"FY{period_end.year}:FY:{start.isoformat()}:{period_end.isoformat()}",
            )
        )
    return facts


@pytest.mark.parametrize(
    ("ticker", "filename", "issuer_id", "min_years", "loss_end", "loss_value", "latest_end", "latest_value", "window"),
    [
        (
            "AMZN",
            "AMZN_CIK0001018724.json",
            "issuer:cik:0001018724",
            19,
            date(2022, 12, 31),
            Decimal("-2722000000"),
            date(2025, 12, 31),
            Decimal("77670000000"),
            "window:2022-12-31..2025-12-31",
        ),
        (
            "MU",
            "MU_CIK0000723125.json",
            "issuer:cik:0000723125",
            18,
            date(2023, 8, 31),
            Decimal("-5833000000"),
            date(2026, 9, 3),
            Decimal("84969000000"),
            "window:2023-08-31..2026-09-03",
        ),
    ],
)
def test_recorded_sec_history_explains_amzn_and_mu_peg_refusal(
    ticker: str,
    filename: str,
    issuer_id: str,
    min_years: int,
    loss_end: date,
    loss_value: Decimal,
    latest_end: date,
    latest_value: Decimal,
    window: str,
) -> None:
    payload = _load_company_facts(filename)
    bundle = build_bundle(payload, CUTOFF.date(), OperatingBranch.NON_FINANCIAL)

    assert len(bundle.net_income_by_period) >= min_years, f"{ticker}: missing annual SEC NetIncomeLoss periods"
    assert max(bundle.net_income_by_period) == latest_end
    assert bundle.net_income == latest_value
    assert bundle.net_income_by_period[loss_end] == loss_value
    assert bundle.gross_profit is not None and bundle.gross_profit > 0
    assert bundle.total_assets is not None and bundle.total_assets > 0
    assert bundle.shares_outstanding is not None and bundle.shares_outstanding > 0

    result = peg(
        _peg_facts(issuer_id, dict(bundle.net_income_by_period)),
        entity_id=issuer_id,
        growth_convention=GrowthConvention.HISTORICAL_CAGR,
        as_of=CUTOFF,
        cagr_years=3,
    )
    assert result.value is None
    assert result.flags == ["insufficient_earnings_history", "cagr_years:3", window]

    # Falsifiable mutation check: replacing only the negative window year makes 3-year PEG finite.
    mutated = deepcopy(dict(bundle.net_income_by_period))
    mutated[loss_end] = Decimal("1000000000")
    mutated_result = peg(
        _peg_facts(issuer_id, mutated),
        entity_id=issuer_id,
        growth_convention=GrowthConvention.HISTORICAL_CAGR,
        as_of=CUTOFF,
        cagr_years=3,
    )
    assert mutated_result.value is not None and mutated_result.value > 0

    records = {
        "gross_profit": (bundle.gross_profit, Decimal("0.92")),
        "total_assets": (bundle.total_assets, Decimal("0.92")),
        "headcount": (Decimal("100000"), Decimal("0.70")),
        "revenue": (bundle.revenue or Decimal("1"), Decimal("0.92")),
        "shares_outstanding": (bundle.shares_outstanding, Decimal("0.92")),
        "last_close": (Decimal("200"), Decimal("0.85")),
        "net_income": (bundle.net_income, Decimal("0.92")),
    }
    [decision] = evaluate_cutoff(
        [
            IssuerInput(
                issuer_id=issuer_id,
                records=records,
                knowable_at=dict.fromkeys(records, CUTOFF),
                periodic_records={
                    "net_income": {
                        f"FY{end.year}:FY:{(end.replace(year=end.year - 1) + timedelta(days=1)).isoformat()}:{end.isoformat()}": (
                            value,
                            Decimal("0.92"),
                        )
                        for end, value in bundle.net_income_by_period.items()
                    }
                },
                issuer_class=IssuerClass.NON_FINANCIAL,
            )
        ],
        definition=load_live_strategy_definition(),
        cutoff_at=CUTOFF,
        risk_free_rate=Decimal("0.04"),
    )
    assert decision.eligible and decision.exclusion_reason is None
    assert decision.peg is None
    assert decision.peg_reason_codes == ("insufficient_earnings_history",)
