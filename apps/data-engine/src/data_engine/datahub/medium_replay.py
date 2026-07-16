"""Deterministic D5 E3 replay over the frozen TOPT denominator."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from truealpha_contracts import CaptureEnvironment, SubjectKind, SubjectRef, canonical_sha256
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
    FetchAttemptOutcome,
    SourceRequest,
)

from data_engine.datahub.control_plane import (
    AttemptLedger,
    expand_obligations,
    frozen_topt_universe,
    replay_retry_policy,
)

_CUTOFFS = (
    datetime(2026, 4, 1, tzinfo=UTC),
    datetime(2026, 4, 2, tzinfo=UTC),
    datetime(2026, 4, 3, tzinfo=UTC),
)
_OUTCOMES = (
    FetchAttemptOutcome.SUCCESS,
    FetchAttemptOutcome.SUCCESS,
    FetchAttemptOutcome.UNCHANGED,
)
_EXPECTED_ISSUER_COUNT = 20
_EXPECTED_INSTRUMENT_COUNT = 21
_EXPECTED_OBLIGATION_COUNT = 84
_EXPECTED_SEMANTIC_TYPES = (
    "market-price",
    "listing-identity",
    "universe-membership",
    "financial-fact",
)


@dataclass(frozen=True)
class ToptRunSummary:
    campaign_id: str
    run_id: str
    cutoff: datetime
    outcome: str
    obligation_count: int
    work_item_count: int
    binding_count: int
    attempt_count: int
    terminal_obligation_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "campaign_id": self.campaign_id,
            "run_id": self.run_id,
            "cutoff": self.cutoff.isoformat(),
            "outcome": self.outcome,
            "obligation_count": self.obligation_count,
            "work_item_count": self.work_item_count,
            "binding_count": self.binding_count,
            "attempt_count": self.attempt_count,
            "terminal_obligation_count": self.terminal_obligation_count,
        }


@dataclass(frozen=True)
class ToptMediumReplayReport:
    corpus_id: str
    list_version_id: str
    issuer_count: int
    instrument_count: int
    obligation_count_per_run: int
    run_count: int
    cutoff_count: int
    source_vintage_count: int
    total_obligation_count: int
    total_work_item_count: int
    total_binding_count: int
    total_attempt_count: int
    total_terminal_obligation_count: int
    semantic_obligation_counts: tuple[tuple[str, int], ...]
    terminal_state_counts: tuple[tuple[str, int], ...]
    run_summaries: tuple[ToptRunSummary, ...]
    goog_instrument_id: str
    googl_instrument_id: str
    source_calls: int
    report_sha256: str = ""

    def as_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "corpus_id": self.corpus_id,
            "list_version_id": self.list_version_id,
            "issuer_count": self.issuer_count,
            "instrument_count": self.instrument_count,
            "obligation_count_per_run": self.obligation_count_per_run,
            "run_count": self.run_count,
            "cutoff_count": self.cutoff_count,
            "source_vintage_count": self.source_vintage_count,
            "total_obligation_count": self.total_obligation_count,
            "total_work_item_count": self.total_work_item_count,
            "total_binding_count": self.total_binding_count,
            "total_attempt_count": self.total_attempt_count,
            "total_terminal_obligation_count": self.total_terminal_obligation_count,
            "semantic_obligation_counts": dict(self.semantic_obligation_counts),
            "terminal_state_counts": dict(self.terminal_state_counts),
            "run_summaries": [summary.as_dict() for summary in self.run_summaries],
            "goog_instrument_id": self.goog_instrument_id,
            "googl_instrument_id": self.googl_instrument_id,
            "source_calls": self.source_calls,
        }
        if include_hash:
            result["report_sha256"] = self.report_sha256
        return result


def _topt_list_version(corpus: Mapping[str, Any]) -> CaptureListVersion:
    denominator = corpus["topt_denominator"]
    instruments = denominator["instruments"]
    if (
        int(denominator["instrument_count"]) != _EXPECTED_INSTRUMENT_COUNT
        or len(instruments) != _EXPECTED_INSTRUMENT_COUNT
    ):
        raise ValueError("TOPT instrument denominator shrink")
    issuer_ids = {str(row[0]) for row in instruments}
    if int(denominator["issuer_count"]) != _EXPECTED_ISSUER_COUNT or len(issuer_ids) != _EXPECTED_ISSUER_COUNT:
        raise ValueError("TOPT issuer denominator drift")
    instrument_ids = tuple(str(row[1]) for row in instruments)
    if len(instrument_ids) != len(set(instrument_ids)):
        raise ValueError("TOPT security denominator contains duplicates")
    listings = tuple(str(row[2]) for row in instruments)
    if len(listings) != len(set(listings)):
        raise ValueError("TOPT listing denominator contains duplicates")
    version = CaptureListVersion(
        universe=frozen_topt_universe(corpus),
        members=tuple(SubjectRef(kind=SubjectKind.LISTING, id=listing) for listing in listings),
        effective_at=_CUTOFFS[0],
    )
    if version.list_version_id != denominator["list_version_id"]:
        raise ValueError("frozen TOPT list identity drift")
    return version


def _schedule_policy() -> CaptureSchedulePolicy:
    return CaptureSchedulePolicy(
        policy_version="d5-medium-replay:v1",
        demanded_cadence=timedelta(days=1),
        provider_availability_cadence="fixture-daily:v1",
        freshness_max_age=timedelta(days=2),
        retry=replay_retry_policy(3),
    )


def _capture_run(
    corpus: Mapping[str, Any], *, cutoff: datetime, sequence: int
) -> tuple[CaptureSchedulePolicy, CaptureCampaign, CaptureRun]:
    universe = frozen_topt_universe(corpus)
    schedule_policy = _schedule_policy()
    campaign = CaptureCampaign(
        campaign_policy_id="capture-policy:d5-medium-v1",
        environment=CaptureEnvironment.LOCAL_DEV,
        cutoff=cutoff,
        universe_refs=(universe,),
    )
    scope_id = f"capture-scope:{canonical_sha256({'corpus_id': corpus['corpus_id'], 'rung': 'E3'})}"
    run = CaptureRun(
        campaign_id=campaign.campaign_id,
        run_sequence=sequence,
        schedule_policy_id=schedule_policy.schedule_policy_id,
        capture_scope_id=scope_id,
    )
    return schedule_policy, campaign, run


def _source_request(
    *,
    member: SubjectRef,
    semantic_types: tuple[str, ...],
    partition: str,
) -> SourceRequest:
    requirement_ids = tuple(f"{semantic_type}:v1" for semantic_type in semantic_types)
    request_coordinate = {
        "member": member.model_dump(mode="json"),
        "requirements": requirement_ids,
        "partition": partition,
    }
    return SourceRequest(
        source_registry_entry_id=f"source-registry-entry:{canonical_sha256({'source': 'd5-medium-fixture:v1', 'semantic_types': semantic_types})}",
        source_policy_id="source-policy:d5-medium-fixture-v1",
        request_fingerprint_version="d5-medium-request:v1",
        canonical_request_sha256=canonical_sha256(request_coordinate),
        subject_refs=(member,),
        capture_requirement_ids=requirement_ids,
        partition=partition,
    )


def _materialize_run(
    corpus: Mapping[str, Any],
    *,
    cutoff: datetime,
    sequence: int,
    outcome: FetchAttemptOutcome,
) -> tuple[ToptRunSummary, tuple[CaptureListObligation, ...], set[str]]:
    denominator = corpus["topt_denominator"]
    semantic_types = tuple(str(value) for value in denominator["obligation_expansion"]["semantic_types"])
    if semantic_types != _EXPECTED_SEMANTIC_TYPES:
        raise ValueError("TOPT semantic denominator drift")
    if int(denominator["obligation_count"]) != _EXPECTED_OBLIGATION_COUNT:
        raise ValueError("TOPT obligation denominator drift")
    list_version = _topt_list_version(corpus)
    schedule_policy, campaign, run = _capture_run(corpus, cutoff=cutoff, sequence=sequence)
    obligations = expand_obligations(
        run_id=run.run_id,
        list_version=list_version,
        semantic_types=semantic_types,
        partition=str(denominator["report_date"]),
    )
    if len(obligations) != _EXPECTED_OBLIGATION_COUNT:
        raise ValueError("TOPT obligation denominator drift")

    binding_ids: set[str] = set()
    work_item_ids: set[str] = set()
    source_vintage_ids: set[str] = set()
    attempt_count = 0
    for obligation in obligations:
        semantic_type = obligation.capture_requirement_id.removesuffix(":v1")
        source_request = _source_request(
            member=obligation.subject,
            semantic_types=(semantic_type,),
            partition=str(denominator["report_date"]),
        )
        work_item = CaptureWorkItem(
            campaign_id=campaign.campaign_id,
            source_request_id=source_request.source_request_id,
            schedule_policy_id=run.schedule_policy_id,
        )
        work_item_ids.add(work_item.work_item_id)
        binding = CaptureObligationWorkBinding(
            obligation_id=obligation.obligation_id,
            work_item_id=work_item.work_item_id,
        )
        binding_ids.add(binding.binding_id)

        ledger = AttemptLedger(work_item_id=work_item.work_item_id, retry_policy=schedule_policy.retry)
        attempt = ledger.start(started_at=cutoff)
        latest_vintage_id = f"source-vintage:{canonical_sha256({'source_request_id': source_request.source_request_id, 'vintage': min(sequence, 2)})}"
        ledger.finish(
            attempt=attempt,
            completed_at=cutoff + timedelta(seconds=1),
            outcome=outcome,
            source_vintage_id=latest_vintage_id if outcome is FetchAttemptOutcome.SUCCESS else None,
            reused_source_vintage_id=latest_vintage_id if outcome is FetchAttemptOutcome.UNCHANGED else None,
        )
        source_vintage_ids.add(latest_vintage_id)
        attempt_count += len(ledger.attempts)

    if len(binding_ids) != len(obligations):
        raise ValueError("TOPT obligation binding coverage drift")
    summary = ToptRunSummary(
        campaign_id=campaign.campaign_id,
        run_id=run.run_id,
        cutoff=cutoff,
        outcome=outcome.value,
        obligation_count=len(obligations),
        work_item_count=len(work_item_ids),
        binding_count=len(binding_ids),
        attempt_count=attempt_count,
        terminal_obligation_count=len(obligations),
    )
    return summary, obligations, source_vintage_ids


def run_topt_medium_replay(corpus: Mapping[str, Any]) -> ToptMediumReplayReport:
    """Run the exact 20/21/84 TOPT denominator at three immutable cutoffs."""

    denominator = corpus["topt_denominator"]
    summaries: list[ToptRunSummary] = []
    all_obligations: list[CaptureListObligation] = []
    source_vintage_ids: set[str] = set()
    terminal_states: Counter[str] = Counter()
    semantic_counts: Counter[str] = Counter()
    for sequence, (cutoff, outcome) in enumerate(zip(_CUTOFFS, _OUTCOMES, strict=True), start=1):
        summary, obligations, run_vintages = _materialize_run(
            corpus,
            cutoff=cutoff,
            sequence=sequence,
            outcome=outcome,
        )
        summaries.append(summary)
        all_obligations.extend(obligations)
        source_vintage_ids.update(run_vintages)
        terminal_states[outcome.value] += len(obligations)
        semantic_counts.update(item.capture_requirement_id.removesuffix(":v1") for item in obligations)

    rows_by_ticker = {str(row[3]): row for row in denominator["instruments"]}
    goog_id = str(rows_by_ticker["GOOG"][1])
    googl_id = str(rows_by_ticker["GOOGL"][1])
    if goog_id == googl_id or rows_by_ticker["GOOG"][2] == rows_by_ticker["GOOGL"][2]:
        raise ValueError("GOOG and GOOGL collapsed to one instrument")
    if len({item.obligation_id for item in all_obligations}) != len(all_obligations):
        raise ValueError("TOPT obligations collided across capture runs")

    report = ToptMediumReplayReport(
        corpus_id=str(corpus["corpus_id"]),
        list_version_id=str(denominator["list_version_id"]),
        issuer_count=int(denominator["issuer_count"]),
        instrument_count=int(denominator["instrument_count"]),
        obligation_count_per_run=int(denominator["obligation_count"]),
        run_count=len(summaries),
        cutoff_count=len({summary.cutoff for summary in summaries}),
        source_vintage_count=len(source_vintage_ids),
        total_obligation_count=len(all_obligations),
        total_work_item_count=sum(summary.work_item_count for summary in summaries),
        total_binding_count=sum(summary.binding_count for summary in summaries),
        total_attempt_count=sum(summary.attempt_count for summary in summaries),
        total_terminal_obligation_count=sum(summary.terminal_obligation_count for summary in summaries),
        semantic_obligation_counts=tuple(sorted(semantic_counts.items())),
        terminal_state_counts=tuple(sorted(terminal_states.items())),
        run_summaries=tuple(summaries),
        goog_instrument_id=goog_id,
        googl_instrument_id=googl_id,
        source_calls=0,
    )
    report_hash = canonical_sha256(report.as_dict(include_hash=False))
    return ToptMediumReplayReport(**{**report.__dict__, "report_sha256": report_hash})
