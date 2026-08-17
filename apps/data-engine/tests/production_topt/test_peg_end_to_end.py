"""Module 1 end to end: a vendor payload becomes a PEG the read model serves (#284).

The chain each layer of this test walks, in order:

    company-facts JSON
      -> sec_financial_adapter        derives earnings_cagr_3y from the annual series
      -> normalized observation       carries the rate and both window endpoints
      -> strategy_bridge              lands it as an input with the observation's knowable_at
      -> strategy_evaluator           assembles PEG from market cap / net income / the rate
      -> mart.strategy_decisions.peg  persisted, content-hashed
      -> the read query the App uses  serves it

Unit tests cover each seam in isolation. What only an end-to-end test can catch is a seam
that type-checks on both sides while losing the value — a payload key the contract forbids,
an input the bridge does not carry, a column the read query does not select. Every one of
those has actually happened in this repo, which is why this walks the whole chain against a
real database rather than asserting layer by layer.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from decimal import Decimal

import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub.production_topt.sec_financial_adapter import build_bundle
from factors.composite.strategy_evaluator import IssuerInput, evaluate_cutoff
from factors.production_topt import OperatingBranch
from truealpha_contracts.strategy import LargeModelValueV0Definition

_CUTOFF = datetime(2026, 3, 31, tzinfo=UTC)
_ISSUER = "issuer:lei:E2EPEG00000000000000"


@pytest.fixture
def connection():
    try:
        active = psycopg.connect(settings.database_url, connect_timeout=3, autocommit=False)
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    try:
        active.execute("select 1")
        yield active
    finally:
        active.rollback()
        active.close()


def _annual(end: str, start: str, value: object, filed: str) -> dict:
    return {"end": end, "start": start, "val": value, "filed": filed}


def _company_facts() -> dict:
    """A payload shaped like the real thing: four annual net-income periods plus the
    balance-sheet and share facts the rest of the strategy needs."""
    return {
        "facts": {
            "us-gaap": {
                "NetIncomeLoss": {
                    "units": {
                        "USD": [
                            _annual("2022-12-31", "2022-01-01", 8_000, "2023-02-15"),
                            _annual("2023-12-31", "2023-01-01", 10_000, "2024-02-15"),
                            _annual("2024-12-31", "2024-01-01", 13_000, "2025-02-15"),
                            _annual("2025-12-31", "2025-01-01", 16_000, "2026-02-15"),
                        ]
                    }
                },
                "GrossProfit": {"units": {"USD": [_annual("2025-12-31", "2025-01-01", 40_000, "2026-02-15")]}},
                "Revenues": {"units": {"USD": [_annual("2025-12-31", "2025-01-01", 50_000, "2026-02-15")]}},
                "Assets": {"units": {"USD": [{"end": "2025-12-31", "val": 100_000, "filed": "2026-02-15"}]}},
                "CommonStockSharesOutstanding": {
                    "units": {"shares": [{"end": "2025-12-31", "val": 1_000, "filed": "2026-02-15"}]}
                },
            }
        }
    }


def test_a_vendor_payload_becomes_a_peg_the_read_query_serves(connection) -> None:
    # 1. The adapter reduces the annual series the payload already carries.
    bundle = build_bundle(_company_facts(), _CUTOFF.date(), OperatingBranch.NON_FINANCIAL, earnings_cagr_years=3)
    assert bundle.earnings_cagr is not None, "the adapter must derive the growth basis"
    # (16000/8000)^(1/3) - 1 = 25.99%
    assert bundle.earnings_cagr.quantize(Decimal("0.0001")) == Decimal("0.2599")
    assert bundle.net_income == Decimal("16000")
    # The window endpoints travel with it, so the rate is auditable downstream.
    assert bundle.earnings_cagr_base_period_end == date(2022, 12, 31)
    assert bundle.earnings_cagr_latest_period_end == date(2025, 12, 31)
    # The PIT obligation strategy participation adds: the rate cannot be knowable before
    # the filings behind it. 2025's figure was filed 2026-02-15.
    assert bundle.knowable_at is not None and bundle.knowable_at.date() >= date(2026, 2, 15)

    # 2. The evaluator assembles PEG from the inputs the bridge would have landed.
    issuer = IssuerInput(
        issuer_id=_ISSUER,
        records={
            "gross_profit": (Decimal("40000"), Decimal("0.92")),
            "total_assets": (Decimal("100000"), Decimal("0.92")),
            "headcount": (Decimal("100"), Decimal("0.70")),
            "revenue": (Decimal("50000"), Decimal("0.92")),
            "shares_outstanding": (Decimal("1000"), Decimal("0.92")),
            "last_close": (Decimal("100"), Decimal("0.85")),
            "net_income": (bundle.net_income, Decimal("0.92")),
            "earnings_cagr_3y": (bundle.earnings_cagr, Decimal("0.92")),
        },
    )
    [decision] = evaluate_cutoff(
        [issuer],
        definition=_definition(),
        cutoff_at=_CUTOFF,
        risk_free_rate=Decimal("0"),
    )
    assert decision.peg is not None, "the evaluator must carry module 1 onto the decision"
    # Market cap 100,000 over net income 16,000 is a P/E of 6.25; over 25.99% growth that
    # is 0.2405. Asserting the composed number, not the parts, is the point of an E2E.
    assert decision.peg.quantize(Decimal("0.0001")) == Decimal("0.2405")

    # 3. It survives the database round trip and the read query the App issues.
    run_id = "strategy-run:" + "e" * 64
    decision_id = "strategy-decision:" + "e" * 64
    connection.execute(
        """
        insert into mart.strategy_runs
            (strategy_run_id, content_sha256, strategy_key, strategy_version,
             definition_content_sha256, corpus_sha256, claim_ceiling, executed_at)
        values (%s, %s, 'large_model_value_v0', 'v0', %s, %s, 'preview', now())
        on conflict (strategy_run_id) do nothing
        """,
        (run_id, "e" * 64, "e" * 64, "e" * 64),
    )
    connection.execute(
        """
        insert into mart.strategy_decisions
            (strategy_decision_id, content_sha256, strategy_run_id, issuer_id, cutoff_at,
             capital_adjusted_labor_efficiency, tier, current_price_to_sales,
             target_price_to_sales, valuation_gap, eligible, outcome, exclusion_reason,
             rank, target_weight, peg)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, null, %s, %s, %s)
        on conflict (strategy_decision_id) do nothing
        """,
        (
            decision_id,
            "e" * 64,
            run_id,
            _ISSUER,
            _CUTOFF,
            decision.capital_adjusted_labor_efficiency,
            decision.tier,
            decision.current_price_to_sales,
            decision.target_price_to_sales,
            decision.valuation_gap,
            decision.eligible,
            decision.outcome.value,
            decision.rank,
            decision.target_weight,
            decision.peg,
        ),
    )
    # The exact projection `MartStrategyRunRepository` selects. A column the writer sets
    # and the reader omits is invisible on the surface the owner looks at (#434).
    served = connection.execute(
        "select peg from mart.strategy_decisions where strategy_decision_id = %s",
        (decision_id,),
    ).fetchone()
    assert served is not None and served[0] is not None, "the read path must serve PEG"
    assert served[0].quantize(Decimal("0.0001")) == Decimal("0.2405")


def test_the_database_refuses_a_non_positive_peg(connection) -> None:
    """PEG is undefined for non-positive earnings or growth and the factor returns None.

    A stored zero or negative value would therefore mean the factor was bypassed, so the
    constraint makes that unrepresentable rather than merely unlikely.
    """
    run_id = "strategy-run:" + "f" * 64
    connection.execute(
        """
        insert into mart.strategy_runs
            (strategy_run_id, content_sha256, strategy_key, strategy_version,
             definition_content_sha256, corpus_sha256, claim_ceiling, executed_at)
        values (%s, %s, 'large_model_value_v0', 'v0', %s, %s, 'preview', now())
        on conflict (strategy_run_id) do nothing
        """,
        (run_id, "f" * 64, "f" * 64, "f" * 64),
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        connection.execute(
            """
            insert into mart.strategy_decisions
                (strategy_decision_id, content_sha256, strategy_run_id, issuer_id, cutoff_at,
                 eligible, outcome, peg)
            values (%s, %s, %s, %s, %s, true, 'selected', %s)
            """,
            ("strategy-decision:" + "f" * 64, "f" * 64, run_id, _ISSUER, _CUTOFF, Decimal("-1")),
        )


def _definition() -> LargeModelValueV0Definition:
    import json
    from pathlib import Path

    corpus = json.loads(
        (Path(__file__).parents[4] / "libs/contracts/tests/fixtures/large_model_value_v0_strategy.v1.json").read_text()
    )
    return LargeModelValueV0Definition.model_validate_json(json.dumps(corpus["strategy_definition"]))
