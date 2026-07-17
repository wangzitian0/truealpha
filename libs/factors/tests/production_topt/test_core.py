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
    ThreeTierV0Definition,
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
        unit="USD",
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
    )
    return compute_topt_core(
        snapshot,
        gppe_result,
        invocation_id=f"topt-core-invocation:{'e' * 64}",
        tier_definition=ThreeTierV0Definition(),
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
    assert result.tier is ValuationTier.TECH
    assert (result.target_ps_lower, result.target_ps_upper, result.target_ps_midpoint) == (
        Decimal("8"),
        Decimal("10"),
        Decimal("9"),
    )
    assert result.current_ps == Decimal("4")
    assert result.valuation_gap == Decimal("1.25")
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


def test_current_ps_aggregates_all_share_classes_once_per_issuer() -> None:
    observation_ids = tuple(sorted((*OBSERVATION_IDS, *SECOND_LISTING_IDS)))
    components = (
        _component(),
        _component(
            instrument_id="instrument:example-b",
            listing_id="listing:example-b",
            financial_observation_id=SECOND_LISTING_IDS[0],
            market_observation_id=SECOND_LISTING_IDS[1],
        ),
    )
    result = _compute(
        _snapshot(
            observation_ids=observation_ids,
            cell_inputs=_cells(observation_ids),
            market_value_components=components,
        )
    )

    assert result.current_ps == Decimal("8")
    assert result.input_observation_ids == observation_ids


def test_financial_branch_uses_pre_provision_profit_without_nonfinancial_valuation() -> None:
    result = _compute(
        _snapshot(
            operating_branch=OperatingBranch.FINANCIAL,
            gross_profit=None,
            total_assets=None,
            revenue=None,
            pre_provision_profit=_metric("pre_provision_profit", "80000000"),
        )
    )

    assert result.availability is ToptCoreAvailability.UNAVAILABLE
    assert result.operating_metric is OperatingEfficiencyMetric.PRE_PROVISION_PROFIT_PER_EMPLOYEE
    assert result.operating_efficiency == Decimal("800000")
    assert result.gppe is result.tier is result.current_ps is None
    assert result.reason_codes == (ToptCoreReasonCode.FINANCIAL_VALUATION_NOT_COMPARABLE,)


def test_tier_composite_rejects_gppe_from_another_snapshot_member() -> None:
    snapshot = _snapshot()
    other_member = _snapshot(issuer_id="issuer:other")
    gppe_result = compute_topt_gppe(
        other_member,
        invocation_id=f"topt-gppe-invocation:{'f' * 64}",
        gppe_definition=GppeV0Definition(risk_free_rate="0.05"),
    )

    with pytest.raises(ValueError, match="does not match its exact snapshot member"):
        compute_topt_core(
            snapshot,
            gppe_result,
            invocation_id=f"topt-core-invocation:{'e' * 64}",
            tier_definition=ThreeTierV0Definition(),
        )


def test_definitions_are_content_addressed_and_reject_binary_floats() -> None:
    first = GppeV0Definition(risk_free_rate="0.05")
    repeated = GppeV0Definition(risk_free_rate=Decimal("0.050"))

    assert first == repeated
    assert first.definition_id.endswith(first.content_sha256)
    assert ThreeTierV0Definition().definition_id.startswith("three-tier-definition:")
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
