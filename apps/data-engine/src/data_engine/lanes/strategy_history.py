"""The strategy history lane (#1139, M3 track H1): historical monthly strategy inputs.

One op reads stored SEC company-facts bytes, the headcount fact table and the unadjusted
daily bars. It writes the inputs of the last 36 monthly cutoffs into
`staging.strategy_backtest_inputs`. It makes zero vendor calls, so it binds no gateway scope.

The tick time of the run derives the cutoffs (`executed_at`), never the wall clock. A replay of
one tick builds the same cutoffs. The projector is idempotent, so a repeated run adds no row
until a new filing, a new bar or a new headcount fact changes an input.

Manual replay: launch the job from the Dagster UI launchpad, or through GraphQL, with the run
config below. `months` is optional.

    ops:
      project_strategy_history_op:
        config:
          executed_at: "2026-10-11T10:07:00+00:00"
"""

from datetime import datetime

import dagster as dg
import psycopg

from data_engine.config import settings
from data_engine.datahub.strategy_history import DEFAULT_MONTHS, project_deployed_history

STRATEGY_HISTORY_JOB_NAME = "strategy_history_projection_pipeline"
# Sunday 10:07 UTC: after the standards backfill (09:07), so a headcount fact the backfill
# landed enters the same week. Weekly is enough: the history of a past month changes only when
# a filing, a bar or a headcount fact arrives late.
STRATEGY_HISTORY_CRON = "7 10 * * 0"


class StrategyHistoryConfig(dg.Config):
    """`executed_at` is the tick time (ISO 8601 with a time zone), never the wall clock."""

    executed_at: str
    months: int = DEFAULT_MONTHS


@dg.op
def project_strategy_history_op(context: dg.OpExecutionContext, config: StrategyHistoryConfig) -> None:
    """Project the point-in-time strategy inputs of every consumed issuer over the monthly cutoffs."""
    tick = datetime.fromisoformat(config.executed_at)
    with psycopg.connect(settings.database_url) as connection:
        summary = project_deployed_history(connection, tick=tick, months=config.months)
        connection.commit()
    context.log.info(
        f"strategy_history: {summary.issuers} issuers over {summary.cutoffs} cutoffs, "
        f"{summary.inserted} rows inserted, {summary.already_present} already present, "
        f"{summary.no_outcome} cutoffs without a financial outcome, {summary.undated} inputs without a filing date"
    )
    if summary.unresolved_issuers:
        context.log.warning(f"strategy_history: no stored lineage for {list(summary.unresolved_issuers)}")
    context.add_output_metadata(
        {
            "issuers": summary.issuers,
            "cutoffs": summary.cutoffs,
            "inserted": summary.inserted,
            "already_present": summary.already_present,
            "no_outcome": summary.no_outcome,
            "undated": summary.undated,
            "unresolved_issuers": list(summary.unresolved_issuers),
        }
    )


@dg.job(name=STRATEGY_HISTORY_JOB_NAME)
def strategy_history_pipeline_job() -> None:
    project_strategy_history_op()


@dg.schedule(
    job=strategy_history_pipeline_job,
    cron_schedule=STRATEGY_HISTORY_CRON,
    execution_timezone="UTC",
    # Production-only, like the other scheduled lanes; staging and development replay it by hand.
    default_status=(
        dg.DefaultScheduleStatus.RUNNING
        if settings.app_env.strip().lower() in ("production", "prod")
        else dg.DefaultScheduleStatus.STOPPED
    ),
)
def strategy_history_schedule(context: dg.ScheduleEvaluationContext) -> dg.RunRequest:
    tick = context.scheduled_execution_time.isoformat()
    return dg.RunRequest(
        run_key=tick,
        run_config={"ops": {"project_strategy_history_op": {"config": {"executed_at": tick}}}},
    )


defs = dg.Definitions(jobs=[strategy_history_pipeline_job], schedules=[strategy_history_schedule])
