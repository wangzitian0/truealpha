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

Degradation is recorded, not erased (#538). `mart.topt_capture_status` is a view over
`raw.capture_*`, and the whole tick is one transaction (`dagster_defs.py`), so the bare
raise this replaced took the run's own evidence down with it: 210 recorded runs across
both environments, every one `84/84`, `failed_count` never once non-zero — not because
capture never degraded but because a degraded run was rolled back. A degraded run now
commits its record, its per-obligation terminal states and a quality report that states
the shortfall, and only then fails the tick. Removing that fail is deliberately NOT part
of this change and waits on the pointer gate (#536): until the pointer refuses to advance
on a shortfall, a partial run reaching `freeze_snapshot` would silently change the
denominator Production serves.

Committing settles the tick's identity, which is the price of recording it: capture-control
rows are append-only and one obligation holds at most one terminal result, so re-running the
same `(cutoff, version)` cannot overwrite a recorded outcome with a better one. A replay of a
recorded degraded tick therefore fails with the reason already on file rather than
re-capturing; recovery is the next tick (or an explicit new `executed_at`), not a rewrite.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import partial
from typing import Any, NoReturn

import psycopg
from factors.production_topt import GppeV0Definition, OperatingBranch
from truealpha_contracts import (
    BitemporalStamp,
    CaptureEnvironment,
    EvidenceGraphWriter,
    EvidenceNode,
    EvidenceNodeKind,
    EvidenceNodeRef,
    ObligationReasonCode,
    canonical_sha256,
)
from truealpha_contracts.capture_control import (
    CaptureListObligation,
    CaptureObligationWorkBinding,
)
from truealpha_contracts.datahub import (
    CaptureCampaign,
    CaptureRun,
    CaptureSchedulePolicy,
    CaptureWorkItem,
    FetchAttemptOutcome,
    ListObligationResult,
    ObligationTerminalState,
    RetryPolicy,
    SourceRequest,
)

from data_engine.datahub import quality_report
from data_engine.datahub.control_plane import AttemptLedger, expand_obligations, replay_retry_policy
from data_engine.datahub.evidence_graph_repository import PostgresEvidenceGraphRepository
from data_engine.datahub.medium_replay import frozen_topt_list_version
from data_engine.datahub.production_topt.capture_orchestration import run_topt_capture
from data_engine.datahub.production_topt.concept_mapping import resolve_ruleset
from data_engine.datahub.production_topt.executor import SourceFetchPort
from data_engine.datahub.production_topt.headcount import PostgresHeadcountExtractor
from data_engine.datahub.production_topt.issuer_registry import resolve_issuer_classifications
from data_engine.datahub.production_topt.market_price_adapter import (
    MarketPriceAdapter,
    MarketPriceTarget,
    last_settled_session_date,
    yahoo_quote_fetcher,
)
from data_engine.datahub.production_topt.materialization import PostgresToptCoreRepository
from data_engine.datahub.production_topt.parser_identity import MAPPING_VERSION
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
from data_engine.datahub.production_topt.universe_corpus import corpus_list_version
from data_engine.datahub.repository import PostgresCaptureControlRepository, ToptCaptureStatus
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


class CaptureNotPublishableError(RuntimeError):
    """A run that degraded: durably recorded, then deliberately not published (#538).

    Raised only AFTER the run, its per-obligation terminal states and its quality
    report have been committed, so the tick still fails exactly as it did before —
    nothing is frozen, nothing is materialized, and `dagster_defs` never reaches the
    pointer advance — while the warehouse keeps the evidence of what went wrong.

    A `RuntimeError` subclass so existing handlers keep catching it, and distinct from
    the bare `RuntimeError` that plan/persistence corruption still raises: this one
    means "some cells did not resolve, and here is the run that says so".
    """

    def __init__(self, *, run_id: str, quality_report_id: str, shortfall: str) -> None:
        super().__init__(f"capture run {run_id} {shortfall}; recorded as {quality_report_id}, not published")
        self.run_id = run_id
        self.quality_report_id = quality_report_id
        self.shortfall = shortfall


def _load_capture_corpus(corpus_filename: str = "corpus.v1.json") -> dict[str, Any]:
    """The frozen capture-control universe corpus, loaded from PACKAGE data so the
    deployed image (site-packages only, no repo tree) can read it. Lazy: importing
    this module must never touch the filesystem (Definitions load hermetically)."""
    from importlib import resources

    raw = resources.files("data_engine.datahub.data").joinpath(corpus_filename).read_bytes()
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
    freshness_windows: Mapping[str, timedelta]
    default_freshness_max_age: timedelta


def plan_and_persist(
    connection: psycopg.Connection[Any],
    *,
    cutoff: datetime,
    version: str,
    corpus_filename: str = "corpus.v1.json",
    label_prefix: str = "production-topt",
    universe_head_kind: str | None = None,
) -> PlannedRun:
    """Freeze the run's scope and persist the dispatch intent; performs no source calls.

    A `universe_head_kind` resolves the universe from the GOVERNED head published
    off the constituent data plane (#539 data-driven universes); the package
    corpus file remains only for the hand-curated TOPT 20.
    """
    if universe_head_kind is not None:
        from data_engine.datahub.production_topt.universe_plane import resolve_universe_corpus

        corpus = resolve_universe_corpus(connection, universe_head_kind)
    else:
        corpus = _load_capture_corpus(corpus_filename)
    denominator = corpus["topt_denominator"]
    coordinates = {
        str(row[2]): (str(row[0]), str(row[1]), str(row[2]), str(row[3])) for row in denominator["instruments"]
    }
    partition = str(denominator["report_date"])
    # The hand-curated TOPT corpus keeps its literal-pinned loader; every
    # self-pinned universe built by scripts/build_universe_corpus.py loads
    # through the generic one (#539 QQQ expansion).
    if "instrument_mapping_sha256" in denominator:
        list_version = corpus_list_version(corpus)
    else:
        list_version = frozen_topt_list_version(corpus)
    source_label = f"{label_prefix}-{version}"

    policy = CaptureSchedulePolicy(
        policy_version=source_label,
        demanded_cadence=timedelta(days=1),
        freshness_max_age=timedelta(days=2),
        # Per-semantic windows (#530 slice 2): the freshest POSSIBLE observation
        # must grade fresh at every scheduled tick.
        # - market-price: a Friday bar is the freshest close at a Sunday or
        #   Monday-holiday 22:15 tick (Fri 00:00 -> Tue 22:15 after a Monday
        #   holiday is ~4.9 days); 5 days admits that and still fails a vendor
        #   serving week-old bars.
        # - financial-fact: knowable_at is the filed date, months old by nature;
        #   730 days aligns with the factor's own period_end staleness bound
        #   (#534) so capture freshness and factor vintage agree on the axis.
        # - identity/universe: release-frozen configuration; a year covers the
        #   universe's own refresh cadence (#67), and the release manifest is the
        #   staleness authority, not the capture.
        semantic_freshness_max_age={
            "market-price": timedelta(days=5),
            "financial-fact": timedelta(days=730),
            "listing-identity": timedelta(days=365),
            "universe-membership": timedelta(days=365),
        },
        provider_availability_cadence="scheduled:v1",
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
        freshness_windows=policy.semantic_freshness_max_age,
        default_freshness_max_age=policy.freshness_max_age,
    )


def predecessor_ciks(
    connection: psycopg.Connection[Any],
    listing_ids: Sequence[str],
    issuer_by_listing: dict[str, str] | None = None,
) -> dict[str, int]:
    """#496: each listing's predecessor company-facts CIK, consulted only when
    the index-mapped CIK's taxonomy is empty (post-reorganization holdco).

    Two registry sources, both our own versioned data, owner-signed rows first:
    1. `staging.issuer_cik_predecessors` — the explicit registry (migration
       0037; every row carries reason + approved_by);
    2. the capture lineage — the most recent vintage whose observation carried
       a revenue value (generic, self-maintaining once the A1 spine has seen
       an issuer parse successfully; empty for issuers that never did).
    """
    resolved: dict[str, int] = {}
    if issuer_by_listing:
        registry = dict(
            connection.execute(
                "select issuer_id, predecessor_cik from staging.issuer_cik_predecessors where issuer_id = any(%s)",
                (sorted(set(issuer_by_listing.values())),),
            ).fetchall()
        )
        for listing_id, issuer_id in issuer_by_listing.items():
            if issuer_id in registry:
                resolved[listing_id] = int(registry[issuer_id])

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
    for subject_id, record_id in rows:
        digits = record_id.removeprefix("companyfacts:CIK")
        if digits.isdigit():
            resolved.setdefault(subject_id, int(digits))
    return resolved


def build_routes(plan: PlannedRun, connection: psycopg.Connection[Any] | None = None) -> dict[str, SourceFetchPort]:
    """Resolve every planned work item to the adapter that owns its semantic.

    Source-facing resolution (CIKs, issuer classification) happens once here, so no
    adapter re-derives it per cell and the generic executor never sees it at all.
    """
    cutoff_date = plan.cutoff.astimezone(UTC).date()
    # Price targets get the last SETTLED session, not the calendar date: a
    # mid-session run must not treat the in-progress bar as a close (#637).
    price_cutoff_date = last_settled_session_date(plan.cutoff)
    tickers = {coordinate[3] for coordinate in plan.coordinates.values()}
    # A plane-published universe already RESOLVED its identities: issuer:cik ids
    # carry the CIK the refresh verified (with the EDGAR fallback for the SEC
    # crosswalk's own holes — AEP is in neither crosswalk file). Trust the
    # governed head first; only LEI-style issuers (the hand-curated TOPT corpus)
    # still consult the crosswalk. The first QQQ run failed HERE, on the same
    # AEP hole the refresh had already worked around — one identity resolution,
    # one place.
    cik_by_ticker: dict[str, int] = {}
    for issuer_id, _, _, ticker in plan.coordinates.values():
        if issuer_id.startswith("issuer:cik:"):
            cik_by_ticker[ticker] = int(issuer_id.removeprefix("issuer:cik:"))
    unresolved = sorted(tickers - set(cik_by_ticker))
    if unresolved:
        index = sec.ticker_cik_index()
        for ticker in unresolved:
            if _sec_ticker(ticker) in index:
                cik_by_ticker[ticker] = index[_sec_ticker(ticker)]
    missing = sorted(tickers - set(cik_by_ticker))
    if missing:
        raise LookupError(f"SEC ticker mapping does not cover: {', '.join(missing)}")
    classifications = resolve_issuer_classifications(cik_by_ticker)
    listing_ids = [coordinate[2] for coordinate in plan.coordinates.values()]
    issuer_by_listing = {coordinate[2]: coordinate[0] for coordinate in plan.coordinates.values()}
    predecessors = predecessor_ciks(connection, listing_ids, issuer_by_listing) if connection is not None else {}

    price_targets: dict[str, MarketPriceTarget] = {}
    sec_targets: dict[str, SecTarget] = {}
    release_targets: dict[str, ReleaseDerivedRecord] = {}
    for work_item_id, binding in plan.bindings.items():
        semantic_type = binding.obligation.capture_requirement_id.removesuffix(":v1")
        issuer_id, instrument_id, listing_id, ticker = plan.coordinates[binding.obligation.subject.id]
        if semantic_type == "market-price":
            price_targets[work_item_id] = MarketPriceTarget(
                symbol=ticker,
                cutoff=price_cutoff_date,
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
                operating_branch=(
                    classifications[cik].operating_branch if cik in classifications else OperatingBranch.NON_FINANCIAL
                ),
                # #533: only the industries whose cost of revenue is ~zero may substitute
                # revenue for an untagged gross profit. Absent from the registry means no.
                revenue_proxy_allowed=cik in classifications and classifications[cik].revenue_proxy_allowed,
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
    # Resolved once per run through the governed pointer, so every cell in the run is
    # parsed by ONE ruleset and the observations can name it.
    ruleset = resolve_ruleset(connection)
    financial_adapter = SecFinancialFactAdapter(
        sec_targets,
        partial(sec_financial_fetcher, ruleset=ruleset),
        # No connection means no headcount plane to read, so every issuer resolves
        # `missing_headcount` — an honest gap. Silently substituting a built-in table
        # would put the deployed denominator back in the code, which is what the fact
        # table exists to end (#70).
        headcount_extractor=None if connection is None else PostgresHeadcountExtractor(connection),
        mapping_version=f"{MAPPING_VERSION}+{ruleset.content_sha256[:12]}",
    )
    release_adapter = ReleaseDerivedAdapter(release_targets, cutoff=cutoff_date)

    routes: dict[str, SourceFetchPort] = {}
    routes.update(dict.fromkeys(price_targets, price_adapter))
    routes.update(dict.fromkeys(sec_targets, financial_adapter))
    routes.update(dict.fromkeys(release_targets, release_adapter))
    return routes


def _unresolved_obligations(connection: psycopg.Connection[Any], run_id: str) -> list[dict[str, Any]]:
    """Every planned cell of this run that did not terminally succeed, with its reason.

    A planned obligation the run never reached (the executor halted before it) has no
    terminal state at all; `unreached` keeps that distinguishable from one that did
    resolve and resolved `unavailable`.
    """
    rows = connection.execute(
        """
        select obligation.subject_id, obligation.capture_requirement_id,
               result.terminal_state, result.reason_codes
        from raw.capture_obligations obligation
        left join raw.capture_obligation_results result
               on result.capture_obligation_id = obligation.obligation_id
        where obligation.run_id = %s
          and (result.result_id is null or result.terminal_state <> 'success')
        order by obligation.obligation_id
        """,
        (run_id,),
    ).fetchall()
    return [
        {
            "subject_id": subject_id,
            "capture_requirement_id": requirement,
            "terminal_state": terminal_state or "unreached",
            "reason_codes": sorted(reason_codes or ()),
        }
        for subject_id, requirement, terminal_state, reason_codes in rows
    ]


def _shortfall_text(status: ToptCaptureStatus) -> str:
    """The run's shortfall, derived from the status alone.

    Derived rather than composed from the live run report on purpose: the same status
    must produce the same words, so re-deriving the record for an already-recorded run
    rebuilds a byte-identical report and collapses onto the report row already there.
    """
    return (
        f"terminally resolved {status.success_count} of {status.obligation_count} obligations "
        f"(unchanged {status.unchanged_count}, unavailable {status.unavailable_count}, "
        f"skipped {status.skipped_count}, failed {status.failed_count}, complete {status.complete})"
    )


def _record_and_refuse(
    connection: psycopg.Connection[Any],
    plan: PlannedRun,
    status: ToptCaptureStatus,
    *,
    halt_reason: ObligationReasonCode | None = None,
) -> NoReturn:
    """Commit the degraded run's record and report, then fail the tick (#538).

    The commit IS the deliverable. `mart.topt_capture_status` is a view over
    `raw.capture_*`, so the run's true counts exist only as long as those rows do, and
    the tick is one transaction — which is why the raise this replaced left every one of
    210 recorded runs reading a perfect 84/84. Committing here puts the run, its
    per-obligation terminal states and its quality report beyond the reach of the abort
    that follows.

    It deliberately does not let the run through. `freeze_snapshot` must never
    materialize a partial universe, and the governed pointer must not advance to one;
    both are downstream of the raise, and both stay refused until #536 lands the pointer
    gate that makes removing the raise safe.

    `halt_reason` colours the raised message only. The persisted report stays purely
    status-derived so that re-deriving it produces the same content hash; the halting
    cell's own reason code is already in `unresolved`.
    """
    shortfall = _shortfall_text(status)
    graded = quality_report.build_report(connection, plan.run_id)
    graded["capture_shortfall"] = {
        "reason": shortfall,
        # The two properties the raise protects, asserted in the record itself.
        "materialized": False,
        "published": False,
        "obligation_count": status.obligation_count,
        "terminal_count": status.terminal_count,
        "success_count": status.success_count,
        "unchanged_count": status.unchanged_count,
        "unavailable_count": status.unavailable_count,
        "skipped_count": status.skipped_count,
        "failed_count": status.failed_count,
        "complete": status.complete,
        "unresolved": _unresolved_obligations(connection, plan.run_id),
    }
    report_id = quality_report.persist(connection, graded)
    connection.commit()
    raise CaptureNotPublishableError(
        run_id=plan.run_id,
        quality_report_id=report_id,
        shortfall=shortfall if halt_reason is None else f"halted on {halt_reason.value}; {shortfall}",
    )


# #635: how far back a committed observation may satisfy a new obligation without
# consulting the vendor. Deliberately much narrower than the semantic freshness
# windows: those govern "still fresh to SERVE"; this governs "do not even CHECK the
# vendor", and a change-detecting semantic (a new 10-Q, a replaced constituent)
# must keep its daily look. Twelve hours covers the whole intra-day duplication
# problem (TOPT 22:15 -> QQQ 23:20 -> canary at any hour) with zero staleness risk.
_REUSE_MAX_AGE = timedelta(hours=12)


def _satisfy_from_recent_observations(
    connection: psycopg.Connection[Any], plan: PlannedRun, *, cutoff: datetime
) -> frozenset[str]:
    """Satisfy obligations from observations another run committed within the last
    twelve hours (#635): the 13 TOPT∩QQQ overlap names were fetched once per
    universe per day — same subject, same semantic, same vendor bytes. A satisfied
    obligation gets the source obligation's FULL observation set bound to it (both
    price origins ride along, so reconciliation is unaffected), a ledger attempt
    finished UNCHANGED with the reused primary vintage, and its work item never
    reaches a vendor. Only #628-committed evidence qualifies by construction —
    an uncommitted capture is invisible to this query.
    """
    rows = connection.execute(
        """
        with mine as (
            select ob.obligation_id, ob.subject_kind, ob.subject_id,
                   regexp_replace(ob.capture_requirement_id, ':v1$', '') as semantic_type
            from raw.capture_obligations ob where ob.run_id = %(run_id)s
              -- Release-derived semantics never ride reuse: their payload IS the
              -- source run's identity, keyed by THAT run's corpus. Reusing them
              -- imported TOPT's LEI-keyed issuers into QQQ/canary mart rows for
              -- eight days against their own CIK-keyed governed heads — caught by
              -- the canary's exact-issuer oracles (#684). Deriving them fresh
              -- costs no vendor call, so exclusion is pure correctness.
              and not (regexp_replace(ob.capture_requirement_id, ':v1$', '') = any(%(release_semantics)s))
        ), anchors as (
            select m.obligation_id as target_obligation_id,
                   m.semantic_type,
                   src_ob.obligation_id as source_obligation_id,
                   o.observation_id as anchor_observation_id,
                   o.source_vintage_id as anchor_vintage_id,
                   o.knowable_at,
                   row_number() over (
                       partition by m.obligation_id
                       order by done.completed_at desc, o.knowable_at desc, o.observation_id desc
                   ) as rn
            from mine m
            join raw.capture_obligations src_ob
              on src_ob.subject_kind = m.subject_kind and src_ob.subject_id = m.subject_id
             and regexp_replace(src_ob.capture_requirement_id, ':v1$', '') = m.semantic_type
             and src_ob.run_id <> %(run_id)s
            join raw.capture_obligation_results done
              on done.capture_obligation_id = src_ob.obligation_id
             and done.terminal_state in ('success', 'unchanged')
             and done.completed_at > %(cutoff)s - %(max_age)s::interval
             and done.completed_at <= %(cutoff)s
            join raw.capture_attempt_results attempt on attempt.attempt_id = done.final_attempt_id
            join staging.capture_normalized_observations o
              on o.source_vintage_id = coalesce(attempt.source_vintage_id, attempt.reused_source_vintage_id)
             and o.subject_kind = m.subject_kind
             and o.subject_id = m.subject_id
             and o.semantic_type = m.semantic_type
             and o.knowable_at <= %(cutoff)s
        )
        select a.target_obligation_id, a.semantic_type, a.anchor_vintage_id, a.knowable_at,
               bound.observation_id
        from anchors a
        join staging.capture_observation_obligations link
          on link.capture_obligation_id = a.source_obligation_id
        join staging.capture_normalized_observations bound on bound.observation_id = link.observation_id
        where a.rn = 1
          -- Look-ahead guard on the whole bound set, not just the anchor: nothing
          -- knowable after THIS run's cutoff may ride into it (review on #664).
          and bound.knowable_at <= %(cutoff)s
        """,
        {
            "run_id": plan.run_id,
            "cutoff": cutoff,
            "max_age": _REUSE_MAX_AGE,
            "release_semantics": sorted(_RELEASE_SEMANTICS),
        },
    ).fetchall()

    settled = last_settled_session_date(cutoff)
    by_target: dict[str, dict[str, Any]] = {}
    for target_id, semantic_type, anchor_vintage, knowable_at, bound_observation in rows:
        entry = by_target.setdefault(
            target_id,
            {"semantic": semantic_type, "vintage": anchor_vintage, "knowable_at": knowable_at, "observations": []},
        )
        entry["observations"].append(bound_observation)

    repository = PostgresCaptureControlRepository(connection)
    satisfied: set[str] = set()
    for work_item_id, binding in plan.bindings.items():
        candidate = by_target.get(binding.obligation.obligation_id)
        if candidate is None:
            continue
        entry = candidate
        # A reused price must BE the settled session's close — anything older is a
        # fetch, not a reuse (the vendor may simply have published since).
        if entry["semantic"] == "market-price" and entry["knowable_at"].date() != settled:
            continue
        ledger = AttemptLedger(work_item_id=work_item_id, retry_policy=plan.retry)
        attempt = ledger.start(started_at=cutoff)
        attempt_result = ledger.finish(
            attempt=attempt,
            completed_at=cutoff,
            outcome=FetchAttemptOutcome.UNCHANGED,
            status_code=None,
            source_vintage_id=None,
            reused_source_vintage_id=entry["vintage"],
        )
        repository.put_attempt(attempt)
        repository.put_attempt_result(attempt_result)
        for observation_id in entry["observations"]:
            repository.bind_observation(binding.obligation.obligation_id, observation_id)
        repository.put_obligation_result(
            binding.obligation.obligation_id,
            ListObligationResult(
                # The result model carries the INNER list-obligation identity; the
                # repository keys on the outer capture-list identity (same split the
                # capture executor makes).
                obligation_id=binding.obligation.obligation.obligation_id,
                terminal_state=ObligationTerminalState.UNCHANGED,
                completed_at=cutoff,
                final_attempt_id=attempt.attempt_id,
                reason_codes=("unchanged",),
            ),
        )
        satisfied.add(work_item_id)
    return frozenset(satisfied)


def run_topt_pipeline(
    connection: psycopg.Connection[Any],
    *,
    cutoff: datetime,
    version: str,
    writer: EvidenceGraphWriter | None = None,
    corpus_filename: str = "corpus.v1.json",
    label_prefix: str = "production-topt",
    universe_head_kind: str | None = None,
) -> ToptPipelineResult:
    """Capture, then commit; freeze → materialize → report in the caller's transaction.

    Two deliberate commits of our own (#538, #628), one principle: evidence outlives
    failures. A run that DEGRADES commits its record before raising (#538). A run that
    captures COMPLETELY commits the capture before freezing (#628) — a publish-side
    failure then costs a retry of the cheap half, not the 39-minute vendor half, and
    the retry RESUMES (re-freezing an existing run is idempotent). Consumers never see
    a half-published run either way: reads resolve through the governed pointer, which
    only advances in the caller's publish transaction.
    """
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError("cutoff must be timezone-aware")

    plan = plan_and_persist(
        connection,
        cutoff=cutoff,
        version=version,
        corpus_filename=corpus_filename,
        label_prefix=label_prefix,
        universe_head_kind=universe_head_kind,
    )
    status = PostgresCaptureControlRepository(connection).status(plan.run_id)
    # #628: a COMPLETE, fully successful capture that already sits committed is the
    # resume case — the tick died (or was killed) between the capture commit below
    # and the publish transaction. Resuming skips the vendor half entirely and
    # re-freezes the existing run, which is idempotent end to end (freeze_snapshot
    # early-returns on its own snapshot; materialize and the report persist
    # conflict-tolerantly). This strengthens the idempotent-retry property the
    # composition always claimed for complete replays: same identities, zero new
    # vendor calls. Refusal stays for every OTHER recorded shape — those are
    # #538's degraded histories, and their outcomes are append-only.
    resumed = (
        status.terminal_count > 0
        and status.complete
        and status.success_count + status.unchanged_count == status.obligation_count
    )
    if status.terminal_count > 0 and not resumed:
        _record_and_refuse(connection, plan, status)

    sink = PostgresCaptureControlSink(
        connection,
        plan.bindings,
        source_label=plan.source_label,
        timeline=plan.timeline,
        retry=plan.retry,
        freshness_windows=plan.freshness_windows,
        default_freshness_max_age=plan.default_freshness_max_age,
    )
    if not resumed:
        satisfied = _satisfy_from_recent_observations(connection, plan, cutoff=cutoff)
        live_items = [item for item in plan.work_items if item.work_item_id not in satisfied]
        if live_items:
            # Route construction is itself vendor work (live CIK resolution); a run
            # fully satisfied by reuse never builds routes at all.
            report = run_topt_capture(
                plan.run_id,
                live_items,
                build_routes(plan, connection),
                writer or PostgresEvidenceGraphRepository(connection),
                sink=sink,
                cutoff=cutoff,
                recorded_at=cutoff,
                max_attempts=_MAX_ATTEMPTS,
            )
            if report.halted:
                status = PostgresCaptureControlRepository(connection).status(plan.run_id)
                _record_and_refuse(connection, plan, status, halt_reason=report.halt_reason)
        else:
            # The capture executor is what appends the run's evidence node; a FULLY
            # reused run skips the executor, and the publish transaction later binds
            # the release manifest to that node — which must exist (the first live
            # full-reuse canary failed exactly here: FK on the bound_to edge).
            run_ref = EvidenceNodeRef(kind=EvidenceNodeKind.CAPTURE_RUN, node_id=plan.run_id)
            run_stamp = BitemporalStamp(valid_from=cutoff.date(), transaction_time=cutoff, recorded_at=cutoff)
            (writer or PostgresEvidenceGraphRepository(connection)).append(
                [EvidenceNode(ref=run_ref, content_sha256=run_ref.content_sha256, stamp=run_stamp)],
                [],
            )
        status = PostgresCaptureControlRepository(connection).status(plan.run_id)
        if not status.complete or status.success_count + status.unchanged_count != status.obligation_count:
            # UNCHANGED is a resolution, not a shortfall — the freeze gate and the
            # 0042 trigger have always accepted success+unchanged; #635's cross-run
            # reuse terminals made composition's stricter predicate visible.
            _record_and_refuse(connection, plan, status)
        # #628: the capture is durable the moment it is complete and successful. A
        # freeze/materialize/report failure after this line loses NOTHING — the
        # 39-minute QQQ captures that vanished with their transactions (take-2,
        # take-3, the first scheduled run) are the incident class this closes.
        # Consumer atomicity is unaffected: reads resolve through the governed
        # pointer, which only advances in the caller's publish transaction.
        connection.commit()

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
    "CaptureNotPublishableError",
    "PlannedRun",
    "ToptPipelineResult",
    "build_routes",
    "live_version_for",
    "plan_and_persist",
    "run_topt_pipeline",
]
