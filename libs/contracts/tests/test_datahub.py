from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import truealpha_contracts
from pydantic import ValidationError
from truealpha_contracts import (
    CaptureEnvironment,
    ProvenanceNeutralInput,
    SubjectKind,
    SubjectRef,
    UniverseRef,
    canonical_sha256,
)
from truealpha_contracts.datahub import (
    AssessmentApplicability,
    AssessmentAvailability,
    AssessmentFreshness,
    AssessmentQuality,
    CaptureCampaign,
    CaptureRun,
    CaptureSchedulePolicy,
    CaptureWorkItem,
    ConfidenceAssessment,
    ConfidenceComponent,
    ConfidenceComponentKind,
    DataHubInterfaceBundle,
    FetchAttempt,
    FetchAttemptOutcome,
    FetchAttemptResult,
    ListObligation,
    ListObligationResult,
    NormalizedObservation,
    ObligationTerminalState,
    ObligationWorkBinding,
    ProvenanceEdge,
    ProvenanceEdgeKind,
    ProvenanceGraph,
    ProvenanceNode,
    ProvenanceNodeKind,
    RawObjectIdentity,
    RecapturePlan,
    RecapturePredicate,
    RetryPolicy,
    SourceRequest,
    SourceVintage,
)

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
AT = datetime(2025, 1, 3, 22, tzinfo=UTC)


def _universe(name: str, digest: str) -> UniverseRef:
    return UniverseRef(universe_id=name, universe_version="v1", content_sha256=digest)


def _subject(identifier: str = "instrument:BBB") -> SubjectRef:
    return SubjectRef(kind=SubjectKind.LISTING, id=identifier)


def _schedule() -> CaptureSchedulePolicy:
    return CaptureSchedulePolicy(
        policy_version="market-daily:v1",
        demanded_cadence=timedelta(days=1),
        provider_availability_cadence="trading-session-close:v1",
        freshness_max_age=timedelta(days=2),
        retry=RetryPolicy(
            max_attempts=2,
            retryable_outcomes=(
                FetchAttemptOutcome.INTERRUPTED,
                FetchAttemptOutcome.RATE_LIMITED,
                FetchAttemptOutcome.SERVER_ERROR,
                FetchAttemptOutcome.TRANSPORT_ERROR,
            ),
            terminal_outcomes=(
                FetchAttemptOutcome.SUCCESS,
                FetchAttemptOutcome.UNCHANGED,
                FetchAttemptOutcome.UNAVAILABLE,
                FetchAttemptOutcome.FAILED,
            ),
        ),
    )


def _source_request(
    *,
    source_policy_id: str = "source-policy:public-bars:v1",
    subject_refs: tuple[SubjectRef, ...] = (_subject(),),
    capture_requirement_ids: tuple[str, ...] = ("daily-bar:v1",),
    partition: str = "2025-01-03",
) -> SourceRequest:
    return SourceRequest(
        source_registry_entry_id=f"source-registry-entry:{SHA_A}",
        source_policy_id=source_policy_id,
        request_fingerprint_version="request-fingerprint:v1",
        canonical_request_sha256=canonical_sha256("GET /bars/BBB?date=2025-01-03"),
        subject_refs=subject_refs,
        capture_requirement_ids=capture_requirement_ids,
        partition=partition,
    )


def _graph(
    *,
    campaign: CaptureCampaign,
    runs: tuple[CaptureRun, ...],
    obligations: tuple[ListObligation, ...],
    source_request: SourceRequest,
    work_item: CaptureWorkItem,
    attempts: tuple[FetchAttempt, ...],
    attempt_results: tuple[FetchAttemptResult, ...],
    raw_object: RawObjectIdentity | None,
    source_vintage: SourceVintage | None,
    observations: tuple[NormalizedObservation, ...],
    assessments: tuple[ConfidenceAssessment, ...],
) -> ProvenanceGraph:
    nodes = [ProvenanceNode(node_id=campaign.campaign_id, kind=ProvenanceNodeKind.CAMPAIGN)]
    nodes.extend(ProvenanceNode(node_id=run.run_id, kind=ProvenanceNodeKind.RUN) for run in runs)
    nodes.extend(
        ProvenanceNode(node_id=obligation.obligation_id, kind=ProvenanceNodeKind.LIST_OBLIGATION)
        for obligation in obligations
    )
    nodes.extend(
        (
            ProvenanceNode(node_id=source_request.source_request_id, kind=ProvenanceNodeKind.SOURCE_REQUEST),
            ProvenanceNode(node_id=work_item.work_item_id, kind=ProvenanceNodeKind.WORK_ITEM),
        )
    )
    nodes.extend(
        ProvenanceNode(node_id=attempt.attempt_id, kind=ProvenanceNodeKind.FETCH_ATTEMPT) for attempt in attempts
    )
    nodes.extend(
        ProvenanceNode(node_id=result.attempt_result_id, kind=ProvenanceNodeKind.FETCH_ATTEMPT_RESULT)
        for result in attempt_results
    )
    if raw_object is not None:
        nodes.append(ProvenanceNode(node_id=raw_object.raw_object_id, kind=ProvenanceNodeKind.RAW_OBJECT))
    if source_vintage is not None:
        nodes.append(ProvenanceNode(node_id=source_vintage.source_vintage_id, kind=ProvenanceNodeKind.SOURCE_VINTAGE))
    nodes.extend(
        ProvenanceNode(node_id=observation.observation_id, kind=ProvenanceNodeKind.NORMALIZED_OBSERVATION)
        for observation in observations
    )
    nodes.extend(
        ProvenanceNode(node_id=assessment.assessment_id, kind=ProvenanceNodeKind.CONFIDENCE_ASSESSMENT)
        for assessment in assessments
    )

    edges = [
        ProvenanceEdge(
            from_node_id=campaign.campaign_id,
            edge_type=ProvenanceEdgeKind.CONTAINS,
            to_node_id=source_request.source_request_id,
            edge_ordinal=0,
        ),
        ProvenanceEdge(
            from_node_id=source_request.source_request_id,
            edge_type=ProvenanceEdgeKind.DISPATCHES,
            to_node_id=work_item.work_item_id,
            edge_ordinal=0,
        ),
    ]
    edges.extend(
        ProvenanceEdge(
            from_node_id=campaign.campaign_id,
            edge_type=ProvenanceEdgeKind.CONTAINS,
            to_node_id=run.run_id,
            edge_ordinal=index + 1,
        )
        for index, run in enumerate(runs)
    )
    edges.extend(
        ProvenanceEdge(
            from_node_id=obligation.run_id,
            edge_type=ProvenanceEdgeKind.REQUIRES,
            to_node_id=obligation.obligation_id,
            edge_ordinal=index,
        )
        for index, obligation in enumerate(obligations)
    )
    edges.extend(
        ProvenanceEdge(
            from_node_id=obligation.obligation_id,
            edge_type=ProvenanceEdgeKind.SATISFIED_BY,
            to_node_id=work_item.work_item_id,
            edge_ordinal=0,
        )
        for obligation in obligations
    )
    edges.extend(
        ProvenanceEdge(
            from_node_id=work_item.work_item_id,
            edge_type=ProvenanceEdgeKind.ATTEMPTED_BY,
            to_node_id=attempt.attempt_id,
            edge_ordinal=index,
        )
        for index, attempt in enumerate(attempts)
    )
    by_attempt = {result.attempt_id: result for result in attempt_results}
    for attempt in attempts:
        result = by_attempt[attempt.attempt_id]
        edges.append(
            ProvenanceEdge(
                from_node_id=attempt.attempt_id,
                edge_type=ProvenanceEdgeKind.COMPLETES,
                to_node_id=result.attempt_result_id,
                edge_ordinal=0,
            )
        )
        source_vintage_id = result.source_vintage_id or result.reused_source_vintage_id
        if source_vintage_id is not None:
            edges.append(
                ProvenanceEdge(
                    from_node_id=result.attempt_result_id,
                    edge_type=(
                        ProvenanceEdgeKind.OBSERVED
                        if result.source_vintage_id is not None
                        else ProvenanceEdgeKind.REUSES
                    ),
                    to_node_id=source_vintage_id,
                    edge_ordinal=0,
                )
            )
    if raw_object is not None and source_vintage is not None:
        edges.append(
            ProvenanceEdge(
                from_node_id=source_vintage.source_vintage_id,
                edge_type=ProvenanceEdgeKind.ARCHIVES_AS,
                to_node_id=raw_object.raw_object_id,
                edge_ordinal=0,
            )
        )
    edges.extend(
        ProvenanceEdge(
            from_node_id=observation.source_vintage_id,
            edge_type=ProvenanceEdgeKind.NORMALIZED_AS,
            to_node_id=observation.observation_id,
            edge_ordinal=index,
        )
        for index, observation in enumerate(observations)
    )
    edges.extend(
        ProvenanceEdge(
            from_node_id=assessment.observation_id or assessment.obligation_id or "",
            edge_type=ProvenanceEdgeKind.ASSESSED_BY,
            to_node_id=assessment.assessment_id,
            edge_ordinal=index,
        )
        for index, assessment in enumerate(assessments)
    )
    return ProvenanceGraph(schema_version="provenance-schema:v2", nodes=tuple(nodes), edges=tuple(edges))


def _bundle(
    *,
    attempt_specs: tuple[tuple[int, FetchAttemptOutcome], ...] = ((1, FetchAttemptOutcome.SUCCESS),),
    observations: tuple[NormalizedObservation, ...] = (),
    assessments: tuple[ConfidenceAssessment, ...] = (),
    source_subject_refs: tuple[SubjectRef, ...] = (_subject(),),
    source_requirement_ids: tuple[str, ...] = ("daily-bar:v1",),
    source_partition: str = "2025-01-03",
) -> tuple[DataHubInterfaceBundle, dict[str, Any]]:
    schedule = _schedule()
    alpha = _universe("tiny-alpha", SHA_A)
    overlap = _universe("tiny-overlap", SHA_B)
    campaign = CaptureCampaign(
        campaign_policy_id="campaign-policy:tiny:v1",
        environment=CaptureEnvironment.GITHUB_CI,
        cutoff=AT,
        universe_refs=(alpha, overlap),
    )
    runs = tuple(
        CaptureRun(
            campaign_id=campaign.campaign_id,
            run_sequence=index,
            schedule_policy_id=schedule.schedule_policy_id,
            capture_scope_id=f"capture-scope:{digest}",
        )
        for index, digest in enumerate((SHA_C, SHA_D), start=1)
    )
    subject = _subject()
    obligations = tuple(
        ListObligation(
            run_id=run.run_id,
            universe_ref=universe,
            subject=subject,
            capture_requirement_id="daily-bar:v1",
            partition="2025-01-03",
        )
        for run, universe in zip(runs, (alpha, overlap), strict=True)
    )
    source_request = _source_request(
        subject_refs=source_subject_refs,
        capture_requirement_ids=source_requirement_ids,
        partition=source_partition,
    )
    work_item = CaptureWorkItem(
        campaign_id=campaign.campaign_id,
        source_request_id=source_request.source_request_id,
        schedule_policy_id=schedule.schedule_policy_id,
    )
    bindings = tuple(
        ObligationWorkBinding(obligation_id=obligation.obligation_id, work_item_id=work_item.work_item_id)
        for obligation in obligations
    )
    content_outcome = next(
        (
            outcome
            for _, outcome in attempt_specs
            if outcome in {FetchAttemptOutcome.SUCCESS, FetchAttemptOutcome.UNCHANGED}
        ),
        None,
    )
    raw_object = RawObjectIdentity(payload_sha256=SHA_C) if content_outcome is not None else None
    source_vintage = (
        SourceVintage(
            source_request_id=source_request.source_request_id,
            source_record_id="provider-record:BBB:2025-01-03",
            source_published_at=AT,
            raw_object_id=raw_object.raw_object_id,
        )
        if raw_object is not None
        else None
    )
    attempts = tuple(
        FetchAttempt(
            work_item_id=work_item.work_item_id,
            attempt_number=number,
            started_at=AT + timedelta(seconds=number),
        )
        for number, _ in attempt_specs
    )
    source_vintage_id = None if source_vintage is None else source_vintage.source_vintage_id
    attempt_results = tuple(
        FetchAttemptResult(
            attempt_id=attempt.attempt_id,
            completed_at=attempt.started_at + timedelta(milliseconds=1),
            outcome=outcome,
            source_vintage_id=(source_vintage_id if outcome is FetchAttemptOutcome.SUCCESS else None),
            reused_source_vintage_id=(source_vintage_id if outcome is FetchAttemptOutcome.UNCHANGED else None),
            reason_codes=(outcome.value,),
        )
        for attempt, (_, outcome) in zip(attempts, attempt_specs, strict=True)
    )
    final_attempt = attempts[-1]
    final_outcome = attempt_results[-1].outcome
    final_state = (
        ObligationTerminalState(final_outcome.value)
        if final_outcome in schedule.retry.terminal_outcomes
        else ObligationTerminalState.FAILED
    )
    results = tuple(
        ListObligationResult(
            obligation_id=obligation.obligation_id,
            terminal_state=final_state,
            completed_at=attempt_results[-1].completed_at,
            final_attempt_id=final_attempt.attempt_id,
            reason_codes=(final_state.value,),
        )
        for obligation in obligations
    )
    bundle = DataHubInterfaceBundle(
        schedule_policies=(schedule,),
        campaigns=(campaign,),
        runs=runs,
        obligations=obligations,
        source_requests=(source_request,),
        work_items=(work_item,),
        bindings=bindings,
        attempts=attempts,
        attempt_results=attempt_results,
        raw_objects=(() if raw_object is None else (raw_object,)),
        source_vintages=(() if source_vintage is None else (source_vintage,)),
        results=results,
        observations=observations,
        assessments=assessments,
        provenance=_graph(
            campaign=campaign,
            runs=runs,
            obligations=obligations,
            source_request=source_request,
            work_item=work_item,
            attempts=attempts,
            attempt_results=attempt_results,
            raw_object=raw_object,
            source_vintage=source_vintage,
            observations=observations,
            assessments=assessments,
        ),
    )
    return bundle, {
        "schedule": schedule,
        "campaign": campaign,
        "runs": runs,
        "obligations": obligations,
        "source_request": source_request,
        "work_item": work_item,
        "attempts": attempts,
        "attempt_results": attempt_results,
        "raw_object": raw_object,
        "source_vintage": source_vintage,
    }


def _observation(
    source_vintage: SourceVintage, *, supersedes: NormalizedObservation | None = None
) -> NormalizedObservation:
    known_at = AT + (timedelta(days=4) if supersedes is not None else timedelta())
    return NormalizedObservation(
        semantic_type="daily-close",
        semantic_version="v1",
        subject=SubjectRef(kind=SubjectKind.LISTING, id="instrument:BBB"),
        valid_from=datetime(2025, 1, 3, 21, tzinfo=UTC),
        knowable_at=known_at,
        source_vintage_id=source_vintage.source_vintage_id,
        parser_version="bars-parser:v1" if supersedes is None else "bars-parser:v2",
        mapping_version="instrument-map:v1",
        normalized_payload_sha256=SHA_B if supersedes is None else SHA_D,
        is_restatement=supersedes is not None,
        supersedes_observation_id=None if supersedes is None else supersedes.observation_id,
    )


def _confidence_components() -> tuple[ConfidenceComponent, ...]:
    score: Any = "0.80"
    return tuple(
        ConfidenceComponent(
            kind=kind,
            score=score,
            evidence_ids=(f"evidence:{kind.value}:v1",),
            reason_codes=("measured",),
        )
        for kind in ConfidenceComponentKind
    )


def test_experimental_module_is_iterable_without_changing_frozen_root_exports() -> None:
    expected = {
        "CaptureCampaign",
        "CaptureRun",
        "SourceRequest",
        "CaptureWorkItem",
        "FetchAttempt",
        "FetchAttemptResult",
        "SourceVintage",
        "NormalizedObservation",
        "ConfidenceAssessment",
        "ProvenanceEdge",
        "RecapturePlan",
    }
    assert expected.isdisjoint(truealpha_contracts.__all__)
    module = __import__("truealpha_contracts.datahub", fromlist=sorted(expected))
    assert all(hasattr(module, name) for name in expected)


def test_cross_run_overlapping_lists_share_campaign_work_without_losing_obligations() -> None:
    bundle, values = _bundle()
    assert len(bundle.runs) == 2
    assert len(bundle.obligations) == 2
    assert len(bundle.work_items) == 1
    assert {binding.work_item_id for binding in bundle.bindings} == {values["work_item"].work_item_id}
    assert {obligation.run_id for obligation in bundle.obligations} == {run.run_id for run in bundle.runs}
    assert bundle.work_items[0].campaign_id == values["campaign"].campaign_id


@pytest.mark.parametrize(
    ("kwargs", "case"),
    [
        ({"source_subject_refs": (_subject("instrument:CCC"),)}, "subject"),
        ({"source_requirement_ids": ("income-statement:v1",)}, "requirement"),
        ({"source_partition": "2025-01-04"}, "partition"),
    ],
)
def test_binding_requires_typed_source_request_coverage(kwargs: dict[str, Any], case: str) -> None:
    with pytest.raises(ValidationError, match="request that covers its obligation"):
        _bundle(**kwargs)


def test_two_phase_attempt_identity_keeps_content_conflicts_visible() -> None:
    bundle, values = _bundle()
    original = values["attempts"][0]
    changed = FetchAttempt(
        work_item_id=original.work_item_id,
        attempt_number=original.attempt_number,
        started_at=original.started_at + timedelta(seconds=1),
    )
    assert changed.attempt_id == original.attempt_id
    assert changed.content_sha256 != original.content_sha256
    with pytest.raises(ValidationError, match="conflicting append-only content"):
        DataHubInterfaceBundle(**{**bundle.model_dump(mode="python"), "attempts": (original, changed)})


@pytest.mark.parametrize(
    ("attempt_specs", "message"),
    [
        (((2, FetchAttemptOutcome.SUCCESS),), "contiguous"),
        (
            ((1, FetchAttemptOutcome.SUCCESS), (2, FetchAttemptOutcome.FAILED)),
            "follow a terminal",
        ),
        (((1, FetchAttemptOutcome.RATE_LIMITED),), "explicit terminal"),
    ],
)
def test_attempt_sequences_fail_closed(
    attempt_specs: tuple[tuple[int, FetchAttemptOutcome], ...],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        _bundle(attempt_specs=attempt_specs)


def test_closed_bundle_requires_at_least_one_attempt_outcome() -> None:
    bundle, _ = _bundle()
    with pytest.raises(ValidationError, match="at least 1 item"):
        DataHubInterfaceBundle(**{**bundle.model_dump(mode="python"), "attempt_results": ()})


def test_closed_bundle_rejects_a_dispatched_attempt_without_an_outcome() -> None:
    bundle, values = _bundle(attempt_specs=((1, FetchAttemptOutcome.RATE_LIMITED), (2, FetchAttemptOutcome.SUCCESS)))
    with pytest.raises(ValidationError, match="outcome for every dispatched"):
        DataHubInterfaceBundle(
            **{
                **bundle.model_dump(mode="python"),
                "attempt_results": values["attempt_results"][:-1],
            }
        )


def test_unchanged_response_can_only_reuse_the_same_pinned_source_request() -> None:
    bundle, values = _bundle(attempt_specs=((1, FetchAttemptOutcome.UNCHANGED),))
    other_request = _source_request(source_policy_id="source-policy:independent-bars:v1")
    other_vintage = SourceVintage(
        source_request_id=other_request.source_request_id,
        source_record_id="provider-record:BBB:2025-01-03",
        source_published_at=AT,
        raw_object_id=values["raw_object"].raw_object_id,
    )
    original_result = values["attempt_results"][0]
    wrong_reuse = FetchAttemptResult(
        attempt_id=original_result.attempt_id,
        completed_at=original_result.completed_at,
        outcome=FetchAttemptOutcome.UNCHANGED,
        reused_source_vintage_id=other_vintage.source_vintage_id,
        reason_codes=("conditional-unchanged",),
    )
    with pytest.raises(ValidationError, match="another request"):
        DataHubInterfaceBundle(
            **{
                **bundle.model_dump(mode="python"),
                "source_requests": (values["source_request"], other_request),
                "source_vintages": (values["source_vintage"], other_vintage),
                "attempt_results": (wrong_reuse,),
            }
        )


def test_mutable_coordinates_and_request_identity_drift_are_rejected() -> None:
    with pytest.raises(ValidationError, match="immutable version"):
        SourceRequest(
            source_registry_entry_id=f"source-registry-entry:{SHA_A}",
            source_policy_id="source-policy:latest",
            request_fingerprint_version="request-fingerprint:v1",
            canonical_request_sha256=SHA_B,
            subject_refs=(_subject(),),
            capture_requirement_ids=("daily-bar:v1",),
            partition="2025-01-03",
        )
    original = _source_request()
    with pytest.raises(ValidationError, match="identity grain"):
        SourceRequest(
            source_request_id=original.source_request_id,
            source_registry_entry_id=original.source_registry_entry_id,
            source_policy_id=original.source_policy_id,
            request_fingerprint_version=original.request_fingerprint_version,
            canonical_request_sha256=SHA_C,
            subject_refs=original.subject_refs,
            capture_requirement_ids=original.capture_requirement_ids,
            partition=original.partition,
        )
    with pytest.raises(ValidationError, match="immutable version"):
        RecapturePredicate(source_policy_ids=("source-policy:current",))


def test_binary_float_and_unexplained_scalar_confidence_are_rejected() -> None:
    binary_float: Any = 0.8
    unexplained_confidence: Any = "0.80"
    with pytest.raises(ValidationError, match="binary float"):
        ConfidenceComponent(
            kind=ConfidenceComponentKind.SOURCE_AUTHORITY,
            score=binary_float,
            evidence_ids=("evidence:source:v1",),
            reason_codes=("measured",),
        )
    _, values = _bundle()
    observation = _observation(values["source_vintage"])
    with pytest.raises(ValidationError, match="every explainable component"):
        ConfidenceAssessment(
            observation_id=observation.observation_id,
            assessment_policy_id="confidence-policy:tiny:v1",
            evidence_set_id="evidence-set:tiny:v1",
            confidence=unexplained_confidence,
            availability=AssessmentAvailability.AVAILABLE,
            freshness=AssessmentFreshness.FRESH,
            applicability=AssessmentApplicability.APPLICABLE,
            quality=AssessmentQuality.VALID,
            reason_codes=("unexplained",),
            evaluation_cutoff=AT,
            assessed_at=AT,
        )


def test_confidence_dimensions_remain_independent_and_reassessment_appends() -> None:
    _, values = _bundle()
    observation = _observation(values["source_vintage"])
    first = ConfidenceAssessment(
        observation_id=observation.observation_id,
        assessment_policy_id="confidence-policy:tiny:v1",
        evidence_set_id="evidence-set:primary:v1",
        components=_confidence_components(),
        confidence=Decimal("0.80"),
        availability=AssessmentAvailability.AVAILABLE,
        freshness=AssessmentFreshness.STALE,
        applicability=AssessmentApplicability.APPLICABLE,
        quality=AssessmentQuality.CONFLICTED,
        reason_codes=("freshness-exceeded", "source-disagreement"),
        evaluation_cutoff=AT,
        assessed_at=AT,
    )
    reassessed = ConfidenceAssessment(
        **{
            **first.model_dump(mode="python", exclude={"assessment_id", "content_sha256"}),
            "assessment_policy_id": "confidence-policy:tiny:v2",
        }
    )
    assert first.assessment_id != reassessed.assessment_id
    assert first.freshness is AssessmentFreshness.STALE
    assert first.availability is AssessmentAvailability.AVAILABLE
    assert first.quality is AssessmentQuality.CONFLICTED


def test_unavailable_invalid_and_future_assessments_cannot_carry_scalar_confidence() -> None:
    _, values = _bundle()
    observation = _observation(values["source_vintage"])
    components = _confidence_components()
    with pytest.raises(ValidationError, match="invalid or unassessed"):
        ConfidenceAssessment(
            observation_id=observation.observation_id,
            assessment_policy_id="confidence-policy:tiny:v1",
            evidence_set_id="evidence-set:invalid:v1",
            components=components,
            confidence=Decimal("0.80"),
            availability=AssessmentAvailability.AVAILABLE,
            freshness=AssessmentFreshness.FRESH,
            applicability=AssessmentApplicability.APPLICABLE,
            quality=AssessmentQuality.INVALID,
            reason_codes=("parser-invalid",),
            evaluation_cutoff=AT,
            assessed_at=AT,
        )
    with pytest.raises(ValidationError, match="explicitly unavailable"):
        ConfidenceAssessment(
            obligation_id=values["obligations"][0].obligation_id,
            assessment_policy_id="confidence-policy:tiny:v1",
            evidence_set_id="evidence-set:absence:v1",
            availability=AssessmentAvailability.UNKNOWN,
            freshness=AssessmentFreshness.UNKNOWN,
            applicability=AssessmentApplicability.APPLICABLE,
            quality=AssessmentQuality.NOT_ASSESSED,
            reason_codes=("missing-observation",),
            evaluation_cutoff=AT,
            assessed_at=AT,
        )


def test_restatement_is_append_only_and_future_knowledge_is_excluded() -> None:
    pit_zero: Any = "0"
    _, values = _bundle()
    original = _observation(values["source_vintage"])
    restatement = _observation(values["source_vintage"], supersedes=original)
    future_exclusion = ConfidenceAssessment(
        observation_id=restatement.observation_id,
        assessment_policy_id="confidence-policy:tiny:v1",
        evidence_set_id="evidence-set:cutoff:v1",
        components=(
            ConfidenceComponent(
                kind=ConfidenceComponentKind.PIT_KNOWABILITY,
                score=pit_zero,
                evidence_ids=("evidence:cutoff:v1",),
                reason_codes=("knowable-after-cutoff",),
            ),
        ),
        confidence=None,
        availability=AssessmentAvailability.AVAILABLE,
        freshness=AssessmentFreshness.FRESH,
        applicability=AssessmentApplicability.NOT_YET_KNOWABLE,
        quality=AssessmentQuality.VALID,
        reason_codes=("knowable-after-cutoff",),
        evaluation_cutoff=AT + timedelta(days=2),
        assessed_at=AT + timedelta(days=4),
    )
    bundle, _ = _bundle(observations=(original, restatement), assessments=(future_exclusion,))
    assert bundle.assessments[0].confidence is None
    invalid = ConfidenceAssessment(
        **{
            **future_exclusion.model_dump(mode="python", exclude={"assessment_id", "content_sha256"}),
            "applicability": AssessmentApplicability.APPLICABLE,
        }
    )
    with pytest.raises(ValidationError, match="future knowledge"):
        _bundle(observations=(original, restatement), assessments=(invalid,))


def test_recapture_is_bounded_by_the_frozen_dry_run_selection() -> None:
    bundle, values = _bundle()
    predicate = RecapturePredicate(
        universe_refs=(values["campaign"].universe_refs[0],),
        source_policy_ids=(values["source_request"].source_policy_id,),
        partitions=("2025-01-03",),
        terminal_states=(ObligationTerminalState.FAILED,),
        freshness_states=(AssessmentFreshness.STALE,),
        parser_versions=("bars-parser:v1",),
        mapping_versions=("instrument-map:v1",),
        assessment_policy_ids=("confidence-policy:tiny:v1",),
    )
    plan = RecapturePlan(
        selection_cutoff=AT,
        predicate=predicate,
        selected_obligation_ids=tuple(obligation.obligation_id for obligation in bundle.obligations),
        planner_version="recapture-planner:v1",
    )
    assert plan.authorize_execution(tuple(reversed(plan.selected_obligation_ids))) == plan.selected_obligation_ids
    with pytest.raises(ValueError, match="differs from its frozen dry-run selection"):
        plan.authorize_execution((plan.selected_obligation_ids[0],))


def test_provenance_supports_forward_and_reverse_source_paths() -> None:
    bundle, values = _bundle()
    first_obligation_id = values["obligations"][0].obligation_id
    assert values["raw_object"].raw_object_id in bundle.provenance.forward_node_ids(first_obligation_id)
    reverse = bundle.provenance.reverse_node_ids(values["raw_object"].raw_object_id)
    assert values["attempts"][0].attempt_id in reverse
    assert values["work_item"].work_item_id in reverse
    assert {obligation.obligation_id for obligation in values["obligations"]} <= set(reverse)


@pytest.mark.parametrize(
    "edge_type",
    (ProvenanceEdgeKind.REQUIRES, ProvenanceEdgeKind.SATISFIED_BY),
)
def test_provenance_rejects_missing_obligation_capture_edges(edge_type: ProvenanceEdgeKind) -> None:
    bundle, _ = _bundle()
    replaced = False
    edges: list[ProvenanceEdge] = []
    for edge in bundle.provenance.edges:
        if not replaced and edge.edge_type is edge_type:
            edges.append(
                ProvenanceEdge(
                    from_node_id=edge.from_node_id,
                    edge_type=ProvenanceEdgeKind.CONTAINS,
                    to_node_id=edge.to_node_id,
                    edge_ordinal=edge.edge_ordinal,
                )
            )
            replaced = True
        else:
            edges.append(edge)
    assert replaced
    provenance = ProvenanceGraph(
        schema_version=bundle.provenance.schema_version,
        nodes=bundle.provenance.nodes,
        edges=tuple(edges),
    )

    with pytest.raises(ValidationError, match=f"missing required {edge_type.value} edge"):
        DataHubInterfaceBundle(**{**bundle.model_dump(mode="python"), "provenance": provenance})


def test_factor_boundary_exposes_no_source_or_provenance_branch_metadata() -> None:
    forbidden = {
        "source",
        "source_policy_id",
        "attempt_id",
        "raw_object_id",
        "provider",
        "provenance_edges",
        "confidence_reason_codes",
    }
    assert forbidden.isdisjoint(ProvenanceNeutralInput.model_fields)


def test_frozen_corpus_declares_every_required_e1_negative_control() -> None:
    fixture = Path(__file__).parent / "fixtures" / "datahub_interface.v1.json"
    corpus = json.loads(fixture.read_text())
    assert corpus["batch_id"] == "D4-datahub-interface"
    assert corpus["schema_version"] == 2
    assert corpus["identity_grains"]["work_item"] == [
        "campaign_ref",
        "source_request_ref",
        "schedule_policy_id",
    ]
    assert corpus["identity_grains"]["fetch_attempt_result"] == ["attempt_ref"]
    assert corpus["identity_grains"]["source_request"] == [
        "source_registry_entry_id",
        "source_policy_id",
        "request_fingerprint_version",
        "canonical_request_fingerprint",
        "subject_refs",
        "capture_requirement_ids",
        "partition",
    ]
    assert corpus["identity_grains"]["source_vintage"] == [
        "source_request_ref",
        "source_record_id",
        "source_published_at",
        "raw_object_ref",
    ]
    source_registry_entry_pattern = re.compile(r"source-registry-entry:[0-9a-f]{64}")
    assert all(
        source_registry_entry_pattern.fullmatch(request["source_registry_entry_id"])
        for request in corpus["source_requests"]
    )
    source_vintage_refs = {vintage["source_vintage_ref"] for vintage in corpus["source_vintages"]}
    assert {observation["source_vintage_ref"] for observation in corpus["observations"]} <= source_vintage_refs
    obligation_refs = {obligation["obligation_ref"] for obligation in corpus["obligations"]}
    selected_obligation_refs = set(corpus["recapture"]["selected_obligation_refs"])
    unaffected_obligation_refs = set(corpus["recapture"]["unaffected_obligation_refs"])
    assert selected_obligation_refs | unaffected_obligation_refs <= obligation_refs
    assert selected_obligation_refs.isdisjoint(unaffected_obligation_refs)
    assert {case["case_id"] for case in corpus["negative_cases"]} == {
        "mutable-universe-alias",
        "duplicate-obligation",
        "work-request-drift",
        "cross-run-work-loss",
        "request-obligation-coverage-mismatch",
        "attempt-gap",
        "retry-after-terminal",
        "retry-over-budget",
        "dispatch-without-outcome",
        "unchanged-cross-request-reuse",
        "raw-dedup-erases-attempt",
        "binary-float-confidence",
        "unexplained-confidence",
        "collapsed-readiness",
        "future-knowledge",
        "missing-obligation-work-edge",
        "missing-run-obligation-edge",
        "recapture-overreach",
        "factor-provenance-branch",
    }
