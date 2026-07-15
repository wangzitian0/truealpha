from __future__ import annotations

import json
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
)

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
AT = datetime(2025, 1, 3, 22, tzinfo=UTC)


def _universe(name: str, digest: str) -> UniverseRef:
    return UniverseRef(universe_id=name, universe_version="v1", content_sha256=digest)


def _schedule() -> CaptureSchedulePolicy:
    return CaptureSchedulePolicy(
        policy_version="market-daily:v1",
        demanded_cadence=timedelta(days=1),
        provider_availability_cadence="trading-session-close:v1",
        freshness_max_age=timedelta(days=2),
        retry=RetryPolicy(
            max_attempts=2,
            retryable_outcomes=(
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


def _graph(
    campaign_id: str,
    run_id: str,
    obligation_ids: tuple[str, ...],
    work_item_id: str,
    attempt_ids: tuple[str, ...],
    raw_object_id: str,
    observations: tuple[NormalizedObservation, ...],
    assessments: tuple[ConfidenceAssessment, ...],
) -> ProvenanceGraph:
    nodes = tuple(
        [
            ProvenanceNode(node_id=campaign_id, kind=ProvenanceNodeKind.CAMPAIGN),
            ProvenanceNode(node_id=run_id, kind=ProvenanceNodeKind.RUN),
        ]
        + [ProvenanceNode(node_id=value, kind=ProvenanceNodeKind.LIST_OBLIGATION) for value in obligation_ids]
        + [
            ProvenanceNode(node_id=work_item_id, kind=ProvenanceNodeKind.WORK_ITEM),
            ProvenanceNode(node_id=raw_object_id, kind=ProvenanceNodeKind.RAW_OBJECT),
        ]
        + [ProvenanceNode(node_id=value, kind=ProvenanceNodeKind.FETCH_ATTEMPT) for value in attempt_ids]
        + [
            ProvenanceNode(node_id=value.observation_id, kind=ProvenanceNodeKind.NORMALIZED_OBSERVATION)
            for value in observations
        ]
        + [
            ProvenanceNode(node_id=value.assessment_id, kind=ProvenanceNodeKind.CONFIDENCE_ASSESSMENT)
            for value in assessments
        ]
    )
    edges = tuple(
        [
            ProvenanceEdge(
                from_node_id=campaign_id,
                edge_type=ProvenanceEdgeKind.CONTAINS,
                to_node_id=run_id,
                edge_ordinal=0,
            )
        ]
        + [
            ProvenanceEdge(
                from_node_id=run_id,
                edge_type=ProvenanceEdgeKind.REQUIRES,
                to_node_id=obligation_id,
                edge_ordinal=index,
            )
            for index, obligation_id in enumerate(obligation_ids)
        ]
        + [
            ProvenanceEdge(
                from_node_id=obligation_id,
                edge_type=ProvenanceEdgeKind.SATISFIED_BY,
                to_node_id=work_item_id,
                edge_ordinal=0,
            )
            for obligation_id in obligation_ids
        ]
        + [
            ProvenanceEdge(
                from_node_id=work_item_id,
                edge_type=ProvenanceEdgeKind.ATTEMPTED_BY,
                to_node_id=attempt_id,
                edge_ordinal=index,
            )
            for index, attempt_id in enumerate(attempt_ids)
        ]
        + [
            ProvenanceEdge(
                from_node_id=attempt_ids[-1],
                edge_type=ProvenanceEdgeKind.OBSERVED,
                to_node_id=raw_object_id,
                edge_ordinal=0,
            )
        ]
        + [
            ProvenanceEdge(
                from_node_id=raw_object_id,
                edge_type=ProvenanceEdgeKind.NORMALIZED_AS,
                to_node_id=value.observation_id,
                edge_ordinal=index,
            )
            for index, value in enumerate(observations)
        ]
        + [
            ProvenanceEdge(
                from_node_id=value.observation_id or value.obligation_id or "",
                edge_type=ProvenanceEdgeKind.ASSESSED_BY,
                to_node_id=value.assessment_id,
                edge_ordinal=index,
            )
            for index, value in enumerate(assessments)
        ]
    )
    return ProvenanceGraph(schema_version="provenance-schema:v1", nodes=nodes, edges=edges)


def _bundle(
    *,
    attempt_specs: tuple[tuple[int, FetchAttemptOutcome], ...] = ((1, FetchAttemptOutcome.SUCCESS),),
    observations: tuple[NormalizedObservation, ...] = (),
    assessments: tuple[ConfidenceAssessment, ...] = (),
) -> tuple[DataHubInterfaceBundle, dict[str, Any]]:
    schedule = _schedule()
    alpha = _universe("tiny-alpha", SHA_A)
    overlap = _universe("tiny-overlap", SHA_B)
    campaign = CaptureCampaign(
        campaign_policy_id="campaign-policy:tiny:v1",
        environment=CaptureEnvironment.GITHUB_CI,
        cutoff=AT,
        universe_refs=(overlap, alpha),
    )
    run = CaptureRun(
        campaign_id=campaign.campaign_id,
        run_sequence=1,
        schedule_policy_id=schedule.schedule_policy_id,
        capture_scope_id=f"capture-scope:{SHA_C}",
    )
    subject = SubjectRef(kind=SubjectKind.LISTING, id="instrument:BBB")
    obligations = tuple(
        ListObligation(
            run_id=run.run_id,
            universe_ref=universe,
            subject=subject,
            capture_requirement_id="daily-bar:v1",
            partition="2025-01-03",
        )
        for universe in (alpha, overlap)
    )
    work_item = CaptureWorkItem(
        run_id=run.run_id,
        source_policy_id="source-policy:public-bars:v1",
        canonical_request_sha256=canonical_sha256("GET /bars/BBB?date=2025-01-03"),
        partition="2025-01-03",
    )
    bindings = tuple(
        ObligationWorkBinding(obligation_id=item.obligation_id, work_item_id=work_item.work_item_id)
        for item in obligations
    )
    raw_object = RawObjectIdentity(payload_sha256=SHA_C)
    attempts = tuple(
        FetchAttempt(
            work_item_id=work_item.work_item_id,
            attempt_number=number,
            observed_at=AT + timedelta(seconds=number),
            outcome=outcome,
            raw_object_id=(
                raw_object.raw_object_id
                if outcome in {FetchAttemptOutcome.SUCCESS, FetchAttemptOutcome.UNCHANGED}
                else None
            ),
            reason_codes=(outcome.value,),
        )
        for number, outcome in attempt_specs
    )
    terminal_attempt = attempts[-1]
    terminal_state = (
        ObligationTerminalState(terminal_attempt.outcome.value)
        if terminal_attempt.outcome in schedule.retry.terminal_outcomes
        else ObligationTerminalState.FAILED
    )
    results = tuple(
        ListObligationResult(
            obligation_id=item.obligation_id,
            terminal_state=terminal_state,
            completed_at=terminal_attempt.observed_at,
            final_attempt_id=terminal_attempt.attempt_id,
            reason_codes=(terminal_state.value,),
        )
        for item in obligations
    )
    bundle = DataHubInterfaceBundle(
        schedule_policies=(schedule,),
        campaigns=(campaign,),
        runs=(run,),
        obligations=obligations,
        work_items=(work_item,),
        bindings=bindings,
        attempts=attempts,
        raw_objects=(raw_object,),
        results=results,
        observations=observations,
        assessments=assessments,
        provenance=_graph(
            campaign.campaign_id,
            run.run_id,
            tuple(item.obligation_id for item in obligations),
            work_item.work_item_id,
            tuple(item.attempt_id for item in attempts),
            raw_object.raw_object_id,
            observations,
            assessments,
        ),
    )
    return bundle, {
        "schedule": schedule,
        "campaign": campaign,
        "run": run,
        "obligations": obligations,
        "work_item": work_item,
        "bindings": bindings,
        "attempts": attempts,
        "raw_object": raw_object,
        "results": results,
    }


def _observation(
    *,
    knowable_at: datetime = AT,
    payload_identity: str = "raw-object:primary:close:v1",
    supersedes: NormalizedObservation | None = None,
) -> NormalizedObservation:
    return NormalizedObservation(
        semantic_type="daily-close",
        semantic_version="v1",
        subject=SubjectRef(kind=SubjectKind.LISTING, id="instrument:BBB"),
        valid_from=datetime(2025, 1, 3, 21, tzinfo=UTC),
        knowable_at=knowable_at,
        source_vintage=f"primary:{knowable_at.date().isoformat()}:v1",
        parser_version="bars-parser:v1",
        mapping_version="instrument-map:v1",
        payload_identity=payload_identity,
        is_restatement=supersedes is not None,
        supersedes_observation_id=None if supersedes is None else supersedes.observation_id,
    )


def _confidence_components() -> tuple[ConfidenceComponent, ...]:
    return tuple(
        ConfidenceComponent(
            kind=kind,
            score="0.80",
            evidence_ids=(f"evidence:{kind.value}:v1",),
            reason_codes=("measured",),
        )
        for kind in ConfidenceComponentKind
    )


def test_experimental_module_is_iterable_without_changing_frozen_root_exports() -> None:
    expected = {
        "CaptureCampaign",
        "CaptureRun",
        "ListObligation",
        "CaptureWorkItem",
        "FetchAttempt",
        "RawObjectIdentity",
        "NormalizedObservation",
        "ConfidenceAssessment",
        "ProvenanceEdge",
        "RecapturePlan",
    }
    assert expected.isdisjoint(truealpha_contracts.__all__)
    module = __import__("truealpha_contracts.datahub", fromlist=sorted(expected))
    assert all(hasattr(module, name) for name in expected)


def test_identity_is_deterministic_but_content_conflicts_remain_visible() -> None:
    bundle, values = _bundle()
    reordered = CaptureCampaign(
        campaign_policy_id=values["campaign"].campaign_policy_id,
        environment=values["campaign"].environment,
        cutoff=values["campaign"].cutoff,
        universe_refs=tuple(reversed(values["campaign"].universe_refs)),
    )
    assert reordered.campaign_id == values["campaign"].campaign_id
    assert reordered.content_sha256 == values["campaign"].content_sha256

    original = values["attempts"][0]
    changed = FetchAttempt(
        work_item_id=original.work_item_id,
        attempt_number=original.attempt_number,
        observed_at=original.observed_at + timedelta(seconds=1),
        outcome=FetchAttemptOutcome.SUCCESS,
        raw_object_id=original.raw_object_id,
        reason_codes=("later-content",),
    )
    assert changed.attempt_id == original.attempt_id
    assert changed.content_sha256 != original.content_sha256
    with pytest.raises(ValidationError, match="conflicting append-only content"):
        DataHubInterfaceBundle(
            **{
                **bundle.model_dump(mode="python"),
                "attempts": (original, changed),
            }
        )


def test_overlapping_lists_share_work_without_losing_obligations_or_attempts() -> None:
    bundle, values = _bundle()
    assert len(bundle.obligations) == 2
    assert len(bundle.work_items) == 1
    assert {binding.work_item_id for binding in bundle.bindings} == {values["work_item"].work_item_id}
    assert len({binding.obligation_id for binding in bundle.bindings}) == 2

    refresh = FetchAttempt(
        work_item_id=values["work_item"].work_item_id,
        attempt_number=2,
        observed_at=AT + timedelta(days=1),
        outcome=FetchAttemptOutcome.UNCHANGED,
        raw_object_id=values["raw_object"].raw_object_id,
        reason_codes=("conditional-unchanged",),
    )
    assert refresh.attempt_id != values["attempts"][0].attempt_id
    assert refresh.raw_object_id == values["attempts"][0].raw_object_id


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


def test_mutable_coordinates_and_identity_drift_are_rejected() -> None:
    with pytest.raises(ValidationError, match="immutable version"):
        CaptureSchedulePolicy(
            policy_version="current",
            demanded_cadence=timedelta(days=1),
            provider_availability_cadence="trading-session-close:v1",
            freshness_max_age=timedelta(days=2),
            retry=_schedule().retry,
        )
    with pytest.raises(ValidationError, match="immutable version"):
        RecapturePredicate(source_policy_ids=("source-policy:latest",))
    observation = _observation()
    with pytest.raises(ValidationError, match="immutable version"):
        ConfidenceAssessment(
            observation_id=observation.observation_id,
            assessment_policy_id="confidence-policy:tiny:v1",
            evidence_set_id="evidence-set:current",
            confidence=None,
            availability=AssessmentAvailability.AVAILABLE,
            freshness=AssessmentFreshness.FRESH,
            applicability=AssessmentApplicability.APPLICABLE,
            quality=AssessmentQuality.NOT_ASSESSED,
            reason_codes=("pending-evidence",),
            evaluation_cutoff=AT,
            assessed_at=AT,
        )

    original = CaptureWorkItem(
        run_id=f"capture-run:{SHA_A}",
        source_policy_id="source-policy:public-bars:v1",
        canonical_request_sha256=SHA_B,
        partition="2025-01-03",
    )
    with pytest.raises(ValidationError, match="identity grain"):
        CaptureWorkItem(
            work_item_id=original.work_item_id,
            run_id=original.run_id,
            source_policy_id=original.source_policy_id,
            canonical_request_sha256=SHA_C,
            partition=original.partition,
        )


def test_binary_float_and_unexplained_scalar_confidence_are_rejected() -> None:
    with pytest.raises(ValidationError, match="binary float"):
        ConfidenceComponent(
            kind=ConfidenceComponentKind.SOURCE_AUTHORITY,
            score=0.8,
            evidence_ids=("evidence:source:v1",),
            reason_codes=("measured",),
        )
    observation = _observation()
    with pytest.raises(ValidationError, match="every explainable component"):
        ConfidenceAssessment(
            observation_id=observation.observation_id,
            assessment_policy_id="confidence-policy:tiny:v1",
            evidence_set_id="evidence-set:tiny:v1",
            components=(),
            confidence="0.80",
            availability=AssessmentAvailability.AVAILABLE,
            freshness=AssessmentFreshness.FRESH,
            applicability=AssessmentApplicability.APPLICABLE,
            quality=AssessmentQuality.VALID,
            reason_codes=("unexplained",),
            evaluation_cutoff=AT,
            assessed_at=AT,
        )


def test_confidence_dimensions_remain_independent_and_reassessment_appends() -> None:
    observation = _observation()
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
    assert first.confidence == Decimal("0.80")


def test_restatement_is_append_only_and_future_knowledge_is_excluded() -> None:
    original = _observation()
    restatement = _observation(
        knowable_at=AT + timedelta(days=4),
        payload_identity="raw-object:restatement:close:v2",
        supersedes=original,
    )
    assert restatement.observation_id != original.observation_id
    assert restatement.supersedes_observation_id == original.observation_id

    future_exclusion = ConfidenceAssessment(
        observation_id=restatement.observation_id,
        assessment_policy_id="confidence-policy:tiny:v1",
        evidence_set_id="evidence-set:cutoff:v1",
        components=(
            ConfidenceComponent(
                kind=ConfidenceComponentKind.PIT_KNOWABILITY,
                score="0",
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
        source_policy_ids=(values["work_item"].source_policy_id,),
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
        selected_obligation_ids=tuple(item.obligation_id for item in bundle.obligations),
        planner_version="recapture-planner:v1",
    )
    assert plan.authorize_execution(tuple(reversed(plan.selected_obligation_ids))) == plan.selected_obligation_ids
    with pytest.raises(ValueError, match="differs from its frozen dry-run selection"):
        plan.authorize_execution((plan.selected_obligation_ids[0],))


def test_provenance_supports_forward_and_reverse_indexed_paths() -> None:
    bundle, values = _bundle()
    first_obligation_id = values["obligations"][0].obligation_id
    assert values["raw_object"].raw_object_id in bundle.provenance.forward_node_ids(first_obligation_id)
    reverse = bundle.provenance.reverse_node_ids(values["raw_object"].raw_object_id)
    assert values["attempts"][0].attempt_id in reverse
    assert values["work_item"].work_item_id in reverse
    assert {item.obligation_id for item in values["obligations"]} <= set(reverse)

    missing = ProvenanceNode(node_id="raw-object:missing", kind=ProvenanceNodeKind.RAW_OBJECT)
    with pytest.raises(ValidationError, match="bundled nodes"):
        ProvenanceGraph(
            schema_version="provenance-schema:v1",
            nodes=(missing,),
            edges=(
                ProvenanceEdge(
                    from_node_id=missing.node_id,
                    edge_type=ProvenanceEdgeKind.NORMALIZED_AS,
                    to_node_id="normalized-observation:missing",
                    edge_ordinal=0,
                ),
            ),
        )


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


def test_frozen_corpus_declares_every_required_negative_control() -> None:
    fixture = Path(__file__).parent / "fixtures" / "datahub_interface.v1.json"
    corpus = json.loads(fixture.read_text())
    assert corpus["batch_id"] == "D4-datahub-interface"
    assert {case["case_id"] for case in corpus["negative_cases"]} == {
        "mutable-universe-alias",
        "duplicate-obligation",
        "work-request-drift",
        "attempt-gap",
        "retry-after-terminal",
        "retry-over-budget",
        "raw-dedup-erases-attempt",
        "binary-float-confidence",
        "unexplained-confidence",
        "collapsed-readiness",
        "future-knowledge",
        "recapture-overreach",
        "factor-provenance-branch",
    }
