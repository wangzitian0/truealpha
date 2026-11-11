"""Weekly universe refresh: constituents, N-PORT holdings, and identity enrichment."""

import traceback
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import dagster as dg
import psycopg
from truealpha_contracts.environment_contract import load_environment_contract

from data_engine.config import settings
from data_engine.external_op import external_op
from data_engine.lanes.capture import is_universe_list_refresh_scheduled, is_universe_scheduled
from data_engine.sources import gateway

UNIVERSE_REFRESH_JOB_NAME = "universe_refresh_pipeline"
UNIVERSE_REFRESH_CRON = "7 8 * * 6"
SP500_UNIVERSE_REFRESH_SCHEDULE_NAME = "sp500_universe_refresh_schedule"
SP500_UNIVERSE_REFRESH_CRON = "37 8 * * 6"


def _is_source_refresh_enabled(source: Any) -> bool:
    return is_universe_list_refresh_scheduled(source.etf, settings.app_env)


def _isolated_db_step(
    context: dg.OpExecutionContext,
    step_key: str,
    log_prefix: str,
    failures: list[tuple[str, Exception]],
    fn: Callable[[psycopg.Connection[Any]], None],
) -> None:
    try:
        with psycopg.connect(settings.database_url) as connection:
            fn(connection)
            connection.commit()
    except Exception as error:  # noqa: BLE001
        context.log.error(f"{log_prefix} failed:\n" + "".join(traceback.format_exception(error)).rstrip())
        failures.append((step_key, error))


@external_op
def refresh_universes_op(context: dg.OpExecutionContext) -> None:
    from data_engine.datahub.production_topt.universe_plane import (
        UNIVERSE_SOURCES,
        latest_quarter_end,
        refresh_and_publish,
    )

    excluded: dict[str, dict[str, object]] = {}
    try:
        with gateway.capacity_scope():
            _refresh_universes(context, UNIVERSE_SOURCES, latest_quarter_end, refresh_and_publish, excluded)
    finally:
        context.add_output_metadata({"constituents_excluded": dg.MetadataValue.json(excluded)})


def _refresh_universes(
    context: dg.OpExecutionContext,
    universe_sources: Any,
    latest_quarter_end: Any,
    refresh_and_publish: Any,
    excluded_by_universe: dict[str, dict[str, object]] | None = None,
) -> None:
    if excluded_by_universe is None:
        excluded_by_universe = {}
    active_sources = [source for source in universe_sources.values() if _is_source_refresh_enabled(source)]
    failures: list[tuple[str, Exception]] = []

    for source in active_sources:

        def _run_constituents(conn: psycopg.Connection[Any], src: Any = source) -> None:
            outcome = refresh_and_publish(
                conn,
                src,
                report_date=latest_quarter_end(datetime.now(UTC).date()),
                note="scheduled weekly refresh",
                openfigi_api_key=settings.openfigi_api_key,
            )
            context.log.info(outcome)
            excluded = getattr(outcome, "excluded", ())
            if excluded:
                excluded_by_universe[src.etf] = {
                    "count": len(excluded),
                    "tickers": [item["ticker"] for item in excluded],
                    "reason": excluded[0]["reason"],
                }
                context.log.warning(f"constituents[{src.etf}] excluded: {outcome}")

        _isolated_db_step(
            context, f"constituents:{source.etf}", f"constituents[{source.etf}]", failures, _run_constituents
        )

    from data_engine.datahub.production_topt.nport_holdings import capture_fund_holdings

    for source in active_sources:
        ticker = source.nport_ticker
        if ticker is None or not is_universe_scheduled(source.etf, settings.app_env):
            continue

        def _run_nport(conn: psycopg.Connection[Any], t: str = ticker) -> None:
            context.log.info(str(capture_fund_holdings(conn, t)))

        _isolated_db_step(context, f"nport:{ticker}", f"nport[{ticker}]", failures, _run_nport)

    from data_engine.datahub.production_topt.holdings_enrichment import enrich_holding_identities

    _isolated_db_step(
        context,
        "identity-enrichment",
        "holdings enrichment",
        failures,
        lambda conn: context.log.info(str(enrich_holding_identities(conn, api_key=settings.openfigi_api_key))),
    )
    if failures:
        raise RuntimeError(f"Universe refresh failed for: {', '.join(step for step, _ in failures)}") from failures[-1][
            1
        ]


@dg.job(name=UNIVERSE_REFRESH_JOB_NAME)
def universe_refresh_pipeline_job() -> None:
    refresh_universes_op()


def _build_schedule(name: str, cron: str, running: bool, key_prefix: str = "") -> dg.ScheduleDefinition:
    @dg.schedule(
        name=name,
        job=universe_refresh_pipeline_job,
        cron_schedule=cron,
        execution_timezone="UTC",
        default_status=dg.DefaultScheduleStatus.RUNNING if running else dg.DefaultScheduleStatus.STOPPED,
    )
    def _sched(context: dg.ScheduleEvaluationContext) -> dg.RunRequest:
        ts = context.scheduled_execution_time.isoformat()
        return dg.RunRequest(run_key=f"{key_prefix}{ts}")

    return _sched


def build_universe_refresh_schedule(app_env: str = settings.app_env) -> dg.ScheduleDefinition:
    running = any(
        is_universe_list_refresh_scheduled(key, app_env)
        for key, universe in load_environment_contract().universes.items()
        if universe.membership.kind in ("index_api", "nport")
    )
    return _build_schedule("universe_refresh_schedule", UNIVERSE_REFRESH_CRON, running)


def build_sp500_universe_refresh_schedule(app_env: str = settings.app_env) -> dg.ScheduleDefinition:
    return _build_schedule(
        SP500_UNIVERSE_REFRESH_SCHEDULE_NAME,
        SP500_UNIVERSE_REFRESH_CRON,
        is_universe_scheduled("sp500", app_env),
        key_prefix="sp500:",
    )


universe_refresh_schedule = build_universe_refresh_schedule(settings.app_env)
sp500_universe_refresh_schedule = build_sp500_universe_refresh_schedule(settings.app_env)


def build_definitions(app_env: str = settings.app_env) -> dg.Definitions:
    schedules = (
        [universe_refresh_schedule, sp500_universe_refresh_schedule]
        if app_env == settings.app_env
        else [build_universe_refresh_schedule(app_env), build_sp500_universe_refresh_schedule(app_env)]
    )
    return dg.Definitions(jobs=[universe_refresh_pipeline_job], schedules=schedules)


defs = build_definitions()
