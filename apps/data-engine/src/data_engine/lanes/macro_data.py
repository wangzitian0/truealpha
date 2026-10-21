"""The daily macro base data refresh (#1255).

One op loads the U.S. Treasury "Daily Treasury Par Yield Curve Rates" into
``mart.macro_series_daily``: the 3-month, 2-year and 10-year par yields. The 3-month yield is the
risk-free rate of the core strategy (``datahub.risk_free``). The three series are the first macro
base data of the admin panel ``/admin/macro``. ``datahub.treasury_rates`` holds the client.
``MACRO_REGISTRATIONS`` in ``source_registrations.py`` holds the series, the ledger seat, the per-key
cost and the cadence.

The first run loads one file per calendar year from the first year of the registration. Every later
run loads the current year. In January it loads the year before too. A file that landed on the same
UTC day is not fetched again (single fetch per key, #1411).

The op records one nightly verdict, ``macro_freshness``. It is green when the newest observation of
every series is not older than the cadence of the registration. It is red when a series has no
observation or is older, and red when the op crashes. The verdict is a dashboard. It gates no merge.
``tools/nightly_verdicts.json`` carries the name. ``common/runtime/tests/test_nightly_verdicts.py``
holds the two sets equal.

The schedule fires every day at 20:30 UTC, ahead of every consumer of the night: the market data
refresh (21:15 UTC), the nightly backtest (21:45 UTC) and the TOPT ticks (22:15 and 22:45 UTC). A
consumer reads the rate that was knowable at its cutoff. That rate is the one of the day before, so
a rate that the Treasury publishes late on the day of the run is not needed before the next run.
The schedule runs in production and in staging. Elsewhere the job runs only by hand.

Manual replay: launch ``macro_data_refresh_pipeline`` from the Dagster UI launchpad, or through
GraphQL. The job takes no run config. A replay on the same UTC day makes no request, because the files
of that day have landed. A replay on a later day fetches the files again and appends the new or
revised observations.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

import dagster as dg
import psycopg

from data_engine.config import settings
from data_engine.datahub.treasury_rates import REGISTRATION, refresh_macro_series
from data_engine.lanes.backtest import schedule_status
from data_engine.quality.nightly_verdicts import TICK_TAG, tick_of, verdict
from data_engine.sources import gateway

MACRO_DATA_REFRESH_JOB_NAME = "macro_data_refresh_pipeline"
# Every day 20:30 UTC. The Treasury publishes on business days, and a run on a day without
# a new file appends nothing. A daily run also repairs a failed run of the day before, including
# on a weekend, and it keeps the cadence of the verdict at 24 hours.
MACRO_DATA_REFRESH_CRON = "30 20 * * *"

#: Verdict name (`mart.nightly_verdicts.check_name`) this lane records.
MACRO_FRESHNESS_VERDICT = "macro_freshness"
NIGHTLY_VERDICTS: tuple[str, ...] = (MACRO_FRESHNESS_VERDICT,)


@dataclass(frozen=True)
class MacroFreshness:
    ok: bool
    summary: str


# One definition of "fresh": the view `mart.macro_series_status` states the age in calendar days and
# the limit of each series. The admin panel reads the same view.
_STATUS_SQL = """
select series_id, observation_date, age_days, max_age_days, is_fresh
from mart.macro_series_status
order by series_id
"""


def judge_freshness(connection: psycopg.Connection) -> MacroFreshness:
    """Is the newest observation of every registered series within the cadence of its registration?

    A series that the registration names and the view lacks is a problem too: it has no catalog row.
    """
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


@dg.op
def refresh_macro_data_op(context: dg.OpExecutionContext) -> None:
    """Load the Treasury files, then judge the freshness of every series.

    The observations are committed before they are judged: a stale verdict fails the run and does
    not roll back what the run did load."""
    with verdict(
        MACRO_FRESHNESS_VERDICT, registered=NIGHTLY_VERDICTS, run_id=context.run_id, tick=tick_of(context)
    ) as outcome:
        now = datetime.now(UTC)
        with (
            gateway.run_scope(f"dagster:{context.run_id}"),
            gateway.capacity_scope(),
            psycopg.connect(settings.database_url) as connection,
        ):
            results = refresh_macro_series(connection, today=now.date())
            for result in results:
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
    # Where the tick and the nightly backtest run (production and staging): both read the rate.
    # Elsewhere by hand.
    default_status=schedule_status(settings.app_env),
)
def macro_data_refresh_schedule(context: dg.ScheduleEvaluationContext) -> dg.RunRequest:
    tick = context.scheduled_execution_time.isoformat()
    # The job takes no config, so the tick travels as a tag: the verdict is dated by it.
    return dg.RunRequest(run_key=tick, tags={TICK_TAG: tick})


defs = dg.Definitions(jobs=[macro_data_refresh_pipeline_job], schedules=[macro_data_refresh_schedule])
