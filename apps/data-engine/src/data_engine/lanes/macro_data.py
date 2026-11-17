"""Daily Treasury yield curve refresh and verdict (#1255)."""

from dataclasses import dataclass
from datetime import UTC, datetime

import dagster as dg
import psycopg

from data_engine.config import settings
from data_engine.datahub.treasury_rates import REGISTRATION, refresh_macro_series
from data_engine.external_op import external_op
from data_engine.lanes.backtest import schedule_status
from data_engine.quality.nightly_verdicts import TICK_TAG, tick_of, verdict
from data_engine.sources import gateway

MACRO_DATA_REFRESH_JOB_NAME = "macro_data_refresh_pipeline"
MACRO_DATA_REFRESH_CRON = "30 20 * * *"

MACRO_FRESHNESS_VERDICT = "macro_freshness"
NIGHTLY_VERDICTS: tuple[str, ...] = (MACRO_FRESHNESS_VERDICT,)


@dataclass(frozen=True)
class MacroFreshness:
    ok: bool
    summary: str


_STATUS_SQL = "select series_id, observation_date, age_days, max_age_days, is_fresh from mart.macro_series_status order by series_id"


def judge_freshness(connection: psycopg.Connection) -> MacroFreshness:
    rows = {row[0]: row for row in connection.execute(_STATUS_SQL).fetchall()}
    problems: list[str] = []
    newest: list[str] = []
    for item in REGISTRATION.series:
        row = rows.get(item.series_id)
        if row is None or row[1] is None:
            problems.append(f"{item.series_id}: no observation")
            continue
        _, observation_date, age_days, max_age_days, is_fresh = row
        if not is_fresh:
            problems.append(
                f"{item.series_id}: newest observation {observation_date.isoformat()} is {age_days} days old, "
                f"the limit is {max_age_days}"
            )
        else:
            newest.append(f"{item.series_id} {observation_date.isoformat()}")
    if problems:
        return MacroFreshness(ok=False, summary="; ".join(problems))
    return MacroFreshness(ok=True, summary=f"newest observations: {', '.join(newest)}")


@external_op
def refresh_macro_data_op(context: dg.OpExecutionContext) -> None:
    with verdict(
        MACRO_FRESHNESS_VERDICT, registered=NIGHTLY_VERDICTS, run_id=context.run_id, tick=tick_of(context)
    ) as outcome:
        now = datetime.now(UTC)
        with gateway.capacity_scope(), psycopg.connect(settings.database_url) as connection:
            for result in refresh_macro_series(connection, today=now.date()):
                context.log.info(
                    f"treasury {result.year}: inserted {result.inserted} rows"
                    + (" (file of this day had landed, no request)" if result.reused else "")
                )
            freshness = judge_freshness(connection)
        outcome.summary = freshness.summary
        if not freshness.ok:
            raise dg.Failure(f"macro freshness: {freshness.summary}")


@dg.job(name=MACRO_DATA_REFRESH_JOB_NAME)
def macro_data_refresh_pipeline_job() -> None:
    refresh_macro_data_op()


@dg.schedule(
    job=macro_data_refresh_pipeline_job,
    cron_schedule=MACRO_DATA_REFRESH_CRON,
    execution_timezone="UTC",
    default_status=schedule_status(settings.app_env),
)
def macro_data_refresh_schedule(context: dg.ScheduleEvaluationContext) -> dg.RunRequest:
    tick = context.scheduled_execution_time.isoformat()
    return dg.RunRequest(run_key=tick, tags={TICK_TAG: tick})


defs = dg.Definitions(jobs=[macro_data_refresh_pipeline_job], schedules=[macro_data_refresh_schedule])
