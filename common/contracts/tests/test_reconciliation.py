from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import ROUND_UP, Decimal, Inexact, localcontext

import pytest
from pydantic import ValidationError
from truealpha_contracts.reconciliation import (
    ReconciliationCell,
    ReconciliationOutcome,
    ReconciliationPolicy,
    SourceAssertion,
    reconcile_source_assertions,
)
from truealpha_contracts.universe import SubjectKind, SubjectRef

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
CUTOFF = datetime(2025, 3, 1, tzinfo=UTC)


def _cell(field_name: str = "gross_profit", subject_id: str = "entity:AAA") -> ReconciliationCell:
    return ReconciliationCell(
        requirement_id=f"data-requirement:{SHA_A}",
        subject=SubjectRef(kind=SubjectKind.ISSUER, id=subject_id),
        field_name=field_name,
        field_semantics_id=f"field-semantics:{SHA_B}",
        unit="USD",
        valid_from=date(2024, 1, 1),
        valid_to=date(2024, 12, 31),
    )


def _policy(
    *,
    absolute_tolerance: Decimal = Decimal("0"),
    relative_tolerance: Decimal = Decimal("0"),
    policy_version: str = "financial-fusion:v1",
    source_priority: tuple[str, ...] = (
        "source:sec:v1",
        "source:vendor-a:v1",
        "source:vendor-b:v1",
    ),
    minimum_independent_origin_groups: int = 2,
) -> ReconciliationPolicy:
    return ReconciliationPolicy(
        policy_version=policy_version,
        source_priority=source_priority,
        absolute_tolerance=absolute_tolerance,
        relative_tolerance=relative_tolerance,
        minimum_independent_origin_groups=minimum_independent_origin_groups,
    )


def _assertion(
    cell: ReconciliationCell,
    *,
    digest: str,
    source_id: str,
    origin_group_id: str,
    value: Decimal | None,
    value_sha256: str | None = None,
    knowable_at: datetime = datetime(2025, 2, 15, tzinfo=UTC),
    lineage_complete: bool = True,
) -> SourceAssertion:
    return SourceAssertion(
        cell_id=cell.cell_id,
        observation_id=f"normalized-observation:{digest}",
        source_id=source_id,
        origin_group_id=origin_group_id,
        knowable_at=knowable_at,
        normalized_value_sha256=value_sha256 or digest,
        numeric_value=value,
        confidence_assessment_id=f"confidence-assessment:{digest}",
        lineage_node_ids=(f"raw-object:{digest}", f"source-vintage:{digest}"),
        lineage_complete=lineage_complete,
    )


def _agreeing_result(cell: ReconciliationCell | None = None):
    cell = cell or _cell()
    primary = _assertion(
        cell,
        digest=SHA_A,
        source_id="source:sec:v1",
        origin_group_id="origin:sec:v1",
        value=Decimal("100"),
    )
    corroborating = _assertion(
        cell,
        digest=SHA_B,
        source_id="source:vendor-a:v1",
        origin_group_id="origin:vendor-a:v1",
        value=Decimal("100"),
    )
    result = reconcile_source_assertions(
        cell=cell,
        assertions=(corroborating, primary),
        policy=_policy(),
        cutoff=CUTOFF,
    )
    return cell, primary, corroborating, result


def test_independent_agreement_uses_priority_not_input_order() -> None:
    cell, primary, corroborating, result = _agreeing_result()

    assert result.outcome is ReconciliationOutcome.AGREED
    assert result.selected_assertion_id == primary.assertion_id
    assert result.origin_group_ids == ("origin:sec:v1", "origin:vendor-a:v1")

    reordered = reconcile_source_assertions(
        cell=cell,
        assertions=(primary, corroborating),
        policy=_policy(),
        cutoff=CUTOFF,
    )
    assert reordered == result
    assert reordered.result_id == result.result_id


def test_same_origin_mirror_does_not_inflate_independence() -> None:
    cell = _cell()
    original = _assertion(
        cell,
        digest=SHA_A,
        source_id="source:sec:v1",
        origin_group_id="origin:sec:v1",
        value=Decimal("100"),
    )
    mirror = _assertion(
        cell,
        digest=SHA_B,
        source_id="source:vendor-a:v1",
        origin_group_id="origin:sec:v1",
        value=Decimal("100"),
    )

    result = reconcile_source_assertions(
        cell=cell,
        assertions=(mirror, original),
        policy=_policy(),
        cutoff=CUTOFF,
    )

    assert result.outcome is ReconciliationOutcome.INSUFFICIENT_INDEPENDENT_ORIGINS
    assert result.representative_assertion_ids == (original.assertion_id,)
    assert result.origin_group_ids == ("origin:sec:v1",)
    assert "reconciliation.same_origin_deduplicated" in result.reason_codes


def test_outcome_uses_the_bound_independent_origin_threshold() -> None:
    cell, primary, corroborating, _ = _agreeing_result()
    minimum_one = reconcile_source_assertions(
        cell=cell,
        assertions=(primary,),
        policy=_policy(
            policy_version="single-origin-fusion:v1",
            minimum_independent_origin_groups=1,
        ),
        cutoff=CUTOFF,
    )
    assert minimum_one.outcome is ReconciliationOutcome.AGREED
    assert minimum_one.minimum_independent_origin_groups == 1

    minimum_three = reconcile_source_assertions(
        cell=cell,
        assertions=(primary, corroborating),
        policy=_policy(
            policy_version="three-origin-fusion:v1",
            minimum_independent_origin_groups=3,
        ),
        cutoff=CUTOFF,
    )
    assert minimum_three.outcome is ReconciliationOutcome.INSUFFICIENT_INDEPENDENT_ORIGINS

    payload = minimum_three.model_dump()
    payload.update({"result_id": "", "content_sha256": "", "outcome": "agreed"})
    with pytest.raises(ValidationError, match="does not meet the bound"):
        type(minimum_three).model_validate(payload)


def test_cross_origin_conflict_is_retained_and_abstains() -> None:
    cell = _cell()
    left = _assertion(
        cell,
        digest=SHA_A,
        source_id="source:sec:v1",
        origin_group_id="origin:sec:v1",
        value=Decimal("100"),
    )
    right = _assertion(
        cell,
        digest=SHA_B,
        source_id="source:vendor-a:v1",
        origin_group_id="origin:vendor-a:v1",
        value=Decimal("101"),
    )

    result = reconcile_source_assertions(
        cell=cell,
        assertions=(left, right),
        policy=_policy(),
        cutoff=CUTOFF,
    )

    assert result.outcome is ReconciliationOutcome.CONFLICT_ABSTAINED
    assert result.selected_assertion_id is None
    assert result.conflicting_assertion_ids == (right.assertion_id,)
    assert "reconciliation.cross_origin_conflict" in result.reason_codes

    payload = result.model_dump()
    payload.update(
        {
            "result_id": "",
            "content_sha256": "",
            "comparison_anchor_assertion_id": None,
        }
    )
    with pytest.raises(ValidationError, match="comparison anchor"):
        type(result).model_validate(payload)


def test_decimal_tolerance_boundary_is_deterministic() -> None:
    cell = _cell()
    left = _assertion(
        cell,
        digest=SHA_A,
        source_id="source:sec:v1",
        origin_group_id="origin:sec:v1",
        value=Decimal("100"),
    )
    right = _assertion(
        cell,
        digest=SHA_B,
        source_id="source:vendor-a:v1",
        origin_group_id="origin:vendor-a:v1",
        value=Decimal("101"),
    )

    result = reconcile_source_assertions(
        cell=cell,
        assertions=(left, right),
        policy=_policy(absolute_tolerance=Decimal("0.5"), relative_tolerance=Decimal("0.005")),
        cutoff=CUTOFF,
    )

    assert result.outcome is ReconciliationOutcome.AGREED


def test_reconciliation_ignores_the_callers_decimal_context() -> None:
    cell = _cell()
    left = _assertion(
        cell,
        digest=SHA_A,
        source_id="source:sec:v1",
        origin_group_id="origin:sec:v1",
        value=Decimal("1"),
    )
    right = _assertion(
        cell,
        digest=SHA_B,
        source_id="source:vendor-a:v1",
        origin_group_id="origin:vendor-a:v1",
        value=Decimal("1.1"),
    )
    policy = _policy(relative_tolerance=Decimal("0.0909090909090909090909090909"))
    expected = reconcile_source_assertions(cell=cell, assertions=(left, right), policy=policy, cutoff=CUTOFF)

    with localcontext() as context:
        context.prec = 6
        context.rounding = ROUND_UP
        context.traps[Inexact] = True
        assert (
            reconcile_source_assertions(cell=cell, assertions=(left, right), policy=policy, cutoff=CUTOFF) == expected
        )


def test_reconciliation_preserves_exact_high_precision_tolerance_boundaries() -> None:
    cell = _cell()
    left = _assertion(
        cell,
        digest=SHA_A,
        source_id="source:sec:v1",
        origin_group_id="origin:sec:v1",
        value=Decimal("0"),
    )
    right_value = Decimal("1." + ("0" * 49) + "4")
    right = _assertion(
        cell,
        digest=SHA_B,
        source_id="source:vendor-a:v1",
        origin_group_id="origin:vendor-a:v1",
        value=right_value,
    )
    policy = _policy(absolute_tolerance=Decimal("1." + ("0" * 49) + "3"))

    with localcontext() as context:
        context.prec = 6
        context.rounding = ROUND_UP
        context.traps[Inexact] = True
        result = reconcile_source_assertions(cell=cell, assertions=(left, right), policy=policy, cutoff=CUTOFF)

    assert result.outcome is ReconciliationOutcome.CONFLICT_ABSTAINED


def test_non_numeric_assertions_use_exact_canonical_value_hashes() -> None:
    cell = _cell("filing_classification")
    left = _assertion(
        cell,
        digest=SHA_A,
        source_id="source:sec:v1",
        origin_group_id="origin:sec:v1",
        value=None,
        value_sha256=SHA_C,
    )
    matching = _assertion(
        cell,
        digest=SHA_B,
        source_id="source:vendor-a:v1",
        origin_group_id="origin:vendor-a:v1",
        value=None,
        value_sha256=SHA_C,
    )

    agreed = reconcile_source_assertions(
        cell=cell,
        assertions=(left, matching),
        policy=_policy(),
        cutoff=CUTOFF,
    )
    assert agreed.outcome is ReconciliationOutcome.AGREED
    assert agreed.selected_value_sha256 == SHA_C
    assert agreed.selected_numeric_value is None

    conflicting = matching.model_copy(
        update={
            "assertion_id": "",
            "content_sha256": "",
            "normalized_value_sha256": SHA_D,
        }
    )
    conflicting = SourceAssertion.model_validate(conflicting.model_dump())
    result = reconcile_source_assertions(
        cell=cell,
        assertions=(left, conflicting),
        policy=_policy(),
        cutoff=CUTOFF,
    )
    assert result.outcome is ReconciliationOutcome.CONFLICT_ABSTAINED


def test_future_knowledge_is_excluded_before_priority_selection() -> None:
    cell = _cell()
    future_primary = _assertion(
        cell,
        digest=SHA_A,
        source_id="source:sec:v1",
        origin_group_id="origin:sec:v1",
        value=Decimal("200"),
        knowable_at=datetime(2025, 3, 2, tzinfo=UTC),
    )
    known_fallback = _assertion(
        cell,
        digest=SHA_B,
        source_id="source:vendor-a:v1",
        origin_group_id="origin:vendor-a:v1",
        value=Decimal("100"),
    )

    result = reconcile_source_assertions(
        cell=cell,
        assertions=(future_primary, known_fallback),
        policy=_policy(),
        cutoff=CUTOFF,
    )

    assert result.outcome is ReconciliationOutcome.INSUFFICIENT_INDEPENDENT_ORIGINS
    assert result.selected_assertion_id == known_fallback.assertion_id
    assert result.future_assertion_ids == (future_primary.assertion_id,)

    future_only = reconcile_source_assertions(
        cell=cell,
        assertions=(future_primary,),
        policy=_policy(),
        cutoff=CUTOFF,
    )
    assert future_only.outcome is ReconciliationOutcome.NOT_YET_KNOWABLE
    assert future_only.selected_assertion_id is None


def test_unregistered_source_is_visible_but_cannot_win() -> None:
    cell = _cell()
    assertion = _assertion(
        cell,
        digest=SHA_A,
        source_id="source:unknown:v1",
        origin_group_id="origin:unknown:v1",
        value=Decimal("100"),
    )

    result = reconcile_source_assertions(
        cell=cell,
        assertions=(assertion,),
        policy=_policy(),
        cutoff=CUTOFF,
    )

    assert result.outcome is ReconciliationOutcome.UNAVAILABLE
    assert result.unregistered_assertion_ids == (assertion.assertion_id,)


def test_agreed_result_cannot_be_deserialized_without_a_selected_assertion() -> None:
    _, _, _, result = _agreeing_result()
    payload = result.model_dump()
    payload.update(
        {
            "result_id": "",
            "content_sha256": "",
            "selected_assertion_id": None,
            "selected_value_sha256": None,
            "selected_numeric_value": None,
        }
    )

    with pytest.raises(ValidationError, match="must select an assertion"):
        type(result).model_validate(payload)


def test_binary_float_inputs_are_rejected() -> None:
    with pytest.raises(ValidationError, match="binary float"):
        _policy(absolute_tolerance=0.1)  # type: ignore[arg-type]

    cell = _cell()
    with pytest.raises(ValidationError, match="binary float"):
        _assertion(
            cell,
            digest=SHA_A,
            source_id="source:sec:v1",
            origin_group_id="origin:sec:v1",
            value=100.0,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")])
def test_non_finite_decimal_inputs_are_rejected(value: Decimal) -> None:
    with pytest.raises(ValidationError, match="non-finite Decimal"):
        _policy(absolute_tolerance=value)

    cell = _cell()
    with pytest.raises(ValidationError, match="non-finite Decimal"):
        _assertion(
            cell,
            digest=SHA_A,
            source_id="source:sec:v1",
            origin_group_id="origin:sec:v1",
            value=value,
        )
