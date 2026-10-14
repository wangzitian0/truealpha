"""The strategy history lane (#1139, M3 track H1): historical strategy inputs.

One op reads stored SEC company-facts bytes, the headcount fact table and the unadjusted
daily bars. It writes the inputs of the last 36 monthly cutoffs, and of each calendar quarter
end of the strategy schedule in that window (#758), into `staging.strategy_backtest_inputs`.
The nightly backtest (`lanes.backtest`) evaluates the quarter-end cutoffs. It makes zero vendor
calls, so it binds no gateway scope.

The tick time of the run derives the cutoffs (`executed_at`), never the wall clock. A replay of
one tick builds the same cutoffs. The projector is idempotent, so a repeated run adds no row
until a new filing, a new bar or a new headcount fact changes an input.

The schedule runs on every weekday, 98 minutes before the nightly backtest, in each environment
where the backtest runs (#1270). History rows are append-only, so a projector fix reaches the
backtest only through a projector run. The next backtest then reads the fix with no manual step.

Manual replay: launch the job from the Dagster UI launchpad, or through GraphQL, with the run
config below. `months` is optional.

    ops:
      project_strategy_history_op:
        config:
          executed_at: "2026-10-13T20:07:00+00:00"
"""

from datetime import datetime

import dagster as dg
import psycopg

from data_engine.config import settings
from data_engine.datahub.strategy_history import DEFAULT_MONTHS, project_deployed_history
from data_engine.lanes.backtest import schedule_status

STRATEGY_HISTORY_JOB_NAME = "strategy_history_projection_pipeline"
# Weekdays 20:07 UTC: 98 minutes before the nightly backtest (21:45 UTC, `lanes.backtest`). A fix of
# the projector then reaches the next backtest with no manual step (#1270). No other lane runs at this
# time. The weekly lanes run from Saturday 08:07 to Monday 09:07 UTC. The daily quality lanes end by
# 06:15 UTC. The market data refresh starts at 21:15 UTC. The projector writes only to the database
# and takes about 100 seconds, so it ends over an hour before that refresh writes new bars.
# A headcount fact that the Sunday or Monday standards lane lands enters the Monday projection.
STRATEGY_HISTORY_CRON = "7 20 * * 1-5"


class StrategyHistoryConfig(dg.Config):
    """`executed_at` is the tick time (ISO 8601 with a time zone), never the wall clock."""

    executed_at: str
    months: int = DEFAULT_MONTHS


@dg.op
def project_strategy_history_op(context: dg.OpExecutionContext, config: StrategyHistoryConfig) -> None:
    """Project the point-in-time strategy inputs of every consumed issuer over the history cutoffs."""
    tick = datetime.fromisoformat(config.executed_at)
    with psycopg.connect(settings.database_url) as connection:
        summary = project_deployed_history(connection, tick=tick, months=config.months)
        connection.commit()
    context.log.info(
        f"strategy_history: {summary.issuers} issuers over {summary.cutoffs} cutoffs, "
        f"{summary.inserted} rows inserted, {summary.already_present} already present, "
        f"{summary.no_outcome} cutoffs without a financial outcome, {summary.undated} inputs without a filing date, "
        f"{summary.merged_ids} old-generation ids merged into their canonical issuers, "
        f"{summary.split_adjusted} share counts put on the share basis of their close; share counts classed as "
        f"cover {summary.share_basis_cover}, statement {summary.share_basis_statement}, "
        f"unrecognised (filing-date fallback) {summary.share_basis_unrecognised}"
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
            "merged_ids": summary.merged_ids,
            "split_adjusted": summary.split_adjusted,
            "share_basis_cover": summary.share_basis_cover,
            "share_basis_statement": summary.share_basis_statement,
            "share_basis_unrecognised": summary.share_basis_unrecognised,
        }
    )


@dg.job(name=STRATEGY_HISTORY_JOB_NAME)
def strategy_history_pipeline_job() -> None:
    project_strategy_history_op()


@dg.schedule(
    job=strategy_history_pipeline_job,
    cron_schedule=STRATEGY_HISTORY_CRON,
    execution_timezone="UTC",
    # Where the nightly backtest runs (production and staging). Elsewhere the job runs by hand.
    default_status=schedule_status(settings.app_env),
)
def strategy_history_schedule(context: dg.ScheduleEvaluationContext) -> dg.RunRequest:
    tick = context.scheduled_execution_time.isoformat()
    return dg.RunRequest(
        run_key=tick,
        run_config={"ops": {"project_strategy_history_op": {"config": {"executed_at": tick}}}},
    )


defs = dg.Definitions(jobs=[strategy_history_pipeline_job], schedules=[strategy_history_schedule])
