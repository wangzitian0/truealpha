from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from truealpha_contracts.common import CaptureEnvironment
from truealpha_contracts.datahub import (
    CaptureCampaign,
    CaptureRun,
    CaptureSchedulePolicy,
    CaptureWorkItem,
    FetchAttempt,
    FetchAttemptOutcome,
    FetchAttemptResult,
    ListObligation,
    ListObligationResult,
    NormalizedObservation,
    ObligationTerminalState,
    RetryPolicy,
    SourceRequest,
    SourceVintage,
)
from truealpha_contracts.universe import SubjectKind, SubjectRef, UniverseRef

SHA = "a" * 64
CUTOFF = datetime(2026, 4, 2, tzinfo=UTC)


def _retry() -> RetryPolicy:
    return RetryPolicy(
        max_attempts=3,
        retryable_outcomes=(FetchAttemptOutcome.RATE_LIMITED, FetchAttemptOutcome.TRANSPORT_ERROR),
        terminal_outcomes=(
            FetchAttemptOutcome.SUCCESS,
            FetchAttemptOutcome.UNCHANGED,
            FetchAttemptOutcome.UNAVAILABLE,
            FetchAttemptOutcome.FAILED,
        ),
    )


def _universe() -> UniverseRef:
    return UniverseRef(universe_id="universe:topt-us-2026-03-31", universe_version="v1", content_sha256=SHA)


def test_schedule_policy_identity_includes_per_semantic_freshness_windows() -> None:
    base = CaptureSchedulePolicy(
        policy_version="topt-schedule:v1",
        demanded_cadence=timedelta(days=1),
        provider_availability_cadence="daily-post-close",
        freshness_max_age=timedelta(days=7),
        retry=_retry(),
    )
    extended = CaptureSchedulePolicy(
        policy_version="topt-schedule:v1",
        demanded_cadence=timedelta(days=1),
        provider_availability_cadence="daily-post-close",
        freshness_max_age=timedelta(days=7),
        semantic_freshness_max_age={"sec.financial_fact": timedelta(days=120)},
        retry=_retry(),
    )
    assert base.schedule_policy_id != extended.schedule_policy_id
    with pytest.raises(ValidationError, match="immutable"):
        CaptureSchedulePolicy(
            policy_version="latest",
            demanded_cadence=timedelta(days=1),
            provider_availability_cadence="daily-post-close",
            freshness_max_age=timedelta(days=7),
            retry=_retry(),
        )


def test_capture_campaign_run_work_item_and_attempt_lifecycle() -> None:
    policy = CaptureSchedulePolicy(
        policy_version="topt-schedule:v1",
        demanded_cadence=timedelta(days=1),
        provider_availability_cadence="daily-post-close",
        freshness_max_age=timedelta(days=7),
        retry=_retry(),
    )
    campaign = CaptureCampaign(
        campaign_policy_id="topt-campaign:v1",
        environment=CaptureEnvironment.LOCAL,
        cutoff=CUTOFF,
        universe_refs=(_universe(),),
    )
    run = CaptureRun(
        campaign_id=campaign.campaign_id,
        run_sequence=1,
        schedule_policy_id=policy.schedule_policy_id,
        capture_scope_id=f"capture-scope:{SHA}",
    )
    subject = SubjectRef(kind=SubjectKind.LISTING, id="listing:xnas:nvda")
    obligation = ListObligation(
        run_id=run.run_id,
        universe_ref=_universe(),
        subject=subject,
        capture_requirement_id="market-price:v1",
        partition="2026-04-01",
    )
    req = SourceRequest(
        source_registry_entry_id=f"source-registry-entry:{SHA}",
        source_policy_id="yahoo:v1",
        request_fingerprint_version="fp:v1",
        canonical_request_sha256=SHA,
        subject_refs=(subject,),
        capture_requirement_ids=("market-price:v1",),
        partition="2026-04-01",
    )
    work = CaptureWorkItem(
        campaign_id=campaign.campaign_id,
        source_request_id=req.source_request_id,
        schedule_policy_id=policy.schedule_policy_id,
    )
    attempt = FetchAttempt(work_item_id=work.work_item_id, attempt_number=1, started_at=CUTOFF)
    vintage = SourceVintage(
        source_request_id=req.source_request_id,
        source_record_id="yahoo:nvda:2026-04-01",
        source_published_at=CUTOFF,
        raw_object_id=f"raw-object:{SHA}",
    )
    result = FetchAttemptResult(
        attempt_id=attempt.attempt_id,
        completed_at=CUTOFF,
        outcome=FetchAttemptOutcome.SUCCESS,
        status_code=200,
        source_vintage_id=vintage.source_vintage_id,
        reason_codes=("http.ok",),
    )
    ob_res = ListObligationResult(
        obligation_id=obligation.obligation_id,
        terminal_state=ObligationTerminalState.SUCCESS,
        completed_at=CUTOFF,
        final_attempt_id=attempt.attempt_id,
        reason_codes=("http.ok",),
    )
    obs = NormalizedObservation(
        semantic_type="market.daily_bar",
        semantic_version="v1",
        subject=subject,
        valid_from=CUTOFF - timedelta(days=1),
        valid_to=CUTOFF - timedelta(days=1),
        knowable_at=CUTOFF,
        source_vintage_id=vintage.source_vintage_id,
        parser_version="yahoo-bar:v1",
        mapping_version="market-bar:v1",
        normalized_payload_sha256=SHA,
    )
    assert result.attempt_result_id.startswith("fetch-attempt-result:")
    assert ob_res.result_id.startswith("list-obligation-result:")
    assert obs.observation_id.startswith("normalized-observation:")

    with pytest.raises(ValidationError, match="restatement"):
        NormalizedObservation(
            semantic_type="market.daily_bar",
            semantic_version="v1",
            subject=subject,
            valid_from=CUTOFF - timedelta(days=1),
            knowable_at=CUTOFF,
            source_vintage_id=vintage.source_vintage_id,
            parser_version="yahoo-bar:v1",
            mapping_version="market-bar:v1",
            normalized_payload_sha256=SHA,
            is_restatement=True,
        )
