from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from factors.production_topt import (
    GppeV0Definition,
    MetricAvailability,
    MetricFreshness,
    OperatingBranch,
    OperatingEfficiencyMetric,
    ToptCellQualityInput,
    ToptCoreAvailability,
    ToptCoreReasonCode,
    ToptCoreSnapshotInput,
    ToptMarketValueComponent,
    ToptMetricInput,
    compute_topt_core,
    compute_topt_gppe,
)
from pydantic import ValidationError
from truealpha_contracts.research import ValuationTier
from truealpha_contracts.strategy import (
    ValuationBasis,
    load_live_strategy_definition,
)

CUTOFF = datetime(2026, 4, 2, tzinfo=UTC)
OBSERVATION_IDS = tuple(f"normalized-observation:{character * 64}" for character in "1234")
SECOND_LISTING_IDS = tuple(f"normalized-observation:{character * 64}" for character in "5678")


def _metric(
    name: str,
    value: str | None,
    *,
    input_id: str = OBSERVATION_IDS[0],
    confidence: str = "0.9",
) -> ToptMetricInput:
    return ToptMetricInput(
        input_id=input_id,
        metric=name,
        value=value,
        confidence=confidence,
        knowable_at=CUTOFF - timedelta(days=1),
        freshness=MetricFreshness.FRESH,
        availability=MetricAvailability.AVAILABLE if value is not None else MetricAvailability.UNAVAILABLE,
    )


def _cell(
    input_id: str,
    *,
    confidence: str = "0.9",
    freshness: MetricFreshness = MetricFreshness.FRESH,
) -> ToptCellQualityInput:
    return ToptCellQualityInput(
        input_id=input_id,
        confidence=confidence,
        knowable_at=CUTOFF - timedelta(days=1),
        freshness=freshness,
    )


def _cells(ids: tuple[str, ...] = OBSERVATION_IDS) -> tuple[ToptCellQualityInput, ...]:
    return tuple(_cell(input_id) for input_id in ids)


def _component(
    *,
    instrument_id: str = "instrument:example",
    listing_id: str = "listing:example",
    financial_observation_id: str = OBSERVATION_IDS[0],
    market_observation_id: str = OBSERVATION_IDS[1],
    price: str | None = "40",
    shares: str | None = "10000000",
) -> ToptMarketValueComponent:
    return ToptMarketValueComponent(
        instrument_id=instrument_id,
        listing_id=listing_id,
        market_price=_metric("market_price", price, input_id=market_observation_id),
        shares_outstanding=_metric("shares_outstanding", shares, input_id=financial_observation_id),
    )


def _snapshot(**overrides: object) -> ToptCoreSnapshotInput:
    values: dict[str, object] = {
        "snapshot_id": f"topt-core-snapshot:{'a' * 64}",
        "run_id": f"capture-run:{'b' * 64}",
        "release_manifest_id": f"release-manifest:{'c' * 64}",
        "universe_id": "universe:topt-candidate-v1",
        "universe_version": "2026-03-31-v1",
        "universe_sha256": "d" * 64,
        "cutoff": CUTOFF,
        "issuer_id": "issuer:example",
        "instrument_id": "instrument:example",
        "listing_id": "listing:example",
        "operating_branch": OperatingBranch.NON_FINANCIAL,
        "observation_ids": OBSERVATION_IDS,
        "cell_inputs": _cells(),
        "gross_profit": _metric("gross_profit", "210000000"),
        "total_assets": _metric("total_assets", "200000000"),
        "headcount": _metric("headcount", "100"),
        "revenue": _metric("revenue", "100000000"),
        "pre_provision_profit": _metric("pre_provision_profit", None),
        "market_value_components": (_component(),),
    }
    values.update(overrides)
    return ToptCoreSnapshotInput.model_validate(values)


def _compute(snapshot: ToptCoreSnapshotInput):
    gppe_result = compute_topt_gppe(
        snapshot,
        invocation_id=f"topt-gppe-invocation:{'f' * 64}",
        gppe_definition=GppeV0Definition(risk_free_rate="0.05"),
        definition=load_live_strategy_definition(),
    )
    return compute_topt_core(
        snapshot,
        gppe_result,
        invocation_id=f"topt-core-invocation:{'e' * 64}",
        definition=load_live_strategy_definition(),
    )


def test_available_result_uses_owner_selected_formula_and_all_cell_confidence() -> None:
    cells = (
        _cell(OBSERVATION_IDS[0], confidence="0.7"),
        _cell(OBSERVATION_IDS[1], confidence="0.6"),
        _cell(OBSERVATION_IDS[2], confidence="0.8"),
        _cell(OBSERVATION_IDS[3], confidence="0.9"),
    )
    result = _compute(_snapshot(cell_inputs=cells))

    assert result.availability is ToptCoreAvailability.AVAILABLE
    assert result.operating_metric is OperatingEfficiencyMetric.CAPITAL_ADJUSTED_GPPE
    assert result.operating_efficiency == Decimal("2000000")
    assert result.capital_adjusted_gross_profit == Decimal("200000000")
    assert result.gppe == Decimal("2000000")
    # #1235: the tech band of the live strategy definition, P/GP 11.43-14.29, midpoint 12.86.
    assert result.tier is ValuationTier.TECH
    assert (result.target_pgp_lower, result.target_pgp_upper, result.target_pgp_midpoint) == (
        Decimal("11.43"),
        Decimal("14.29"),
        Decimal("12.86"),
    )
    # Market value 40 x 10M = 400M; P/S = 400M / 100M = 4; P/GP = 400M / 210M = 1.9047...
    assert result.current_ps == Decimal("4")
    assert result.current_pgp is not None and result.current_pgp.quantize(Decimal("0.0001")) == Decimal("1.9048")
    # Gap = 12.86 / (400 / 210) - 1 = 12.86 x 0.525 - 1 = 5.7515.
    assert result.valuation_gap is not None and result.valuation_gap.quantize(Decimal("0.0001")) == Decimal("5.7515")
    # The P/S targets are the band at this issuer's margin, 210M / 100M = 2.1: never a P/GP value.
    assert (result.target_ps_lower, result.target_ps_upper, result.target_ps_midpoint) == (
        Decimal("24.003"),
        Decimal("30.009"),
        Decimal("27.006"),
    )
    assert result.target_ps_midpoint is not None and result.current_ps is not None
    ps_gap = result.target_ps_midpoint / result.current_ps - 1
    assert abs(ps_gap - result.valuation_gap) < Decimal("1e-30")
    assert result.confidence == Decimal("0.6")
    assert result.reason_codes == ()
    assert result.gppe_result_id.startswith("topt-gppe-result:")


@pytest.mark.parametrize(
    ("adjusted_gross_profit", "expected_tier"),
    (
        ("99999900", ValuationTier.TRADITIONAL),
        ("100000000", ValuationTier.TECH),
        ("300000000", ValuationTier.LARGE_MODEL_NATIVE),
    ),
)
def test_tier_boundaries_are_lower_inclusive(adjusted_gross_profit: str, expected_tier: ValuationTier) -> None:
    total_assets = Decimal("200000000")
    gross_profit = Decimal(adjusted_gross_profit) + total_assets * Decimal("0.05")
    result = _compute(_snapshot(gross_profit=_metric("gross_profit", str(gross_profit))))

    assert result.tier is expected_tier


@pytest.mark.parametrize(
    ("field", "metric", "value", "expected_reason", "expected_confidence"),
    (
        ("headcount", "headcount", None, ToptCoreReasonCode.MISSING_HEADCOUNT, Decimal("0")),
        ("revenue", "revenue", "0", ToptCoreReasonCode.NONPOSITIVE_REVENUE, Decimal("0.9")),
    ),
)
def test_invalid_input_is_explicitly_unavailable(
    field: str,
    metric: str,
    value: str | None,
    expected_reason: ToptCoreReasonCode,
    expected_confidence: Decimal,
) -> None:
    result = _compute(_snapshot(**{field: _metric(metric, value)}))

    assert result.availability is ToptCoreAvailability.UNAVAILABLE
    assert result.confidence == expected_confidence
    assert result.reason_codes == (expected_reason,)
    assert result.gppe is None


def test_identity_cell_freshness_and_confidence_are_not_dropped() -> None:
    stale_identity = list(_cells())
    stale_identity[2] = _cell(OBSERVATION_IDS[2], freshness=MetricFreshness.STALE)
    stale = _compute(_snapshot(cell_inputs=tuple(stale_identity)))

    assert stale.availability is ToptCoreAvailability.UNAVAILABLE
    assert stale.reason_codes == (ToptCoreReasonCode.STALE_INPUT,)
    assert stale.freshness is MetricFreshness.STALE

    low_confidence_identity = list(_cells())
    low_confidence_identity[3] = _cell(OBSERVATION_IDS[3], confidence="0.2")
    available = _compute(_snapshot(cell_inputs=tuple(low_confidence_identity)))
    assert available.confidence == Decimal("0.2")


def test_current_ps_values_company_total_shares_once_per_issuer() -> None:
    # #705: every listing's dei share count is the COMPANY total, so identical
    # counts across classes are one company, not one per class — the old pin
    # here was 8, encoding Alphabet valued twice. The execution listing's price
    # values it once; the second class's differing price must not leak in.
    observation_ids = tuple(sorted((*OBSERVATION_IDS, *SECOND_LISTING_IDS)))
    components = (
        _component(),
        _component(
            instrument_id="instrument:example-b",
            listing_id="listing:example-b",
            financial_observation_id=SECOND_LISTING_IDS[0],
            market_observation_id=SECOND_LISTING_IDS[1],
            price="50",
        ),
    )
    result = _compute(
        _snapshot(
            observation_ids=observation_ids,
            cell_inputs=_cells(observation_ids),
            market_value_components=components,
        )
    )

    assert result.current_ps == Decimal("4"), "40 x 10M / 100M — the company once, at the execution listing's price"
    assert result.input_observation_ids == observation_ids


def test_current_ps_sums_distinct_per_class_share_counts() -> None:
    # The #63 instrument tranche's world: per-CLASS counts differ, and the cap
    # is genuinely the sum of class caps. 40x6M + 50x4M = 440M / 100M = 4.4.
    observation_ids = tuple(sorted((*OBSERVATION_IDS, *SECOND_LISTING_IDS)))
    components = (
        _component(shares="6000000"),
        _component(
            instrument_id="instrument:example-b",
            listing_id="listing:example-b",
            financial_observation_id=SECOND_LISTING_IDS[0],
            market_observation_id=SECOND_LISTING_IDS[1],
            price="50",
            shares="4000000",
        ),
    )
    result = _compute(
        _snapshot(
            observation_ids=observation_ids,
            cell_inputs=_cells(observation_ids),
            market_value_components=components,
        )
    )

    assert result.current_ps == Decimal("4.4")


#: A bank whose measured tangible common equity is a fifth of its total assets (200,000,000).
#: TCE = 50,000,000 - 5,000,000 - (3,000,000 + 2,000,000) = 40,000,000. The bank's own numbers, written out
#: here and not read from the code under test.
_BANK_LEAVES = {
    "stockholders_equity": "50000000",
    "preferred_stock_value": "5000000",
    "goodwill": "3000000",
    "intangible_assets_net_excluding_goodwill": "2000000",
}


def _bank_snapshot(**overrides: object) -> ToptCoreSnapshotInput:
    leaves = {name: _metric(name, value) for name, value in _BANK_LEAVES.items()}
    values: dict[str, object] = {
        "operating_branch": OperatingBranch.FINANCIAL,
        "gross_profit": None,
        "pre_provision_profit": _metric("pre_provision_profit", "80000000"),
        **leaves,
    }
    values.update(overrides)
    return _snapshot(**values)


def test_financial_issuer_takes_the_banking_tce_path() -> None:
    # #1518, owner decision D2: the definition pins the banking tree for FINANCIAL issuers. The capital
    # charge is measured TCE x rate, not total assets x rate, and the bank flows through the tier / P-S path.
    result = _compute(_bank_snapshot())

    assert result.availability is ToptCoreAvailability.AVAILABLE
    assert result.operating_metric is OperatingEfficiencyMetric.CAPITAL_ADJUSTED_GPPE
    # Hand value: (80_000_000 - 40_000_000 * 0.05) / 100 = 78_000_000 / 100 = 780_000.
    # The total-assets value of the same bank is (80_000_000 - 200_000_000 * 0.05) / 100 = 700_000.
    assert result.gppe == Decimal("780000")
    assert result.operating_efficiency == Decimal("780000")
    assert result.capital_adjusted_gross_profit == Decimal("78000000")
    assert result.tier is ValuationTier.TRADITIONAL
    assert result.current_ps is not None


def test_a_bank_needs_its_banking_leaves_and_not_total_assets() -> None:
    # Total assets is not an input of the banking tree: a bank without it is still evaluated.
    without_assets = _compute(_bank_snapshot(total_assets=None))
    assert without_assets.availability is ToptCoreAvailability.AVAILABLE
    assert without_assets.gppe == Decimal("780000")

    # A missing leaf is named by its snapshot field. No other leaf and no total assets replaces it.
    without_goodwill = _compute(_bank_snapshot(goodwill=None))
    assert without_goodwill.availability is ToptCoreAvailability.UNAVAILABLE
    assert without_goodwill.reason_codes == (ToptCoreReasonCode.MISSING_GOODWILL,)


def test_a_non_financial_issuer_still_needs_total_assets() -> None:
    result = _compute(_snapshot(total_assets=None))

    assert result.availability is ToptCoreAvailability.UNAVAILABLE
    assert ToptCoreReasonCode.MISSING_TOTAL_ASSETS in result.reason_codes


def test_tier_composite_rejects_gppe_from_another_snapshot_member() -> None:
    snapshot = _snapshot()
    other_member = _snapshot(issuer_id="issuer:other")
    gppe_result = compute_topt_gppe(
        other_member,
        invocation_id=f"topt-gppe-invocation:{'f' * 64}",
        gppe_definition=GppeV0Definition(risk_free_rate="0.05"),
        definition=load_live_strategy_definition(),
    )

    with pytest.raises(ValueError, match="does not match its exact snapshot member"):
        compute_topt_core(
            snapshot,
            gppe_result,
            invocation_id=f"topt-core-invocation:{'e' * 64}",
            definition=load_live_strategy_definition(),
        )


def test_definitions_are_content_addressed_and_reject_binary_floats() -> None:
    first = GppeV0Definition(risk_free_rate="0.05")
    repeated = GppeV0Definition(risk_free_rate=Decimal("0.050"))

    assert first == repeated
    assert first.definition_id.endswith(first.content_sha256)
    with pytest.raises(ValidationError, match="binary float"):
        GppeV0Definition(risk_free_rate=0.05)


def test_snapshot_rejects_future_dated_or_unselected_inputs() -> None:
    future_price = _metric("market_price", "40", input_id=OBSERVATION_IDS[1]).model_copy(
        update={"knowable_at": CUTOFF + timedelta(seconds=1)}
    )
    with pytest.raises(ValidationError, match="future-dated"):
        _snapshot(market_value_components=(_component().model_copy(update={"market_price": future_price}),))

    unselected_price = _metric(
        "market_price",
        "40",
        input_id=f"normalized-observation:{'f' * 64}",
    )
    with pytest.raises(ValidationError, match="not selected"):
        _snapshot(market_value_components=(_component().model_copy(update={"market_price": unselected_price}),))


def test_snapshot_rejects_malformed_observation_identity() -> None:
    malformed = (*OBSERVATION_IDS[:-1], "normalized-observation:not-a-sha256")

    with pytest.raises(ValidationError, match="normalized observation identities"):
        _snapshot(observation_ids=malformed)


def test_the_core_equals_the_composite_for_identical_inputs() -> None:
    # #394 cross-path equivalence, kept by #1518: the core and the strategy composite evaluate the one
    # tree that the definition pins for the class. Same inputs, same value, for a non-financial issuer
    # (gross-profit numerator, total-assets charge) and for a bank (pre-provision numerator, TCE charge).
    from factors.composite.strategy_evaluator import IssuerInput, evaluate_cutoff
    from factors.forest import IssuerClass

    def decision_value(records: dict[str, str], issuer_class: IssuerClass) -> Decimal:
        full = {
            **records,
            "revenue": "100000000",
            "shares_outstanding": "10000000",
            "last_close": "40",
        }
        issuer = IssuerInput(
            issuer_id="issuer:x",
            records={key: (Decimal(value), Decimal("0.9")) for key, value in full.items()},
            knowable_at=dict.fromkeys(full, CUTOFF - timedelta(days=1)),
            issuer_class=issuer_class,
        )
        [decision] = evaluate_cutoff(
            [issuer], definition=load_live_strategy_definition(), cutoff_at=CUTOFF, risk_free_rate=Decimal("0.05")
        )
        assert decision.capital_adjusted_labor_efficiency is not None
        return decision.capital_adjusted_labor_efficiency

    core_non_financial = _compute(_snapshot()).gppe
    assert core_non_financial == Decimal("2000000")  # (210M - 200M x 0.05) / 100
    assert decision_value(
        {"gross_profit": "210000000", "total_assets": "200000000", "headcount": "100"}, IssuerClass.NON_FINANCIAL
    ) == Decimal("2000000.00")

    core_bank = _compute(_bank_snapshot()).gppe
    assert core_bank == Decimal("780000")  # (80M - 40M x 0.05) / 100
    assert decision_value(
        {"gross_profit": "80000000", "total_assets": "200000000", "headcount": "100", **_BANK_LEAVES},
        IssuerClass.FINANCIAL,
    ) == Decimal("780000.00")


def test_rows_stamp_the_strategy_definition_that_supplied_the_bands() -> None:
    """#1235: one band set. The core row names the same definition the strategy tick runs."""
    definition = load_live_strategy_definition()
    result = _compute(_snapshot())
    assert result.tier_definition_id == definition.strategy_definition_id
    assert result.tier_definition_sha256 == definition.content_sha256
    unavailable = _compute(_snapshot(headcount=_metric("headcount", None)))
    assert unavailable.tier_definition_sha256 == definition.content_sha256


def test_the_core_engine_refuses_a_ps_basis_definition() -> None:
    from unittest.mock import MagicMock

    snapshot = _snapshot()
    gppe_result = compute_topt_gppe(
        snapshot,
        invocation_id=f"topt-gppe-invocation:{'f' * 64}",
        gppe_definition=GppeV0Definition(risk_free_rate="0.05"),
        definition=load_live_strategy_definition(),
    )
    ps_definition = MagicMock(
        valuation_basis=ValuationBasis.PRICE_TO_SALES,
        definition_version="0.1.0",
    )
    with pytest.raises(ValueError, match="values by P/GP"):
        compute_topt_core(
            snapshot,
            gppe_result,
            invocation_id=f"topt-core-invocation:{'e' * 64}",
            definition=ps_definition,
        )


def test_pgp_divides_by_the_numerator_gppe_reads_for_the_class() -> None:
    """A bank's P/GP divides by its pre-provision profit, the GPPE tree's financial numerator."""
    result = _compute(_bank_snapshot())
    # (80M - 40M x 0.05) / 100 = 780,000 -> traditional, P/GP 7.50-10.00; 400M / 80M = 5.
    assert result.tier is ValuationTier.TRADITIONAL
    assert result.current_pgp == Decimal("5")
    assert result.target_pgp_midpoint == Decimal("8.75")
    assert result.valuation_gap == Decimal("0.75")


def test_nonpositive_gross_profit_is_unavailable_not_divided() -> None:
    # Adjusted GPPE is negative (charge 10M > gross profit 0), so GPPE exists; P/GP does not.
    result = _compute(_snapshot(gross_profit=_metric("gross_profit", "0")))
    assert result.availability is ToptCoreAvailability.UNAVAILABLE
    assert result.reason_codes == (ToptCoreReasonCode.NONPOSITIVE_GROSS_PROFIT,)
    assert result.current_pgp is None and result.valuation_gap is None


# -- #1277: a share count that the snapshot builder dropped names its cause ----------------------


def _unavailable_shares(reason: ToptCoreReasonCode | None) -> ToptMetricInput:
    return ToptMetricInput(
        input_id=OBSERVATION_IDS[0],
        metric="shares_outstanding",
        value=None,
        confidence="0.9",
        knowable_at=CUTOFF - timedelta(days=1),
        freshness=MetricFreshness.FRESH,
        availability=MetricAvailability.UNAVAILABLE,
        unavailable_reason=reason,
    )


def _snapshot_with_shares(shares: ToptMetricInput) -> ToptCoreSnapshotInput:
    component = _component().model_copy(update={"shares_outstanding": shares})
    return _snapshot(market_value_components=(component,))


def test_a_dropped_share_count_reports_its_named_cause_and_not_a_missing_count() -> None:
    """Red on main: the engine reported `missing_shares_outstanding` for every dropped count."""
    result = _compute(_snapshot_with_shares(_unavailable_shares(ToptCoreReasonCode.NO_SPLIT_ADJUSTED_SERIES)))

    assert result.availability is ToptCoreAvailability.UNAVAILABLE
    assert result.reason_codes == (ToptCoreReasonCode.NO_SPLIT_ADJUSTED_SERIES,)
    assert result.current_pgp is None and result.current_ps is None


def test_a_share_count_with_no_named_cause_still_reports_a_missing_count() -> None:
    result = _compute(_snapshot_with_shares(_unavailable_shares(None)))

    assert result.reason_codes == (ToptCoreReasonCode.MISSING_SHARES_OUTSTANDING,)


def test_two_listings_with_different_causes_report_both() -> None:
    first = _component()
    second = _component(
        instrument_id="instrument:second",
        listing_id="listing:second",
        financial_observation_id=SECOND_LISTING_IDS[0],
        market_observation_id=SECOND_LISTING_IDS[1],
    ).model_copy(
        update={
            "shares_outstanding": _unavailable_shares(ToptCoreReasonCode.SPLIT_DERIVATION_FAILED).model_copy(
                update={"input_id": SECOND_LISTING_IDS[0]}
            )
        }
    )
    first = first.model_copy(update={"shares_outstanding": _unavailable_shares(None)})
    snapshot = _snapshot(
        observation_ids=tuple(sorted((*OBSERVATION_IDS, *SECOND_LISTING_IDS))),
        cell_inputs=_cells(tuple(sorted((*OBSERVATION_IDS, *SECOND_LISTING_IDS)))),
        market_value_components=(first, second),
    )

    result = _compute(snapshot)

    assert result.reason_codes == (
        ToptCoreReasonCode.MISSING_SHARES_OUTSTANDING,
        ToptCoreReasonCode.SPLIT_DERIVATION_FAILED,
    )


def test_a_metric_without_a_named_cause_keeps_its_serialized_form() -> None:
    """A snapshot frozen before #1277 must keep its content hash. The key is absent unless a cause is set."""
    without = _metric("revenue", None).model_dump(mode="json")
    with_cause = _unavailable_shares(ToptCoreReasonCode.SHARE_BASIS_AMBIGUOUS).model_dump(mode="json")

    assert "unavailable_reason" not in without
    assert with_cause["unavailable_reason"] == "share_basis_ambiguous"
    assert ToptMetricInput.model_validate(with_cause).unavailable_reason is ToptCoreReasonCode.SHARE_BASIS_AMBIGUOUS


def test_an_available_metric_cannot_carry_a_cause() -> None:
    with pytest.raises(ValidationError, match="available metric cannot carry an unavailable reason"):
        ToptMetricInput(
            input_id=OBSERVATION_IDS[0],
            metric="shares_outstanding",
            value="10",
            confidence="0.9",
            knowable_at=CUTOFF - timedelta(days=1),
            freshness=MetricFreshness.FRESH,
            availability=MetricAvailability.AVAILABLE,
            unavailable_reason=ToptCoreReasonCode.NO_SPLIT_ADJUSTED_SERIES,
        )


def test_an_issuer_with_no_declared_primary_listing_is_unavailable_with_its_reason() -> None:
    """#1518: complete inputs give no value when the snapshot builder cannot name the primary listing."""
    complete = _compute(_snapshot())
    undeclared = _compute(_snapshot(primary_listing_undeclared=True))

    assert complete.availability.value == "available"
    assert undeclared.availability.value == "unavailable"
    assert undeclared.reason_codes == (ToptCoreReasonCode.PRIMARY_LISTING_UNDECLARED,)
    assert undeclared.operating_efficiency is None and undeclared.current_ps is None


def test_a_snapshot_input_keeps_its_serialized_form_unless_the_listing_is_undeclared() -> None:
    """A stored factor input must keep its content hash. The key is absent unless the flag is set."""
    assert "primary_listing_undeclared" not in _snapshot().model_dump(mode="json")
    assert _snapshot(primary_listing_undeclared=True).model_dump(mode="json")["primary_listing_undeclared"] is True


def test_a_negative_tangible_common_equity_is_unavailable_in_the_core_and_in_the_composite() -> None:
    # #1518 review. TCE = 20M - 5M - (15M + 5M) = -5M. The capital charge is never a credit: the core gives
    # no value and names the reason, and the composite excludes the bank with the same reason.
    from factors.composite.strategy_evaluator import IssuerInput, evaluate_cutoff
    from factors.forest import IssuerClass

    leaves = {
        "stockholders_equity": "20000000",
        "preferred_stock_value": "5000000",
        "goodwill": "15000000",
        "intangible_assets_net_excluding_goodwill": "5000000",
    }
    core = _compute(_bank_snapshot(**{name: _metric(name, value) for name, value in leaves.items()}))
    assert core.availability is ToptCoreAvailability.UNAVAILABLE
    assert core.gppe is None
    assert core.reason_codes == (ToptCoreReasonCode.NEGATIVE_TANGIBLE_COMMON_EQUITY,)

    full = {
        **leaves,
        "gross_profit": "80000000",
        "total_assets": "200000000",
        "headcount": "100",
        "revenue": "100000000",
        "shares_outstanding": "10000000",
        "last_close": "40",
    }
    issuer = IssuerInput(
        issuer_id="issuer:x",
        records={key: (Decimal(value), Decimal("0.9")) for key, value in full.items()},
        knowable_at=dict.fromkeys(full, CUTOFF - timedelta(days=1)),
        issuer_class=IssuerClass.FINANCIAL,
    )
    [decision] = evaluate_cutoff(
        [issuer], definition=load_live_strategy_definition(), cutoff_at=CUTOFF, risk_free_rate=Decimal("0.05")
    )
    assert decision.eligible is False
    assert decision.exclusion_reason is not None and decision.exclusion_reason.value == core.reason_codes[0].value


def test_a_zero_tangible_common_equity_is_a_zero_charge_in_the_core() -> None:
    # TCE = 25M - 5M - (15M + 5M) = 0. Real profit = 80M - 0 x 0.05 = 80M. Value = 80M / 100 = 800,000.
    leaves = {
        "stockholders_equity": "25000000",
        "preferred_stock_value": "5000000",
        "goodwill": "15000000",
        "intangible_assets_net_excluding_goodwill": "5000000",
    }
    result = _compute(_bank_snapshot(**{name: _metric(name, value) for name, value in leaves.items()}))
    assert result.availability is ToptCoreAvailability.AVAILABLE
    assert result.gppe == Decimal("800000")
    assert result.capital_adjusted_gross_profit == Decimal("80000000")
