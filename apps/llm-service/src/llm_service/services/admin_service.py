"""Administrator operational, DataHub, and manual pipeline trigger services."""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from typing import Any

import psycopg
from pydantic import BaseModel, Field
from truealpha_contracts.environment_contract import load_environment_contract
from truealpha_contracts.universes import id_prefix_like, job_name
from truealpha_runtime.checks import ObjectStorageCheck

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
    select distinct on (
      h.universe_id,
      o.oracle_kind,
      o.period,
      coalesce(o.observation_id::text, o.outcome_id::text),
      o.subject_id,
      o.field
    )
      h.universe_id, o.field, o.result, o.reason, o.compared_at
    from mart.served_head h
    join mart.oracle_outcomes o on o.run_id = coalesce(h.run_id, h.head_run_id)
    order by h.universe_id, o.oracle_kind, o.period,
             coalesce(o.observation_id::text, o.outcome_id::text),
             o.subject_id, o.field, o.compared_at desc, o.recorded_at desc, o.outcome_id desc
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

ORACLE_DISAGREEMENTS_SQL = """
  select distinct on (h.universe_id, o.oracle_kind, o.period, o.subject_id, o.field)
    h.universe_id,
    o.subject_id,
    o.field,
    o.period,
    o.oracle_kind,
    o.source,
    o.served_value::text as served_value,
    o.oracle_value::text as oracle_value,
    o.ratio::text as ratio,
    o.tolerance::text as tolerance,
    o.reason,
    to_char(o.compared_at at time zone 'utc', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') as compared_at
  from mart.served_head h
  join mart.oracle_outcomes o on o.run_id = coalesce(h.run_id, h.head_run_id)
  where o.result = 'disagree'
  order by h.universe_id, o.oracle_kind, o.period, o.subject_id, o.field,
           o.compared_at desc, o.recorded_at desc, o.outcome_id desc
  limit 100
"""

SERVED_HEADS_SQL = """
  select environment, universe_id, universe_version, factor_id, sequence,
         head_run_id, run_id, artifact_key, family,
         age_hours::float as age_hours, limit_hours::float as limit_hours,
         freshness, availability, staleness_reason,
         to_char(advanced_at at time zone 'utc', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') as advanced_at
  from mart.served_head
  order by universe_id, factor_id
"""

NIGHTLY_VERDICTS_SQL = """
  select distinct on (check_name)
    check_name, ok, summary, dagster_run_id,
    to_char(ran_at at time zone 'utc', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') as ran_at,
    to_char(recorded_at at time zone 'utc', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') as recorded_at
  from mart.nightly_verdicts
  order by check_name, ran_at desc, recorded_at desc, verdict_id desc
"""

TRIGGER_QUEUE_SQL = """
  select request_id, job_name, requested_by, dedupe_key, force_fetch, launched_run_key,
         to_char(executed_at at time zone 'utc', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') as executed_at,
         to_char(requested_at at time zone 'utc', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') as requested_at,
         to_char(consumed_at at time zone 'utc', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') as consumed_at
  from staging.pipeline_trigger_requests
  order by request_id desc
  limit 20
"""

ROLLBACK_QUEUE_SQL = """
  select request_id, environment, universe_id, universe_version, factor_id,
         target_run_id, reason, requested_by, dedupe_key, result_pointer_id,
         to_char(requested_at at time zone 'utc', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') as requested_at,
         to_char(consumed_at at time zone 'utc', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') as consumed_at
  from staging.head_rollback_requests
  order by request_id desc
  limit 20
"""

FAILED_JOB_ERRORS_SQL = """
  with recent_failures as (
    (select id, run_id, event, timestamp from dagster.event_logs where dagster_event_type = 'STEP_FAILURE' order by id desc limit 60)
    union all
    (select id, run_id, event, timestamp from dagster.event_logs where dagster_event_type = 'PIPELINE_FAILURE' order by id desc limit 60)
    union all
    (select id, run_id, event, timestamp from dagster.event_logs where dagster_event_type = 'RUN_FAILURE' order by id desc limit 60)
  ),
  deduped_runs as (
    select distinct on (run_id) id, run_id, event, timestamp
    from recent_failures
    order by run_id, id desc
  ),
  top_runs as (
    select id, run_id, event, timestamp
    from deduped_runs
    order by id desc
    limit 25
  )
  select
    t.run_id,
    split_part(j.err_text, E'\\n', 1) as first_line_error,
    to_char(t.timestamp, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') as logged_at
  from top_runs t
  cross join lateral (
    select coalesce(
      nullif(t.event::jsonb #>> '{dagster_event,event_specific_data,error,message}', ''),
      nullif(t.event::jsonb #>> '{error_info,message}', ''),
      nullif(t.event::jsonb ->> 'user_message', ''),
      nullif(t.event::jsonb ->> 'message', ''),
      ''
    ) as err_text
  ) j
  where j.err_text <> ''
  order by t.id desc
  limit 20
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
    served_heads: list[dict[str, Any]] = Field(default_factory=list)
    nightly_verdicts: list[dict[str, Any]] = Field(default_factory=list)
    dependencies: list[dict[str, Any]] = Field(default_factory=list)
    failed_job_errors: list[dict[str, Any]] = Field(default_factory=list)
    trigger_queue: list[dict[str, Any]] = Field(default_factory=list)
    rollback_requests: list[dict[str, Any]] = Field(default_factory=list)


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
    served_heads: list[dict[str, Any]] = Field(default_factory=list)
    served_artifacts: list[dict[str, Any]] = Field(default_factory=list)
    nightly_verdicts: list[dict[str, Any]] = Field(default_factory=list)
    output_invariants: list[dict[str, Any]] = Field(default_factory=list)
    oracle_disagreements: list[dict[str, Any]] = Field(default_factory=list)
    llm_usage: list[dict[str, Any]] = Field(default_factory=list)
    backfill_health: list[dict[str, Any]] = Field(default_factory=list)
    trigger_queue: list[dict[str, Any]] = Field(default_factory=list)
    rollback_requests: list[dict[str, Any]] = Field(default_factory=list)


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


class HeadRollbackRequest(BaseModel):
    universe_id: str = Field(min_length=1)
    universe_version: str = Field(min_length=1)
    factor_id: str = Field(default="gross_profit_per_employee", min_length=1)
    target_run_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    environment: str | None = None


class HeadRollbackResponse(BaseModel):
    request_id: int
    dedupe_key: str
    environment: str
    universe_id: str
    universe_version: str
    factor_id: str
    target_run_id: str
    reason: str
    requested_at: str


def _optional_rows(
    cur: Any,
    sql: str,
    params: tuple[Any, ...] = (),
    *,
    table_check: str | None = None,
) -> list[dict[str, Any]]:
    if table_check:
        cur.execute("select to_regclass(%s) as reg", (table_check,))
        reg_row = cur.fetchone()
        if reg_row is not None and reg_row.get("reg") is None:
            return []
    try:
        if params:
            cur.execute(sql, params)
        else:
            cur.execute(sql)
        return [dict(r) for r in cur.fetchall()]
    except (psycopg.errors.UndefinedTable, psycopg.errors.InsufficientPrivilege):
        cur.connection.rollback()
        return []


def _ledger_source_match(account_name: str, usage_by_source: dict[str, dict[str, Any]]) -> dict[str, Any]:
    norm = account_name.strip().lower()
    if norm in usage_by_source:
        return usage_by_source[norm]
    for source, stats in usage_by_source.items():
        if norm in source.lower() or source.lower() in norm:
            return stats
    return {}


def _build_vendor_capacity(
    contract: Any, env_name: str, usage_rows: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    usage_by_source = {str(r["source"]): r for r in usage_rows if r.get("source") is not None}
    accounts_out: list[dict[str, Any]] = []
    capacity_out: list[dict[str, Any]] = []
    for name, acct in sorted(contract.vendor_accounts.accounts.items()):
        share = contract.account_share(name, env_name)
        usage = _ledger_source_match(name, usage_by_source)
        calls_24h = int(usage.get("calls_24h") or 0)
        calls_7d = int(usage.get("calls_7d") or 0)
        rate_limited_24h = int(usage.get("rate_limited_24h") or 0)
        limits_list = [
            {
                "limit_id": lim.limit_id,
                "unit": lim.unit,
                "window_seconds": lim.window_seconds,
                "amount": lim.amount,
                "share": lim.share,
            }
            for lim in share.limits
        ]
        accounts_out.append(
            {
                "account": name,
                "holder": acct.holder,
                "environment": share.environment,
                "percent": share.percent,
                "seats": list(acct.seats),
                "outside_consumers": bool(acct.outside_consumers),
                "calls_24h": calls_24h,
                "calls_7d": calls_7d,
                "rate_limited_24h": rate_limited_24h,
                "limits": limits_list,
            }
        )
        for lim in share.limits:
            window = lim.window_seconds or 0
            observed = calls_24h if window <= 86400 else calls_7d
            share_val = lim.share
            util = round((observed / share_val) * 100.0, 2) if share_val and share_val > 0 else None
            remaining = max(share_val - observed, 0) if share_val is not None else None
            capacity_out.append(
                {
                    "account": name,
                    "environment": share.environment,
                    "limit_id": lim.limit_id,
                    "unit": lim.unit,
                    "window_seconds": lim.window_seconds,
                    "share": share_val,
                    "observed_calls": observed,
                    "remaining_estimate": remaining,
                    "utilization_pct": util,
                    "rate_limited_24h": rate_limited_24h,
                }
            )
    return accounts_out, capacity_out


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
            cur.execute("select to_regclass('dagster.runs') as reg")
            dag_reg = cur.fetchone()
            if dag_reg is None or dag_reg.get("reg") is not None:
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
        except (psycopg.errors.UndefinedTable, psycopg.errors.InsufficientPrivilege):
            cur.connection.rollback()

        pg_t0 = time.perf_counter()
        cur.execute("select 1 as ok")
        pg_row = cur.fetchone()
        pg_ms = round((time.perf_counter() - pg_t0) * 1000, 2)
        deps: list[dict[str, Any]] = [
            {
                "name": "database",
                "status": "present" if pg_row is not None else "unknown",
                "elapsed_ms": pg_ms,
                "detail": "Postgres ops reader reachable" if pg_row is not None else "Probe unverified",
            }
        ]
        graph_t0 = time.perf_counter()
        cur.execute(
            "select to_regclass('staging.entities') as e, "
            "to_regclass('staging.entity_aliases') as a, "
            "to_regclass('staging.entity_relations') as r"
        )
        graph_row = cur.fetchone()
        graph_ms = round((time.perf_counter() - graph_t0) * 1000, 2)
        if graph_row is None:
            deps.append({"name": "graph_store", "status": "unknown", "elapsed_ms": graph_ms, "detail": "Not queried"})
        elif all(graph_row.get(k) is not None for k in ("e", "a", "r")):
            deps.append(
                {
                    "name": "graph_store",
                    "status": "present",
                    "elapsed_ms": graph_ms,
                    "detail": "Postgres entity graph tables present",
                }
            )
        else:
            deps.append(
                {
                    "name": "graph_store",
                    "status": "absent",
                    "elapsed_ms": graph_ms,
                    "detail": "Postgres entity graph tables missing",
                }
            )
        if pg_row is not None and (settings.s3_endpoint or "").strip():
            s3_res = ObjectStorageCheck(settings).probe()
            deps.append(
                {
                    "name": s3_res.name,
                    "status": s3_res.status.value,
                    "elapsed_ms": round(s3_res.duration_ms, 2),
                    "detail": s3_res.detail,
                }
            )

        served_heads = _optional_rows(cur, SERVED_HEADS_SQL)
        nightly_verdicts = _optional_rows(cur, NIGHTLY_VERDICTS_SQL)
        failed_job_errors = _optional_rows(cur, FAILED_JOB_ERRORS_SQL, table_check="dagster.event_logs")
        trigger_queue = _optional_rows(cur, TRIGGER_QUEUE_SQL)
        rollback_requests = _optional_rows(cur, ROLLBACK_QUEUE_SQL)

        return OpsOverviewResponse(
            runs=runs,
            data_engine=de,
            app_git_sha=settings.git_commit_sha,
            served_heads=served_heads,
            nightly_verdicts=nightly_verdicts,
            dependencies=deps,
            failed_job_errors=failed_job_errors,
            trigger_queue=trigger_queue,
            rollback_requests=rollback_requests,
        )

    return with_ops_reader(_q, database_url=database_url)


def get_datahub_stats(*, database_url: str | None = None) -> DatahubStatsResponse:
    contract = load_environment_contract()
    sentinel_likes = [id_prefix_like(k) for k, u in contract.universes.items() if u.is_sentinel]

    def _rows(cur: Any, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]

    def _q(cur: Any) -> DatahubStatsResponse:
        env_rows = _optional_rows(cur, "select environment from mart.environment_identity limit 1")
        env_name = str(env_rows[0]["environment"]) if env_rows and env_rows[0].get("environment") else settings.app_env
        normalized_env = contract.normalize_environment(env_name)

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
        usage_rows = _optional_rows(
            cur,
            """
            select l.source,
                   (count(*) filter (where l.called_at > now() - interval '24 hours'))::int as calls_24h,
                   (count(*) filter (where l.called_at > now() - interval '24 hours' and not l.ok))::int as failed_24h,
                   (count(*) filter (where l.called_at > now() - interval '24 hours' and (l.status_code = 429 or coalesce(l.error, '') ilike %s)))::int as rate_limited_24h,
                   (count(*) filter (where l.called_at > now() - interval '7 days'))::int as calls_7d,
                   (count(*) filter (where l.called_at > now() - interval '7 days' and not l.ok))::int as failed_7d
            from staging.api_call_ledger l
            where l.called_at > now() - interval '7 days'
            group by l.source
            order by l.source
            """,
            ("%1302%",),
        )
        vendor_accounts, capacity = _build_vendor_capacity(contract, normalized_env, usage_rows)
        model_1302_by_day = _optional_rows(
            cur,
            f"""
            select to_char((l.called_at at time zone 'utc')::date, 'YYYY-MM-DD') as day,
                   l.source,
                   (count(*) filter (where l.status_code = 429))::int as status_429,
                   (count(*) filter (where coalesce(l.error, '') ilike %s or coalesce(l.error, '') ilike %s))::int as code_1302,
                   count(*)::int as total_calls,
                   (count(*) filter (where not l.ok))::int as failed_calls
            from staging.api_call_ledger l
            where l.called_at >= {UTC_DAY_START_SQL} - interval '13 days'
            group by 1, 2
            having count(*) filter (where l.status_code = 429 or not l.ok) > 0
            order by 1 desc, 2
            """,
            ("%1302%", "%rate limit%"),
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
        oracle_disagreements = _optional_rows(cur, ORACLE_DISAGREEMENTS_SQL)
        served_heads = _optional_rows(cur, SERVED_HEADS_SQL)
        served_artifacts = _optional_rows(
            cur,
            "select artifact_key, lane, schedule_name, universe_like, family, served_to, wired "
            "from mart.served_artifact order by lane, artifact_key",
        )
        nightly_verdicts = _optional_rows(cur, NIGHTLY_VERDICTS_SQL)
        output_invariants = _optional_rows(
            cur,
            """
            select distinct on (invariant_id)
              invariant_id, kind, population, violation_count, deferred, sample_rows, dagster_run_id,
              to_char(checked_at at time zone 'utc', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') as checked_at
            from mart.output_invariant_results
            order by invariant_id, checked_at desc, result_id desc
            """,
        )
        llm_usage = _optional_rows(
            cur,
            f"""
            select to_char((started_at at time zone 'utc')::date, 'YYYY-MM-DD') as day,
                   provider,
                   coalesce(served_model, model) as served_model,
                   standard,
                   count(*)::int as calls,
                   coalesce(sum(prompt_tokens), 0)::bigint as prompt_tokens,
                   coalesce(sum(completion_tokens), 0)::bigint as completion_tokens,
                   coalesce(sum(cost), 0)::text as cost,
                   (count(*) filter (where status_code is distinct from 200))::int as error_calls,
                   (count(*) filter (where coalesce(decision->>'abstained', 'false') = 'true'
                                         or coalesce(decision->>'refusal', '') <> ''))::int as refusal_calls
            from staging.model_invocations
            where started_at >= {UTC_DAY_START_SQL} - interval '13 days'
            group by 1, 2, 3, 4
            order by 1 desc, calls desc
            limit 100
            """,
        )
        backfill_health = _optional_rows(
            cur,
            """
            select id, source, metric, value::text as value, note,
                   to_char(logged_at at time zone 'utc', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') as logged_at
            from staging.ingestion_health_log
            order by logged_at desc, id desc
            limit 40
            """,
        )
        trigger_queue = _optional_rows(cur, TRIGGER_QUEUE_SQL)
        rollback_requests = _optional_rows(cur, ROLLBACK_QUEUE_SQL)
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
            capacity=capacity,
            vendor_accounts=vendor_accounts,
            model_1302_by_day=model_1302_by_day,
            traffic=traffic,
            recent_calls=recent_calls,
            ledger_coverage=ledger_cov,
            question_coverage=question_cov,
            confidence=confidence,
            oracle_outcomes=oracle_outcomes,
            served_heads=served_heads,
            served_artifacts=served_artifacts,
            nightly_verdicts=nightly_verdicts,
            output_invariants=output_invariants,
            oracle_disagreements=oracle_disagreements,
            llm_usage=llm_usage,
            backfill_health=backfill_health,
            trigger_queue=trigger_queue,
            rollback_requests=rollback_requests,
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


def request_head_rollback(
    principal_id: str, req: HeadRollbackRequest, *, database_url: str | None = None
) -> HeadRollbackResponse:
    universe_id = req.universe_id.strip()
    universe_version = req.universe_version.strip()
    factor_id = req.factor_id.strip()
    target_run_id = req.target_run_id.strip()
    reason = req.reason.strip()
    if not universe_id or not universe_version or not factor_id or not reason:
        raise ValueError("universe_id, universe_version, factor_id, and reason must be non-empty")
    if not target_run_id.startswith("capture-run:"):
        raise ValueError("target_run_id must start with 'capture-run:'")

    contract = load_environment_contract()

    def _read_env(cur: Any) -> str:
        cur.execute("select environment from mart.environment_identity limit 1")
        row = cur.fetchone()
        if row and row.get("environment"):
            return str(row["environment"])
        return contract.normalize_environment(req.environment or settings.app_env)

    declared_env = with_ops_reader(_read_env, database_url=database_url)
    if req.environment is not None and contract.normalize_environment(
        req.environment
    ) != contract.normalize_environment(declared_env):
        raise ValueError(f"environment {req.environment!r} does not match declared environment {declared_env!r}")

    dedupe_key = str(uuid.uuid4())

    def _insert(cur: Any) -> tuple[int, str]:
        cur.execute(
            """
            insert into staging.head_rollback_requests (
                environment, universe_id, universe_version, factor_id,
                target_run_id, reason, requested_by, dedupe_key
            )
            values (%s, %s, %s, %s, %s, %s, %s, %s)
            returning request_id, to_char(requested_at at time zone 'utc', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') as requested_at
            """,
            (declared_env, universe_id, universe_version, factor_id, target_run_id, reason, principal_id, dedupe_key),
        )
        row = cur.fetchone()
        return int(row["request_id"]), str(row["requested_at"])

    req_id, requested_at = with_app_runtime(_insert, database_url=database_url)
    return HeadRollbackResponse(
        request_id=req_id,
        dedupe_key=dedupe_key,
        environment=declared_env,
        universe_id=universe_id,
        universe_version=universe_version,
        factor_id=factor_id,
        target_run_id=target_run_id,
        reason=reason,
        requested_at=requested_at,
    )


def build_datahub_fact_diff(stats: DatahubStatsResponse) -> dict[str, Any]:
    """Summarize per-universe question deltas and head withhold/staleness state for AI/MCP consumers."""
    withheld_heads = [h for h in stats.served_heads if h.get("run_id") is None]
    stale_heads = [h for h in stats.served_heads if h.get("freshness") == "stale"]
    question_deltas: dict[str, Any] = {}
    for row in stats.question_coverage:
        universe_id = str(row.get("universe_id") or "")
        payload = row.get("payload")
        if not universe_id or not isinstance(payload, dict):
            continue
        q_map = payload.get("questions") or {}
        deltas = {
            q_key: q_val["change"]
            for q_key, q_val in q_map.items()
            if isinstance(q_val, dict) and q_val.get("change") is not None
        }
        question_deltas[universe_id] = {
            "previous_run_id": payload.get("previous_run_id"),
            "deltas": deltas,
        }
    return {
        "withheld_head_count": len(withheld_heads),
        "withheld_heads": withheld_heads,
        "stale_head_count": len(stale_heads),
        "stale_heads": stale_heads,
        "question_deltas": question_deltas,
    }


__all__ = [
    "CONFIDENCE_REPORT_SQL",
    "DatahubStatsResponse",
    "FAILED_JOB_ERRORS_SQL",
    "HeadRollbackRequest",
    "HeadRollbackResponse",
    "LEDGER_COVERAGE_DAYS",
    "NIGHTLY_VERDICTS_SQL",
    "NON_CANARY_UNIVERSE_WHERE",
    "ORACLE_DISAGREEMENTS_SQL",
    "ORACLE_OUTCOMES_SQL",
    "OpsDataEngineBuildDto",
    "OpsOverviewResponse",
    "OpsRunRowDto",
    "QUESTION_COVERAGE_SQL",
    "REGISTERED_RAW_SOURCE_FILTER",
    "ROLLBACK_QUEUE_SQL",
    "SERVED_HEADS_SQL",
    "SWEEP_RUN_SOURCE_REGEX_SOURCE",
    "TRIGGER_QUEUE_SQL",
    "TriggerPipelineRequest",
    "TriggerPipelineResponse",
    "UTC_DAY_START_SQL",
    "build_datahub_fact_diff",
    "get_datahub_stats",
    "get_ops_overview",
    "governed_latest_per_universe_sql",
    "request_head_rollback",
    "trigger_pipeline",
]
