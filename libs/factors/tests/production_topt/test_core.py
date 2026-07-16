from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from factors.production_topt import (
    GppeV0Definition,
    MetricAvailability,
    MetricFreshness,
    ThreeTierV0Definition,
    ToptCoreAvailability,
    ToptCoreReasonCode,
    ToptCoreSnapshotInput,
    ToptMetricInput,
    compute_topt_core,
)
from pydantic import ValidationError
from truealpha_contracts.research import ValuationTier

CUTOFF = datetime(2026, 4, 2, tzinfo=UTC)
OBSERVATION_IDS = tuple(f"normalized-observation:{character * 64}" for character in "1234")


def _metric(
    name: str,
    value: str | None,
    *,
    input_index: int = 0,
    confidence: str = "0.9",
    freshness: MetricFreshness = MetricFreshness.FRESH,
) -> ToptMetricInput:
    return ToptMetricInput(
        input_id=OBSERVATION_IDS[input_index],
        metric=name,
        value=value,
        unit="USD",
        confidence=confidence,
        knowable_at=CUTOFF - timedelta(days=1),
        freshness=freshness,
        availability=MetricAvailability.AVAILABLE if value is not None else MetricAvailability.UNAVAILABLE,
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
        "observation_ids": OBSERVATION_IDS,
        "gross_profit": _metric("gross_profit", "210000000", input_index=0),
        "total_assets": _metric("total_assets", "200000000", input_index=0),
        "headcount": _metric("headcount", "100", input_index=0, confidence="0.7"),
        "revenue": _metric("revenue", "100000000", input_index=0),
        "shares_outstanding": _metric("shares_outstanding", "10000000", input_index=0),
        "market_price": _metric("market_price", "40", input_index=1, confidence="0.8"),
    }
    values.update(overrides)
    return ToptCoreSnapshotInput.model_validate(values)


def _compute(snapshot: ToptCoreSnapshotInput):
    return compute_topt_core(
        snapshot,
        invocation_id=f"topt-core-invocation:{'e' * 64}",
        gppe_definition=GppeV0Definition(risk_free_rate="0.05"),
        tier_definition=ThreeTierV0Definition(),
    )


def test_available_result_uses_owner_selected_formula_and_minimum_confidence() -> None:
    result = _compute(_snapshot())

    assert result.availability is ToptCoreAvailability.AVAILABLE
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
    assert result.confidence == Decimal("0.7")
    assert result.reason_codes == ()


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
    ("field", "metric", "value", "expected_reason"),
    (
        ("headcount", "headcount", None, ToptCoreReasonCode.MISSING_HEADCOUNT),
        ("revenue", "revenue", "0", ToptCoreReasonCode.NONPOSITIVE_REVENUE),
    ),
)
def test_invalid_input_is_explicitly_unavailable(
    field: str,
    metric: str,
    value: str | None,
    expected_reason: ToptCoreReasonCode,
) -> None:
    result = _compute(_snapshot(**{field: _metric(metric, value)}))

    assert result.availability is ToptCoreAvailability.UNAVAILABLE
    assert result.confidence == 0
    assert result.reason_codes == (expected_reason,)
    assert result.gppe is None


def test_stale_input_never_silently_computes() -> None:
    result = _compute(
        _snapshot(market_price=_metric("market_price", "40", input_index=1, freshness=MetricFreshness.STALE))
    )

    assert result.availability is ToptCoreAvailability.UNAVAILABLE
    assert result.reason_codes == (ToptCoreReasonCode.STALE_INPUT,)
    assert result.freshness is MetricFreshness.STALE


def test_definitions_are_content_addressed_and_reject_binary_floats() -> None:
    first = GppeV0Definition(risk_free_rate="0.05")
    repeated = GppeV0Definition(risk_free_rate=Decimal("0.050"))

    assert first == repeated
    assert first.definition_id.endswith(first.content_sha256)
    assert ThreeTierV0Definition().definition_id.startswith("three-tier-definition:")
    with pytest.raises(ValidationError, match="binary float"):
        GppeV0Definition(risk_free_rate=0.05)


def test_snapshot_rejects_future_dated_or_unselected_inputs() -> None:
    future = _metric("market_price", "40", input_index=1).model_copy(
        update={"knowable_at": CUTOFF + timedelta(seconds=1)}
    )
    with pytest.raises(ValidationError, match="future-dated"):
        _snapshot(market_price=future)

    unselected = _metric("market_price", "40", input_index=1).model_copy(
        update={"input_id": f"normalized-observation:{'f' * 64}"}
    )
    with pytest.raises(ValidationError, match="not selected"):
        _snapshot(market_price=unselected)
