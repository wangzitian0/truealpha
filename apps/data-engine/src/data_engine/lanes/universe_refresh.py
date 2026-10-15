"""The weekly universe refresh: constituents, N-PORT holdings, identity enrichment
(#539; #731 split of `data_engine.dagster_defs`, behaviour unchanged)."""

import traceback
from datetime import UTC, datetime

import dagster as dg
import psycopg

from data_engine.config import settings
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


def _production_only(app_env: str) -> dg.DefaultScheduleStatus:
    """Enable recurring schedules in production (`production` / `prod`)."""
    return (
        dg.DefaultScheduleStatus.RUNNING
        if app_env.strip().lower() in ("production", "prod")
        else dg.DefaultScheduleStatus.STOPPED
    )


def active_universe_sources():
    """Return the active constituent universe sources, including SP500 (#67 / #1281)."""
    from data_engine.datahub.production_topt.universe_plane import UNIVERSE_SOURCES, UniverseSource

    sources = dict(UNIVERSE_SOURCES)
    if "sp500" not in sources:
        sources["sp500"] = UniverseSource(
            etf="sp500",
            index_api="https://api.nasdaq.com/api/quote/list-type/sp500",
            universe_prefix="sp500-us",
            label="S&P 500 constituents, from the index constituent plane",
            mic="xnys",
            nport_ticker="IVV",
        )
    return sources


@dg.op
def refresh_universes_op(context: dg.OpExecutionContext) -> None:
    """Refresh every configured universe's constituent plane; publish on change."""
    from data_engine.datahub.production_topt.universe_plane import (
        latest_quarter_end,
        refresh_and_publish,
    )

    # Every vendor call in the refresh is attributed to this Dagster run in the
    # external call ledger (#729) and admitted by the rule-6 gate first.
    with gateway.run_scope(f"dagster:{context.run_id}"), gateway.capacity_scope():
        _refresh_universes(context, active_universe_sources(), latest_quarter_end, refresh_and_publish)


def _refresh_universes(
    context: dg.OpExecutionContext, universe_sources, latest_quarter_end, refresh_and_publish
) -> None:
    with psycopg.connect(settings.database_url) as connection:
        for source in universe_sources.values():
            outcome = refresh_and_publish(
                connection,
                source,
                report_date=latest_quarter_end(datetime.now(UTC).date()),
                note="scheduled weekly refresh",
                openfigi_api_key=settings.openfigi_api_key,
            )
            context.log.info(outcome)
        connection.commit()

    # Holdings ride a separate connection per fund (#628's lesson): an SEC outage
    # must fail this half loudly without rolling back the published universes,
    # and one fund's failure must not starve the next. Weekly retry is the
    # policy — filings are quarterly with a ~60-day lag, a missed week is noise.
    from data_engine.datahub.production_topt.nport_holdings import capture_fund_holdings

    failures: list[tuple[str, Exception]] = []
    for source in universe_sources.values():
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
            failures.append((source.nport_ticker, error))
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
        raise RuntimeError(
            f"N-PORT holdings capture failed for: {', '.join(ticker for ticker, _ in failures)}"
        ) from failures[-1][1]


@dg.job(name=UNIVERSE_REFRESH_JOB_NAME)
def universe_refresh_pipeline_job() -> None:
    refresh_universes_op()


@dg.schedule(
    job=universe_refresh_pipeline_job,
    cron_schedule=UNIVERSE_REFRESH_CRON,
    execution_timezone="UTC",
    # Production-only, like the QQQ capture itself; staging exercises the plane
    # via the operator script when needed.
    default_status=_production_only(settings.app_env),
)
def universe_refresh_schedule(context: dg.ScheduleEvaluationContext) -> dg.RunRequest:
    return dg.RunRequest(run_key=context.scheduled_execution_time.isoformat())


def build_sp500_universe_refresh_schedule(app_env: str = settings.app_env) -> dg.ScheduleDefinition:
    """Build the staggered Saturday UTC schedule for SP500 constituent refresh (#67)."""

    @dg.schedule(
        name=SP500_UNIVERSE_REFRESH_SCHEDULE_NAME,
        job=universe_refresh_pipeline_job,
        cron_schedule=SP500_UNIVERSE_REFRESH_CRON,
        execution_timezone="UTC",
        default_status=_production_only(app_env),
    )
    def _sp500_universe_refresh_schedule(context: dg.ScheduleEvaluationContext) -> dg.RunRequest:
        executed_at = context.scheduled_execution_time.isoformat()
        return dg.RunRequest(run_key=f"sp500:{executed_at}")

    return _sp500_universe_refresh_schedule


defs = dg.Definitions(jobs=[universe_refresh_pipeline_job], schedules=[universe_refresh_schedule])


def build_definitions(app_env: str = settings.app_env) -> dg.Definitions:
    """Build universe refresh jobs and schedules for `app_env`, including SP500 (#67)."""

    @dg.schedule(
        name="universe_refresh_schedule",
        job=universe_refresh_pipeline_job,
        cron_schedule=UNIVERSE_REFRESH_CRON,
        execution_timezone="UTC",
        default_status=_production_only(app_env),
    )
    def _universe_refresh_schedule(context: dg.ScheduleEvaluationContext) -> dg.RunRequest:
        return dg.RunRequest(run_key=context.scheduled_execution_time.isoformat())

    return dg.Definitions(
        jobs=[universe_refresh_pipeline_job],
        schedules=[_universe_refresh_schedule, build_sp500_universe_refresh_schedule(app_env)],
    )
