"""Administrator operational, DataHub, funnel, macro, universe, and trigger services."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import psycopg
from pydantic import BaseModel, Field
from truealpha_contracts.environment_contract import EnvironmentContract, load_environment_contract
from truealpha_contracts.universes import id_prefix_like, job_name, key_of_lane, keys, lane_name, universe_with_role
from truealpha_runtime.access import AccessContext
from truealpha_runtime.research_report_mart import MetricNodeValueReport, PostgresMetricNodeValueRepository
from truealpha_runtime.strategy_run_postgres import PostgresStrategyRunRepository

from llm_service.config import settings
from llm_service.services._db import with_app_runtime, with_mart_readonly, with_ops_reader
from llm_service.services.research_service import DASHBOARD_STRATEGY_ID, SERVED_HEAD_SQL

ACCEPTED_SERVICE_OBJECTIVES = {"minimum_availability": 0.95, "minimum_corroborated_share": 0.95}
SWEEP_RUN_SOURCE_REGEX_SOURCE = "-[0-9]{8}T[0-9]{4}$"
REGISTERED_RAW_SOURCE_FILTER = f"not (source ~ '{SWEEP_RUN_SOURCE_REGEX_SOURCE}')"
UTC_DAY_START_SQL = "(date_trunc('day', now() at time zone 'utc') at time zone 'utc')"
NON_CANARY_UNIVERSE_WHERE = "s.universe_id not like all (%s::text[])"
LEDGER_COVERAGE_DAYS = 8
PIPELINE_WINDOW_DAYS = 7
PIPELINE_LISTED_DAYS = 100
_UNDEFINED_TABLE = "42P01"
_DEFAULT_POINTER_LIMIT_HOURS = 48.0


def governed_latest_per_universe_sql(table: str, extra_cols: str = "") -> str:
    return f"""
  select distinct on (universe_id) universe_id{extra_cols}, cutoff::text, created_at::text, payload
  from {table} r
  where universe_id in (select universe_id from mart.served_head)
  order by universe_id,
           exists (
             select 1 from mart.served_head h
             where h.universe_id = r.universe_id
               and coalesce(h.run_id, h.head_run_id) = r.run_id
           ) desc,
           created_at desc
"""


QUESTION_COVERAGE_SQL = governed_latest_per_universe_sql("mart.question_coverage_report")
CONFIDENCE_REPORT_SQL = governed_latest_per_universe_sql("mart.datahub_confidence_report", ", run_id")
JOBS_SQL = """
  select r.pipeline_name as job,
         count(*) filter (where r.status = 'SUCCESS' and r.create_timestamp >= w.since)::int as successes,
         count(*) filter (where r.status = 'FAILURE' and r.create_timestamp >= w.since)::int as failures,
         max(r.create_timestamp) filter (where r.status = 'SUCCESS') as last_success,
         max(r.create_timestamp) filter (where r.status = 'FAILURE') as last_failure,
         (array_agg(r.run_id order by r.create_timestamp desc) filter (where r.status = 'FAILURE'))[1]
           as last_failure_run_id,
         max(case when r.status = 'SUCCESS' and r.end_time is not null and r.start_time is not null and r.end_time >= r.start_time then (r.end_time - r.start_time) else null end)
           filter (where r.create_timestamp >= (now() at time zone 'utc') - make_interval(days => %(p3)s::int)) as longest_success_seconds
  from dagster.runs r
  cross join (select (now() at time zone 'utc') - make_interval(days => %(p1)s::int) as since) w
  where r.status in ('SUCCESS', 'FAILURE')
  group by r.pipeline_name
  having max(r.create_timestamp) >= (now() at time zone 'utc') - make_interval(days => %(p2)s::int)
  order by r.pipeline_name
"""
TIMEOUTS_SQL = """
  select r.pipeline_name as job, count(*)::int as count
  from dagster.runs r
  join dagster.event_logs e on e.run_id = r.run_id
  where r.status = 'FAILURE'
    and r.create_timestamp >= (now() at time zone 'utc') - make_interval(days => %(p1)s::int)
    and e.dagster_event_type = 'PIPELINE_FAILURE'
    and e.event like '%%Run timed out due to taking longer than%%'
  group by r.pipeline_name
"""
FAILURE_EVENTS_SQL = """
  select distinct on (run_id) run_id, dagster_event_type, event
  from dagster.event_logs
  where run_id = any(%(p1)s::text[]) and dagster_event_type in ('STEP_FAILURE', 'PIPELINE_FAILURE')
  order by run_id, id
"""
RELEASE_START_SQL = """
  select min(i.created_at) as since
  from mart.data_engine_identity i
  where i.git_sha = %(p1)s
    and i.created_at > coalesce(
      (select max(j.created_at) from mart.data_engine_identity j where j.git_sha <> %(p1)s and j.git_sha <> 'unknown'),
      '-infinity'::timestamptz
    )
"""
ORACLE_OUTCOMES_SQL = """
  with deduped as (
    select distinct on (h.universe_id, o.observation_id, o.subject_id, o.field)
      h.universe_id, o.field, o.result, o.reason, o.compared_at
    from mart.served_head h
    join mart.oracle_outcomes o on o.run_id = coalesce(h.run_id, h.head_run_id)
    order by h.universe_id, o.observation_id, o.subject_id, o.field, o.compared_at desc, o.recorded_at desc, o.outcome_id desc
  ),
  rolled as (
    select universe_id, field,
      (count(*) filter (where result in ('agree', 'disagree')))::int as compared,
      (count(*) filter (where result = 'agree'))::int as agree,
      (count(*) filter (where result = 'disagree'))::int as disagree,
      (count(*) filter (where result = 'not_comparable'))::int as not_comparable,
      (count(*) filter (where result = 'not_comparable' and reason = 'no_served_value'))::int as no_served_value_count,
      to_char(max(compared_at) at time zone 'utc', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') as newest_compared_at
    from deduped
    group by universe_id, field
  )
  select r.universe_id, r.field, r.compared, r.agree, r.disagree, r.not_comparable, r.no_served_value_count, r.newest_compared_at,
    case when r.compared <= 0 then null else least(1.0::numeric, greatest(0.0::numeric, round(((
      (w.p + w.z2 / (2.0::numeric * r.compared::numeric)
        + w.z * sqrt((w.p * (1.0::numeric - w.p) + w.z2 / (4.0::numeric * r.compared::numeric)) / r.compared::numeric)
      ) / (1.0::numeric + w.z2 / r.compared::numeric)
    )::numeric), 6)))::text end as wilson_95_upper_bound
  from rolled r
  cross join lateral (
    select 1.959963984540054::numeric as z, (1.959963984540054::numeric * 1.959963984540054::numeric) as z2,
           case when r.compared > 0 then r.disagree::numeric / r.compared::numeric else 0.0::numeric end as p
  ) w
  order by r.universe_id, r.field
"""


def _resolve_head_freshness(
    contract: EnvironmentContract, raw_freshness: str | None, universe_id: str, environment: str | None
) -> str:
    try:
        if not contract.is_head_scheduled(universe_id, environment):
            return "manual"
    except LookupError:
        pass
    if raw_freshness in {"fresh", "stale", "manual"}:
        return raw_freshness
    return "unknown"


# --- Pydantic DTOs ------------------------------------------------------------------------


class OpsRunRowDto(BaseModel):
    run_id: str
    job_name: str
    status: str
    created_at: str
    duration_seconds: int | None = None


class OpsPointerRowDto(BaseModel):
    universe_id: str
    target_run_id: str
    sequence: int
    advanced_at: str
    age_hours: float | None = None
    freshness: str | None = None
    availability: str | None = None
    staleness_reason: str | None = None


class OpsDataEngineBuildDto(BaseModel):
    git_sha: str
    image_digest: str
    run_id: str
    created_at: str


class OpsQuotaRowDto(BaseModel):
    source: str
    fetches: int


class OpsOverviewResponse(BaseModel):
    runs: list[OpsRunRowDto] | str = "unavailable"
    pointers: list[OpsPointerRowDto] = Field(default_factory=list)
    data_engine: OpsDataEngineBuildDto | None = None
    app_git_sha: str = "unknown"
    quota_today: list[OpsQuotaRowDto] = Field(default_factory=list)


class PipelineFailureDto(BaseModel):
    run_id: str
    at: str
    first_error: str | None = None


class PipelineJobRowDto(BaseModel):
    job: str
    runs: int
    successes: int
    failures: int
    success_rate: float | None = None
    last_success_at: str | None = None
    last_failure: PipelineFailureDto | None = None
    longest_success_seconds: int | None = None
    derived_bound_seconds: int | None = None
    runs_ended_by_bound: int = 0


class PipelineOverviewResponse(BaseModel):
    available: bool = True
    reason: str | None = None
    window_days: int = PIPELINE_WINDOW_DAYS
    jobs: list[PipelineJobRowDto] = Field(default_factory=list)
    job_run_health: dict[str, Any] | None = None
    release: dict[str, Any] | None = None
    app_git_sha: str = "unknown"


class FunnelLayerDto(BaseModel):
    key: str
    title: str
    headline: str
    ratio: float | None = None
    status: str
    detail: str


class QualityOverviewResponse(BaseModel):
    available: bool = True
    reason: str | None = None
    run_id: str | None = None
    layers: list[FunnelLayerDto] = Field(default_factory=list)
    node_values_report: dict[str, Any] | None = None


class MacroSeriesDto(BaseModel):
    series_id: str
    label: str
    unit: str
    source_id: str
    max_age_days: int
    latest: dict[str, Any] | None = None
    is_fresh: bool = False


class MacroPanelResponse(BaseModel):
    available: bool = True
    reason: str | None = None
    series: list[MacroSeriesDto] = Field(default_factory=list)
    months: list[dict[str, Any]] = Field(default_factory=list)


class UniverseOverviewResponse(BaseModel):
    heads: list[dict[str, Any]] = Field(default_factory=list)
    history: list[dict[str, Any]] = Field(default_factory=list)
    members: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)


class DatahubStatsResponse(BaseModel):
    heads: list[dict[str, Any]] = Field(default_factory=list)
    sources: list[dict[str, Any]] = Field(default_factory=list)
    runs: list[dict[str, Any]] = Field(default_factory=list)
    validation: list[dict[str, Any]] = Field(default_factory=list)
    capacity: list[dict[str, Any]] = Field(default_factory=list)
    vendor_accounts: list[dict[str, Any]] = Field(default_factory=list)
    model_1302_by_day: list[dict[str, Any]] = Field(default_factory=list)
    traffic: list[dict[str, Any]] = Field(default_factory=list)
    recent_calls: list[dict[str, Any]] = Field(default_factory=list)
    ledger_coverage: list[dict[str, Any]] = Field(default_factory=list)
    question_coverage: list[dict[str, Any]] = Field(default_factory=list)
    confidence: list[dict[str, Any]] = Field(default_factory=list)
    oracle_outcomes: list[dict[str, Any]] = Field(default_factory=list)
    nightly_verdicts: list[dict[str, Any]] = Field(default_factory=list)


class VerdictHistoryResponse(BaseModel):
    check_name: str
    rows: list[dict[str, Any]] = Field(default_factory=list)


class TriggerPipelineRequest(BaseModel):
    job_name: str | None = None
    pipeline: str | None = None
    universe: str | None = None
    executed_at: str | None = None
    force_fetch: bool = False


class TriggerPipelineResponse(BaseModel):
    request_id: int
    dedupe_key: str
    executed_at: str
    force_fetch: bool
    job_name: str
    run_key: str


# --- Services -----------------------------------------------------------------------------


def get_ops_overview(*, database_url: str | None = None) -> OpsOverviewResponse:
    contract = load_environment_contract()

    def _q(cur: Any) -> OpsOverviewResponse:
        de: OpsDataEngineBuildDto | None = None
        try:
            cur.execute(
                "select run_id, git_sha, image_digest, created_at from mart.data_engine_identity order by created_at desc limit 1"
            )
            if row := cur.fetchone():
                de = OpsDataEngineBuildDto(
                    run_id=str(row["run_id"]),
                    git_sha=str(row["git_sha"]),
                    image_digest=str(row["image_digest"]),
                    created_at=row["created_at"].isoformat(),
                )
        except psycopg.Error:
            cur.connection.rollback()

        runs: list[OpsRunRowDto] | str = "unavailable"
        try:
            cur.execute(
                "select run_id, pipeline_name, status, create_timestamp, start_time, end_time from dagster.runs order by create_timestamp desc limit 10"
            )
            runs = [
                OpsRunRowDto(
                    run_id=str(r["run_id"]),
                    job_name=str(r["pipeline_name"]),
                    status=str(r["status"]),
                    created_at=r["create_timestamp"].isoformat(),
                    duration_seconds=(
                        round(float(r["end_time"]) - float(r["start_time"]))
                        if r.get("start_time") is not None and r.get("end_time") is not None
                        else None
                    ),
                )
                for r in cur.fetchall()
            ]
        except psycopg.Error:
            cur.connection.rollback()

        pointers: list[OpsPointerRowDto] = []
        try:
            cur.execute(
                "select environment, universe_id, coalesce(run_id, head_run_id) as target_run_id, sequence, "
                "advanced_at, age_hours, freshness, availability, staleness_reason "
                "from mart.served_head order by universe_id"
            )
            for r in cur.fetchall():
                uid = str(r["universe_id"])
                env = str(r["environment"]) if r.get("environment") is not None else None
                pointers.append(
                    OpsPointerRowDto(
                        universe_id=uid,
                        target_run_id=str(r["target_run_id"]),
                        sequence=int(r["sequence"]),
                        advanced_at=r["advanced_at"].isoformat(),
                        age_hours=float(r["age_hours"]) if r.get("age_hours") is not None else None,
                        freshness=_resolve_head_freshness(contract, r.get("freshness"), uid, env),
                        availability=r.get("availability"),
                        staleness_reason=r.get("staleness_reason"),
                    )
                )
        except psycopg.Error:
            cur.connection.rollback()

        quota: list[OpsQuotaRowDto] = []
        try:
            cur.execute(
                f"select source, count(*)::int as fetches from raw.fetches "
                f"where fetched_at >= {UTC_DAY_START_SQL} and {REGISTERED_RAW_SOURCE_FILTER} "
                "group by source order by fetches desc"
            )
            quota = [OpsQuotaRowDto(source=str(r["source"]), fetches=int(r["fetches"])) for r in cur.fetchall()]
        except psycopg.Error:
            cur.connection.rollback()

        return OpsOverviewResponse(
            runs=runs,
            pointers=pointers,
            data_engine=de,
            app_git_sha=settings.git_commit_sha,
            quota_today=quota,
        )

    return with_ops_reader(_q, database_url=database_url)


def _first_error_line(event_json: str) -> str | None:
    try:
        parsed = json.loads(event_json)
        msg = parsed.get("message") or parsed.get("error", {}).get("message")
        if isinstance(msg, str) and msg.strip():
            return msg.strip().splitlines()[0][:240]
    except Exception:  # noqa: BLE001
        pass
    lines = [line.strip() for line in event_json.splitlines() if line.strip()]
    return lines[0][:240] if lines else None


def get_pipeline_overview(*, database_url: str | None = None) -> PipelineOverviewResponse:
    contract = load_environment_contract()
    bounds = contract.job_runtime_bounds

    def _q(cur: Any) -> PipelineOverviewResponse:
        try:
            cur.execute(
                JOBS_SQL,
                {"p1": PIPELINE_WINDOW_DAYS, "p2": PIPELINE_LISTED_DAYS, "p3": bounds.window_days},
            )
            grouped = cur.fetchall()
        except psycopg.Error as exc:
            cur.connection.rollback()
            if exc.sqlstate == _UNDEFINED_TABLE:
                return PipelineOverviewResponse(
                    available=False, reason="Dagster run log table missing", app_git_sha=settings.git_commit_sha
                )
            raise

        if not grouped:
            return PipelineOverviewResponse(
                available=False, reason="No pipeline runs recorded yet", app_git_sha=settings.git_commit_sha
            )

        failed_ids = [str(r["last_failure_run_id"]) for r in grouped if r.get("last_failure_run_id")]
        failures_map: dict[str, str | None] = {}
        if failed_ids:
            try:
                cur.execute(FAILURE_EVENTS_SQL, {"p1": failed_ids})
                for r in cur.fetchall():
                    failures_map[str(r["run_id"])] = _first_error_line(str(r["event"]))
            except psycopg.Error:
                cur.connection.rollback()

        timeouts: dict[str, int] = {}
        try:
            cur.execute(TIMEOUTS_SQL, {"p1": PIPELINE_WINDOW_DAYS})
            timeouts = {str(r["job"]): int(r["count"]) for r in cur.fetchall()}
        except psycopg.Error:
            cur.connection.rollback()

        jobs: list[PipelineJobRowDto] = []
        for r in grouped:
            succ = int(r["successes"])
            fail = int(r["failures"])
            total = succ + fail
            longest = (
                round(float(r["longest_success_seconds"])) if r.get("longest_success_seconds") is not None else None
            )
            bound = contract.derive_max_runtime_seconds(float(longest)) if longest is not None else None
            frid = r.get("last_failure_run_id")
            fl = r.get("last_failure")
            jobs.append(
                PipelineJobRowDto(
                    job=str(r["job"]),
                    runs=total,
                    successes=succ,
                    failures=fail,
                    success_rate=(succ / total) if total else None,
                    last_success_at=r["last_success"].isoformat() if r.get("last_success") else None,
                    last_failure=(
                        PipelineFailureDto(run_id=str(frid), at=fl.isoformat(), first_error=failures_map.get(str(frid)))
                        if frid and fl
                        else None
                    ),
                    longest_success_seconds=longest,
                    derived_bound_seconds=bound,
                    runs_ended_by_bound=timeouts.get(str(r["job"]), 0),
                )
            )
        verdict: dict[str, Any] | None = None
        try:
            cur.execute(
                "select ok, ran_at, summary from mart.nightly_verdicts where check_name = 'job_run_health' order by ran_at desc limit 1"
            )
            if v := cur.fetchone():
                verdict = {"ok": bool(v["ok"]), "ran_at": v["ran_at"].isoformat(), "summary": str(v["summary"])}
        except psycopg.Error:
            cur.connection.rollback()
        if verdict is None and jobs:
            failed = [j for j in jobs if j.failures > 0]
            verdict = {
                "ok": not failed,
                "ran_at": "",
                "summary": ", ".join(f"{j.job} ({j.failures} of {j.runs} runs failed)" for j in failed)
                or f"{len(jobs)} jobs healthy",
            }

        release: dict[str, Any] | None = None
        try:
            cur.execute("select git_sha from mart.data_engine_identity order by created_at desc limit 1")
            if rel := cur.fetchone():
                sha = str(rel["git_sha"])
                cur.execute(RELEASE_START_SQL, {"p1": sha})
                start_row = cur.fetchone()
                created = start_row["since"] if start_row and start_row.get("since") else None
                if created is not None:
                    since = created if created.tzinfo else created.replace(tzinfo=UTC)
                    age_h = round((datetime.now(UTC) - since).total_seconds() / 3600.0, 2)
                    release = {"git_sha": sha, "since": since.isoformat(), "age_hours": age_h}
        except psycopg.Error:
            cur.connection.rollback()

        return PipelineOverviewResponse(
            jobs=jobs,
            job_run_health=verdict,
            release=release,
            app_git_sha=settings.git_commit_sha,
        )

    return with_ops_reader(_q, database_url=database_url)


def get_quality_overview(context: AccessContext, *, database_url: str | None = None) -> QualityOverviewResponse:
    db_url = database_url or settings.database_url
    node_repo = PostgresMetricNodeValueRepository(database_url=db_url)
    node_report = node_repo.latest(node_key="gppe_uniform_charge_v0", limit=500)
    if not isinstance(node_report, MetricNodeValueReport):
        return QualityOverviewResponse(available=False, reason=str(node_report.reason))
    run_id = node_report.run_id
    contract = load_environment_contract()

    def _q(cur: Any) -> tuple[dict[str, Any], int, int, dict[str, Any] | None]:
        cur.execute(
            "select payload from mart.datahub_quality_report where run_id = %s order by created_at desc limit 1",
            (run_id,),
        )
        q_row = cur.fetchone()
        quality = dict(q_row["payload"]) if q_row and isinstance(q_row.get("payload"), dict) else {}
        cur.execute(
            "select count(*)::int as total, coalesce(sum((present_count = required_count)::int), 0)::int as complete from mart.strategy_input_coverage where run_id = %s",
            (run_id,),
        )
        cov_row = cur.fetchone() or {"total": 0, "complete": 0}
        strat_like = id_prefix_like(universe_with_role("strategy"))
        cur.execute(
            "select environment, universe_id, age_hours as age, freshness, limit_hours, availability "
            "from mart.served_head where coalesce(run_id, head_run_id) = %s or universe_id like %s "
            "order by (coalesce(run_id, head_run_id) = %s) desc, advanced_at desc limit 1",
            (run_id, strat_like, run_id),
        )
        ptr = cur.fetchone()
        return quality, int(cov_row["complete"]), int(cov_row["total"]), dict(ptr) if ptr else None

    quality, cov_complete, cov_total, ptr = with_mart_readonly(_q, database_url=db_url)
    strat = PostgresStrategyRunRepository(database_url=db_url).get_latest(
        strategy_id=DASHBOARD_STRATEGY_ID, context=context
    )
    decisions = list(strat.decisions) if hasattr(strat, "decisions") else []
    selected = sum(1 for d in decisions if d.outcome.value == "selected")
    ranked = sum(1 for d in decisions if d.rank is not None)
    avail_ratio = float(quality["availability"]) if isinstance(quality.get("availability"), (int, float)) else None
    ptr_age = float(ptr["age"]) if ptr and ptr.get("age") is not None else None
    ptr_limit = float(ptr["limit_hours"]) if ptr and ptr.get("limit_hours") else _DEFAULT_POINTER_LIMIT_HOURS
    ptr_fresh = (
        _resolve_head_freshness(contract, ptr.get("freshness"), str(ptr["universe_id"]), ptr.get("environment"))
        if ptr
        else "unknown"
    )
    layers = [
        FunnelLayerDto(
            key="L1",
            title="Capture",
            headline=f"{quality.get('available_count', '?')} / {quality.get('requested_count', '?')} cells",
            ratio=avail_ratio,
            status="muted"
            if avail_ratio is None
            else ("ok" if avail_ratio >= 1 else ("warn" if avail_ratio >= 0.9 else "crit")),
            detail=f"freshness {quality.get('freshness', '?')} · reconciliation {quality.get('independent_reconciliation', '?')}",
        ),
        FunnelLayerDto(
            key="L2",
            title="Facts",
            headline="no coverage rows yet"
            if cov_total == 0
            else f"{cov_complete} / {cov_total} issuers input-complete",
            ratio=None if cov_total == 0 else cov_complete / cov_total,
            status="muted"
            if cov_total == 0
            else ("ok" if cov_complete == cov_total else ("warn" if cov_complete / cov_total >= 0.75 else "crit")),
            detail="missing_keys per issuer in mart.strategy_input_coverage",
        ),
        FunnelLayerDto(
            key="L3",
            title="Factors",
            headline=f"GPPE {node_report.available_count} / {len(node_report.cells)} listings",
            ratio=None if not node_report.cells else node_report.available_count / len(node_report.cells),
            status="muted"
            if not node_report.cells
            else ("ok" if node_report.available_count == len(node_report.cells) else "warn"),
            detail="mart.metric_node_values (gppe_uniform_charge_v0)",
        ),
        FunnelLayerDto(
            key="L4",
            title="Strategy",
            headline=f"{len(decisions)} evaluated -> {ranked} ranked -> {selected} selected",
            ratio=None if not decisions else max(selected / len(decisions), 0.04),
            status="crit" if not decisions else "ok",
            detail="claim ceiling: preview",
        ),
        FunnelLayerDto(
            key="L5",
            title="Consumption",
            headline="no pointer" if ptr_age is None else f"pointer {ptr_age:.1f}h old ({ptr_fresh})",
            ratio=None if ptr_age is None else max(0.04, min(1.0, 1.0 - ptr_age / ptr_limit)),
            status="crit"
            if ptr is None or ptr.get("availability") == "unavailable"
            else ("ok" if ptr_fresh in {"fresh", "manual"} else "warn"),
            detail=SERVED_HEAD_SQL,
        ),
    ]
    return QualityOverviewResponse(
        run_id=run_id,
        layers=layers,
        node_values_report=node_report.model_dump(mode="json"),
    )


def get_macro_panel(*, database_url: str | None = None) -> MacroPanelResponse:
    status_sql = (
        "select series_id, label, unit, source_id, max_age_days, "
        "to_char(observation_date, 'YYYY-MM-DD') as observation_day, value::text as value, "
        "source, fetched_at, age_days, is_fresh from mart.macro_series_status order by series_id"
    )
    months_sql = (
        "select series_id, to_char(observation_date, 'YYYY-MM-DD') as observation_day, value::text as value "
        "from mart.macro_series_current "
        "where observation_date >= ((now() at time zone 'UTC')::date - interval '12 months')::date "
        "order by observation_date desc, series_id"
    )

    def _q(cur: Any) -> MacroPanelResponse:
        cur.execute(status_sql)
        status_rows = cur.fetchall()
        cur.execute(months_sql)
        month_rows = cur.fetchall()
        if not status_rows:
            return MacroPanelResponse(available=False, reason="No macro series configured")
        series = [
            MacroSeriesDto(
                series_id=str(r["series_id"]),
                label=str(r["label"]),
                unit=str(r["unit"]),
                source_id=str(r["source_id"]),
                max_age_days=int(r["max_age_days"]),
                is_fresh=bool(r["is_fresh"]),
                latest=(
                    None
                    if r.get("observation_day") is None
                    else {
                        "observation_date": str(r["observation_day"])[:10],
                        "value": str(r["value"]),
                        "source": str(r["source"]),
                        "fetched_at": r["fetched_at"].isoformat()
                        if hasattr(r["fetched_at"], "isoformat")
                        else str(r["fetched_at"]),
                        "age_days": int(r["age_days"]),
                    }
                ),
            )
            for r in status_rows
        ]
        months = [
            {
                "series_id": str(r["series_id"]),
                "observation_date": str(r["observation_day"])[:10],
                "value": str(r["value"]),
            }
            for r in month_rows
        ]
        return MacroPanelResponse(series=series, months=months)

    try:
        return with_mart_readonly(_q, database_url=database_url)
    except psycopg.Error as exc:
        if exc.sqlstate == _UNDEFINED_TABLE:
            return MacroPanelResponse(available=False, reason="Macro mart views not yet migrated")
        return MacroPanelResponse(available=False, reason=str(exc))


def get_universes_overview(*, database_url: str | None = None) -> UniverseOverviewResponse:
    def _q(cur: Any) -> UniverseOverviewResponse:
        cur.execute(
            """
            select r.kind, r.sequence, coalesce(r.note, '') as note, r.advanced_at::text as advanced_at,
                   o.payload->>'universe_id' as universe_id,
                   (o.payload->>'instrument_count')::int as instrument_count,
                   o.payload->>'instrument_mapping_sha256' as mapping_sha256
            from staging.accepted_rulesets r
            join staging.contract_objects o on o.contract_id = r.contract_id
            where r.kind = any(%s::text[])
            order by r.kind, r.sequence desc
            """,
            ([lane_name(k) for k in keys()],),
        )
        history = [dict(r) for r in cur.fetchall()]
        heads: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in history:
            kind = str(row["kind"])
            if kind not in seen:
                seen.add(kind)
                heads.append(row)
        members: dict[str, list[dict[str, Any]]] = {}
        for head in heads:
            ukey = key_of_lane(str(head["kind"]))
            cur.execute(
                """
                select f.ticker, f.company_name, f.cik, f.figi, f.market_cap::text as market_cap,
                       f.as_of::text as as_of, f.source, r.payload_sha256 as fetch_sha256, r.object_uri
                from staging.etf_constituent_facts f
                join raw.fetches r on r.id = f.raw_fetch_id
                where f.etf_symbol = %s
                  and f.as_of = (select max(as_of) from staging.etf_constituent_facts where etf_symbol = %s)
                order by f.ticker
                """,
                (ukey, ukey),
            )
            members[str(head["kind"])] = [dict(r) for r in cur.fetchall()]
        return UniverseOverviewResponse(heads=heads, history=history, members=members)

    return with_ops_reader(_q, database_url=database_url)


def get_datahub_stats(*, database_url: str | None = None) -> DatahubStatsResponse:
    contract = load_environment_contract()
    sentinel_likes = [id_prefix_like(k) for k, u in contract.universes.items() if u.is_sentinel]

    def _safe(cur: Any, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        try:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]
        except psycopg.Error:
            cur.connection.rollback()
            return []

    def _q(cur: Any) -> DatahubStatsResponse:
        heads_raw = _safe(
            cur,
            """
            select h.environment, h.universe_id, h.sequence, h.advanced_at::text as advanced_at,
                   coalesce(h.run_id, h.head_run_id) as target_run_id,
                   q.payload->>'availability' as availability,
                   h.availability as head_availability,
                   (select count(*)::int from jsonb_each(coalesce(q.payload->'reconciliation_cells', '{}'::jsonb)) c
                     where c.value->>'outcome' = 'agreed') as agreed_cells,
                   (select count(*)::int from jsonb_each(coalesce(q.payload->'reconciliation_cells', '{}'::jsonb))) as total_cells,
                   h.freshness, h.age_hours::float as age_hours, h.limit_hours::float as limit_hours,
                   h.staleness_reason, coalesce(q.payload->'factor_availability', '{}'::jsonb) as factor_availability
            from mart.served_head h
            left join mart.datahub_quality_report q on q.run_id = coalesce(h.run_id, h.head_run_id)
            order by h.universe_id
            """,
        )
        active_env = settings.app_env or (heads_raw[0]["environment"] if heads_raw else "production")
        for h in heads_raw:
            h["freshness"] = _resolve_head_freshness(
                contract, h.get("freshness"), str(h["universe_id"]), h.get("environment")
            )

        sources = _safe(
            cur,
            f"select source, count(*)::int as fetches_total, "
            f"(count(*) filter (where recorded_at > now() - interval '24 hours'))::int as fetches_24h, "
            f"max(recorded_at)::text as last_fetch from raw.fetches where {REGISTERED_RAW_SOURCE_FILTER} "
            "group by source order by max(recorded_at) desc, count(*) desc limit 12",
        )
        runs = _safe(
            cur,
            "select run_id, universe_id, cutoff::text as cutoff, obligation_count as obligations, "
            "(success_count + unchanged_count)::int as resolved, failed_count as failed, complete "
            "from mart.capture_status order by cutoff desc limit 12",
        )
        verdicts_raw = _safe(
            cur,
            "select distinct on (check_name) check_name, "
            "to_char(ran_at at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS\"Z\"') as ran_at, "
            "ok, summary, dagster_run_id from mart.nightly_verdicts order by check_name, ran_at desc, recorded_at desc",
        )
        verdicts = [v for v in verdicts_raw if contract.expects_verdict(str(v["check_name"]), active_env)]
        traffic = _safe(
            cur,
            f"select l.source, count(*)::int as calls, (count(*) filter (where not l.ok))::int as failed, "
            f"round(avg(l.duration_ms))::int as avg_ms, "
            f"to_char(max(l.called_at) at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS\"Z\"') as last_call "
            f"from staging.api_call_ledger l where l.called_at >= {UTC_DAY_START_SQL} group by l.source order by calls desc",
        )
        recent_calls = _safe(
            cur,
            "select l.id, to_char(l.called_at at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS\"Z\"') as called_at, "
            "l.source, l.endpoint, l.caller, l.ok, l.status_code, l.duration_ms, left(l.error, 160) as error, l.run_key "
            "from staging.api_call_ledger l order by l.called_at desc, l.id desc limit 40",
        )
        ledger_cov = _safe(
            cur,
            f"with ledger as (select (l.called_at at time zone 'utc')::date as day, l.source, l.endpoint, "
            f"l.ok, l.fetch_key, l.run_key, l.repeat_reason from staging.api_call_ledger l "
            f"where l.called_at >= {UTC_DAY_START_SQL} - (%s::int - 1) * interval '1 day'), "
            "per_reason as (select day, source, endpoint, jsonb_object_agg(repeat_reason, rows) as by_reason "
            "from (select day, source, endpoint, repeat_reason, count(*)::int as rows from ledger "
            "where repeat_reason is not null group by 1, 2, 3, 4) reasons group by 1, 2, 3), "
            "twice as (select day, source, endpoint, count(*)::int as keys_twice from (select day, source, endpoint, fetch_key "
            "from ledger where ok and fetch_key is not null and repeat_reason is null group by 1, 2, 3, 4 having count(*) > 1) rep "
            "group by 1, 2, 3), "
            "totals as (select day, source, endpoint, count(*)::int as calls, "
            "(count(*) filter (where fetch_key is null))::int as no_key, "
            "round(count(*) filter (where run_key is not null)::numeric / count(*), 4)::text as run_share, "
            "round(count(*) filter (where run_key is not null)::numeric / nullif(count(distinct run_key), 0), 1)::text as calls_per_run "
            "from ledger group by 1, 2, 3) "
            "select to_char(t.day, 'YYYY-MM-DD') as day, t.source, t.endpoint, t.calls, t.no_key, "
            "coalesce(r.by_reason, '{}'::jsonb) as by_reason, coalesce(w.keys_twice, 0) as keys_twice, "
            "t.run_share, t.calls_per_run from totals t left join per_reason r using (day, source, endpoint) "
            "left join twice w using (day, source, endpoint) order by t.day desc, t.source, t.endpoint",
            (LEDGER_COVERAGE_DAYS,),
        )
        question_cov = _safe(cur, QUESTION_COVERAGE_SQL)
        confidence = _safe(cur, CONFIDENCE_REPORT_SQL)
        oracle_outcomes = _safe(cur, ORACLE_OUTCOMES_SQL)
        validation = _safe(
            cur,
            f"select 'plausibility sign checks' as check, "
            f"case when coalesce((q.payload->>'implausible_count')::int, 0) = 0 then 'pass' else 'violations' end as verdict, "
            f"coalesce(q.payload->>'implausible_count', '0') || ' implausible cells in the newest report' as detail "
            f"from mart.datahub_quality_report q join mart.capture_status s using (run_id) where {NON_CANARY_UNIVERSE_WHERE} "
            "order by s.cutoff desc limit 1",
            (sentinel_likes,),
        ) + _safe(
            cur,
            "select 'cross-run reuse (24h)' as check, "
            "case when coalesce(sum(unchanged_count), 0) > 0 then 'active' else 'idle' end as verdict, "
            "coalesce(sum(unchanged_count), 0) || ' vendor fetches avoided across ' || count(*) || ' runs' as detail "
            "from mart.capture_status where cutoff > now() - interval '24 hours'",
        )
        return DatahubStatsResponse(
            heads=heads_raw,
            sources=sources,
            runs=runs,
            validation=validation,
            traffic=traffic,
            recent_calls=recent_calls,
            ledger_coverage=ledger_cov,
            question_coverage=question_cov,
            confidence=confidence,
            oracle_outcomes=oracle_outcomes,
            nightly_verdicts=verdicts,
        )

    return with_ops_reader(_q, database_url=database_url)


def get_verdict_history(check_name: str, *, database_url: str | None = None) -> VerdictHistoryResponse:
    def _q(cur: Any) -> VerdictHistoryResponse:
        cur.execute(
            "select check_name, to_char(ran_at at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS\"Z\"') as ran_at, "
            "ok, summary, dagster_run_id from mart.nightly_verdicts where check_name = %s "
            "order by ran_at desc, recorded_at desc limit 400",
            (check_name,),
        )
        return VerdictHistoryResponse(check_name=check_name, rows=[dict(r) for r in cur.fetchall()])

    return with_ops_reader(_q, database_url=database_url)


def trigger_pipeline(
    principal_id: str, req: TriggerPipelineRequest, *, database_url: str | None = None
) -> TriggerPipelineResponse:
    contract = load_environment_contract()
    requested = (req.job_name or req.pipeline or req.universe or "").strip().lower()
    allowed_jobs = {job_name(k): job_name(k) for k in contract.universes}
    if not requested:
        target_job = job_name("topt")
    elif requested in contract.universes:
        target_job = job_name(requested)
    elif requested in allowed_jobs:
        target_job = allowed_jobs[requested]
    else:
        raise ValueError(f"unsupported pipeline or universe: {requested}")

    if req.executed_at:
        parsed_dt = datetime.fromisoformat(req.executed_at.replace("Z", "+00:00"))
        if parsed_dt.tzinfo is None:
            parsed_dt = parsed_dt.replace(tzinfo=UTC)
        executed_at_dt = parsed_dt.astimezone(UTC)
    else:
        executed_at_dt = datetime.now(UTC)

    executed_iso = executed_at_dt.isoformat()
    dedupe_key = str(uuid.uuid4())

    def _q(cur: Any) -> int:
        cur.execute(
            """
            insert into staging.pipeline_trigger_requests (job_name, executed_at, requested_by, dedupe_key, force_fetch)
            values (%s, %s, %s, %s, %s)
            returning request_id
            """,
            (target_job, executed_at_dt, principal_id, dedupe_key, bool(req.force_fetch)),
        )
        row = cur.fetchone()
        return int(row["request_id"])

    req_id = with_app_runtime(_q, database_url=database_url)
    return TriggerPipelineResponse(
        request_id=req_id,
        dedupe_key=dedupe_key,
        executed_at=executed_iso,
        force_fetch=bool(req.force_fetch),
        job_name=target_job,
        run_key=f"manual:{dedupe_key}",
    )
