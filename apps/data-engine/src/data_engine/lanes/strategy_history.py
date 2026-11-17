"""Strategy point-in-time input history projection lane (#1139, M3 track H1)."""

from datetime import datetime

import dagster as dg
import psycopg

from data_engine.config import settings
from data_engine.datahub.strategy_history import DEFAULT_MONTHS, project_deployed_history
from data_engine.external_op import external_op
from data_engine.lanes.backtest import schedule_status

STRATEGY_HISTORY_JOB_NAME = "strategy_history_projection_pipeline"
STRATEGY_HISTORY_CRON = "7 20 * * 1-5"


class StrategyHistoryConfig(dg.Config):
    """Run config carrying tick timestamp and cutoff month count."""

    executed_at: str
    months: int = DEFAULT_MONTHS


@external_op
def project_strategy_history_op(context: dg.OpExecutionContext, config: StrategyHistoryConfig) -> None:
    """Project point-in-time strategy inputs across historical cutoffs."""
    tick = datetime.fromisoformat(config.executed_at)
    with psycopg.connect(settings.database_url) as connection:
        summary = project_deployed_history(connection, tick=tick, months=config.months)
        connection.commit()
    metadata = {
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
    context.log.info("strategy_history: %s", metadata)
    if summary.unresolved_issuers:
        context.log.warning(f"strategy_history: no stored lineage for {list(summary.unresolved_issuers)}")
    context.add_output_metadata(metadata)


@dg.job(name=STRATEGY_HISTORY_JOB_NAME)
def strategy_history_pipeline_job() -> None:
    project_strategy_history_op()


@dg.schedule(
    job=strategy_history_pipeline_job,
    cron_schedule=STRATEGY_HISTORY_CRON,
    execution_timezone="UTC",
    default_status=schedule_status(settings.app_env),
)
def strategy_history_schedule(context: dg.ScheduleEvaluationContext) -> dg.RunRequest:
    tick = context.scheduled_execution_time.isoformat()
    return dg.RunRequest(
        run_key=tick,
        run_config={"ops": {"project_strategy_history_op": {"config": {"executed_at": tick}}}},
    )


defs = dg.Definitions(jobs=[strategy_history_pipeline_job], schedules=[strategy_history_schedule])
