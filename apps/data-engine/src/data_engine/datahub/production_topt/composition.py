"""Deployed TOPT capture composition root (#171 A3, ADR A1)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, NoReturn

import psycopg
from factors.production_topt import GppeV0Definition
from truealpha_contracts.capture_control import (
    CaptureListObligation,
    CaptureObligationWorkBinding,
)
from truealpha_contracts.common import CaptureEnvironment, canonical_sha256
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
from truealpha_contracts.environment_contract import load_environment_contract
from truealpha_contracts.evidence_graph import (
    BitemporalStamp,
    EvidenceGraphWriter,
    EvidenceNode,
    EvidenceNodeKind,
    EvidenceNodeRef,
)
from truealpha_contracts.obligation_reason_codes import ObligationReasonCode
from truealpha_contracts.universes import id_prefix_like, schedule_name

from data_engine.datahub import quality_report
from data_engine.datahub.control_plane import AttemptLedger, expand_obligations, replay_retry_policy
from data_engine.datahub.evidence_graph_repository import PostgresEvidenceGraphRepository
from data_engine.datahub.production_topt.capture_orchestration import run_topt_capture
from data_engine.datahub.production_topt.executor import SourceFetchPort
from data_engine.datahub.production_topt.issuer_registry import UNAVAILABLE_PAYLOAD_KEY
from data_engine.datahub.production_topt.market_price_adapter import last_settled_session_date
from data_engine.datahub.production_topt.materialization import PostgresToptCoreRepository
from data_engine.datahub.production_topt.parser_identity import PARSER_VERSION, PARSER_VERSION_HISTORY
from data_engine.datahub.production_topt.persistence import (
    CaptureTimeline,
    ObligationBinding,
    PostgresCaptureControlSink,
    build_source_request,
)
from data_engine.datahub.production_topt.sec_financial_adapter import (
    predecessor_ciks,  # noqa: F401 — re-exported for callers
)
from data_engine.datahub.production_topt.source_registrations import (
    FRESHNESS_WINDOWS,
    REGISTRATIONS,
    RELEASE_SEMANTICS,
    SOURCE_BY_PARSER,
    RouteCell,
    RouteContext,
    configured_origin_sources,
    last_settled_accession_date,
    registered_semantic_types,
    registration_for,
)
from data_engine.datahub.production_topt.universe_corpus import (
    TOPT_CORPUS_FILENAME,
    corpus_list_version,
    frozen_topt_list_version,
)
from data_engine.datahub.repository import PostgresCaptureControlRepository, ToptCaptureStatus
from data_engine.datahub.risk_free import RiskFreeRate, risk_free_rate_at
from data_engine.release_identity import measure as measure_release_identity

SEMANTIC_TYPES = registered_semantic_types()
_RELEASE_SEMANTICS = RELEASE_SEMANTICS
_MAX_ATTEMPTS = 3
FORCED_VERSION_SUFFIX = "-forced"
_REUSE_MAX_AGE = timedelta(hours=12)


def live_version_for(cutoff: datetime) -> str:
    return f"live-{cutoff.astimezone(UTC):%Y%m%dT%H%M}"


def forced_capture_version(version: str) -> str:
    return version if version.endswith(FORCED_VERSION_SUFFIX) else f"{version}{FORCED_VERSION_SUFFIX}"


@dataclass(frozen=True)
class ToptPipelineResult:
    run_id: str
    release_manifest_id: str
    core_result_count: int
    quality_report_id: str
    quality: dict[str, Any]
    risk_free_rate: RiskFreeRate
    forced_fetch: bool = False


class CaptureNotPublishableError(RuntimeError):
    """Raised after committing a degraded capture run and its quality report (#538)."""

    def __init__(self, *, run_id: str, quality_report_id: str, shortfall: str) -> None:
        super().__init__(f"capture run {run_id} {shortfall}; recorded as {quality_report_id}, not published")
        self.run_id = run_id
        self.quality_report_id = quality_report_id
        self.shortfall = shortfall


def _load_capture_corpus(corpus_filename: str = TOPT_CORPUS_FILENAME) -> dict[str, Any]:
    from importlib import resources

    return json.loads(resources.files("data_engine.datahub.data").joinpath(corpus_filename).read_bytes())


def _source_request(obligation: CaptureListObligation, *, ordinal: int, source: str) -> SourceRequest:
    return build_source_request(obligation, ordinal=ordinal, source=source)


@dataclass(frozen=True)
class PlannedRun:
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
    universe_published_at: datetime | None
    forced_fetch: bool = False
    drill: Any | None = None
    raw_coordinates: dict[str, tuple[str, str, str, str]] | None = None
    universe_id: str | None = None


def ensure_environment_identity(
    connection: psycopg.Connection[Any],
    environment: str | CaptureEnvironment,
) -> None:
    """Declare the authoritative environment identity in `mart.environment_identity` (#756, #1412)."""
    env_str = environment.value if isinstance(environment, CaptureEnvironment) else str(environment)
    connection.execute(
        """
        insert into mart.environment_identity (singleton, environment, declared_at)
        select true, %(environment)s, clock_timestamp()
        where not exists (select 1 from mart.environment_identity where environment = %(environment)s)
        on conflict (singleton) do update set
            environment = excluded.environment,
            declared_at = excluded.declared_at
        """,
        {"environment": env_str},
    )


def ensure_served_head_artifacts(connection: psycopg.Connection[Any]) -> int:
    """Register every contract universe head in `mart.served_artifact` (#1522)."""
    contract = load_environment_contract()
    wanted = {
        f"head:{key}": (
            "capture",
            schedule_name(key),
            id_prefix_like(key),
            "daily",
            "operators" if universe.is_sentinel else "consumers",
            True,
        )
        for key, universe in contract.universes.items()
    }
    present = {
        str(row[0])
        for row in connection.execute(
            "select artifact_key from mart.served_artifact where artifact_key = any(%s)", (list(wanted),)
        ).fetchall()
    }
    missing = {artifact_key: row for artifact_key, row in wanted.items() if artifact_key not in present}
    for artifact_key, row in missing.items():
        connection.execute(
            "insert into mart.served_artifact (artifact_key, lane, schedule_name, universe_like, family, served_to, wired) "
            "values (%s, %s, %s, %s, %s, %s, %s) on conflict (artifact_key) do nothing",
            (artifact_key, *row),
        )
    return len(missing)


def plan_and_persist(
    connection: psycopg.Connection[Any],
    *,
    cutoff: datetime,
    version: str,
    corpus_filename: str = TOPT_CORPUS_FILENAME,
    label_prefix: str = "production-topt",
    universe_head_kind: str | None = None,
    force_fetch: bool = False,
    drill: Any | None = None,
    environment: CaptureEnvironment | None = None,
) -> PlannedRun:
    """Freeze scope and persist capture-control dispatch intent without calling external vendors."""
    from data_engine.config import settings

    if environment is None:
        environment = settings.capture_environment

    ensure_environment_identity(connection, environment)
    if universe_head_kind is not None:
        from data_engine.datahub.production_topt.universe_plane import resolve_universe_corpus

        corpus = resolve_universe_corpus(connection, universe_head_kind)
    else:
        corpus = _load_capture_corpus(corpus_filename)
    denominator = corpus["topt_denominator"]
    published_at_raw = corpus.get("published_at")
    universe_published_at = datetime.fromisoformat(published_at_raw) if published_at_raw else None
    raw_coordinates = {
        str(row[2]): (str(row[0]), str(row[1]), str(row[2]), str(row[3])) for row in denominator["instruments"]
    }
    as_of = (
        date.fromisoformat(str(denominator["report_date"]))
        if "report_date" in denominator
        else cutoff.astimezone(UTC).date()
    )
    from data_engine.datahub.resolve_coordinates import resolve_coordinates

    with connection.transaction():
        coordinates = resolve_coordinates(connection, denominator["instruments"], as_of=as_of, known_at=cutoff)
    partition = str(denominator["report_date"])
    list_version = (
        corpus_list_version(corpus) if "instrument_mapping_sha256" in denominator else frozen_topt_list_version(corpus)
    )
    source_label = f"{label_prefix}-{version}"

    policy = CaptureSchedulePolicy(
        policy_version=source_label,
        demanded_cadence=timedelta(days=1),
        freshness_max_age=timedelta(days=2),
        semantic_freshness_max_age=FRESHNESS_WINDOWS,
        provider_availability_cadence="scheduled:v1",
        retry=replay_retry_policy(_MAX_ATTEMPTS),
    )
    campaign = CaptureCampaign(
        campaign_policy_id=f"capture-policy:{source_label}",
        environment=environment,
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

    identity = measure_release_identity()
    release_manifest_id = identity.manifest_id
    connection.execute(
        "insert into staging.contract_objects (contract_id, contract_kind, content_sha256, payload) "
        "values (%s, 'release_manifest', %s, %s) on conflict (contract_id) do nothing",
        (release_manifest_id, identity.content_sha256, psycopg.types.json.Jsonb(identity.payload)),
    )
    run_plan = {
        "run_id": run.run_id,
        "release_manifest_id": release_manifest_id,
        "data_engine_git_sha": settings.git_commit_sha or "unknown",
        "data_engine_image_digest": settings.data_engine_image_digest or "unknown",
        "declared_release_manifest_id": settings.release_manifest_id or "unknown",
        "capture_approved_by": settings.capture_approved_by or "unknown",
        "forced_fetch": force_fetch,
    }
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
        universe_published_at=universe_published_at,
        forced_fetch=force_fetch,
        drill=drill,
        raw_coordinates=raw_coordinates,
        universe_id=list_version.universe.universe_id,
    )


def build_routes(plan: PlannedRun, connection: psycopg.Connection[Any] | None = None) -> dict[str, SourceFetchPort]:
    """Resolve planned work items to the registered `SourceFetchPort` adapters."""
    context = RouteContext(
        cutoff=plan.cutoff,
        cutoff_date=last_settled_accession_date(plan.cutoff),
        price_cutoff_date=last_settled_session_date(plan.cutoff),
        partition_start=plan.timeline.partition_start,
        universe_published_at=plan.universe_published_at,
        coordinates=plan.coordinates,
        connection=connection,
        drill=plan.drill,
        raw_coordinates=plan.raw_coordinates,
        universe_id=plan.universe_id,
    )
    cells_by_source: dict[str, list[RouteCell]] = {}
    for work_item_id, binding in plan.bindings.items():
        semantic_type = binding.obligation.capture_requirement_id.removesuffix(":v1")
        registration = registration_for(semantic_type)
        issuer_id, instrument_id, listing_id, ticker = plan.coordinates[binding.obligation.subject.id]
        cells_by_source.setdefault(registration.source_id, []).append(
            RouteCell(
                work_item_id=work_item_id,
                semantic_type=semantic_type,
                issuer_id=issuer_id,
                instrument_id=instrument_id,
                listing_id=listing_id,
                ticker=ticker,
            )
        )
    routes: dict[str, SourceFetchPort] = {}
    for registration in REGISTRATIONS:
        cells = cells_by_source.pop(registration.source_id, [])
        if cells:
            adapter = registration.resolve_route_builder()(context, cells)
            routes.update(dict.fromkeys((cell.work_item_id for cell in cells), adapter))
    return routes


def _unresolved_obligations(connection: psycopg.Connection[Any], run_id: str) -> list[dict[str, Any]]:
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
    shortfall = _shortfall_text(status)
    graded = quality_report.build_report(connection, plan.run_id)
    graded["capture_shortfall"] = {
        "reason": shortfall,
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


def _is_settled_session(knowable_at: datetime, settled: date) -> bool:
    return knowable_at.astimezone(UTC).date() == settled


def _satisfy_from_recent_observations(
    connection: psycopg.Connection[Any], plan: PlannedRun, *, cutoff: datetime
) -> frozenset[str]:
    """Satisfy obligations from committed observations within `_REUSE_MAX_AGE` (#635, #788, #885)."""
    coordinates_param = json.dumps(
        [
            {"subject_id": subject_id, "issuer_id": issuer_id, "instrument_id": instrument_id, "listing_id": listing_id}
            for subject_id, (issuer_id, instrument_id, listing_id, _ticker) in plan.coordinates.items()
        ]
    )
    rows = connection.execute(
        """
        with mine as (
            select ob.obligation_id, ob.subject_kind, ob.subject_id,
                   regexp_replace(ob.capture_requirement_id, ':v1$', '') as semantic_type,
                   coordinate.issuer_id, coordinate.instrument_id, coordinate.listing_id
            from raw.capture_obligations ob
            join jsonb_to_recordset(%(coordinates)s::jsonb)
                 as coordinate(subject_id text, issuer_id text, instrument_id text, listing_id text)
              on coordinate.subject_id = ob.subject_id
             and ob.run_id = %(run_id)s
             and not (regexp_replace(ob.capture_requirement_id, ':v1$', '') = any(%(release_semantics)s::text[]))
        ), anchors as (
            select m.obligation_id as target_obligation_id,
                   m.semantic_type,
                   src_ob.obligation_id as source_obligation_id,
                   o.observation_id as anchor_observation_id,
                   o.source_vintage_id as anchor_vintage_id,
                   o.knowable_at,
                   row_number() over (
                       partition by m.obligation_id
                       order by orig_done.completed_at desc,
                                done.completed_at desc,
                                coalesce((src_plan.payload->>'forced_fetch')::boolean, false) desc,
                                o.knowable_at desc, o.observation_id desc
                   ) as rn
            from mine m
            join raw.capture_obligations src_ob
              on src_ob.subject_kind = m.subject_kind and src_ob.subject_id = m.subject_id
             and regexp_replace(src_ob.capture_requirement_id, ':v1$', '') = m.semantic_type
             and src_ob.run_id <> %(run_id)s
            left join raw.production_topt_run_plans src_plan on src_plan.run_id = src_ob.run_id
            join raw.capture_obligation_results done
              on done.capture_obligation_id = src_ob.obligation_id
             and done.terminal_state in ('success', 'unchanged')
             and done.completed_at <= %(cutoff)s
            join raw.capture_attempt_results attempt on attempt.attempt_id = done.final_attempt_id
            join staging.capture_normalized_observations o
              on o.source_vintage_id = coalesce(attempt.source_vintage_id, attempt.reused_source_vintage_id)
             and o.subject_kind = m.subject_kind
             and o.subject_id = m.subject_id
             and o.semantic_type = m.semantic_type
             and o.knowable_at <= %(cutoff)s
             and o.parser_version = %(parser_version)s
             and (o.valid_from at time zone 'UTC')::date <= (%(cutoff)s::timestamptz at time zone 'UTC')::date
             and (o.valid_to is null
                  or (o.valid_to at time zone 'UTC')::date >= (%(cutoff)s::timestamptz at time zone 'UTC')::date)
            join raw.capture_obligation_results orig_done
              on orig_done.capture_obligation_id = o.capture_obligation_id
             and orig_done.terminal_state = 'success'
             and orig_done.completed_at > %(cutoff)s - %(max_age)s::interval
             and orig_done.completed_at <= %(cutoff)s
            join staging.capture_observation_payloads anchor_payload
              on anchor_payload.observation_id = o.observation_id
             and anchor_payload.normalized_payload->>'listing_id' = m.listing_id
             and anchor_payload.normalized_payload->>%(unavailable_key)s is distinct from 'true'
        ), bound_set as (
            select a.target_obligation_id, a.semantic_type, a.anchor_vintage_id, a.knowable_at,
                   bound.observation_id,
                   bound.parser_version,
                   coalesce(
                       bound_payload.normalized_payload->>'listing_id' = m.listing_id
                       and bound.knowable_at <= %(cutoff)s
                       and (bound.valid_from at time zone 'UTC')::date
                           <= (%(cutoff)s::timestamptz at time zone 'UTC')::date
                       and (bound.valid_to is null
                            or (bound.valid_to at time zone 'UTC')::date
                               >= (%(cutoff)s::timestamptz at time zone 'UTC')::date),
                       false
                   ) as qualifies
            from anchors a
            join mine m on m.obligation_id = a.target_obligation_id
            join staging.capture_observation_obligations link
              on link.capture_obligation_id = a.source_obligation_id
            join staging.capture_normalized_observations bound on bound.observation_id = link.observation_id
            left join staging.capture_observation_payloads bound_payload
              on bound_payload.observation_id = bound.observation_id
            where a.rn = 1
        )
        select target_obligation_id, semantic_type, anchor_vintage_id, knowable_at,
               array_agg(observation_id order by observation_id) as observations,
               array_agg(parser_version order by observation_id) as parser_versions
        from bound_set
        group by target_obligation_id, semantic_type, anchor_vintage_id, knowable_at
        having bool_and(qualifies)
        """,
        {
            "run_id": plan.run_id,
            "cutoff": cutoff,
            "max_age": _REUSE_MAX_AGE,
            "release_semantics": sorted(_RELEASE_SEMANTICS),
            "coordinates": coordinates_param,
            "parser_version": PARSER_VERSION,
            "unavailable_key": UNAVAILABLE_PAYLOAD_KEY,
        },
    ).fetchall()

    settled = last_settled_session_date(cutoff)
    by_target: dict[str, dict[str, Any]] = {}
    for target_id, semantic_type, anchor_vintage, knowable_at, observations, parser_versions in rows:
        required_sources = configured_origin_sources(semantic_type, universe_id=plan.universe_id)
        present_sources = set()
        disqualified = False
        for pv in parser_versions:
            if pv in PARSER_VERSION_HISTORY or pv == PARSER_VERSION:
                present_sources.add("primary")
            elif pv in SOURCE_BY_PARSER:
                present_sources.add(SOURCE_BY_PARSER[pv][0])
            else:
                disqualified = True
                break
        if disqualified or present_sources != required_sources:
            continue
        by_target[target_id] = {
            "semantic": semantic_type,
            "vintage": anchor_vintage,
            "knowable_at": knowable_at,
            "observations": list(observations),
        }

    repository = PostgresCaptureControlRepository(connection)
    satisfied: set[str] = set()
    for work_item_id, binding in plan.bindings.items():
        entry = by_target.get(binding.obligation.obligation_id)
        if entry is None:
            continue
        if registration_for(entry["semantic"]).session_bound and not _is_settled_session(entry["knowable_at"], settled):
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
    corpus_filename: str = TOPT_CORPUS_FILENAME,
    label_prefix: str = "production-topt",
    universe_head_kind: str | None = None,
    force_fetch: bool = False,
    drill: Any | None = None,
) -> ToptPipelineResult:
    """Capture, commit durable evidence, then freeze, materialize, and grade in the caller's transaction."""
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError("cutoff must be timezone-aware")
    if force_fetch:
        version = forced_capture_version(version)

    plan = plan_and_persist(
        connection,
        cutoff=cutoff,
        version=version,
        corpus_filename=corpus_filename,
        label_prefix=label_prefix,
        universe_head_kind=universe_head_kind,
        force_fetch=force_fetch,
        drill=drill,
    )
    status = PostgresCaptureControlRepository(connection).status(plan.run_id)
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
        coordinates=plan.coordinates,
        raw_coordinates=plan.raw_coordinates,
    )
    if not resumed:
        satisfied = frozenset() if force_fetch else _satisfy_from_recent_observations(connection, plan, cutoff=cutoff)
        live_items = [item for item in plan.work_items if item.work_item_id not in satisfied]
        if live_items:
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
            run_ref = EvidenceNodeRef(kind=EvidenceNodeKind.CAPTURE_RUN, node_id=plan.run_id)
            run_stamp = BitemporalStamp(valid_from=cutoff.date(), transaction_time=cutoff, recorded_at=cutoff)
            (writer or PostgresEvidenceGraphRepository(connection)).append(
                [EvidenceNode(ref=run_ref, content_sha256=run_ref.content_sha256, stamp=run_stamp)],
                [],
            )
        status = PostgresCaptureControlRepository(connection).status(plan.run_id)
        if not status.complete or status.success_count + status.unchanged_count != status.obligation_count:
            _record_and_refuse(connection, plan, status)
        connection.commit()

    rate = risk_free_rate_at(connection, cutoff)
    core = PostgresToptCoreRepository(connection)
    snapshot = core.freeze_snapshot(run_id=plan.run_id, release_manifest_id=plan.release_manifest_id)
    results = core.materialize(snapshot, gppe_definition=GppeV0Definition(risk_free_rate=rate.value))

    graded = quality_report.build_report(connection, plan.run_id)
    return ToptPipelineResult(
        run_id=plan.run_id,
        release_manifest_id=plan.release_manifest_id,
        core_result_count=len(results),
        quality_report_id=quality_report.persist(connection, graded),
        quality=graded,
        risk_free_rate=rate,
        forced_fetch=plan.forced_fetch,
    )


__all__ = [
    "CaptureNotPublishableError",
    "FORCED_VERSION_SUFFIX",
    "PlannedRun",
    "ToptPipelineResult",
    "build_routes",
    "ensure_environment_identity",
    "ensure_served_head_artifacts",
    "forced_capture_version",
    "live_version_for",
    "plan_and_persist",
    "run_topt_pipeline",
]
