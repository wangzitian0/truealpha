"""The deployed TOPT capture composition root (#171 A3, ADR A1).

One schedulable unit: plan the run, dispatch every obligation through the generic
executor and its per-semantic adapters, persist the capture-control tables and the
evidence graph in one transaction, then freeze → materialize → grade.

This is the shape init.md rule 22 requires and the retired single-class capture monolith
violated: nothing here branches on source or record type. The executor iterates work
items; `CompositeSourceFetchPort` routes each to the adapter that owns its semantic; the
adapters own their vendors; `PostgresCaptureControlSink` owns persistence.

The universe (21 listings / 84 cells) comes from the frozen capture-control corpus — that
is versioned scope configuration (init.md rule 13), not input data.

`cutoff` and `version` come from the schedule tick, never the wall clock, so two
consecutive ticks produce two distinct content-addressed runs and a retried tick
reproduces the same identities (conflict-tolerant inserts make the replay idempotent).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg
from factors.production_topt import GppeV0Definition, OperatingBranch
from truealpha_contracts import CaptureEnvironment, EvidenceGraphWriter, canonical_sha256
from truealpha_contracts.capture_control import (
    CaptureListObligation,
    CaptureObligationWorkBinding,
)
from truealpha_contracts.datahub import (
    CaptureCampaign,
    CaptureRun,
    CaptureSchedulePolicy,
    CaptureWorkItem,
    RetryPolicy,
    SourceRequest,
)

from data_engine.datahub import quality_report
from data_engine.datahub.control_plane import expand_obligations, replay_retry_policy
from data_engine.datahub.evidence_graph_repository import PostgresEvidenceGraphRepository
from data_engine.datahub.medium_replay import frozen_topt_list_version
from data_engine.datahub.production_topt.capture_orchestration import run_topt_capture
from data_engine.datahub.production_topt.executor import SourceFetchPort
from data_engine.datahub.production_topt.headcount import StopgapHeadcountExtractor, headcounts_by_cik
from data_engine.datahub.production_topt.issuer_registry import resolve_operating_branches
from data_engine.datahub.production_topt.market_price_adapter import (
    MarketPriceAdapter,
    MarketPriceTarget,
    yahoo_quote_fetcher,
)
from data_engine.datahub.production_topt.materialization import PostgresToptCoreRepository
from data_engine.datahub.production_topt.persistence import (
    CaptureTimeline,
    ObligationBinding,
    PostgresCaptureControlSink,
)
from data_engine.datahub.production_topt.release_derived_adapter import (
    ReleaseDerivedAdapter,
    ReleaseDerivedRecord,
)
from data_engine.datahub.production_topt.sec_financial_adapter import (
    SecFinancialFactAdapter,
    SecTarget,
    sec_financial_fetcher,
)
from data_engine.datahub.production_topt.twelve_data_origin import twelve_data_origin
from data_engine.datahub.repository import PostgresCaptureControlRepository
from data_engine.sources import sec

SEMANTIC_TYPES = ("market-price", "listing-identity", "universe-membership", "financial-fact")
_RELEASE_SEMANTICS = frozenset({"listing-identity", "universe-membership"})
_RELEASE_PAYLOAD = {"kind": "production-topt-live-release"}
_MAX_ATTEMPTS = 3
_RISK_FREE_RATE = Decimal("0.05")


def live_version_for(cutoff: datetime) -> str:
    """The per-tick capture version: distinct per scheduled tick, stable on retry."""
    return f"live-{cutoff.astimezone(UTC):%Y%m%dT%H%M}"


@dataclass(frozen=True)
class ToptPipelineResult:
    run_id: str
    release_manifest_id: str
    core_result_count: int
    quality_report_id: str
    quality: dict[str, Any]


def _load_capture_corpus() -> dict[str, Any]:
    """The frozen capture-control universe corpus, loaded from PACKAGE data so the
    deployed image (site-packages only, no repo tree) can read it. Lazy: importing
    this module must never touch the filesystem (Definitions load hermetically)."""
    from importlib import resources

    raw = resources.files("data_engine.datahub.data").joinpath("corpus.v1.json").read_bytes()
    return json.loads(raw)


def _sec_ticker(ticker: str) -> str:
    # SEC's company_tickers file uses a hyphen for share-class tickers (BRK.B -> BRK-B).
    return ticker.replace(".", "-")


def _source_request(obligation: CaptureListObligation, *, ordinal: int, source: str) -> SourceRequest:
    coordinate: dict[str, Any] = {
        "ordinal": ordinal,
        "subject": obligation.subject.model_dump(mode="json"),
        "requirement": obligation.capture_requirement_id,
        "partition": obligation.partition,
    }
    return SourceRequest(
        source_registry_entry_id=f"source-registry-entry:{canonical_sha256({'source': source})}",
        source_policy_id=f"source-policy:{source}",
        request_fingerprint_version=f"{source}:v1",
        canonical_request_sha256=canonical_sha256(coordinate),
        subject_refs=(obligation.subject,),
        capture_requirement_ids=(obligation.capture_requirement_id,),
        partition=obligation.partition,
    )


@dataclass(frozen=True)
class PlannedRun:
    """One planned run: its identities, its work items and everything keyed off them."""

    run_id: str
    release_manifest_id: str
    cutoff: datetime
    timeline: CaptureTimeline
    work_items: tuple[CaptureWorkItem, ...]
    bindings: dict[str, ObligationBinding]
    coordinates: dict[str, tuple[str, str, str, str]]
    source_label: str
    retry: RetryPolicy


def plan_and_persist(connection: psycopg.Connection[Any], *, cutoff: datetime, version: str) -> PlannedRun:
    """Freeze the run's scope and persist the dispatch intent; performs no source calls."""
    corpus = _load_capture_corpus()
    denominator = corpus["topt_denominator"]
    coordinates = {
        str(row[2]): (str(row[0]), str(row[1]), str(row[2]), str(row[3])) for row in denominator["instruments"]
    }
    partition = str(denominator["report_date"])
    list_version = frozen_topt_list_version(corpus)
    source_label = f"production-topt-{version}"

    policy = CaptureSchedulePolicy(
        policy_version=source_label,
        demanded_cadence=timedelta(days=1),
        provider_availability_cadence="scheduled:v1",
        freshness_max_age=timedelta(days=2),
        retry=replay_retry_policy(_MAX_ATTEMPTS),
    )
    campaign = CaptureCampaign(
        campaign_policy_id=f"capture-policy:{source_label}",
        environment=CaptureEnvironment.PRODUCTION,
        cutoff=cutoff,
        universe_refs=(list_version.universe,),
    )
    run = CaptureRun(
        campaign_id=campaign.campaign_id,
        run_sequence=1,
        schedule_policy_id=policy.schedule_policy_id,
        capture_scope_id=f"capture-scope:{canonical_sha256({'scope': source_label})}",
    )
    obligations = expand_obligations(
        run_id=run.run_id,
        list_version=list_version,
        semantic_types=SEMANTIC_TYPES,
        partition=partition,
    )

    repository = PostgresCaptureControlRepository(connection)
    repository.put_schedule_policy(policy)
    repository.put_campaign(campaign)
    repository.put_list_version(list_version)
    repository.bind_campaign_list(campaign.campaign_id, list_version.list_version_id)
    repository.put_run(run)

    release_sha256 = canonical_sha256(_RELEASE_PAYLOAD)
    release_manifest_id = f"release-manifest:{release_sha256}"
    connection.execute(
        "insert into staging.contract_objects (contract_id, contract_kind, content_sha256, payload) "
        "values (%s, 'release_manifest', %s, %s) on conflict (contract_id) do nothing",
        (release_manifest_id, release_sha256, psycopg.types.json.Jsonb(_RELEASE_PAYLOAD)),
    )
    run_plan = {"run_id": run.run_id, "release_manifest_id": release_manifest_id}
    connection.execute(
        "insert into raw.production_topt_run_plans (run_id, release_manifest_id, content_sha256, payload) "
        "values (%s, %s, %s, %s) on conflict (run_id) do nothing",
        (run.run_id, release_manifest_id, canonical_sha256(run_plan), psycopg.types.json.Jsonb(run_plan)),
    )

    work_items: list[CaptureWorkItem] = []
    bindings: dict[str, ObligationBinding] = {}
    for ordinal, obligation in enumerate(obligations):
        request = _source_request(obligation, ordinal=ordinal, source=source_label)
        work_item = CaptureWorkItem(
            campaign_id=campaign.campaign_id,
            source_request_id=request.source_request_id,
            schedule_policy_id=policy.schedule_policy_id,
        )
        repository.put_obligation(campaign.campaign_id, obligation)
        repository.put_source_request(request)
        repository.put_work_item(work_item, policy.retry)
        repository.put_binding(
            CaptureObligationWorkBinding(obligation_id=obligation.obligation_id, work_item_id=work_item.work_item_id)
        )
        work_items.append(work_item)
        bindings[work_item.work_item_id] = ObligationBinding(
            ordinal=ordinal, obligation=obligation, source_request=request
        )

    return PlannedRun(
        run_id=run.run_id,
        release_manifest_id=release_manifest_id,
        cutoff=cutoff,
        timeline=CaptureTimeline.for_partition(cutoff, partition),
        work_items=tuple(work_items),
        bindings=bindings,
        coordinates=coordinates,
        source_label=source_label,
        retry=policy.retry,
    )


def predecessor_ciks(connection: psycopg.Connection[Any], listing_ids: Sequence[str]) -> dict[str, int]:
    """#496: each listing's most recent successfully-parsed company-facts CIK,
    from OUR OWN capture lineage — the registry the predecessor-CIK fallback
    consults when the index-mapped CIK's taxonomy is empty (post-reorganization
    holdco). "Successfully parsed" means the observation's payload carried a
    revenue value; the lineage join runs entirely on archived, immutable rows.
    """
    rows = connection.execute(
        """
        select distinct on (o.subject_id) o.subject_id, v.source_record_id
        from staging.capture_normalized_observations o
        join staging.capture_observation_payloads p on p.observation_id = o.observation_id
        join raw.capture_source_vintages v on v.source_vintage_id = o.source_vintage_id
        where o.subject_id = any(%s)
          and o.semantic_type = 'financial-fact'
          and p.normalized_payload->>'revenue' is not null
          and v.source_record_id like 'companyfacts:CIK%%'
        order by o.subject_id, o.knowable_at desc
        """,
        (list(listing_ids),),
    ).fetchall()
    resolved: dict[str, int] = {}
    for subject_id, record_id in rows:
        digits = record_id.removeprefix("companyfacts:CIK")
        if digits.isdigit():
            resolved[subject_id] = int(digits)
    return resolved


def build_routes(plan: PlannedRun, connection: psycopg.Connection[Any] | None = None) -> dict[str, SourceFetchPort]:
    """Resolve every planned work item to the adapter that owns its semantic.

    Source-facing resolution (CIKs, issuer classification) happens once here, so no
    adapter re-derives it per cell and the generic executor never sees it at all.
    """
    cutoff_date = plan.cutoff.astimezone(UTC).date()
    tickers = {coordinate[3] for coordinate in plan.coordinates.values()}
    index = sec.ticker_cik_index()
    cik_by_ticker = {ticker: index[_sec_ticker(ticker)] for ticker in sorted(tickers) if _sec_ticker(ticker) in index}
    missing = sorted(tickers - set(cik_by_ticker))
    if missing:
        raise LookupError(f"SEC ticker mapping does not cover: {', '.join(missing)}")
    branches = resolve_operating_branches(cik_by_ticker)
    listing_ids = [coordinate[2] for coordinate in plan.coordinates.values()]
    predecessors = predecessor_ciks(connection, listing_ids) if connection is not None else {}

    price_targets: dict[str, MarketPriceTarget] = {}
    sec_targets: dict[str, SecTarget] = {}
    release_targets: dict[str, ReleaseDerivedRecord] = {}
    for work_item_id, binding in plan.bindings.items():
        semantic_type = binding.obligation.capture_requirement_id.removesuffix(":v1")
        issuer_id, instrument_id, listing_id, ticker = plan.coordinates[binding.obligation.subject.id]
        if semantic_type == "market-price":
            price_targets[work_item_id] = MarketPriceTarget(
                symbol=ticker,
                cutoff=cutoff_date,
                issuer_id=issuer_id,
                instrument_id=instrument_id,
                listing_id=listing_id,
            )
        elif semantic_type == "financial-fact":
            cik = cik_by_ticker[ticker]
            sec_targets[work_item_id] = SecTarget(
                cik=cik,
                cutoff=cutoff_date,
                issuer_id=issuer_id,
                instrument_id=instrument_id,
                listing_id=listing_id,
                operating_branch=branches.get(cik, OperatingBranch.NON_FINANCIAL),
                # only meaningful when it differs from the mapped CIK — the
                # fallback would otherwise refetch the same empty document.
                predecessor_cik=(
                    predecessors.get(listing_id) if predecessors.get(listing_id) not in (None, cik) else None
                ),
            )
        elif semantic_type in _RELEASE_SEMANTICS:
            release_targets[work_item_id] = ReleaseDerivedRecord(
                semantic_type=semantic_type,
                subject_id=listing_id,
                payload={
                    "issuer_id": issuer_id,
                    "instrument_id": instrument_id,
                    "listing_id": listing_id,
                    "ticker": ticker,
                },
                knowable_at=plan.timeline.partition_start,
            )
        else:  # pragma: no cover - the frozen denominator declares exactly four semantics
            raise ValueError(f"no adapter owns the {semantic_type} semantic")

    second_origin = twelve_data_origin()
    price_adapter = MarketPriceAdapter(
        price_targets,
        yahoo_quote_fetcher,
        corroborating_origins=() if second_origin is None else (second_origin,),
    )
    financial_adapter = SecFinancialFactAdapter(
        sec_targets,
        sec_financial_fetcher,
        headcount_extractor=StopgapHeadcountExtractor(headcounts_by_cik(cik_by_ticker)),
    )
    release_adapter = ReleaseDerivedAdapter(release_targets, cutoff=cutoff_date)

    routes: dict[str, SourceFetchPort] = {}
    routes.update(dict.fromkeys(price_targets, price_adapter))
    routes.update(dict.fromkeys(sec_targets, financial_adapter))
    routes.update(dict.fromkeys(release_targets, release_adapter))
    return routes


def run_topt_pipeline(
    connection: psycopg.Connection[Any],
    *,
    cutoff: datetime,
    version: str,
    writer: EvidenceGraphWriter | None = None,
) -> ToptPipelineResult:
    """Capture → freeze → materialize → quality report, in the caller's transaction."""
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError("cutoff must be timezone-aware")

    plan = plan_and_persist(connection, cutoff=cutoff, version=version)
    sink = PostgresCaptureControlSink(
        connection,
        plan.bindings,
        source_label=plan.source_label,
        timeline=plan.timeline,
        retry=plan.retry,
    )
    report = run_topt_capture(
        plan.run_id,
        plan.work_items,
        build_routes(plan, connection),
        writer or PostgresEvidenceGraphRepository(connection),
        sink=sink,
        cutoff=cutoff,
        recorded_at=cutoff,
        max_attempts=_MAX_ATTEMPTS,
    )
    if report.halted:
        raise RuntimeError(f"capture run {plan.run_id} halted on {report.halt_reason}")

    status = PostgresCaptureControlRepository(connection).status(plan.run_id)
    if not status.complete or status.success_count != status.obligation_count:
        raise RuntimeError(f"capture run {plan.run_id} did not terminally resolve every obligation: {status}")

    core = PostgresToptCoreRepository(connection)
    snapshot = core.freeze_snapshot(run_id=plan.run_id, release_manifest_id=plan.release_manifest_id)
    results = core.materialize(snapshot, gppe_definition=GppeV0Definition(risk_free_rate=_RISK_FREE_RATE))

    graded = quality_report.build_report(connection, plan.run_id)
    return ToptPipelineResult(
        run_id=plan.run_id,
        release_manifest_id=plan.release_manifest_id,
        core_result_count=len(results),
        quality_report_id=quality_report.persist(connection, graded),
        quality=graded,
    )


__all__ = [
    "PlannedRun",
    "ToptPipelineResult",
    "build_routes",
    "live_version_for",
    "plan_and_persist",
    "run_topt_pipeline",
]
