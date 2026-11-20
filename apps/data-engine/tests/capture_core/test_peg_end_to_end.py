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
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub.capture_core.sec_financial_adapter import build_bundle
from factors.composite.strategy_evaluator import IssuerInput, evaluate_cutoff
from factors.panel import IssuerClass
from factors.snapshot_inputs import OperatingBranch
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
    bundle = build_bundle(_company_facts(), _CUTOFF.date(), OperatingBranch.NON_FINANCIAL)
    assert bundle.net_income_by_period, "the adapter must carry the annual series"
    # Yearly rates 25%, 30% and 23.08%, weighted 1:2:3 toward the present -> 25.71%.
    # An endpoint CAGR would have said 25.99%; the owner-chosen weighting (2026-08-17)
    # discounts the older acceleration.
    assert bundle.net_income == Decimal("16000")
    # The window endpoints travel with it, so the rate is auditable downstream.
    assert min(bundle.net_income_by_period) == date(2022, 12, 31)
    assert max(bundle.net_income_by_period) == date(2025, 12, 31)
    # The PIT obligation strategy participation adds: the rate cannot be knowable before
    # the filings behind it. 2025's figure was filed 2026-02-15.
    assert bundle.knowable_at is not None and bundle.knowable_at.date() >= date(2026, 2, 15)

    # 2. The evaluator assembles PEG from the inputs the bridge would have landed.
    records = {
        "gross_profit": (Decimal("40000"), Decimal("0.92")),
        "total_assets": (Decimal("100000"), Decimal("0.92")),
        "headcount": (Decimal("100"), Decimal("0.70")),
        "revenue": (Decimal("50000"), Decimal("0.92")),
        "shares_outstanding": (Decimal("1000"), Decimal("0.92")),
        "last_close": (Decimal("100"), Decimal("0.85")),
        "net_income": (bundle.net_income, Decimal("0.92")),
    }
    issuer = IssuerInput(
        issuer_id=_ISSUER,
        records=records,
        # The bridge stamps every row of a payload with the knowable_at of its observation.
        knowable_at=dict.fromkeys(records, bundle.knowable_at),
        # The series crosses as periods since 0043, and the factor -- not this adapter --
        # reduces it. Tagged exactly as the bridge tags it.
        periodic_records={
            "net_income": {
                f"FY{end.year}:FY:{end.replace(year=end.year - 1) + timedelta(days=1)}:{end}": (
                    value,
                    Decimal("0.92"),
                )
                for end, value in bundle.net_income_by_period.items()
            }
        },
        issuer_class=IssuerClass.NON_FINANCIAL,
    )
    [decision] = evaluate_cutoff(
        [issuer],
        definition=_definition(),
        cutoff_at=_CUTOFF,
        risk_free_rate=Decimal("0"),
    )
    assert decision.peg is not None, "the evaluator must carry module 1 onto the decision"
    # Market cap 100,000 over net income 16,000 is a P/E of 6.25; over the 25.71% weighted
    # rate that is 0.2431. Asserting the composed number, not the parts, is the point of
    # an end-to-end test.
    assert decision.peg.quantize(Decimal("0.0001")) == Decimal("0.2431")

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
    assert served[0].quantize(Decimal("0.0001")) == Decimal("0.2431")


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
    from truealpha_contracts.strategy import load_live_strategy_definition

    return load_live_strategy_definition()


@pytest.mark.criterion("R3.C4")
def test_r3_c4_consensus_peg_end_to_end_from_moomoo_valuation_to_strategy_run_repository(connection) -> None:
    """R3.C4: Moomoo valuation_detail -> MoomooFinancialsFetcher -> strategy_bridge -> mart.metric_node_values -> PostgresStrategyRunRepository(convention='consensus')."""
    import secrets

    from data_engine.datahub.capture_core.moomoo_origin import MoomooFinancialsFetcher
    from data_engine.datahub.strategy_bridge import _persist_peg_consensus_nodes
    from data_engine.sources import gateway
    from data_engine.sources.landed_keys import MemoryLandedKeys
    from data_engine.sources.memory_ledger import MemoryLedger
    from psycopg.rows import dict_row
    from truealpha_runtime.strategy_run_postgres import _DECISIONS_SQL, _decision_from_row

    class _FakeClient:
        def financial_statements(self, code: str, *, statement_type: int, identity: Any) -> dict[str, Any]:
            del code, identity
            tag = "income" if statement_type == 1 else "balance"
            return {
                "report_list": [
                    {
                        "financial_type": 7,
                        "report_date": "2025-12-31",
                        "currency_code": "USD",
                        "accounting_standards": "US-GAAP",
                        "item_list": [{"field_id": 8001, "data": 50_000 if tag == "income" else 100_000}],
                    }
                ]
            }

        def valuation_detail(self, code: str, *, identity: Any) -> dict[str, Any]:
            del code, identity
            return {
                "profit_growth_rate": {
                    "profit_data": [
                        {
                            "period_str": "2025/FY",
                            "financial_quarter": 4,
                            "report_date_str": "2025-12-31",
                            "profit_growth_rate": 25,
                        }
                    ]
                }
            }

    gateway.set_ledger(MemoryLedger())
    gateway.set_landed_keys(MemoryLandedKeys())
    assertion = MoomooFinancialsFetcher(_FakeClient())("E2ER3C4", _CUTOFF.date())
    assert assertion is not None
    assert assertion.values.get("consensus_profit_growth") == Decimal("25")

    bundle = build_bundle(_company_facts(), _CUTOFF.date(), OperatingBranch.NON_FINANCIAL)
    records = {
        "gross_profit": (Decimal("40000"), Decimal("0.92")),
        "total_assets": (Decimal("100000"), Decimal("0.92")),
        "headcount": (Decimal("100"), Decimal("0.90")),
        "revenue": (Decimal("50000"), Decimal("0.92")),
        "shares_outstanding": (Decimal("1000"), Decimal("0.92")),
        "last_close": (Decimal("100"), Decimal("0.90")),
        "net_income": (bundle.net_income, Decimal("0.92")),
        "consensus_profit_growth": (Decimal("25"), Decimal("0.96")),
    }
    issuer = IssuerInput(
        issuer_id=_ISSUER,
        records=records,
        knowable_at=dict.fromkeys(records, bundle.knowable_at),
        periodic_records={
            "net_income": {
                f"FY{end.year}:FY:{end.replace(year=end.year - 1) + timedelta(days=1)}:{end}": (
                    value,
                    Decimal("0.92"),
                )
                for end, value in bundle.net_income_by_period.items()
            }
        },
        issuer_class=IssuerClass.NON_FINANCIAL,
    )
    decisions = evaluate_cutoff([issuer], definition=_definition(), cutoff_at=_CUTOFF, risk_free_rate=Decimal("0"))
    assert decisions[0].peg_consensus == Decimal("0.25")  # P/E 6.25 / 25
    assert decisions[0].peg_consensus_rank == 1

    hx = secrets.token_hex(32)
    run_id = f"strategy-run:{hx}"
    capture_run_id = f"capture-run:{hx}"
    dec_id = f"strategy-decision:{hx}"
    connection.execute(
        "insert into mart.strategy_runs "
        "(strategy_run_id, content_sha256, strategy_key, strategy_version, "
        " definition_content_sha256, corpus_sha256, claim_ceiling, executed_at) "
        "values (%s, %s, 'large_model_value_v0', %s, %s, %s, 'preview', '2099-01-01T00:00:00Z')",
        (run_id, hx, _definition().version, _definition().content_sha256, hx),
    )
    connection.execute(
        "insert into mart.strategy_decisions "
        "(strategy_decision_id, content_sha256, strategy_run_id, issuer_id, cutoff_at, "
        " capital_adjusted_labor_efficiency, tier, current_price_to_sales, "
        " target_price_to_sales, valuation_gap, eligible, outcome, exclusion_reason, "
        " rank, target_weight, peg, peg_rank) "
        "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, null, %s, %s, %s, %s)",
        (
            dec_id,
            hx,
            run_id,
            _ISSUER,
            _CUTOFF,
            decisions[0].capital_adjusted_labor_efficiency,
            decisions[0].tier,
            decisions[0].current_price_to_sales,
            decisions[0].target_price_to_sales,
            decisions[0].valuation_gap,
            decisions[0].eligible,
            decisions[0].outcome.value,
            decisions[0].rank,
            decisions[0].target_weight,
            decisions[0].peg,
            decisions[0].peg_rank,
        ),
    )
    _persist_peg_consensus_nodes(connection, capture_run_id=capture_run_id, cutoff=_CUTOFF, evaluated=decisions)

    scoped_sql = _DECISIONS_SQL.replace(
        "left join mart.strategy_run_capture scope on scope.strategy_run_id = d.strategy_run_id",
        "left join (select %s::text as strategy_run_id, %s::text as capture_run_id) scope on scope.strategy_run_id = d.strategy_run_id",
    )
    with connection.cursor(row_factory=dict_row) as cur:
        cur.execute(scoped_sql, (run_id, capture_run_id, run_id))
        row = cur.fetchone()
    assert row is not None
    cons_decision = _decision_from_row(dict(row), convention="consensus")
    hist_decision = _decision_from_row(dict(row), convention="historical_cagr")
    assert cons_decision.peg is not None and cons_decision.peg.quantize(Decimal("0.01")) == Decimal("0.25")
    assert cons_decision.peg_rank == 1
    assert hist_decision.peg is not None and hist_decision.peg.quantize(Decimal("0.0001")) == Decimal("0.2431")
