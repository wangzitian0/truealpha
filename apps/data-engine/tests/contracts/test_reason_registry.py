"""Every reason code that the code defines, and every code served on 2026-11-24, is registered (#1851).

A literal code outside these sets is checked on served rows by the nightly completeness check.
"""

from __future__ import annotations

import pytest
from data_engine.contracts.datahub import FetchAttemptOutcome, ObligationTerminalState
from data_engine.contracts.obligation_reason_codes import ObligationReasonCode
from data_engine.datahub import oracle_outcomes
from data_engine.datahub.capture_core import theme_purity as theme_purity_capture
from data_engine.datahub.capture_core.executor import SERVED_BY_FAILOVER
from data_engine.datahub.confidence_report import _OUTCOME_REASONS, UNSERVED_REASONS
from factors.base import gross_profit_per_employee, supply_chain_exposure
from factors.snapshot_inputs import CoreReasonCode
from truealpha_contracts.reasons import ReasonKind, is_registered, registration
from truealpha_contracts.strategy import ExclusionReason
from truealpha_runtime.issuer_modules_mart import NO_SERVED_ROW
from truealpha_runtime.research_report_mart import MART_READ_FAILED, PEG_NOT_RECORDED, ROW_AFTER_REPORT_CUTOFF

DEFINED: dict[str, tuple[str, ...]] = {
    "CoreReasonCode": tuple(code.value for code in CoreReasonCode),
    "ExclusionReason": tuple(code.value for code in ExclusionReason),
    "ObligationReasonCode": tuple(code.value for code in ObligationReasonCode),
    "FetchAttemptOutcome": tuple(code.value for code in FetchAttemptOutcome),
    "ObligationTerminalState": tuple(code.value for code in ObligationTerminalState),
    "reconciliation outcomes": tuple(_OUTCOME_REASONS.values()),
    "unserved cell confidence": tuple(sorted(UNSERVED_REASONS)),
    # The literals of `confidence_report.classify_cell`, written as `reason:<code>` on `mart.cell_confidence`.
    "cell confidence reasons": (
        "no_origin_value",
        "single_origin",
        "second_origin_other_day",
        "second_origin_other_period",
        "same_lineage",
        "no_agreement_policy",
        "two_origins_agree",
        "independent_origins_agree",
        "unknown",
    ),  # fmt: skip
    "supply chain": (
        supply_chain_exposure.NO_SUPPLY_CHAIN_EXTRACTION,
        supply_chain_exposure.NO_DISCLOSED_SUPPLIERS,
        supply_chain_exposure.UNQUANTIFIED_PARTNER_SHARES,
        supply_chain_exposure.CUSTOMER_ONLY_GRAPH,
    ),
    "gross profit per employee": (
        gross_profit_per_employee.MISSING_TCE_FLAG,
        gross_profit_per_employee.MISSING_PREFERRED_FLAG,
        gross_profit_per_employee.NEGATIVE_TCE_FLAG,
        gross_profit_per_employee.NONPOSITIVE_EMPLOYEES_FLAG,
    ),
    "theme purity": (theme_purity_capture.NO_DESCRIPTION, theme_purity_capture.NO_ANSWER),
    "oracle": (oracle_outcomes.NO_SERVED_VALUE, oracle_outcomes.NO_OPERATOR_ROW),
    "served heads and reports": (
        SERVED_BY_FAILOVER,
        NO_SERVED_ROW,
        MART_READ_FAILED,
        PEG_NOT_RECORDED,
        ROW_AFTER_REPORT_CUTOFF,
    ),
}

#: `select distinct` over every served reason column of staging and production, 2026-11-24 10:10 UTC
#: (read-only; prefixed codes stand for their family).
SERVED_ON_2026_11_24 = (
    "below_minimum_classified_share", "confidence.definitional", "confidence.evaluated",
    "confidence.no-origin-value", "customer_only_graph", "excluded:primary_listing_undeclared",
    "fetch_error:MoomooConnectionError", "fetch_error:ValueError", "growth_convention_unsourced:analyst_consensus",
    "insufficient_earnings_history", "missing_bank_net_revenue", "missing_goodwill", "missing_gross_profit",
    "missing_gross_profit_fact", "missing_headcount", "missing_headcount_disclosure",
    "missing_intangible_assets_net_excluding_goodwill", "missing_market_value_input", "missing_net_income",
    "missing_pre_provision_profit", "missing_preferred_stock_value", "missing_shares_outstanding",
    "missing_stockholders_equity", "missing_tangible_common_equity", "missing_total_assets",
    "no_analyst_coverage", "no_disclosed_suppliers", "no_operator_row", "no_oracle_value", "no_price",
    "no_segment_partition", "no_served_value", "no_supply_chain_extraction", "non_positive_earnings",
    "non_positive_growth", "nonpositive_gross_profit", "oracle:agree", "oracle:disagree", "oracle:none",
    "partial_valued_mass", "partition_residual", "primary_listing_undeclared", "quality.source-disagreement",
    "reason:cross_origin_conflict", "reason:independent_origins_agree", "reason:no_origin_value",
    "reason:same_lineage", "reason:second_origin_other_period", "reason:served_by_failover",
    "reason:single_origin", "reason:two_origins_agree",
    "reliability.provisional-unobserved-ceiling", "resolved_weight_below_minimum", "stale_input", "stale_period",
    "stale_required_input", "superseded", "support.same-origin-deduplicated", "support.single-origin-ceiling",
    "unclassified_revenue", "unquantified_partner_shares", "unresolved_holdings", "valued_weight_below_minimum",
    "vendor_timestamp",
)  # fmt: skip


@pytest.mark.parametrize("source", sorted(DEFINED))
def test_every_code_the_code_defines_is_registered(source: str) -> None:
    assert [code for code in DEFINED[source] if not is_registered(code)] == []


def test_every_code_served_on_staging_and_production_is_registered() -> None:
    assert [code for code in SERVED_ON_2026_11_24 if not is_registered(code)] == []


def test_only_a_connection_error_is_a_transient_fetch_error() -> None:
    """`fetch_error:ValueError` repeats on the same payload; an unlisted exception is our own gap."""
    assert str(registration("fetch_error:ValueError").reason_class) == "disputed"  # type: ignore[union-attr]
    assert str(registration("fetch_error:MoomooConnectionError").reason_class) == "transient"  # type: ignore[union-attr]
    assert str(registration("fetch_error:KeyError").reason_class) == "pipeline_gap"  # type: ignore[union-attr]


@pytest.mark.parametrize("code", sorted(UNSERVED_REASONS))
def test_the_reason_of_an_unserved_confidence_cell_is_an_outcome(code: str) -> None:
    """On an unserved `mart.cell_confidence` row `reason:<code>` is the only reason (#1851 review)."""
    assert registration(f"reason:{code}").kind is ReasonKind.OUTCOME  # type: ignore[union-attr]


def test_a_strategy_exclusion_is_never_evidence() -> None:
    """An exclusion is why a decision has no value: each one is an outcome with a class."""
    kinds = {code.value: registration(code.value).kind for code in ExclusionReason}  # type: ignore[union-attr]
    assert set(kinds.values()) == {ReasonKind.OUTCOME}
