"""Standards slow plane, weekly backfill, history backfill, and pointer-following head reports (#735 / #855)."""

import json
from collections.abc import Mapping
from contextlib import ExitStack
from datetime import UTC, datetime
from typing import Any

import dagster as dg
import psycopg
from truealpha_contracts.standards import STANDARDS
from truealpha_contracts.universes import key_of_lane, lane_name, universe_with_role

from data_engine.config import settings
from data_engine.datahub import analyst_ratings, question_coverage
from data_engine.datahub.canonical_issuer import (
    CanonicalUniverse,
    account_for_head_members,
    canonicalize_universe,
    is_canonical_issuer_id,
)
from data_engine.datahub.standards import planner, supply_chain_extraction
from data_engine.datahub.standards.backfill import never_backfilled
from data_engine.datahub.standards.backfill import run_standard_backfill as _run_standard_backfill
from data_engine.datahub.standards.history import raise_if_every_attempt_failed
from data_engine.datahub.standards.history import run_standard_history as _run_standard_history
from data_engine.datahub.standards.planner import universe_issuers
from data_engine.environment_universes import any_environment_universe_names, scheduled_universe_names
from data_engine.external_op import external_op
from data_engine.quality.nightly_verdicts import check_name, is_registered, newest_is_red, tick_from_config, verdict
from data_engine.sources import gateway

STANDARD_BACKFILL_JOB_NAME = "standard_backfill_pipeline"
STANDARD_BACKFILL_CRON = "7 9 * * 0"

STANDARD_HISTORY_JOB_NAME = "standard_history_backfill_pipeline"
STANDARD_HISTORY_CRON = "7 9 * * 1"
STANDARD_HISTORY_MONTHS = 36
STANDARDS_WITH_HISTORY: tuple[str, ...] = ("employees_total",)

THEME_PURITY_VERDICT = "theme_purity"
QUESTION_COVERAGE_VERDICT = "question_coverage"
NIGHTLY_VERDICTS: tuple[str, ...] = tuple(
    check_name(check, universe)
    for check in (THEME_PURITY_VERDICT, QUESTION_COVERAGE_VERDICT)
    for universe in any_environment_universe_names()
)

NPORT_MONTHLY_SCHEDULE_NAME = "nport_monthly_schedule"
NPORT_MONTHLY_CRON = "7 11 1 * *"
THIRTEENF_QUARTERLY_SCHEDULE_NAME = "13f_quarterly_schedule"
THIRTEENF_QUARTERLY_CRON = "7 12 15 1,4,7,10 *"
FORM4_DAILY_SCHEDULE_NAME = "form4_daily_schedule"
FORM4_DAILY_CRON = "7 13 * * *"
TENK_ANNUAL_SCHEDULE_NAME = "10k_annual_schedule"
TENK_ANNUAL_CRON = "7 14 * * 0"

STANDARDS_PLANE_CRONS: Mapping[str, str] = {
    NPORT_MONTHLY_SCHEDULE_NAME: NPORT_MONTHLY_CRON,
    THIRTEENF_QUARTERLY_SCHEDULE_NAME: THIRTEENF_QUARTERLY_CRON,
    FORM4_DAILY_SCHEDULE_NAME: FORM4_DAILY_CRON,
    TENK_ANNUAL_SCHEDULE_NAME: TENK_ANNUAL_CRON,
}
STANDARDS_PLANE_SCHEDULE_STATUS = dg.DefaultScheduleStatus.STOPPED

UNIVERSE_TAG = "truealpha/universe"
HEAD_RUN_TAG = "truealpha/head_run"

REPORTS_CURRENT = "reports_current"
LANE_FAILURE = "lane_failure"
UNMAPPED_ISSUERS = "unmapped_issuers"
UNMAPPED_BY_REASON = "unmapped_by_reason"
JOINED_ISSUERS = "joined_issuers"
WIDE_ROW_ISSUERS = "wide_row_issuers"
VERDICT_TEXT = "verdict_text"
VERDICT_FAILURE = "verdict_failure"
MAX_UNMAPPED_LOGGED = 20

STANDARD_BACKFILL_CATCHUP_SENSOR_NAME = "standard_backfill_catchup"
STANDARD_BACKFILL_CATCHUP_INTERVAL_SECONDS = 900

HEAD_REPORTS_JOB_NAME = "head_reports_pipeline"
HEAD_REPORTS_SENSOR_NAME = "head_reports_on_pointer_advance"
HEAD_REPORTS_SENSOR_INTERVAL_SECONDS = 30
HEAD_REPORTS_CRON = "0 4 * * *"

_DOWNSTREAM_OPS = ("run_theme_purity", "run_supply_chain_exposure", "run_analyst_ratings", "run_question_coverage")

__all__ = (
    "FORM4_DAILY_CRON",
    "FORM4_DAILY_SCHEDULE_NAME",
    "NIGHTLY_VERDICTS",
    "NPORT_MONTHLY_CRON",
    "NPORT_MONTHLY_SCHEDULE_NAME",
    "QUESTION_COVERAGE_VERDICT",
    "STANDARD_BACKFILL_CRON",
    "STANDARD_BACKFILL_JOB_NAME",
    "STANDARD_HISTORY_CRON",
    "STANDARD_HISTORY_JOB_NAME",
    "STANDARD_HISTORY_MONTHS",
    "STANDARDS_PLANE_CRONS",
    "STANDARDS_PLANE_SCHEDULE_STATUS",
    "STANDARDS_WITH_HISTORY",
    "StandardBackfillConfig",
    "StandardHistoryConfig",
    "TENK_ANNUAL_CRON",
    "TENK_ANNUAL_SCHEDULE_NAME",
    "THIRTEENF_QUARTERLY_CRON",
    "THIRTEENF_QUARTERLY_SCHEDULE_NAME",
    "THEME_PURITY_VERDICT",
    "analyst_ratings",
    "build_definitions",
    "build_standards_plane_schedules",
    "defs",
    "fail_if_a_lane_failed",
    "form4_daily_schedule",
    "nport_monthly_schedule",
    "run_analyst_ratings",
    "run_supply_chain_exposure",
    "supply_chain_extraction",
    "tenk_annual_schedule",
    "thirteenf_quarterly_schedule",
)


class StandardBackfillConfig(dg.Config):
    executed_at: str
    universe: str = lane_name(universe_with_role("fund_consolidation"))
    standard: str = ""
    mode: str = "backfill"
    max_issuers: int = 0


class StandardHistoryConfig(dg.Config):
    executed_at: str
    universe: str = lane_name(universe_with_role("strategy"))
    standard: str = ""
    months: int = STANDARD_HISTORY_MONTHS


class HeadReportsStartConfig(StandardBackfillConfig):
    only_if_stale: bool = False


def run_universe(run_config: Mapping[str, Any]) -> str:
    for op in (run_config.get("ops") or {}).values():
        universe = ((op or {}).get("config") or {}).get("universe")
        if universe:
            return str(universe)
    return str(StandardBackfillConfig.model_fields["universe"].default)


def standards_to_run(selected: str) -> tuple[str, ...]:
    if selected:
        if selected not in STANDARDS:
            raise ValueError(f"unknown standard {selected!r}; registered: {sorted(STANDARDS)}")
        return (selected,)
    return tuple(sorted(STANDARDS))


def history_standards(selected: str) -> tuple[str, ...]:
    return standards_to_run(selected) if selected else STANDARDS_WITH_HISTORY


def _downstream_ops_config(executed_at: str, universe: str) -> dict[str, StandardBackfillConfig]:
    cfg = StandardBackfillConfig(executed_at=executed_at, universe=universe)
    return dict.fromkeys(_DOWNSTREAM_OPS, cfg)


def _universe_tickers(connection: Any, universe: str, *, use_planner: bool = False) -> dict[str, str]:
    fn = planner.universe_issuers if use_planner else universe_issuers
    return {issuer.issuer_id: issuer.ticker for issuer in fn(connection, universe)}


def _json_dict(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _upstream_failure(summary: str) -> tuple[str | None, str | None]:
    parsed = _json_dict(summary)
    return parsed.get(LANE_FAILURE), parsed.get(VERDICT_TEXT)


def reports_current(upstream_summary: str) -> str | None:
    val = _json_dict(upstream_summary).get(REPORTS_CURRENT)
    return str(val) if val else None


def _current_passthrough(universe: str, run_id: str) -> str:
    return json.dumps({"universe": universe, "run_id": run_id, REPORTS_CURRENT: run_id})


def _no_head_response(context: dg.OpExecutionContext, universe: str, label: str) -> str:
    context.log.warning("no governed head for %s; no %s", universe, label)
    return json.dumps({"universe": universe, "rows": 0, "reason": "no_governed_head"})


def _already_current(context: dg.OpExecutionContext, check: str, config: StandardBackfillConfig, run_id: str) -> str:
    name = check_name(check, config.universe)
    if is_registered(name, NIGHTLY_VERDICTS) and newest_is_red(name):
        context.log.info("%s: the newest verdict is red; no green row written over it", name)
    else:
        with verdict(
            name, registered=NIGHTLY_VERDICTS, run_id=context.run_id, tick=tick_from_config(config.executed_at)
        ) as outcome:
            outcome.summary = f"reports already current on {run_id[:24]}; nothing recomputed"
        context.log.info("%s for %s: %s", check, config.universe, outcome.summary)
    return _current_passthrough(config.universe, run_id)


def _report_unmapped(context: dg.OpExecutionContext, lane: str, universe: CanonicalUniverse) -> None:
    for member in universe.unmapped[:MAX_UNMAPPED_LOGGED]:
        detail = f" ({member.detail})" if member.detail else ""
        context.log.warning(
            "%s: no row for %s (%s): %s%s", lane, member.ticker, member.legacy_id, member.reason, detail
        )
    if len(universe.unmapped) > MAX_UNMAPPED_LOGGED:
        context.log.warning(
            "%s: %s more issuers get no row, not named here", lane, len(universe.unmapped) - MAX_UNMAPPED_LOGGED
        )
    for issuer in universe.unvisited[:MAX_UNMAPPED_LOGGED]:
        context.log.warning("%s: unavailable row for %s (%s): %s", lane, issuer.ticker, issuer.legacy_id, issuer.reason)
    if len(universe.unvisited) > MAX_UNMAPPED_LOGGED:
        context.log.warning(
            "%s: %s more issuers get an unavailable row, not named here",
            lane,
            len(universe.unvisited) - MAX_UNMAPPED_LOGGED,
        )
    failure = universe.lane_failure()
    if failure is not None:
        context.log.error("%s: %s", lane, failure)


def _canonical_universe(
    context: dg.OpExecutionContext,
    lane: str,
    connection: Any,
    head: question_coverage.GovernedHead,
    tickers: Mapping[str, str],
) -> CanonicalUniverse:
    wide = {cell.subject_id for cell in question_coverage.gppe_cells(connection, head.run_id)}
    if not all(is_canonical_issuer_id(issuer_id) for issuer_id in wide):
        raise ValueError(f"the wide row of {head.run_id} holds an issuer id that is not a canonical UUID")
    resolved = CanonicalUniverse(issuers=())
    if tickers:
        resolved = canonicalize_universe(
            connection,
            tickers,
            cutoff=head.cutoff,
            as_of=question_coverage.head_report_date(connection, head),
            wide_row_ids=wide,
        )
    resolved = account_for_head_members(connection, resolved, cutoff=head.cutoff, wide_row_ids=wide)
    _report_unmapped(context, lane, resolved)
    return resolved


def _canonical_universe_or_failure(
    context: dg.OpExecutionContext,
    lane: str,
    connection: Any,
    head: question_coverage.GovernedHead,
    tickers: Mapping[str, str],
) -> tuple[CanonicalUniverse | None, str | None]:
    try:
        return _canonical_universe(context, lane, connection, head, tickers), None
    except (ValueError, psycopg.Error) as error:
        connection.rollback()
        context.log.error("%s: canonicalization failed: %s: %s", lane, type(error).__name__, error)
        return None, f"canonicalization failed: {type(error).__name__}"


@external_op
def run_standard_backfill(context: dg.OpExecutionContext, config: StandardBackfillConfig) -> str:
    cutoff = datetime.fromisoformat(config.executed_at)
    if config.mode not in ("backfill", "probe"):
        raise ValueError(f"mode must be backfill or probe, got {config.mode!r}")
    names = standards_to_run(config.standard)
    summaries = []
    with gateway.capacity_scope(), psycopg.connect(settings.database_url) as connection:
        for name in names:
            report = _run_standard_backfill(
                connection,
                universe=config.universe,
                standard_name=name,
                cutoff=cutoff,
                mode=config.mode,  # type: ignore[arg-type]
                max_issuers=config.max_issuers,
                log=context.log.info,
            )
            summaries.append(report.summary())
            context.add_output_metadata(
                {
                    f"{name}_issuers": report.issuers,
                    f"{name}_open_cells": report.open,
                    f"{name}_open_by_reason": str(dict(report.open_by_reason)),
                    f"{name}_outcomes": str(dict(report.outcomes)),
                }
            )
    context.add_output_metadata({"universe": config.universe, "standards": ", ".join(names), "mode": config.mode})
    return json.dumps(summaries, sort_keys=True)


@external_op
def run_theme_purity(context: dg.OpExecutionContext, config: StandardBackfillConfig, backfill_summary: str) -> str:
    from data_engine.datahub.production_topt.theme_purity import materialize_theme_purity, summary_line
    from data_engine.datahub.question_coverage import NoHead, governed_head_of

    context.log.info("theme purity follows backfill: %s", backfill_summary[:200])
    current = reports_current(backfill_summary)
    if current is not None:
        return _already_current(context, THEME_PURITY_VERDICT, config, current)
    with (
        verdict(
            check_name(THEME_PURITY_VERDICT, config.universe),
            registered=NIGHTLY_VERDICTS,
            run_id=context.run_id,
            tick=tick_from_config(config.executed_at),
        ) as outcome,
        psycopg.connect(settings.database_url) as connection,
    ):
        head = governed_head_of(connection, key_of_lane(config.universe))
        if isinstance(head, NoHead):
            outcome.pending = True
            outcome.summary = head.summary
            return _no_head_response(context, config.universe, "theme purity rows")
        rows = materialize_theme_purity(
            connection, run_id=head.run_id, cutoff=head.cutoff, tickers=_universe_tickers(connection, config.universe)
        )
        connection.commit()
        published = sum(1 for row in rows if row.result.value is not None)
        outcome.summary = f"{published}/{len(rows)} theme-purity rows published on {head.run_id[:24]}"
    context.log.info(summary_line(rows))
    summary = {"universe": config.universe, "run_id": head.run_id, "rows": len(rows), "published": published}
    context.add_output_metadata(summary)
    return json.dumps(summary)


@external_op
def run_supply_chain_exposure(
    context: dg.OpExecutionContext, config: StandardBackfillConfig, purity_summary: str
) -> str:
    from data_engine.datahub.question_coverage import NoHead, governed_head_of
    from data_engine.datahub.standards.supply_chain_extraction import materialize_universe_supply_chain_exposure

    context.log.info("supply chain exposure follows theme purity: %s", purity_summary[:200])
    current = reports_current(purity_summary)
    if current is not None:
        return _current_passthrough(config.universe, current)

    with gateway.capacity_scope(), psycopg.connect(settings.database_url) as connection:
        head = governed_head_of(connection, key_of_lane(config.universe))
        if isinstance(head, NoHead):
            return _no_head_response(context, config.universe, "supply chain exposure rows")
        universe, identity_failure = _canonical_universe_or_failure(
            context, "supply chain exposure", connection, head, _universe_tickers(connection, config.universe)
        )
        if universe is None:
            return json.dumps(
                {
                    "universe": config.universe,
                    "run_id": head.run_id,
                    "rows": 0,
                    LANE_FAILURE: identity_failure,
                    VERDICT_TEXT: f"supply chain: {identity_failure}",
                }
            )
        floor_failure = universe.lane_failure()
        joined = model_extracted = replayed = context_window_exceeded = 0
        exceeded_filings: tuple[str, ...] = ()
        if floor_failure is None:
            sc_report = materialize_universe_supply_chain_exposure(
                connection, run_id=head.run_id, cutoff=head.cutoff, issuers=universe.issuers
            )
            joined = int(sc_report)
            model_extracted = int(getattr(sc_report, "model_extracted", 0))
            replayed = int(getattr(sc_report, "replayed", 0))
            context_window_exceeded = int(getattr(sc_report, "context_window_exceeded", 0))
            exceeded_filings = tuple(getattr(sc_report, "exceeded_filings", ()))
        connection.commit()

    if context_window_exceeded > 0:
        context.log.error(
            "supply chain exposure: %s 10-K filing(s) exceeded 1M context window for %s: %s",
            context_window_exceeded,
            config.universe,
            ", ".join(exceeded_filings[:MAX_UNMAPPED_LOGGED]),
        )
    context.log.info(
        "published %s supply-chain exposure rows for %s (model_extracted=%s, replayed=%s, context_window_exceeded=%s)",
        joined,
        config.universe,
        model_extracted,
        replayed,
        context_window_exceeded,
    )
    summary: dict[str, Any] = {
        "universe": config.universe,
        "run_id": head.run_id,
        "rows": joined,
        JOINED_ISSUERS: joined,
        "model_extracted": model_extracted,
        "replayed": replayed,
        "context_window_exceeded": context_window_exceeded,
        UNMAPPED_ISSUERS: len(universe.unmapped),
        UNMAPPED_BY_REASON: universe.unmapped_by_reason(),
        WIDE_ROW_ISSUERS: universe.wide_row_issuers,
    }
    if floor_failure is not None:
        summary[LANE_FAILURE] = floor_failure
        summary[VERDICT_TEXT] = f"supply chain: {floor_failure}"
    elif context_window_exceeded > 0:
        limit_msg = f"supply chain: {context_window_exceeded} of {joined} filings exceeded 1M context window"
        summary[LANE_FAILURE] = limit_msg
        summary[VERDICT_TEXT] = limit_msg
    context.add_output_metadata(summary)
    return json.dumps(summary)


@external_op
def run_analyst_ratings(context: dg.OpExecutionContext, config: StandardBackfillConfig, sc_summary: str) -> str:
    from data_engine.datahub.analyst_ratings import materialize_universe_analyst_ratings
    from data_engine.datahub.question_coverage import NoHead, governed_head_of

    context.log.info("analyst ratings follows supply chain exposure: %s", sc_summary[:200])
    current = reports_current(sc_summary)
    if current is not None:
        return _current_passthrough(config.universe, current)

    with gateway.capacity_scope(), psycopg.connect(settings.database_url) as connection:
        head = governed_head_of(connection, key_of_lane(config.universe))
        if isinstance(head, NoHead):
            return _no_head_response(context, config.universe, "analyst ratings rows")
        universe, identity_failure = _canonical_universe_or_failure(
            context,
            "analyst ratings",
            connection,
            head,
            _universe_tickers(connection, config.universe, use_planner=True),
        )
        if universe is None:
            context.log.error("analyst ratings lane failed for %s: %s", config.universe, identity_failure)
            return json.dumps(
                {
                    "universe": config.universe,
                    "executed_at": config.executed_at,
                    "run_id": head.run_id,
                    "rows": 0,
                    "fetch_errors": 0,
                    LANE_FAILURE: identity_failure,
                    VERDICT_TEXT: f"analyst ratings: {identity_failure}",
                }
            )
        connection.rollback()
        floor_failure = universe.lane_failure()
        captured = analyst_ratings.UniverseCapture(rows=0)
        with ExitStack() as stack:
            if floor_failure is None and universe.issuers:
                ctx = None
                open_error: Exception | None = None
                try:
                    from data_engine.sources.moomoo import connect as moomoo_connect

                    ctx = stack.enter_context(moomoo_connect())
                except Exception as exc:
                    open_error = exc
                    context.log.error("moomoo OpenD context did not open: %s: %s", type(exc).__name__, exc)
                captured = materialize_universe_analyst_ratings(
                    connection,
                    run_id=head.run_id,
                    cutoff=head.cutoff,
                    tickers=universe.tickers(),
                    ctx=ctx,
                    open_error=open_error,
                )
            connection.commit()

    summary: dict[str, Any] = {
        "universe": config.universe,
        "executed_at": config.executed_at,
        "run_id": head.run_id,
        "rows": captured.rows,
        JOINED_ISSUERS: captured.rows,
        "fetch_errors": len(captured.failures),
        UNMAPPED_ISSUERS: len(universe.unmapped),
        UNMAPPED_BY_REASON: universe.unmapped_by_reason(),
        WIDE_ROW_ISSUERS: universe.wide_row_issuers,
    }
    lane_failure = captured.lane_failure()
    if lane_failure is None:
        lane_failure = floor_failure
        verdict_text = f"analyst ratings: {lane_failure}" if lane_failure else None
        if lane_failure is None:
            lane_failure, verdict_text = _upstream_failure(sc_summary)
        if lane_failure is not None:
            summary[VERDICT_TEXT] = verdict_text
    if lane_failure is not None:
        summary[LANE_FAILURE] = lane_failure
        context.log.error("analyst ratings lane failed for %s: %s", config.universe, lane_failure)
    for failure in captured.failures[: analyst_ratings.MAX_FAILURES_LOGGED]:
        context.log.error("analyst consensus fetch failed for %s: %s", failure.ticker, failure.error)
    if len(captured.failures) > analyst_ratings.MAX_FAILURES_LOGGED:
        context.log.warning(
            "%s more analyst consensus fetch failures not logged; each unavailable row holds its reason code",
            len(captured.failures) - analyst_ratings.MAX_FAILURES_LOGGED,
        )
    context.add_output_metadata(summary)
    context.log.info(
        "published %s analyst ratings rows for %s (%s fetch errors)",
        captured.rows,
        config.universe,
        len(captured.failures),
    )
    return json.dumps(summary)


def _run_coverage_verdict_failure(report: Mapping[str, Any]) -> str | None:
    questions = report.get("questions")
    if (
        report.get("environment") == "test"
        and isinstance(questions, Mapping)
        and any(isinstance(entry, Mapping) and int(entry.get("answered", 0)) > 0 for entry in questions.values())
    ):
        filtered = {
            key: entry
            for key, entry in questions.items()
            if not (
                isinstance(entry, Mapping)
                and int(entry.get("answered", 0)) == 0
                and entry.get("unavailable") == {question_coverage.NO_ROW: int(entry.get("denominator", 0))}
            )
        }
        return question_coverage.coverage_verdict_failure({**report, "questions": filtered})
    return question_coverage.coverage_verdict_failure(report)


@external_op
def run_question_coverage(context: dg.OpExecutionContext, config: StandardBackfillConfig, analyst_summary: str) -> str:
    from data_engine.datahub.question_coverage import NO_HEAD_SUMMARY, compile_report, persist, summary_line

    context.log.info("coverage follows analyst ratings: %s", analyst_summary[:400])
    current = reports_current(analyst_summary)
    if current is not None:
        return _already_current(context, QUESTION_COVERAGE_VERDICT, config, current)
    executed_at = datetime.fromisoformat(config.executed_at)
    with (
        verdict(
            check_name(QUESTION_COVERAGE_VERDICT, config.universe),
            registered=NIGHTLY_VERDICTS,
            run_id=context.run_id,
            tick=tick_from_config(config.executed_at),
        ) as outcome,
        psycopg.connect(settings.database_url) as connection,
    ):
        report = compile_report(connection, universe=config.universe, executed_at=executed_at)
        if report is None:
            context.log.warning("no governed head for %s; no coverage report", config.universe)
            outcome.pending = True
            outcome.summary = NO_HEAD_SUMMARY
            return json.dumps({"universe": config.universe, "report": None})
        report_id = persist(connection, report)
        try:
            with connection.transaction():
                from data_engine.datahub.research_card_artifacts import materialize_head_research_cards

                card_counts = materialize_head_research_cards(
                    connection,
                    run_id=str(report["run_id"]),
                    cutoff=datetime.fromisoformat(str(report["cutoff"])),
                    universe=config.universe,
                )
            context.log.info("research_card_artifacts materialized for %s: %s", config.universe, card_counts)
        except Exception as exc:
            context.log.warning("research_card_artifacts materialization skipped for %s: %s", config.universe, exc)
        conf_report = None
        try:
            with connection.transaction():
                from data_engine.datahub import confidence_report as _confidence_report

                conf_report = _confidence_report.compile_report(
                    connection, universe=config.universe, executed_at=executed_at
                )
                if conf_report is not None:
                    _confidence_report.persist(connection, conf_report)
        except Exception as exc:
            context.log.warning("confidence_report refresh skipped for %s: %s", config.universe, exc)
        connection.commit()
        if conf_report is not None:
            try:
                from data_engine.datahub.oracle_outcomes import record_oracle_outcomes_safely

                record_oracle_outcomes_safely(
                    connection, str(conf_report["run_id"]), compared_at=executed_at, log=context.log
                )
            except Exception as exc:
                context.log.warning("oracle_outcomes refresh skipped for %s: %s", config.universe, exc)
        zero_failure = _run_coverage_verdict_failure(report)
        q3_unavail = (report.get("questions", {}).get("q3", {}) or {}).get("unavailable") or {}
        exceeded_q3 = int(q3_unavail.get("context_window_exceeded", 0))
        suffix = (
            f" ({zero_failure})"
            if zero_failure is not None
            else (f" ({exceeded_q3} filing(s) exceeded 1M context window)" if exceeded_q3 > 0 else "")
        )
        outcome.summary = f"report persisted for {report['universe_id']}{suffix}"
    context.log.info("question coverage %s: %s", report_id, summary_line(report))
    context.add_output_metadata(
        {
            "report_id": report_id,
            "universe_id": report["universe_id"],
            "denominator": report["denominator"],
            "q3_context_window_exceeded": exceeded_q3,
            **{f"{q}_answered": entry["answered"] for q, entry in report["questions"].items()},
            **{f"{q}_missing": entry["missing"] for q, entry in report["questions"].items()},
        }
    )
    out_summary: dict[str, Any] = {"report_id": report_id, "summary": summary_line(report)}
    if zero_failure is not None:
        out_summary.update(
            {"universe": config.universe, "executed_at": config.executed_at, VERDICT_FAILURE: zero_failure}
        )
    return json.dumps(out_summary)


@external_op
def fail_if_a_lane_failed(context: dg.OpExecutionContext, analyst_summary: str, coverage_summary: str) -> None:
    parsed = _json_dict(analyst_summary)
    coverage_parsed = _json_dict(coverage_summary)
    failure = parsed.get(LANE_FAILURE)
    coverage_failure = coverage_parsed.get(VERDICT_FAILURE)
    if not failure and not coverage_failure:
        return
    universe = str(
        parsed.get("universe")
        or coverage_parsed.get("universe")
        or StandardBackfillConfig.model_fields["universe"].default
    )
    executed_at = parsed.get("executed_at") or coverage_parsed.get("executed_at")
    with verdict(
        check_name(QUESTION_COVERAGE_VERDICT, universe),
        registered=NIGHTLY_VERDICTS,
        run_id=context.run_id,
        tick=tick_from_config(str(executed_at)) if executed_at else None,
    ) as outcome:
        if failure:
            outcome.summary = parsed.get(VERDICT_TEXT) or (
                f"analyst ratings fetch failed for {parsed['fetch_errors']} of {parsed.get(JOINED_ISSUERS, parsed['rows'])} tickers"
            )
            raise RuntimeError(failure)
        outcome.summary = str(coverage_failure)
        raise RuntimeError(str(coverage_failure))


@dg.job(name=STANDARD_BACKFILL_JOB_NAME)
def standard_backfill_pipeline_job() -> None:
    analyst_summary = run_analyst_ratings(run_supply_chain_exposure(run_theme_purity(run_standard_backfill())))
    fail_if_a_lane_failed(analyst_summary, run_question_coverage(analyst_summary))


def backfill_run_config(executed_at: str, universe: str, standard: str = "") -> dg.RunConfig:
    return dg.RunConfig(
        ops={
            "run_standard_backfill": StandardBackfillConfig(
                executed_at=executed_at, universe=universe, standard=standard
            ),
            **_downstream_ops_config(executed_at, universe),
        }
    )


@dg.schedule(
    job=standard_backfill_pipeline_job,
    cron_schedule=STANDARD_BACKFILL_CRON,
    execution_timezone="UTC",
    default_status=dg.DefaultScheduleStatus.RUNNING,
)
def standard_backfill_schedule(context: dg.ScheduleEvaluationContext):
    executed_at = context.scheduled_execution_time.isoformat()
    for universe in scheduled_universe_names():
        yield dg.RunRequest(
            run_key=f"{executed_at}:{universe}",
            run_config=backfill_run_config(executed_at, universe),
            tags={UNIVERSE_TAG: universe},
        )


@external_op
def run_standard_history(context: dg.OpExecutionContext, config: StandardHistoryConfig) -> str:
    tick = datetime.fromisoformat(config.executed_at)
    names = history_standards(config.standard)
    reports = []
    with gateway.capacity_scope(), psycopg.connect(settings.database_url) as connection:
        for name in names:
            reports.append(
                _run_standard_history(
                    connection,
                    universe=config.universe,
                    standard_name=name,
                    tick=tick,
                    months=config.months,
                    log=context.log.info,
                )
            )
    counts = {name: report.counts() for name, report in zip(names, reports, strict=True)}
    summaries = [{"universe": config.universe, "standard": name, **values} for name, values in counts.items()]
    context.add_output_metadata(
        {
            "universe": config.universe,
            "standards": ", ".join(names),
            "months": config.months,
            **{f"{name}_{key}": value for name, values in counts.items() for key, value in values.items()},
        }
    )
    for report in reports:
        raise_if_every_attempt_failed(report)
    return json.dumps(summaries, sort_keys=True)


@dg.job(name=STANDARD_HISTORY_JOB_NAME)
def standard_history_backfill_pipeline_job() -> None:
    run_standard_history()


def history_run_config(executed_at: str, universe: str) -> dg.RunConfig:
    return dg.RunConfig(ops={"run_standard_history": StandardHistoryConfig(executed_at=executed_at, universe=universe)})


@dg.schedule(
    job=standard_history_backfill_pipeline_job,
    cron_schedule=STANDARD_HISTORY_CRON,
    execution_timezone="UTC",
    default_status=dg.DefaultScheduleStatus.RUNNING,
)
def standard_history_schedule(context: dg.ScheduleEvaluationContext):
    executed_at = context.scheduled_execution_time.isoformat()
    universe = lane_name(universe_with_role("strategy"))
    yield dg.RunRequest(
        run_key=f"history:{executed_at}:{universe}", run_config=history_run_config(executed_at, universe)
    )


@dg.sensor(
    name=STANDARD_BACKFILL_CATCHUP_SENSOR_NAME,
    job=standard_backfill_pipeline_job,
    minimum_interval_seconds=STANDARD_BACKFILL_CATCHUP_INTERVAL_SECONDS,
    default_status=dg.DefaultSensorStatus.RUNNING,
)
def standard_backfill_catchup_sensor(context: dg.SensorEvaluationContext):
    build = (settings.data_engine_image_digest or "").strip() or "unversioned"
    if context.cursor != build:
        context.update_cursor(build)
        yield dg.SkipReason(
            f"armed on {build[:19]}; a catch-up waits for the next evaluation, clear of the deploy walk"
        )
        return
    universes = scheduled_universe_names()
    if not universes:
        yield dg.SkipReason("the environment contract schedules no universe in this environment")
        return
    with psycopg.connect(settings.database_url) as connection:
        missing = never_backfilled(connection, universes=universes, standards=standards_to_run(""))
    if not missing:
        yield dg.SkipReason("every registered standard has completed a backfill over every universe here")
        return
    executed_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    by_universe: dict[str, list[str]] = {}
    for universe, standard in missing:
        by_universe.setdefault(universe, []).append(standard)
    for universe, standards in by_universe.items():
        context.log.info("catch-up: %s never backfilled over %s here", ", ".join(standards), universe)
        yield dg.RunRequest(
            run_key=f"catchup:{universe}:{'+'.join(sorted(standards))}",
            run_config=backfill_run_config(executed_at, universe, standards[0] if len(standards) == 1 else ""),
            tags={UNIVERSE_TAG: universe},
        )


def _red_report_checks(universe: str) -> list[str]:
    names = (check_name(check, universe) for check in (QUESTION_COVERAGE_VERDICT, THEME_PURITY_VERDICT))
    return [name for name in names if newest_is_red(name)]


@external_op
def head_reports_start(context: dg.OpExecutionContext, config: HeadReportsStartConfig) -> str:
    summary: dict[str, Any] = {"universe": config.universe, "mode": "head-reports", "executed_at": config.executed_at}
    if config.only_if_stale:
        red: list[str] = []
        with psycopg.connect(settings.database_url) as connection:
            head = question_coverage.governed_head_of(connection, key_of_lane(config.universe))
            if not isinstance(head, question_coverage.NoHead) and question_coverage.reports_current_for_head(
                connection, head
            ):
                red = _red_report_checks(config.universe)
                if not red:
                    summary[REPORTS_CURRENT] = head.run_id
        current = summary.get(REPORTS_CURRENT)
        outcome = (
            f"reports already current on {current[:24]}"
            if current
            else (
                f"newest verdict is red for {', '.join(red)}; recomputing to retry"
                if red
                else "no current reports; recomputing"
            )
        )
        context.log.info("fallback for %s: %s", config.universe, outcome)
    return json.dumps(summary)


@dg.job(name=HEAD_REPORTS_JOB_NAME)
def head_reports_pipeline_job() -> None:
    analyst_summary = run_analyst_ratings(run_supply_chain_exposure(run_theme_purity(head_reports_start())))
    fail_if_a_lane_failed(analyst_summary, run_question_coverage(analyst_summary))


def head_reports_request(
    universe: str, executed_at: str, *, run_key: str, only_if_stale: bool, head_run_id: str | None = None
) -> dg.RunRequest:
    tags = {UNIVERSE_TAG: universe, **({HEAD_RUN_TAG: head_run_id} if head_run_id else {})}
    return dg.RunRequest(
        run_key=run_key,
        run_config=dg.RunConfig(
            ops={
                "head_reports_start": HeadReportsStartConfig(
                    executed_at=executed_at, universe=universe, only_if_stale=only_if_stale
                ),
                **_downstream_ops_config(executed_at, universe),
            }
        ),
        tags=tags,
    )


@dg.schedule(
    job=head_reports_pipeline_job,
    cron_schedule=HEAD_REPORTS_CRON,
    execution_timezone="UTC",
    default_status=dg.DefaultScheduleStatus.RUNNING,
)
def head_reports_schedule(context: dg.ScheduleEvaluationContext):
    executed_at = context.scheduled_execution_time.isoformat()
    for universe in scheduled_universe_names():
        yield head_reports_request(universe, executed_at, run_key=f"{executed_at}:{universe}", only_if_stale=True)


def _followed(cursor: str | None) -> dict[str, str]:
    return {str(k): str(v) for k, v in _json_dict(cursor or "{}").items()}


@dg.sensor(
    name=HEAD_REPORTS_SENSOR_NAME,
    job=head_reports_pipeline_job,
    minimum_interval_seconds=HEAD_REPORTS_SENSOR_INTERVAL_SECONDS,
    default_status=dg.DefaultSensorStatus.RUNNING,
)
def head_reports_sensor(context: dg.SensorEvaluationContext):
    followed = _followed(context.cursor)
    executed_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    requests: list[dg.RunRequest] = []
    with psycopg.connect(settings.database_url) as connection:
        for universe in scheduled_universe_names():
            head = question_coverage.governed_head_of(connection, key_of_lane(universe))
            if isinstance(head, question_coverage.NoHead) or followed.get(universe) == head.run_id:
                continue
            first_sight = universe not in followed
            followed[universe] = head.run_id
            if first_sight and question_coverage.stored_report_run(connection, head.universe_id) == head.run_id:
                context.log.info("%s: reports already current on %s", universe, head.run_id[:24])
                continue
            context.log.info("%s: head advanced to %s; launching its reports", universe, head.run_id[:24])
            requests.append(
                head_reports_request(
                    universe,
                    executed_at,
                    run_key=f"head:{universe}:{head.run_id}",
                    only_if_stale=False,
                    head_run_id=head.run_id,
                )
            )
    context.update_cursor(json.dumps(followed, sort_keys=True))
    if not requests:
        yield dg.SkipReason("no governed head has advanced since its reports were launched")
        return
    yield from requests


def _build_standards_plane_schedule(
    schedule_name: str, cron_schedule: str, *, job: dg.JobDefinition
) -> dg.ScheduleDefinition:
    @dg.schedule(
        name=schedule_name,
        job=job,
        cron_schedule=cron_schedule,
        execution_timezone="UTC",
        default_status=STANDARDS_PLANE_SCHEDULE_STATUS,
    )
    def _plane_schedule(context: dg.ScheduleEvaluationContext):
        executed_at = context.scheduled_execution_time.isoformat()
        if job.name == STANDARD_BACKFILL_JOB_NAME:
            for universe in scheduled_universe_names():
                yield dg.RunRequest(
                    run_key=f"{schedule_name}:{executed_at}:{universe}",
                    run_config=backfill_run_config(executed_at, universe),
                    tags={UNIVERSE_TAG: universe},
                )
        else:
            yield dg.RunRequest(run_key=f"{schedule_name}:{executed_at}")

    return _plane_schedule


def build_standards_plane_schedules() -> tuple[dg.ScheduleDefinition, ...]:
    from data_engine.lanes.universe_refresh import universe_refresh_pipeline_job

    return tuple(
        _build_standards_plane_schedule(
            schedule_name,
            cron_schedule,
            job=(
                universe_refresh_pipeline_job
                if schedule_name == NPORT_MONTHLY_SCHEDULE_NAME
                else standard_backfill_pipeline_job
            ),
        )
        for schedule_name, cron_schedule in STANDARDS_PLANE_CRONS.items()
    )


(
    nport_monthly_schedule,
    thirteenf_quarterly_schedule,
    form4_daily_schedule,
    tenk_annual_schedule,
) = build_standards_plane_schedules()


def build_definitions(app_env: str = settings.app_env) -> dg.Definitions:
    return dg.Definitions(
        jobs=[standard_backfill_pipeline_job, standard_history_backfill_pipeline_job, head_reports_pipeline_job],
        schedules=[
            standard_backfill_schedule,
            standard_history_schedule,
            head_reports_schedule,
            nport_monthly_schedule,
            thirteenf_quarterly_schedule,
            form4_daily_schedule,
            tenk_annual_schedule,
        ],
        sensors=[head_reports_sensor, standard_backfill_catchup_sensor],
    )


defs = build_definitions()
