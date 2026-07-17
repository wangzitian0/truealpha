from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import ROUND_UP, Decimal, Inexact, localcontext

import pytest
from pydantic import ValidationError
from truealpha_contracts import SubjectKind, SubjectRef
from truealpha_contracts.datahub import AssessmentFreshness, ObligationTerminalState
from truealpha_contracts.reconciliation import (
    DataHubQualityCell,
    DataHubQualityDenominator,
    DataHubQualitySummary,
    ReconciliationCell,
    ReconciliationOutcome,
    ReconciliationPolicy,
    SourceAssertion,
    VersionedDataHubQualityReport,
    reconcile_source_assertions,
)

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
    confidence_score: Decimal = Decimal("0.80"),
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
        confidence_score=confidence_score,
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
        confidence_score=Decimal("0.75"),
    )
    corroborating = _assertion(
        cell,
        digest=SHA_B,
        source_id="source:vendor-a:v1",
        origin_group_id="origin:vendor-a:v1",
        value=Decimal("100"),
        confidence_score=Decimal("0.99"),
    )
    result = reconcile_source_assertions(
        cell=cell,
        assertions=(corroborating, primary),
        policy=_policy(),
        cutoff=CUTOFF,
    )
    return cell, primary, corroborating, result


def test_independent_agreement_uses_priority_not_confidence_or_input_order() -> None:
    cell, primary, corroborating, result = _agreeing_result()

    assert result.outcome is ReconciliationOutcome.AGREED
    assert result.selected_assertion_id == primary.assertion_id
    assert result.selected_confidence_score == Decimal("0.75")
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


def test_quality_summary_ignores_the_callers_decimal_context() -> None:
    policy = _policy()
    rows = (
        DataHubQualityCell(
            cell=_cell("gross_profit"),
            reconciliation_policy_id=policy.policy_id,
            planned=True,
            lineage_complete=False,
            reason_codes=("quality.pending",),
        ),
        DataHubQualityCell(
            cell=_cell("revenue"),
            reconciliation_policy_id=policy.policy_id,
            planned=False,
            lineage_complete=False,
            reason_codes=("quality.no_source_plan",),
        ),
        DataHubQualityCell(
            cell=_cell("net_income"),
            reconciliation_policy_id=policy.policy_id,
            planned=False,
            lineage_complete=False,
            reason_codes=("quality.no_source_plan",),
        ),
    )

    def build() -> VersionedDataHubQualityReport:
        return VersionedDataHubQualityReport(
            report_schema_version="datahub-quality-report:v1",
            denominator=DataHubQualityDenominator(
                service_demand_id=f"datahub-service-demand:{SHA_D}",
                requested_cell_ids=tuple(row.cell.cell_id for row in rows),
            ),
            reconciliation_policies=(policy,),
            cutoff=CUTOFF,
            generated_at=datetime(2025, 3, 1, 1, tzinfo=UTC),
            cells=rows,
        )

    expected = build()
    assert expected.summary is not None
    assert expected.summary.planned_coverage == Decimal("0." + "3" * 50)
    with localcontext() as context:
        context.prec = 6
        context.rounding = ROUND_UP
        context.traps[Inexact] = True
        assert build() == expected


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


def test_quality_report_keeps_missing_failed_and_unplanned_cells_in_denominator() -> None:
    policy = _policy()
    secondary_policy = _policy(
        policy_version="headcount-fusion:v1",
        source_priority=("source:vendor-a:v1", "source:sec:v1", "source:vendor-b:v1"),
    )
    available_cell, _, _, available_result = _agreeing_result()
    conflict_cell = _cell("revenue")
    conflict_left = _assertion(
        conflict_cell,
        digest=SHA_A,
        source_id="source:sec:v1",
        origin_group_id="origin:sec:v1",
        value=Decimal("10"),
    )
    conflict_right = _assertion(
        conflict_cell,
        digest=SHA_B,
        source_id="source:vendor-a:v1",
        origin_group_id="origin:vendor-a:v1",
        value=Decimal("20"),
    )
    conflict_result = reconcile_source_assertions(
        cell=conflict_cell,
        assertions=(conflict_left, conflict_right),
        policy=policy,
        cutoff=CUTOFF,
    )
    failed_cell = _cell("employee_count")
    unplanned_cell = _cell("operating_profit")

    cells = (
        DataHubQualityCell(
            cell=available_cell,
            reconciliation_policy_id=policy.policy_id,
            planned=True,
            terminal_state=ObligationTerminalState.SUCCESS,
            reconciliation=available_result,
            freshness=AssessmentFreshness.FRESH,
            lineage_complete=True,
            attempt_count=1,
            reason_codes=("quality.available",),
        ),
        DataHubQualityCell(
            cell=conflict_cell,
            reconciliation_policy_id=policy.policy_id,
            planned=True,
            terminal_state=ObligationTerminalState.SUCCESS,
            reconciliation=conflict_result,
            lineage_complete=True,
            attempt_count=2,
            retry_count=1,
            reason_codes=("quality.conflicted",),
        ),
        DataHubQualityCell(
            cell=failed_cell,
            reconciliation_policy_id=secondary_policy.policy_id,
            planned=True,
            terminal_state=ObligationTerminalState.FAILED,
            lineage_complete=False,
            attempt_count=2,
            retry_count=1,
            reason_codes=("quality.fetch_failed",),
        ),
        DataHubQualityCell(
            cell=unplanned_cell,
            reconciliation_policy_id=secondary_policy.policy_id,
            planned=False,
            lineage_complete=False,
            reason_codes=("quality.no_source_plan",),
        ),
    )

    report = VersionedDataHubQualityReport(
        report_schema_version="datahub-quality-report:v1",
        denominator=DataHubQualityDenominator(
            service_demand_id=f"datahub-service-demand:{SHA_D}",
            requested_cell_ids=tuple(cell.cell.cell_id for cell in cells),
        ),
        reconciliation_policies=(secondary_policy, policy),
        cutoff=CUTOFF,
        generated_at=datetime(2025, 3, 1, 1, tzinfo=UTC),
        cells=tuple(reversed(cells)),
    )

    assert report.summary is not None
    assert report.summary.requested_count == 4
    assert report.summary.planned_coverage == Decimal("0.75")
    assert report.summary.terminal_coverage == Decimal("0.75")
    assert report.summary.availability == Decimal("0.25")
    assert report.summary.freshness == Decimal("0.25")
    assert report.summary.independent_reconciliation == Decimal("0.25")
    assert report.summary.conflicted_count == 1
    assert report.summary.denominator_mean_confidence_score == Decimal("0.1875")
    assert tuple(item.policy_id for item in report.reconciliation_policies) == tuple(
        sorted((policy.policy_id, secondary_policy.policy_id))
    )
    assert tuple(cell.cell.cell_id for cell in report.cells) == tuple(sorted(cell.cell.cell_id for cell in cells))


def test_report_rejects_a_shrunken_or_forged_summary() -> None:
    policy = _policy()
    cell, _, _, result = _agreeing_result()
    quality_cell = DataHubQualityCell(
        cell=cell,
        reconciliation_policy_id=policy.policy_id,
        planned=True,
        terminal_state=ObligationTerminalState.SUCCESS,
        reconciliation=result,
        freshness=AssessmentFreshness.FRESH,
        lineage_complete=True,
        attempt_count=1,
        reason_codes=("quality.available",),
    )
    forged = DataHubQualitySummary(
        requested_count=1,
        planned_count=0,
        terminal_count=0,
        available_count=0,
        fresh_count=0,
        independently_reconciled_count=0,
        conflicted_count=0,
        complete_lineage_count=0,
        planned_coverage=Decimal("0"),
        terminal_coverage=Decimal("0"),
        availability=Decimal("0"),
        freshness=Decimal("0"),
        independent_reconciliation=Decimal("0"),
        lineage_completeness=Decimal("0"),
        denominator_mean_confidence_score=Decimal("0"),
        origin_composition=(),
    )

    with pytest.raises(ValidationError, match="summary does not match"):
        VersionedDataHubQualityReport(
            report_schema_version="datahub-quality-report:v1",
            denominator=DataHubQualityDenominator(
                service_demand_id=f"datahub-service-demand:{SHA_D}",
                requested_cell_ids=(quality_cell.cell.cell_id,),
            ),
            reconciliation_policies=(policy,),
            cutoff=CUTOFF,
            generated_at=datetime(2025, 3, 1, 1, tzinfo=UTC),
            cells=(quality_cell,),
            summary=forged,
        )

    with pytest.raises(ValidationError, match="exactly one row"):
        VersionedDataHubQualityReport(
            report_schema_version="datahub-quality-report:v1",
            denominator=DataHubQualityDenominator(
                service_demand_id=f"datahub-service-demand:{SHA_D}",
                requested_cell_ids=(quality_cell.cell.cell_id,),
            ),
            reconciliation_policies=(policy,),
            cutoff=CUTOFF,
            generated_at=datetime(2025, 3, 1, 1, tzinfo=UTC),
            cells=(quality_cell, quality_cell),
        )

    omitted_cell = _cell("revenue")
    with pytest.raises(ValidationError, match="exactly match the demand denominator"):
        VersionedDataHubQualityReport(
            report_schema_version="datahub-quality-report:v1",
            denominator=DataHubQualityDenominator(
                service_demand_id=f"datahub-service-demand:{SHA_D}",
                requested_cell_ids=(quality_cell.cell.cell_id, omitted_cell.cell_id),
            ),
            reconciliation_policies=(policy,),
            cutoff=CUTOFF,
            generated_at=datetime(2025, 3, 1, 1, tzinfo=UTC),
            cells=(quality_cell,),
        )

    threshold_payload = result.model_dump()
    threshold_payload.update(
        {
            "result_id": "",
            "content_sha256": "",
            "minimum_independent_origin_groups": 1,
        }
    )
    mismatched_result = type(result).model_validate(threshold_payload)
    mismatched_cell = quality_cell.model_copy(
        update={
            "quality_cell_id": "",
            "content_sha256": "",
            "reconciliation": mismatched_result,
        }
    )
    mismatched_cell = DataHubQualityCell.model_validate(mismatched_cell.model_dump())
    with pytest.raises(ValidationError, match="threshold must match"):
        VersionedDataHubQualityReport(
            report_schema_version="datahub-quality-report:v1",
            denominator=DataHubQualityDenominator(
                service_demand_id=f"datahub-service-demand:{SHA_D}",
                requested_cell_ids=(quality_cell.cell.cell_id,),
            ),
            reconciliation_policies=(policy,),
            cutoff=CUTOFF,
            generated_at=datetime(2025, 3, 1, 1, tzinfo=UTC),
            cells=(mismatched_cell,),
        )


def test_quality_cell_rejects_inconsistent_lineage_and_terminal_counters() -> None:
    cell, _, _, result = _agreeing_result()
    with pytest.raises(ValidationError, match="lineage completeness"):
        DataHubQualityCell(
            cell=cell,
            reconciliation_policy_id=result.policy_id,
            planned=True,
            terminal_state=ObligationTerminalState.SUCCESS,
            reconciliation=result,
            freshness=AssessmentFreshness.FRESH,
            lineage_complete=False,
            attempt_count=1,
            reason_codes=("quality.lineage_mismatch",),
        )

    with pytest.raises(ValidationError, match="unchanged response"):
        DataHubQualityCell(
            cell=cell,
            reconciliation_policy_id=result.policy_id,
            planned=True,
            terminal_state=ObligationTerminalState.UNCHANGED,
            reconciliation=result,
            freshness=AssessmentFreshness.FRESH,
            lineage_complete=True,
            attempt_count=1,
            unchanged_response_count=0,
            reason_codes=("quality.unchanged",),
        )

    with pytest.raises(ValidationError, match="unplanned requested cell"):
        DataHubQualityCell(
            cell=cell,
            reconciliation_policy_id=result.policy_id,
            planned=False,
            lineage_complete=True,
            reason_codes=("quality.no_source_plan",),
        )


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
            "selected_confidence_score": None,
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
    with pytest.raises(ValidationError, match="non-finite Decimal"):
        _assertion(
            cell,
            digest=SHA_A,
            source_id="source:sec:v1",
            origin_group_id="origin:sec:v1",
            value=Decimal("100"),
            confidence_score=value,
        )


def test_confidence_uses_the_repository_normalized_scale() -> None:
    cell = _cell()
    with pytest.raises(ValidationError, match="less than or equal to 1"):
        _assertion(
            cell,
            digest=SHA_A,
            source_id="source:sec:v1",
            origin_group_id="origin:sec:v1",
            value=Decimal("100"),
            confidence_score=Decimal("75"),
        )
