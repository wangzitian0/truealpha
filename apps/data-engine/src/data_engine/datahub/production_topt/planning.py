"""Fail-closed planning for an operator-triggered Production TOPT run."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from truealpha_contracts import CaptureEnvironment, canonical_sha256
from truealpha_contracts.capture_control import (
    CaptureListObligation,
    CaptureListVersion,
    CaptureObligationWorkBinding,
)
from truealpha_contracts.datahub import (
    CaptureCampaign,
    CaptureRun,
    CaptureSchedulePolicy,
    CaptureWorkItem,
    SourceRequest,
)
from truealpha_contracts.release import ReleaseManifest

from data_engine.datahub.control_plane import expand_obligations, replay_retry_policy
from data_engine.datahub.medium_replay import frozen_topt_list_version

PRODUCTION_CONFIRMATION = "RUN PRODUCTION TOPT"
_EXPECTED_ISSUER_COUNT = 20
_EXPECTED_INSTRUMENT_COUNT = 21
_EXPECTED_OBLIGATION_COUNT = 84
_SEMANTIC_TYPES = (
    "market-price",
    "listing-identity",
    "universe-membership",
    "financial-fact",
)
_SOURCE_KEYS = {
    "financial-fact": "sec-companyfacts-public:v1",
    "listing-identity": "release-listing-identity:v1",
    "market-price": "yahoo-chart-public:v1",
    "universe-membership": "release-universe-membership:v1",
}


@dataclass(frozen=True)
class ManualProductionToptRequest:
    release_manifest_id: str
    release: ReleaseManifest
    cutoff: datetime
    run_sequence: int
    confirmation: str


@dataclass(frozen=True)
class ManualProductionToptPlan:
    release_manifest_id: str
    issuer_count: int
    instrument_count: int
    schedule_policy: CaptureSchedulePolicy
    campaign: CaptureCampaign
    run: CaptureRun
    list_version: CaptureListVersion
    obligations: tuple[CaptureListObligation, ...]
    source_requests: tuple[SourceRequest, ...]
    work_items: tuple[CaptureWorkItem, ...]
    bindings: tuple[CaptureObligationWorkBinding, ...]

    @property
    def obligation_count(self) -> int:
        return len(self.obligations)


def _schedule_policy(release: ReleaseManifest) -> CaptureSchedulePolicy:
    return CaptureSchedulePolicy(
        policy_version=f"production-topt:{release.manifest_sha256}",
        demanded_cadence=timedelta(days=1),
        provider_availability_cadence="provider-natural-cadence:v1",
        freshness_max_age=timedelta(days=2),
        retry=replay_retry_policy(3),
    )


def _source_request(
    *,
    release: ReleaseManifest,
    obligation: CaptureListObligation,
) -> SourceRequest:
    semantic_type = obligation.capture_requirement_id.removesuffix(":v1")
    source_key = _SOURCE_KEYS[semantic_type]
    request_coordinate = {
        "release_manifest_id": release.release_manifest_id,
        "source_key": source_key,
        "subject": obligation.subject.model_dump(mode="json"),
        "capture_requirement_id": obligation.capture_requirement_id,
        "partition": obligation.partition,
    }
    source_entry_hash = canonical_sha256(
        {
            "source_registry_id": release.source_registry_id,
            "source_key": source_key,
        }
    )
    return SourceRequest(
        source_registry_entry_id=f"source-registry-entry:{source_entry_hash}",
        source_policy_id=f"source-policy:production-topt-{release.manifest_sha256}",
        request_fingerprint_version="production-topt-request:v1",
        canonical_request_sha256=canonical_sha256(request_coordinate),
        subject_refs=(obligation.subject,),
        capture_requirement_ids=(obligation.capture_requirement_id,),
        partition=obligation.partition,
    )


def plan_manual_production_topt(
    corpus: Mapping[str, Any],
    request: ManualProductionToptRequest,
) -> ManualProductionToptPlan:
    """Freeze one Production run without dispatching work or registering a schedule."""

    if request.confirmation != PRODUCTION_CONFIRMATION:
        raise ValueError(f"production confirmation must be exactly {PRODUCTION_CONFIRMATION!r}")
    if request.release_manifest_id != request.release.release_manifest_id:
        raise ValueError("requested release does not match the resolved ReleaseManifest")
    if request.cutoff.tzinfo is None or request.cutoff.utcoffset() is None:
        raise ValueError("Production cutoff must be timezone-aware")
    if request.run_sequence < 1:
        raise ValueError("run_sequence must be positive")

    denominator = corpus["topt_denominator"]
    list_version = frozen_topt_list_version(corpus)
    if request.release.universe != list_version.universe:
        raise ValueError("release UniverseRef does not match the frozen TOPT list")
    if request.cutoff < list_version.effective_at:
        raise ValueError("Production cutoff precedes the frozen TOPT list")
    if tuple(denominator["obligation_expansion"]["semantic_types"]) != _SEMANTIC_TYPES:
        raise ValueError("TOPT semantic denominator drift")

    issuer_count = len({str(row[0]) for row in denominator["instruments"]})
    instrument_count = len({str(row[1]) for row in denominator["instruments"]})
    if (issuer_count, instrument_count) != (_EXPECTED_ISSUER_COUNT, _EXPECTED_INSTRUMENT_COUNT):
        raise ValueError("TOPT issuer or instrument denominator drift")

    schedule_policy = _schedule_policy(request.release)
    campaign = CaptureCampaign(
        campaign_policy_id=f"capture-policy:production-topt-{request.release.manifest_sha256}",
        environment=CaptureEnvironment.PRODUCTION,
        cutoff=request.cutoff,
        universe_refs=(list_version.universe,),
    )
    run = CaptureRun(
        campaign_id=campaign.campaign_id,
        run_sequence=request.run_sequence,
        schedule_policy_id=schedule_policy.schedule_policy_id,
        capture_scope_id=request.release.capture_scope_id,
    )
    obligations = expand_obligations(
        run_id=run.run_id,
        list_version=list_version,
        semantic_types=_SEMANTIC_TYPES,
        partition=str(denominator["report_date"]),
    )
    if len(obligations) != _EXPECTED_OBLIGATION_COUNT:
        raise ValueError("TOPT obligation denominator drift")

    source_requests = tuple(_source_request(release=request.release, obligation=item) for item in obligations)
    work_items = tuple(
        CaptureWorkItem(
            campaign_id=campaign.campaign_id,
            source_request_id=source_request.source_request_id,
            schedule_policy_id=schedule_policy.schedule_policy_id,
        )
        for source_request in source_requests
    )
    bindings = tuple(
        CaptureObligationWorkBinding(
            obligation_id=obligation.obligation_id,
            work_item_id=work_item.work_item_id,
        )
        for obligation, work_item in zip(obligations, work_items, strict=True)
    )
    if len({item.binding_id for item in bindings}) != _EXPECTED_OBLIGATION_COUNT:
        raise ValueError("TOPT work binding coverage drift")

    return ManualProductionToptPlan(
        release_manifest_id=request.release_manifest_id,
        issuer_count=issuer_count,
        instrument_count=instrument_count,
        schedule_policy=schedule_policy,
        campaign=campaign,
        run=run,
        list_version=list_version,
        obligations=obligations,
        source_requests=source_requests,
        work_items=work_items,
        bindings=bindings,
    )
