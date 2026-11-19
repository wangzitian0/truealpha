"""Administrator operational, DataHub, and manual pipeline trigger services."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import psycopg
from pydantic import BaseModel, Field
from truealpha_contracts.environment_contract import load_environment_contract
from truealpha_contracts.universes import id_prefix_like, job_name

from llm_service.config import settings
from llm_service.services._db import with_app_runtime, with_ops_reader

SWEEP_RUN_SOURCE_REGEX_SOURCE = "-[0-9]{8}T[0-9]{4}$"
REGISTERED_RAW_SOURCE_FILTER = f"not (source ~ '{SWEEP_RUN_SOURCE_REGEX_SOURCE}')"
UTC_DAY_START_SQL = "(date_trunc('day', now() at time zone 'utc') at time zone 'utc')"
NON_CANARY_UNIVERSE_WHERE = "s.universe_id not like all (%s::text[])"
LEDGER_COVERAGE_DAYS = 8


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


class OpsRunRowDto(BaseModel):
    run_id: str
    job_name: str
    status: str
    created_at: str
    duration_seconds: int | None = None


class OpsDataEngineBuildDto(BaseModel):
    git_sha: str
    image_digest: str
    run_id: str
    created_at: str


class OpsOverviewResponse(BaseModel):
    runs: list[OpsRunRowDto] | str = "unavailable"
    data_engine: OpsDataEngineBuildDto | None = None
    app_git_sha: str = "unknown"


class DatahubStatsResponse(BaseModel):
    sources: list[dict[str, Any]] = Field(default_factory=list)
    capture_runs: list[dict[str, Any]] = Field(default_factory=list)
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


def get_ops_overview(*, database_url: str | None = None) -> OpsOverviewResponse:
    def _q(cur: Any) -> OpsOverviewResponse:
        de: OpsDataEngineBuildDto | None = None
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
        except psycopg.errors.UndefinedTable:
            cur.connection.rollback()

        return OpsOverviewResponse(
            runs=runs,
            data_engine=de,
            app_git_sha=settings.git_commit_sha,
        )

    return with_ops_reader(_q, database_url=database_url)


def get_datahub_stats(*, database_url: str | None = None) -> DatahubStatsResponse:
    contract = load_environment_contract()
    sentinel_likes = [id_prefix_like(k) for k, u in contract.universes.items() if u.is_sentinel]

    def _rows(cur: Any, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]

    def _q(cur: Any) -> DatahubStatsResponse:
        sources = _rows(
            cur,
            f"select source, count(*)::int as fetches_total, "
            f"(count(*) filter (where recorded_at > now() - interval '24 hours'))::int as fetches_24h, "
            f"max(recorded_at)::text as last_fetch from raw.fetches where {REGISTERED_RAW_SOURCE_FILTER} "
            "group by source order by max(recorded_at) desc, count(*) desc limit 12",
        )
        capture_runs = _rows(
            cur,
            "select run_id, universe_id, cutoff::text as cutoff, obligation_count as obligations, "
            "(success_count + unchanged_count)::int as resolved, failed_count as failed, complete "
            "from mart.capture_status order by cutoff desc limit 12",
        )
        traffic = _rows(
            cur,
            f"select l.source, count(*)::int as calls, (count(*) filter (where not l.ok))::int as failed, "
            f"round(avg(l.duration_ms))::int as avg_ms, "
            f"to_char(max(l.called_at) at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS\"Z\"') as last_call "
            f"from staging.api_call_ledger l where l.called_at >= {UTC_DAY_START_SQL} group by l.source order by calls desc",
        )
        recent_calls = _rows(
            cur,
            "select l.id, to_char(l.called_at at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS\"Z\"') as called_at, "
            "l.source, l.endpoint, l.caller, l.ok, l.status_code, l.duration_ms, left(l.error, 160) as error, l.run_key "
            "from staging.api_call_ledger l order by l.called_at desc, l.id desc limit 40",
        )
        ledger_cov = _rows(
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
        question_cov = _rows(cur, QUESTION_COVERAGE_SQL)
        confidence = _rows(cur, CONFIDENCE_REPORT_SQL)
        oracle_outcomes = _rows(cur, ORACLE_OUTCOMES_SQL)
        validation = _rows(
            cur,
            f"select 'plausibility sign checks' as check, "
            f"case when coalesce((q.payload->>'implausible_count')::int, 0) = 0 then 'pass' else 'violations' end as verdict, "
            f"coalesce(q.payload->>'implausible_count', '0') || ' implausible cells in the newest report' as detail "
            f"from mart.datahub_quality_report q join mart.capture_status s using (run_id) where {NON_CANARY_UNIVERSE_WHERE} "
            "order by s.cutoff desc limit 1",
            (sentinel_likes,),
        ) + _rows(
            cur,
            "select 'cross-run reuse (24h)' as check, "
            "case when coalesce(sum(unchanged_count), 0) > 0 then 'active' else 'idle' end as verdict, "
            "coalesce(sum(unchanged_count), 0) || ' vendor fetches avoided across ' || count(*) || ' runs' as detail "
            "from mart.capture_status where cutoff > now() - interval '24 hours'",
        )
        return DatahubStatsResponse(
            sources=sources,
            capture_runs=capture_runs,
            validation=validation,
            traffic=traffic,
            recent_calls=recent_calls,
            ledger_coverage=ledger_cov,
            question_coverage=question_cov,
            confidence=confidence,
            oracle_outcomes=oracle_outcomes,
        )

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


__all__ = [
    "CONFIDENCE_REPORT_SQL",
    "DatahubStatsResponse",
    "LEDGER_COVERAGE_DAYS",
    "NON_CANARY_UNIVERSE_WHERE",
    "ORACLE_OUTCOMES_SQL",
    "OpsDataEngineBuildDto",
    "OpsOverviewResponse",
    "OpsRunRowDto",
    "QUESTION_COVERAGE_SQL",
    "REGISTERED_RAW_SOURCE_FILTER",
    "SWEEP_RUN_SOURCE_REGEX_SOURCE",
    "TriggerPipelineRequest",
    "TriggerPipelineResponse",
    "UTC_DAY_START_SQL",
    "get_datahub_stats",
    "get_ops_overview",
    "governed_latest_per_universe_sql",
    "trigger_pipeline",
]
