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
    DataHubInterfaceBundle,
    FetchAttempt,
    FetchAttemptOutcome,
    FetchAttemptResult,
    ListObligation,
    ListObligationResult,
    ObligationTerminalState,
    ObligationWorkBinding,
    ProvenanceEdge,
    ProvenanceEdgeKind,
    ProvenanceGraph,
    ProvenanceNode,
    ProvenanceNodeKind,
    RawObjectIdentity,
    SourceRequest,
    SourceVintage,
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
_EXPECTED_INSTRUMENT_MAPPING_SHA256 = "e240ebf2239b94f2eb6463ad73aba89525787b52e6614b382428e8135a1a0c2e"
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
    raw_object_count: int
    source_vintage_count: int
    bundle_sha256: str

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
            "raw_object_count": self.raw_object_count,
            "source_vintage_count": self.source_vintage_count,
            "bundle_sha256": self.bundle_sha256,
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
    raw_object_count: int
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
            "raw_object_count": self.raw_object_count,
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


def frozen_topt_list_version(corpus: Mapping[str, Any]) -> CaptureListVersion:
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
    mapping_sha256 = canonical_sha256(
        {
            "fields": denominator["instrument_tuple_fields"],
            "instruments": instruments,
        }
    )
    if mapping_sha256 != _EXPECTED_INSTRUMENT_MAPPING_SHA256:
        raise ValueError("frozen TOPT instrument mapping drift")
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


def _provenance_graph(
    *,
    campaign: CaptureCampaign,
    run: CaptureRun,
    obligations: tuple[ListObligation, ...],
    source_requests: tuple[SourceRequest, ...],
    work_items: tuple[CaptureWorkItem, ...],
    bindings: tuple[ObligationWorkBinding, ...],
    attempts: tuple[FetchAttempt, ...],
    attempt_results: tuple[FetchAttemptResult, ...],
    raw_objects: tuple[RawObjectIdentity, ...],
    source_vintages: tuple[SourceVintage, ...],
) -> ProvenanceGraph:
    typed_nodes = (
        ((campaign,), "campaign_id", ProvenanceNodeKind.CAMPAIGN),
        ((run,), "run_id", ProvenanceNodeKind.RUN),
        (obligations, "obligation_id", ProvenanceNodeKind.LIST_OBLIGATION),
        (source_requests, "source_request_id", ProvenanceNodeKind.SOURCE_REQUEST),
        (work_items, "work_item_id", ProvenanceNodeKind.WORK_ITEM),
        (attempts, "attempt_id", ProvenanceNodeKind.FETCH_ATTEMPT),
        (attempt_results, "attempt_result_id", ProvenanceNodeKind.FETCH_ATTEMPT_RESULT),
        (raw_objects, "raw_object_id", ProvenanceNodeKind.RAW_OBJECT),
        (source_vintages, "source_vintage_id", ProvenanceNodeKind.SOURCE_VINTAGE),
    )
    nodes = tuple(
        ProvenanceNode(node_id=str(getattr(value, id_field)), kind=kind)
        for values, id_field, kind in typed_nodes
        for value in values
    )

    edges: list[ProvenanceEdge] = [
        ProvenanceEdge(
            from_node_id=campaign.campaign_id,
            edge_type=ProvenanceEdgeKind.CONTAINS,
            to_node_id=run.run_id,
            edge_ordinal=0,
        )
    ]
    edges.extend(
        ProvenanceEdge(
            from_node_id=campaign.campaign_id,
            edge_type=ProvenanceEdgeKind.CONTAINS,
            to_node_id=request.source_request_id,
            edge_ordinal=index + 1,
        )
        for index, request in enumerate(source_requests)
    )
    edges.extend(
        ProvenanceEdge(
            from_node_id=run.run_id,
            edge_type=ProvenanceEdgeKind.REQUIRES,
            to_node_id=obligation.obligation_id,
            edge_ordinal=index,
        )
        for index, obligation in enumerate(obligations)
    )
    edges.extend(
        ProvenanceEdge(
            from_node_id=binding.obligation_id,
            edge_type=ProvenanceEdgeKind.SATISFIED_BY,
            to_node_id=binding.work_item_id,
            edge_ordinal=0,
        )
        for binding in bindings
    )

    requests_by_id = {request.source_request_id: request for request in source_requests}
    attempts_by_work = {attempt.work_item_id: attempt for attempt in attempts}
    results_by_attempt = {result.attempt_id: result for result in attempt_results}
    vintages_by_id = {vintage.source_vintage_id: vintage for vintage in source_vintages}
    for work_item in work_items:
        source_request = requests_by_id[work_item.source_request_id]
        attempt = attempts_by_work[work_item.work_item_id]
        attempt_result = results_by_attempt[attempt.attempt_id]
        source_vintage_id = attempt_result.source_vintage_id or attempt_result.reused_source_vintage_id
        if source_vintage_id is None:
            raise ValueError("TOPT content attempt is missing source vintage lineage")
        source_vintage = vintages_by_id[source_vintage_id]
        edges.extend(
            (
                ProvenanceEdge(
                    from_node_id=source_request.source_request_id,
                    edge_type=ProvenanceEdgeKind.DISPATCHES,
                    to_node_id=work_item.work_item_id,
                    edge_ordinal=0,
                ),
                ProvenanceEdge(
                    from_node_id=work_item.work_item_id,
                    edge_type=ProvenanceEdgeKind.ATTEMPTED_BY,
                    to_node_id=attempt.attempt_id,
                    edge_ordinal=0,
                ),
                ProvenanceEdge(
                    from_node_id=attempt.attempt_id,
                    edge_type=ProvenanceEdgeKind.COMPLETES,
                    to_node_id=attempt_result.attempt_result_id,
                    edge_ordinal=0,
                ),
                ProvenanceEdge(
                    from_node_id=attempt_result.attempt_result_id,
                    edge_type=(
                        ProvenanceEdgeKind.OBSERVED
                        if attempt_result.source_vintage_id is not None
                        else ProvenanceEdgeKind.REUSES
                    ),
                    to_node_id=source_vintage.source_vintage_id,
                    edge_ordinal=0,
                ),
                ProvenanceEdge(
                    from_node_id=source_vintage.source_vintage_id,
                    edge_type=ProvenanceEdgeKind.ARCHIVES_AS,
                    to_node_id=source_vintage.raw_object_id,
                    edge_ordinal=0,
                ),
            )
        )
    return ProvenanceGraph(schema_version="d5-medium-provenance:v1", nodes=nodes, edges=tuple(edges))


def _materialize_run(
    corpus: Mapping[str, Any],
    *,
    cutoff: datetime,
    sequence: int,
    outcome: FetchAttemptOutcome,
) -> tuple[ToptRunSummary, tuple[CaptureListObligation, ...], set[str], set[str]]:
    denominator = corpus["topt_denominator"]
    semantic_types = tuple(str(value) for value in denominator["obligation_expansion"]["semantic_types"])
    if semantic_types != _EXPECTED_SEMANTIC_TYPES:
        raise ValueError("TOPT semantic denominator drift")
    if int(denominator["obligation_count"]) != _EXPECTED_OBLIGATION_COUNT:
        raise ValueError("TOPT obligation denominator drift")
    list_version = frozen_topt_list_version(corpus)
    schedule_policy, campaign, run = _capture_run(corpus, cutoff=cutoff, sequence=sequence)
    obligations = expand_obligations(
        run_id=run.run_id,
        list_version=list_version,
        semantic_types=semantic_types,
        partition=str(denominator["report_date"]),
    )
    if len(obligations) != _EXPECTED_OBLIGATION_COUNT:
        raise ValueError("TOPT obligation denominator drift")

    source_requests: list[SourceRequest] = []
    work_items: list[CaptureWorkItem] = []
    capture_bindings: list[CaptureObligationWorkBinding] = []
    bundle_bindings: list[ObligationWorkBinding] = []
    attempts: list[FetchAttempt] = []
    attempt_results: list[FetchAttemptResult] = []
    raw_objects: list[RawObjectIdentity] = []
    source_vintages: list[SourceVintage] = []
    terminal_results: list[ListObligationResult] = []
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
        capture_binding = CaptureObligationWorkBinding(
            obligation_id=obligation.obligation_id,
            work_item_id=work_item.work_item_id,
        )
        bundle_binding = ObligationWorkBinding(
            obligation_id=obligation.obligation.obligation_id,
            work_item_id=work_item.work_item_id,
        )

        vintage_wave = min(sequence, 2)
        raw_object = RawObjectIdentity(
            payload_sha256=canonical_sha256(
                {
                    "fixture": "d5-medium-replay:v1",
                    "source_request_id": source_request.source_request_id,
                    "vintage_wave": vintage_wave,
                }
            )
        )
        source_vintage = SourceVintage(
            source_request_id=source_request.source_request_id,
            source_record_id=f"fixture-record:{source_request.content_sha256}:{vintage_wave}",
            source_published_at=_CUTOFFS[vintage_wave - 1],
            raw_object_id=raw_object.raw_object_id,
        )

        ledger = AttemptLedger(work_item_id=work_item.work_item_id, retry_policy=schedule_policy.retry)
        attempt = ledger.start(started_at=cutoff)
        attempt_result = ledger.finish(
            attempt=attempt,
            completed_at=cutoff + timedelta(seconds=1),
            outcome=outcome,
            source_vintage_id=(source_vintage.source_vintage_id if outcome is FetchAttemptOutcome.SUCCESS else None),
            reused_source_vintage_id=(
                source_vintage.source_vintage_id if outcome is FetchAttemptOutcome.UNCHANGED else None
            ),
        )
        terminal_result = ListObligationResult(
            obligation_id=obligation.obligation.obligation_id,
            terminal_state=ObligationTerminalState(outcome.value),
            completed_at=attempt_result.completed_at,
            final_attempt_id=attempt.attempt_id,
            reason_codes=(outcome.value,),
        )

        source_requests.append(source_request)
        work_items.append(work_item)
        capture_bindings.append(capture_binding)
        bundle_bindings.append(bundle_binding)
        attempts.append(attempt)
        attempt_results.append(attempt_result)
        raw_objects.append(raw_object)
        source_vintages.append(source_vintage)
        terminal_results.append(terminal_result)

    if len({binding.binding_id for binding in capture_bindings}) != len(obligations):
        raise ValueError("TOPT obligation binding coverage drift")
    source_request_tuple = tuple(source_requests)
    work_item_tuple = tuple(work_items)
    bundle_binding_tuple = tuple(bundle_bindings)
    attempt_tuple = tuple(attempts)
    attempt_result_tuple = tuple(attempt_results)
    raw_object_tuple = tuple(raw_objects)
    source_vintage_tuple = tuple(source_vintages)
    terminal_result_tuple = tuple(terminal_results)
    bundle = DataHubInterfaceBundle(
        schedule_policies=(schedule_policy,),
        campaigns=(campaign,),
        runs=(run,),
        obligations=tuple(obligation.obligation for obligation in obligations),
        source_requests=source_request_tuple,
        work_items=work_item_tuple,
        bindings=bundle_binding_tuple,
        attempts=attempt_tuple,
        attempt_results=attempt_result_tuple,
        raw_objects=raw_object_tuple,
        source_vintages=source_vintage_tuple,
        results=terminal_result_tuple,
        provenance=_provenance_graph(
            campaign=campaign,
            run=run,
            obligations=tuple(obligation.obligation for obligation in obligations),
            source_requests=source_request_tuple,
            work_items=work_item_tuple,
            bindings=bundle_binding_tuple,
            attempts=attempt_tuple,
            attempt_results=attempt_result_tuple,
            raw_objects=raw_object_tuple,
            source_vintages=source_vintage_tuple,
        ),
    )
    bundle_sha256 = canonical_sha256(bundle.model_dump(mode="json"))
    summary = ToptRunSummary(
        campaign_id=campaign.campaign_id,
        run_id=run.run_id,
        cutoff=cutoff,
        outcome=outcome.value,
        obligation_count=len(bundle.obligations),
        work_item_count=len(bundle.work_items),
        binding_count=len(bundle.bindings),
        attempt_count=len(bundle.attempts),
        terminal_obligation_count=len(bundle.results),
        raw_object_count=len(bundle.raw_objects),
        source_vintage_count=len(bundle.source_vintages),
        bundle_sha256=bundle_sha256,
    )
    return (
        summary,
        obligations,
        {vintage.source_vintage_id for vintage in bundle.source_vintages},
        {raw_object.raw_object_id for raw_object in bundle.raw_objects},
    )


def run_topt_medium_replay(corpus: Mapping[str, Any]) -> ToptMediumReplayReport:
    """Run the exact 20/21/84 TOPT denominator at three immutable cutoffs."""

    denominator = corpus["topt_denominator"]
    summaries: list[ToptRunSummary] = []
    all_obligations: list[CaptureListObligation] = []
    raw_object_ids: set[str] = set()
    source_vintage_ids: set[str] = set()
    terminal_states: Counter[str] = Counter()
    semantic_counts: Counter[str] = Counter()
    for sequence, (cutoff, outcome) in enumerate(zip(_CUTOFFS, _OUTCOMES, strict=True), start=1):
        summary, obligations, run_vintages, run_raw_objects = _materialize_run(
            corpus,
            cutoff=cutoff,
            sequence=sequence,
            outcome=outcome,
        )
        summaries.append(summary)
        all_obligations.extend(obligations)
        raw_object_ids.update(run_raw_objects)
        source_vintage_ids.update(run_vintages)
        terminal_states[outcome.value] += summary.terminal_obligation_count
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
        raw_object_count=len(raw_object_ids),
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
