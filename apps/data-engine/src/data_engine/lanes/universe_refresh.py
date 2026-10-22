"""The weekly universe refresh: constituents, N-PORT holdings, identity enrichment
(#539; #731 split of `data_engine.dagster_defs`, behaviour unchanged)."""

import traceback
from datetime import UTC, datetime

import dagster as dg
import psycopg
from truealpha_contracts.environment_contract import load_environment_contract

from data_engine.config import settings
from data_engine.lanes import production_only_schedule_status
from data_engine.lanes.capture import is_universe_scheduled
from data_engine.sources import gateway

# -- universe refresh (#539 owner requirement: automatic pulls) ------------------------

UNIVERSE_REFRESH_JOB_NAME = "universe_refresh_pipeline"
# Saturday 08:07 UTC: markets closed, no capture window anywhere near, and a
# membership change published on Saturday governs Monday's ticks. Weekly matches
# the index's actual cadence (annual reconstitution + episodic swaps); the
# publish leg is change-gated, so a quiet week advances nothing.
UNIVERSE_REFRESH_CRON = "7 8 * * 6"
SP500_UNIVERSE_REFRESH_SCHEDULE_NAME = "sp500_universe_refresh_schedule"
# Saturday 08:37 UTC: 30 minutes after the QQQ/canary constituent refresh (08:07 UTC),
# so the SEC crosswalk and OpenFIGI batch resolution never overlap on Saturday morning.
SP500_UNIVERSE_REFRESH_CRON = "37 8 * * 6"


def _is_source_refresh_enabled(source) -> bool:
    """Refresh the constituents and the N-PORT holdings of a universe that the process environment runs.

    The contract is the only source of "scheduled" and "stopped" (#67, #1310): a stopped universe, and
    a universe that the environment does not list, are not refreshed. A source that the contract does not
    name at all (a universe added before the contract lists it) is refreshed: the contract has no rule for it.
    """
    if source.etf not in load_environment_contract().known_tick_keys:
        return True
    return is_universe_scheduled(source.etf, settings.app_env)


@dg.op
def refresh_universes_op(context: dg.OpExecutionContext) -> None:
    """Refresh every configured universe's constituent plane; publish on change."""
    from data_engine.datahub.production_topt.universe_plane import (
        UNIVERSE_SOURCES,
        latest_quarter_end,
        refresh_and_publish,
    )

    # Every vendor call in the refresh is attributed to this Dagster run in the
    # external call ledger (#729) and admitted by the rule-6 gate first.
    # The constituents that left a denominator for want of a listing exchange, by universe (#1411 K6).
    # A run that ends well reports them as output metadata. A run that fails a step keeps no metadata
    # (a Dagster 1.13 step failure carries none), so `_refresh_universes` also logs a warning for each
    # universe at once. That log line survives a failure.
    excluded: dict[str, dict[str, object]] = {}
    try:
        with gateway.run_scope(f"dagster:{context.run_id}"), gateway.capacity_scope():
            _refresh_universes(context, UNIVERSE_SOURCES, latest_quarter_end, refresh_and_publish, excluded)
    finally:
        context.add_output_metadata({"constituents_excluded": dg.MetadataValue.json(excluded)})


def _refresh_universes(
    context: dg.OpExecutionContext,
    universe_sources,
    latest_quarter_end,
    refresh_and_publish,
    excluded_by_universe: dict[str, dict[str, object]] | None = None,
) -> None:
    """Refresh each active universe. `excluded_by_universe` collects the excluded constituents of each."""
    if excluded_by_universe is None:
        excluded_by_universe = {}
    active_sources = [source for source in universe_sources.values() if _is_source_refresh_enabled(source)]
    failures: list[tuple[str, Exception]] = []
    # Each universe rides its own connection and transaction so a single index endpoint failure
    # never rolls back another universe's published constituent list (#67, #628).
    for source in active_sources:
        try:
            with psycopg.connect(settings.database_url) as connection:
                outcome = refresh_and_publish(
                    connection,
                    source,
                    report_date=latest_quarter_end(datetime.now(UTC).date()),
                    note="scheduled weekly refresh",
                    openfigi_api_key=settings.openfigi_api_key,
                )
                context.log.info(outcome)
                excluded = getattr(outcome, "excluded", ())
                if excluded:
                    excluded_by_universe[source.etf] = {
                        "count": len(excluded),
                        "tickers": [item["ticker"] for item in excluded],
                        "reason": excluded[0]["reason"],
                    }
                    context.log.warning(f"constituents[{source.etf}] excluded: {outcome}")
                connection.commit()
        except Exception as error:  # noqa: BLE001 — per-universe isolation, re-raised below
            context.log.error(
                f"constituents[{source.etf}] failed:\n" + "".join(traceback.format_exception(error)).rstrip()
            )
            failures.append((f"constituents:{source.etf}", error))

    # Holdings ride a separate connection per fund (#628's lesson): an SEC outage
    # must fail this half loudly without rolling back the published universes,
    # and one fund's failure must not starve the next. Weekly retry is the
    # policy — filings are quarterly with a ~60-day lag, a missed week is noise.
    from data_engine.datahub.production_topt.nport_holdings import capture_fund_holdings

    for source in active_sources:
        if source.nport_ticker is None:
            continue
        try:
            with psycopg.connect(settings.database_url) as connection:
                context.log.info(str(capture_fund_holdings(connection, source.nport_ticker)))
                connection.commit()
        except Exception as error:  # noqa: BLE001 — per-fund isolation, re-raised below
            context.log.error(
                f"nport[{source.nport_ticker}] failed:\n" + "".join(traceback.format_exception(error)).rstrip()
            )
            failures.append((f"nport:{source.nport_ticker}", error))
    # Identity enrichment sweeps AFTER every fund captured: re-points minted
    # company:isin identities onto the plane's issuer:cik keying (#63 tranche 2).
    # Its own connection for the same isolation reason as the captures.
    from data_engine.datahub.production_topt.holdings_enrichment import enrich_holding_identities

    try:
        with psycopg.connect(settings.database_url) as connection:
            context.log.info(str(enrich_holding_identities(connection, api_key=settings.openfigi_api_key)))
            connection.commit()
    except Exception as error:  # noqa: BLE001 — reported with the fund failures below
        context.log.error("holdings enrichment failed:\n" + "".join(traceback.format_exception(error)).rstrip())
        failures.append(("identity-enrichment", error))
    if failures:
        raise RuntimeError(f"Universe refresh failed for: {', '.join(step for step, _ in failures)}") from failures[-1][
            1
        ]


@dg.job(name=UNIVERSE_REFRESH_JOB_NAME)
def universe_refresh_pipeline_job() -> None:
    refresh_universes_op()


def build_universe_refresh_schedule(app_env: str = settings.app_env) -> dg.ScheduleDefinition:
    """Build the Saturday 08:07 UTC constituent refresh schedule for `app_env`."""

    @dg.schedule(
        name="universe_refresh_schedule",
        job=universe_refresh_pipeline_job,
        cron_schedule=UNIVERSE_REFRESH_CRON,
        execution_timezone="UTC",
        # Production-only, like the QQQ capture itself; staging exercises the plane
        # via the operator script when needed.
        default_status=production_only_schedule_status(app_env),
    )
    def _universe_refresh_schedule(context: dg.ScheduleEvaluationContext) -> dg.RunRequest:
        return dg.RunRequest(run_key=context.scheduled_execution_time.isoformat())

    return _universe_refresh_schedule


def build_sp500_universe_refresh_schedule(app_env: str = settings.app_env) -> dg.ScheduleDefinition:
    """Build the staggered Saturday UTC schedule for SP500 constituent refresh (#67, #1310).

    Follows the status of the SP500 universe in the contract (#67 / #206 / #1310).
    """
    status = (
        dg.DefaultScheduleStatus.RUNNING
        if is_universe_scheduled("sp500", app_env)
        else dg.DefaultScheduleStatus.STOPPED
    )

    @dg.schedule(
        name=SP500_UNIVERSE_REFRESH_SCHEDULE_NAME,
        job=universe_refresh_pipeline_job,
        cron_schedule=SP500_UNIVERSE_REFRESH_CRON,
        execution_timezone="UTC",
        default_status=status,
    )
    def _sp500_universe_refresh_schedule(context: dg.ScheduleEvaluationContext) -> dg.RunRequest:
        executed_at = context.scheduled_execution_time.isoformat()
        return dg.RunRequest(run_key=f"sp500:{executed_at}")

    return _sp500_universe_refresh_schedule


universe_refresh_schedule = build_universe_refresh_schedule(settings.app_env)
sp500_universe_refresh_schedule = build_sp500_universe_refresh_schedule(settings.app_env)


def build_definitions(app_env: str = settings.app_env) -> dg.Definitions:
    """Build universe refresh jobs and schedules for `app_env`, including SP500 (#67)."""
    schedules = (
        [universe_refresh_schedule, sp500_universe_refresh_schedule]
        if app_env == settings.app_env
        else [build_universe_refresh_schedule(app_env), build_sp500_universe_refresh_schedule(app_env)]
    )
    return dg.Definitions(
        jobs=[universe_refresh_pipeline_job],
        schedules=schedules,
    )


defs = build_definitions()
