"""Administrator operational, DataHub, and manual pipeline trigger services."""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from typing import Any

import psycopg
from pydantic import BaseModel, Field
from truealpha_contracts.confidence import wilson_95_upper_bound
from truealpha_contracts.environment_contract import load_environment_contract
from truealpha_contracts.goal_criteria import load_goal_criteria
from truealpha_contracts.universes import id_prefix_like, job_name
from truealpha_runtime.checks import ObjectStorageCheck

from llm_service.config import settings
from llm_service.services._db import with_app_runtime, with_ops_reader

SWEEP_RUN_SOURCE_REGEX_SOURCE = "-[0-9]{8}T[0-9]{4}$"
REGISTERED_RAW_SOURCE_FILTER = f"not (source ~ '{SWEEP_RUN_SOURCE_REGEX_SOURCE}')"
UTC_DAY_START_SQL = "(date_trunc('day', now() at time zone 'utc') at time zone 'utc')"
NON_CANARY_UNIVERSE_WHERE = "s.universe_id not like all (%s::text[])"
LEDGER_COVERAGE_DAYS = 8


def governed_latest_per_universe_sql(table: str, extra_cols: str = "", payload_expr: str = "r.payload") -> str:
    return f"""
  select distinct on (r.universe_id)
         r.universe_id{extra_cols},
         r.run_id as report_run_id,
         coalesce(h.run_id, h.head_run_id) as served_head_run_id,
         (h.run_id is not null and r.run_id = h.run_id) as matches_served_head,
         r.cutoff::text, r.created_at::text, {payload_expr} as payload
  from {table} r
  join mart.served_head h on h.universe_id = r.universe_id
  where h.factor_id = 'gross_profit_per_employee'
    and not starts_with(h.universe_id, 'universe:canary-')
  order by r.universe_id,
           (r.run_id = coalesce(h.run_id, h.head_run_id)) desc,
           r.created_at desc
"""


QUESTION_COVERAGE_SQL = governed_latest_per_universe_sql("mart.question_coverage_report")
CONFIDENCE_REPORT_SQL = """
  with latest as (
    select distinct on (r.universe_id)
           r.universe_id, r.run_id,
           r.run_id as report_run_id,
           coalesce(h.run_id, h.head_run_id) as served_head_run_id,
           (h.run_id is not null and r.run_id = h.run_id) as matches_served_head,
           r.cutoff::text, r.created_at::text, r.payload
    from mart.datahub_confidence_report r
    join mart.served_head h on h.universe_id = r.universe_id
    where h.factor_id = 'gross_profit_per_employee'
      and not starts_with(h.universe_id, 'universe:canary-')
    order by r.universe_id,
             (r.run_id = coalesce(h.run_id, h.head_run_id)) desc,
             r.created_at desc
  )
  select universe_id, run_id, report_run_id, served_head_run_id,
         matches_served_head, cutoff, created_at,
         (payload - 'cells' - 'sample') || jsonb_build_object(
           'tier_counts',
           case
             when jsonb_typeof(latest.payload->'tier_counts') = 'object'
               then latest.payload->'tier_counts'
             else coalesce((
               select jsonb_object_agg(fam_key, counts)
               from (
                 select fam.key as fam_key,
                   jsonb_build_object(
                     'high', count(*) filter (where tier_val = 'high' or (tier_val = '' and conf_num >= 0.80)),
                     'medium', count(*) filter (where tier_val = 'medium' or (tier_val = '' and conf_num >= 0.60 and conf_num < 0.80)),
                     'low', count(*) filter (where not coalesce(tier_val in ('high', 'medium') or (tier_val = '' and conf_num >= 0.60), false))
                   ) as counts
                 from jsonb_each(case when jsonb_typeof(latest.payload->'cells') = 'object' then latest.payload->'cells' else '{}'::jsonb end) as fam(key, value)
                 cross join lateral jsonb_each(case when jsonb_typeof(fam.value) = 'object' then fam.value else '{}'::jsonb end) as cell(key, value)
                 cross join lateral (
                   select btrim(lower(coalesce(cell.value->>'tier', ''))) as tier_val,
                          case when coalesce(cell.value->>'confidence', '') ~ '^-?[0-9]+(\\.[0-9]+)?$'
                               then (cell.value->>'confidence')::numeric else null end as conf_num
                 ) parsed
                 group by fam.key
               ) t
             ), '{}'::jsonb)
           end
         ) as payload
  from latest
"""
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
  )
  select universe_id, field,
    (count(*) filter (where result in ('agree', 'disagree')))::int as compared,
    (count(*) filter (where result = 'agree'))::int as agree,
    (count(*) filter (where result = 'disagree'))::int as disagree,
    (count(*) filter (where result = 'not_comparable'))::int as not_comparable,
    (count(*) filter (where result = 'not_comparable' and reason = 'no_served_value'))::int as no_served_value_count,
    to_char(max(compared_at) at time zone 'utc', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') as newest_compared_at
  from deduped
  group by universe_id, field
  order by universe_id, field
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
  select h.environment, h.universe_id, h.universe_version, h.factor_id, h.sequence,
         h.head_run_id, h.run_id, h.artifact_key, h.family,
         sa.served_to,
         b.strategy_run_id as bound_strategy_run_id,
         h.age_hours::float as age_hours, h.limit_hours::float as limit_hours,
         h.freshness, h.availability, h.staleness_reason,
         to_char(h.advanced_at at time zone 'utc', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') as advanced_at
  from mart.served_head h
  left join mart.served_artifact sa on sa.artifact_key = h.artifact_key
  left join lateral (
    select b.strategy_run_id
    from mart.strategy_run_capture_bindings b
    where b.capture_run_id = coalesce(h.run_id, h.head_run_id)
    order by b.bound_at desc, b.strategy_run_id desc
    limit 1
  ) b on true
  order by h.universe_id, h.factor_id
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


STRATEGY_RUN_COUNTS_SQL = """
  with latest as (
    select distinct on (strategy_key)
      strategy_run_id, strategy_key,
      to_char(executed_at at time zone 'utc', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') as executed_at
    from mart.strategy_runs
    order by strategy_key, executed_at desc, created_at desc, strategy_run_id desc
  )
  select l.strategy_key, l.strategy_run_id, l.executed_at,
         count(d.strategy_decision_id)::int as total_count,
         (count(*) filter (where d.eligible))::int as eligible_count,
         (count(*) filter (where d.outcome = 'selected'))::int as selected_count
  from latest l
  left join mart.strategy_decisions d on d.strategy_run_id = l.strategy_run_id
  group by l.strategy_key, l.strategy_run_id, l.executed_at
  order by l.strategy_key
"""

BACKTEST_LATEST_SQL = """
  select distinct on (strategy_key, universe_id)
    strategy_key, universe_id, status, run_id,
    to_char(executed_at at time zone 'utc', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') as executed_at,
    cagr_monthly::text as cagr_monthly,
    sharpe_daily::text as sharpe_daily
  from mart.backtest_runs
  order by strategy_key, universe_id, executed_at desc, created_at desc, run_id desc
"""


class ConstructionQuestionDto(BaseModel):
    question_id: str
    answered: int = 0
    denominator: int = 0
    actionable_denominator: int = 0
    actionable_coverage_pct: float | None = None
    world_absence: int = 0
    pipeline_gap: int = 0
    not_applicable: int = 0


class ConstructionUniverseDto(BaseModel):
    universe_key: str
    universe_id: str | None = None
    scheduled: bool = False
    head_run_id: str | None = None
    served_run_id: str | None = None
    freshness: str | None = None
    availability: str | None = None
    report_matches_head: bool = False
    questions: list[ConstructionQuestionDto] = Field(default_factory=list)


class ConstructionStrategyRunDto(BaseModel):
    strategy_key: str
    strategy_run_id: str
    executed_at: str
    selected_count: int = 0
    eligible_count: int = 0
    total_count: int = 0


class ConstructionBacktestDto(BaseModel):
    strategy_key: str
    universe_id: str
    status: str
    run_id: str
    executed_at: str
    cagr_monthly: str | None = None
    sharpe_daily: str | None = None


class ConstructionProgressDto(BaseModel):
    environment: str
    criteria_total: int = 0
    criteria_verified: int = 0
    criteria_partial: int = 0
    criteria_pending: int = 0
    criteria_failed_or_stale: int = 0
    criteria: list[dict[str, Any]] = Field(default_factory=list)
    universes: list[ConstructionUniverseDto] = Field(default_factory=list)
    strategy_runs: list[ConstructionStrategyRunDto] = Field(default_factory=list)
    backtest_runs: list[ConstructionBacktestDto] = Field(default_factory=list)


def _parse_iso_utc(raw: Any) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.astimezone(UTC) if raw.tzinfo else raw.replace(tzinfo=UTC)
    text = str(raw).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _verdict_cadence_map(contract: Any, env: str | None) -> dict[str, float]:
    verdicts_cfg = getattr(contract, "nightly_verdicts", None) or {}
    checks = verdicts_cfg.get("checks", {})
    per_universe = verdicts_cfg.get("per_universe", {})
    cadences: dict[str, float] = {
        str(name): float(meta.get("cadence_hours", 24)) for name, meta in checks.items() if isinstance(meta, dict)
    }
    scheduled_lanes = [contract.universes[k].lane_name for k in contract.scheduled_universes(env)]
    if not scheduled_lanes and not env:
        scheduled_lanes = [u.lane_name for u in contract.universes.values() if not u.is_sentinel]
    for name, meta in per_universe.items():
        if isinstance(meta, dict):
            cadence = float(meta.get("cadence_hours", 24))
            for lane in scheduled_lanes:
                cadences[f"{name}@{lane}"] = cadence
    return cadences


def _enrich_nightly_verdicts(
    verdicts: list[dict[str, Any]],
    *,
    env: str | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    contract = load_environment_contract()
    check_now = now or datetime.now(UTC)
    cadence_by_name = _verdict_cadence_map(contract, env)
    all_per_universe = (getattr(contract, "nightly_verdicts", None) or {}).get("per_universe", {})
    enriched: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in verdicts:
        item = dict(row)
        name = str(item.get("check_name") or "")
        seen.add(name)
        base_name = name.split("@", 1)[0]
        cadence_hours = cadence_by_name.get(name)
        if cadence_hours is None and "@" in name and isinstance(all_per_universe.get(base_name), dict):
            cadence_hours = float(all_per_universe[base_name].get("cadence_hours", 24))
        max_age_hours = round(cadence_hours * 2.0, 2) if cadence_hours is not None else None
        ran_at_dt = _parse_iso_utc(item.get("ran_at"))
        age_hours = round(max(0.0, (check_now - ran_at_dt).total_seconds() / 3600.0), 2) if ran_at_dt else None
        ok_val = item.get("ok")
        if age_hours is not None and max_age_hours is not None and age_hours > max_age_hours:
            freshness_state = "stale"
        elif ok_val is True:
            freshness_state = "pass"
        elif ok_val is False:
            freshness_state = "fail"
        else:
            freshness_state = "pending"
        expected = contract.expects_verdict(name, env) if name else False
        item.update(
            {
                "freshness_state": freshness_state,
                "age_hours": age_hours,
                "cadence_hours": cadence_hours,
                "max_age_hours": max_age_hours,
                "expected": expected,
            }
        )
        enriched.append(item)
    for name, cadence_hours in sorted(cadence_by_name.items()):
        if name in seen or not contract.expects_verdict(name, env):
            continue
        enriched.append(
            {
                "check_name": name,
                "ok": None,
                "summary": "missing from mart.nightly_verdicts",
                "dagster_run_id": None,
                "ran_at": None,
                "recorded_at": None,
                "freshness_state": "missing",
                "age_hours": None,
                "cadence_hours": cadence_hours,
                "max_age_hours": round(cadence_hours * 2.0, 2),
                "expected": True,
            }
        )
    return enriched


def _question_dto_from_payload(question_id: str, raw: Any) -> ConstructionQuestionDto:
    entry = raw if isinstance(raw, dict) else {}
    answered = int(entry.get("answered", 0) or 0)
    denominator = int(entry.get("denominator", 0) or 0)
    not_applicable = int(entry.get("not_applicable", 0) or 0)
    raw_by_class = entry.get("by_class")
    by_class: dict[str, Any] = raw_by_class if isinstance(raw_by_class, dict) else {}
    world_absence = int(entry.get("world_absence", by_class.get("world_absence", 0)) or 0)
    pipeline_gap = int(entry.get("pipeline_gap", by_class.get("pipeline_gap", 0)) or 0)
    actionable_raw = entry.get("actionable_denominator")
    if actionable_raw is not None:
        actionable_denominator = int(actionable_raw)
    else:
        actionable_denominator = max(0, denominator - world_absence)
    pct_raw = entry.get("actionable_coverage_pct")
    if pct_raw is not None:
        actionable_pct: float | None = float(pct_raw)
    elif actionable_denominator > 0:
        actionable_pct = round(answered * 100.0 / actionable_denominator, 2)
    else:
        actionable_pct = None
    return ConstructionQuestionDto(
        question_id=question_id,
        answered=answered,
        denominator=denominator,
        actionable_denominator=actionable_denominator,
        actionable_coverage_pct=actionable_pct,
        world_absence=world_absence,
        pipeline_gap=pipeline_gap,
        not_applicable=not_applicable,
    )


def _evaluate_criteria_progress(
    enriched_verdicts: list[dict[str, Any]],
    *,
    contract: Any,
    env: str | None,
) -> tuple[dict[str, int], list[dict[str, Any]]]:
    by_name = {str(v.get("check_name") or ""): v for v in enriched_verdicts}
    scheduled_lanes = {contract.universes[k].lane_name for k in contract.scheduled_universes(env)}
    counts = {"total": 0, "verified": 0, "partial": 0, "pending": 0, "failed_or_stale": 0}
    rows: list[dict[str, Any]] = []
    for criterion in load_goal_criteria().criteria:
        counts["total"] += 1
        applicable = [
            key
            for key in criterion.verdict_keys
            if "@" not in key or not scheduled_lanes or key.split("@", 1)[1] in scheduled_lanes
        ]
        if criterion.verdict_keys and not applicable:
            counts["verified"] += 1
            rows.append(
                {
                    "id": criterion.id,
                    "root": criterion.root,
                    "tier": criterion.tier,
                    "status": criterion.status,
                    "outcome": "not_applicable",
                    "evidence": f"not scheduled in {env or 'environment'}",
                }
            )
            continue
        if not applicable:
            if criterion.status == "backed":
                counts["verified"] += 1
                outcome = "verified"
                evidence = "backed by contract/CI suite"
            else:
                outcome = "partial" if criterion.status == "partial" else "pending"
                counts[outcome] += 1
                evidence = f"awaiting {criterion.pending_on}" if criterion.pending_on else "unwired"
            rows.append(
                {
                    "id": criterion.id,
                    "root": criterion.root,
                    "tier": criterion.tier,
                    "status": criterion.status,
                    "outcome": outcome,
                    "evidence": evidence,
                }
            )
            continue
        parts: list[str] = []
        bad = False
        for key in applicable:
            v = by_name.get(key)
            state = str(v.get("freshness_state")) if v is not None else "missing"
            if state != "pass":
                bad = True
                parts.append(f"{key}={state}")
            else:
                parts.append(f"{key}=pass")
        if bad:
            counts["failed_or_stale"] += 1
            outcome = "failed_or_stale"
        elif criterion.status == "partial":
            counts["partial"] += 1
            outcome = "partial"
            if criterion.pending_on:
                parts.append(f"partial({criterion.pending_on})")
        elif criterion.status == "pending":
            counts["pending"] += 1
            outcome = "pending"
            if criterion.pending_on:
                parts.append(f"pending({criterion.pending_on})")
        else:
            counts["verified"] += 1
            outcome = "verified"
        rows.append(
            {
                "id": criterion.id,
                "root": criterion.root,
                "tier": criterion.tier,
                "status": criterion.status,
                "outcome": outcome,
                "evidence": "; ".join(parts),
            }
        )
    return counts, rows


def _build_construction_progress(
    cur: Any,
    *,
    env: str | None,
    enriched_verdicts: list[dict[str, Any]],
    served_heads: list[dict[str, Any]],
) -> ConstructionProgressDto:
    contract = load_environment_contract()
    counts, criteria_rows = _evaluate_criteria_progress(enriched_verdicts, contract=contract, env=env)
    question_cov = _optional_rows(cur, QUESTION_COVERAGE_SQL, table_check="mart.question_coverage_report")
    strategy_rows = _optional_rows(cur, STRATEGY_RUN_COUNTS_SQL, table_check="mart.strategy_runs")
    backtest_rows = _optional_rows(cur, BACKTEST_LATEST_SQL, table_check="mart.backtest_runs")

    head_by_universe: dict[str, dict[str, Any]] = {}
    for row in served_heads:
        if (
            row.get("factor_id") in ("gross_profit_per_employee", None)
            or row.get("universe_id") not in head_by_universe
        ):
            uid = str(row.get("universe_id") or "")
            if uid:
                head_by_universe[uid] = row

    cov_by_universe: dict[str, dict[str, Any]] = {
        str(row.get("universe_id") or ""): row for row in question_cov if row.get("universe_id")
    }
    scheduled = set(contract.scheduled_universes(env))
    universes: list[ConstructionUniverseDto] = []
    for ukey, urec in contract.universes.items():
        if urec.is_sentinel:
            continue
        matched_uid = next(
            (uid for uid in head_by_universe if uid.startswith(urec.id_prefix)),
            next((uid for uid in cov_by_universe if uid.startswith(urec.id_prefix)), None),
        )
        head = head_by_universe.get(matched_uid or "", {})
        cov = cov_by_universe.get(matched_uid or "", {})
        raw_payload = cov.get("payload")
        payload: dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}
        raw_q_map = payload.get("questions")
        q_map: dict[str, Any] = raw_q_map if isinstance(raw_q_map, dict) else {}
        questions = [_question_dto_from_payload(str(qid), q_map[qid]) for qid in sorted(q_map)]
        universes.append(
            ConstructionUniverseDto(
                universe_key=ukey,
                universe_id=matched_uid,
                scheduled=ukey in scheduled,
                head_run_id=head.get("head_run_id"),
                served_run_id=head.get("run_id"),
                freshness=head.get("freshness"),
                availability=head.get("availability"),
                report_matches_head=bool(cov.get("matches_served_head", False)),
                questions=questions,
            )
        )

    return ConstructionProgressDto(
        environment=env or "unknown",
        criteria_total=counts["total"],
        criteria_verified=counts["verified"],
        criteria_partial=counts["partial"],
        criteria_pending=counts["pending"],
        criteria_failed_or_stale=counts["failed_or_stale"],
        criteria=criteria_rows,
        universes=universes,
        strategy_runs=[
            ConstructionStrategyRunDto(
                strategy_key=str(r["strategy_key"]),
                strategy_run_id=str(r["strategy_run_id"]),
                executed_at=str(r["executed_at"]),
                selected_count=int(r.get("selected_count") or 0),
                eligible_count=int(r.get("eligible_count") or 0),
                total_count=int(r.get("total_count") or 0),
            )
            for r in strategy_rows
        ],
        backtest_runs=[
            ConstructionBacktestDto(
                strategy_key=str(r["strategy_key"]),
                universe_id=str(r["universe_id"]),
                status=str(r["status"]),
                run_id=str(r["run_id"]),
                executed_at=str(r["executed_at"]),
                cagr_monthly=r.get("cagr_monthly"),
                sharpe_daily=r.get("sharpe_daily"),
            )
            for r in backtest_rows
        ],
    )


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
    construction_progress: ConstructionProgressDto | None = None


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
    db_hygiene: dict[str, Any] = Field(default_factory=dict)


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
    try:
        with cur.connection.transaction():
            if table_check:
                cur.execute("select to_regclass(%s) as reg", (table_check,))
                reg_row = cur.fetchone()
                if reg_row is not None and reg_row.get("reg") is None:
                    return []
            if params:
                cur.execute(sql, params)
            else:
                cur.execute(sql)
            return [dict(r) for r in cur.fetchall()]
    except (psycopg.errors.UndefinedTable, psycopg.errors.InsufficientPrivilege):
        return []


def _ledger_source_match(account_name: str, usage_by_source: dict[str, dict[str, Any]]) -> dict[str, Any]:
    norm = account_name.strip().lower()
    if norm in usage_by_source:
        return usage_by_source[norm]
    for source, stats in usage_by_source.items():
        if norm in source.lower() or source.lower() in norm:
            return stats
    return {}


def _compute_tier_counts(cells_by_family: Any) -> dict[str, dict[str, int]]:
    if not isinstance(cells_by_family, dict):
        return {}
    counts: dict[str, dict[str, int]] = {}
    for fam_name, entries in cells_by_family.items():
        if not isinstance(entries, dict):
            continue
        high = med = low = 0
        for cell in entries.values():
            if not isinstance(cell, dict):
                continue
            tier = str(cell.get("tier") or "").strip().lower()
            conf_raw = cell.get("confidence")
            try:
                conf_num = float(conf_raw) if conf_raw is not None and str(conf_raw).strip() != "" else None
            except (TypeError, ValueError):
                conf_num = None
            if tier == "high" or (not tier and conf_num is not None and conf_num >= 0.80):
                high += 1
            elif tier == "medium" or (not tier and conf_num is not None and conf_num >= 0.60):
                med += 1
            else:
                low += 1
        counts[str(fam_name)] = {"high": high, "medium": med, "low": low}
    return counts


def _summarize_confidence_row(row: dict[str, Any]) -> dict[str, Any]:
    """Strip multi-megabyte per-entity maps from confidence report rows while preserving tier counts and summaries."""
    out = dict(row)
    raw_payload = out.get("payload")
    if isinstance(raw_payload, dict):
        tier_counts = raw_payload.get("tier_counts")
        if not isinstance(tier_counts, dict) or not tier_counts:
            tier_counts = _compute_tier_counts(raw_payload.get("cells"))
        compact_payload = {k: v for k, v in raw_payload.items() if k not in ("cells", "sample")}
        if tier_counts:
            compact_payload["tier_counts"] = tier_counts
        families = compact_payload.get("families")
        accuracy = compact_payload.get("accuracy")
        sources_connected = compact_payload.get("sources_connected")
        out["payload"] = compact_payload
        out["summary"] = {
            "subjects": compact_payload.get("subjects"),
            "sources_connected": sources_connected if isinstance(sources_connected, list) else [],
            "sources_count": len(sources_connected) if isinstance(sources_connected, list) else 0,
            "families_count": len(families) if isinstance(families, dict) else 0,
            "accuracy_fields": len(accuracy) if isinstance(accuracy, dict) else 0,
            "tier_counts": tier_counts,
            "report_version": compact_payload.get("report_version"),
            "generated_at": compact_payload.get("generated_at"),
            "mean_confidence": compact_payload.get("mean_confidence"),
        }
    return out


def _attach_wilson_upper_bounds(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        bound = wilson_95_upper_bound(int(item.get("disagree") or 0), int(item.get("compared") or 0))
        item["wilson_95_upper_bound"] = f"{bound:.6f}" if bound is not None else None
        out.append(item)
    return out


def _build_vendor_capacity(
    contract: Any, env_name: str, usage_rows: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    usage_by_source = {str(r["source"]): r for r in usage_rows if r.get("source") is not None}
    accounts_out: list[dict[str, Any]] = []
    capacity_out: list[dict[str, Any]] = []
    for name, acct in sorted(contract.vendor_accounts.accounts.items()):
        share = contract.account_share(name, env_name)
        usage = _ledger_source_match(name, usage_by_source)
        calls_1s = int(usage.get("calls_1s") or 0)
        calls_30s = int(usage.get("calls_30s") or 0)
        calls_60s = int(usage.get("calls_60s") or 0)
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
            window = lim.window_seconds
            share_val = lim.share
            if window is None:
                observed = calls_24h
                util = None
                remaining = None
            else:
                if window <= 1:
                    observed = calls_1s
                elif window <= 30:
                    observed = calls_30s
                elif window <= 60:
                    observed = calls_60s
                elif window <= 86400:
                    observed = calls_24h
                else:
                    observed = calls_7d
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

        env_name = str(getattr(settings, "environment", "") or "").strip() or None
        served_heads = _optional_rows(cur, SERVED_HEADS_SQL)
        raw_verdicts = _optional_rows(cur, NIGHTLY_VERDICTS_SQL)
        nightly_verdicts = _enrich_nightly_verdicts(raw_verdicts, env=env_name)
        failed_job_errors = _optional_rows(cur, FAILED_JOB_ERRORS_SQL, table_check="dagster.event_logs")
        trigger_queue = _optional_rows(cur, TRIGGER_QUEUE_SQL)
        rollback_requests = _optional_rows(cur, ROLLBACK_QUEUE_SQL)
        construction_progress = _build_construction_progress(
            cur,
            env=env_name,
            enriched_verdicts=nightly_verdicts,
            served_heads=served_heads,
        )

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
            construction_progress=construction_progress,
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
        env_name = (
            str(env_rows[0]["environment"]) if env_rows and env_rows[0].get("environment") else settings.environment
        )
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
                   (count(*) filter (where l.called_at > now() - interval '1 second'))::int as calls_1s,
                   (count(*) filter (where l.called_at > now() - interval '30 seconds'))::int as calls_30s,
                   (count(*) filter (where l.called_at > now() - interval '60 seconds'))::int as calls_60s,
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
        confidence = [_summarize_confidence_row(r) for r in _rows(cur, CONFIDENCE_REPORT_SQL)]
        oracle_outcomes = _attach_wilson_upper_bounds(_rows(cur, ORACLE_OUTCOMES_SQL))
        oracle_disagreements = _optional_rows(cur, ORACLE_DISAGREEMENTS_SQL)
        served_heads = _optional_rows(cur, SERVED_HEADS_SQL)
        served_artifacts = _optional_rows(
            cur,
            "select artifact_key, lane, schedule_name, universe_like, family, served_to, wired "
            "from mart.served_artifact order by lane, artifact_key",
        )
        env_name = str(getattr(settings, "environment", "") or "").strip() or None
        nightly_verdicts = _enrich_nightly_verdicts(_optional_rows(cur, NIGHTLY_VERDICTS_SQL), env=env_name)
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
        stats_reset_rows = _optional_rows(
            cur,
            "select to_char(stats_reset at time zone 'utc', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') as stats_reset "
            "from pg_stat_database where datname = current_database()",
        )
        zero_scan_indexes = _optional_rows(
            cur,
            "select s.schemaname, s.relname as table_name, s.indexrelname as index_name, "
            "pg_relation_size(s.indexrelid)::bigint as size_bytes "
            "from pg_stat_user_indexes s "
            "join pg_index i on i.indexrelid = s.indexrelid "
            "where s.schemaname in ('raw', 'staging', 'mart', 'app') "
            "  and not i.indisprimary and not i.indisunique and s.idx_scan = 0 "
            "order by pg_relation_size(s.indexrelid) desc, s.schemaname, s.relname, s.indexrelname limit 50",
        )
        idle_tables = _optional_rows(
            cur,
            "select schemaname, relname as table_name, coalesce(n_live_tup, 0)::bigint as n_live_tup, "
            "(coalesce(seq_scan, 0) + coalesce(idx_scan, 0))::bigint as total_scans, "
            "pg_total_relation_size(relid)::bigint as size_bytes "
            "from pg_stat_user_tables "
            "where schemaname in ('raw', 'staging', 'mart', 'app') "
            "  and coalesce(seq_scan, 0) = 0 and coalesce(idx_scan, 0) = 0 "
            "order by n_live_tup desc, pg_total_relation_size(relid) desc, schemaname, relname limit 50",
        )
        db_hygiene = {
            "stats_reset": stats_reset_rows[0].get("stats_reset") if stats_reset_rows else None,
            "zero_scan_indexes": zero_scan_indexes,
            "idle_tables": idle_tables,
        }
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
            db_hygiene=db_hygiene,
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
        return contract.normalize_environment(req.environment or settings.environment)

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
    """Summarize per-universe question deltas and head withhold/staleness/hygiene state for AI/MCP consumers."""
    withheld_heads = [h for h in stats.served_heads if h.get("run_id") is None]
    stale_heads = [h for h in stats.served_heads if h.get("freshness") == "stale"]
    mismatched_reports = [
        {
            "universe_id": r.get("universe_id"),
            "report_run_id": r.get("report_run_id"),
            "served_head_run_id": r.get("served_head_run_id"),
        }
        for r in stats.question_coverage
        if r.get("matches_served_head") is False
    ]
    zero_scan_indexes = stats.db_hygiene.get("zero_scan_indexes", [])
    idle_tables = stats.db_hygiene.get("idle_tables", [])
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
        "mismatched_report_count": len(mismatched_reports),
        "mismatched_reports": mismatched_reports,
        "zero_scan_index_count": len(zero_scan_indexes),
        "idle_table_count": len(idle_tables),
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
