"""The run health of every Dagster job: did its runs of the last 7 days succeed? (#1466)

Staging, 2026-10-20 and 2026-10-21: `topt_live_pipeline` failed on one SEC timeout, and
`head_reports_pipeline` failed three times on one defect. No verdict, gate or dashboard row
showed either failure. The nightly verdict set knew only the jobs that write a verdict.

This module reads `dagster.runs` and `dagster.run_tags` and judges every job that finished a run in
the last `WINDOW_DAYS` days. The result is a dashboard row (`mart.nightly_verdicts`), not a gate: a red
verdict blocks no merge and no deploy, and the Dagster run that records it stays green.

What counts as a run: a run in the state SUCCESS or FAILURE. A run that waits, runs, or that a person
cancelled says nothing about the job.

A job is red on either of these:

* its last two scheduled runs both failed. A scheduled run carries the Dagster tag
  `dagster/schedule_name`. Two failures in a row with the same trigger are not chance. The two runs
  come from the last `BASELINE_DAYS` days, so a weekly schedule can show them. A job needs at least
  one finished run in the last `WINDOW_DAYS` days to be judged at all;
* its failure rate over `WINDOW_DAYS` days is higher than the failure rate of all other jobs over
  `BASELINE_DAYS` days. "Higher" means that the lower end of the 95% Wilson interval of the job's rate
  lies above the rate of the other jobs. The job needs at least `MIN_RUNS` runs, because an interval
  over two runs is wider than any bound, and at least `MIN_FAILURES` failures: one failure is an event and
  not a rate. Without that floor, a fleet with no failure at all would make one failure in six runs red.

The bound of the second rule is a measurement, not a constant. It is computed from the same table on
the same night. Its own job is left out of it, so a job that fails all week does not raise the bar that
it is judged against.

A job that fails by design when its own check is red (`output_invariants_check`, `runtime_shape_check`)
is judged like any other. Its verdict row and this row then both say red. They say different things: the
check judged the system, the run failed.

The summary shows job names and counts only, because the verdict is published on `/health`. The text of
an error stays on `/admin/pipeline`.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

#: Days of runs that one verdict judges, and days of runs that set the failure rate of the other jobs.
WINDOW_DAYS = 7
BASELINE_DAYS = 30
#: Fewer runs than this in the window give no judgement by rate. One failure never gives one.
MIN_RUNS = 3
MIN_FAILURES = 2
#: z of the two-sided 95% interval.
Z95 = 1.959963984540054

FAILURE = "FAILURE"

#: Dagster stores `create_timestamp` as a UTC time without a zone.
RUNS_SQL = """
select r.pipeline_name,
       r.status,
       r.create_timestamp at time zone 'UTC',
       exists (select 1 from dagster.run_tags t
                where t.run_id = r.run_id and t.key = 'dagster/schedule_name') as scheduled
from dagster.runs r
where r.status in ('SUCCESS', 'FAILURE')
  and r.create_timestamp >= (%s::timestamptz at time zone 'UTC')
order by r.create_timestamp desc
"""


@dataclass(frozen=True)
class Run:
    job: str
    failed: bool
    created_at: datetime
    scheduled: bool


@dataclass(frozen=True)
class JobHealth:
    job: str
    runs: int
    failures: int
    #: The 95% Wilson interval of failures / runs.
    interval: tuple[float, float]
    #: The newest scheduled runs of the last `BASELINE_DAYS` days that failed one after the other.
    scheduled_in_a_row: int
    #: Failure rate of all other jobs over `BASELINE_DAYS` days, or None when no other job ran.
    others_rate: float | None
    red: tuple[str, ...]


@dataclass(frozen=True)
class Judgement:
    jobs: tuple[JobHealth, ...]
    runs: int
    failures: int

    @property
    def red(self) -> tuple[JobHealth, ...]:
        return tuple(job for job in self.jobs if job.red)

    @property
    def summary(self) -> str:
        head = f"{len(self.jobs)} jobs, {self.failures} of {self.runs} runs failed in {WINDOW_DAYS} days"
        if not self.red:
            return f"ok: {head}"
        # `verdict()` puts "failed: " before a red summary, so the text starts with the first job.
        return "; ".join(f"{job.job} ({', '.join(job.red)})" for job in self.red) + f". {head}"


def wilson(failures: int, n: int, z: float = Z95) -> tuple[float, float]:
    """The Wilson score interval of the proportion failures / n. `n` is at least 1."""
    if n < 1:
        raise ValueError("a Wilson interval needs at least one run")
    p = failures / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return max(0.0, centre - margin), min(1.0, centre + margin)


def read_runs(connection: Any, *, now: datetime) -> list[Run]:
    """Every finished run of the last `BASELINE_DAYS` days, newest first."""
    rows = connection.execute(RUNS_SQL, (now - timedelta(days=BASELINE_DAYS),)).fetchall()
    return [
        Run(str(job), status == FAILURE, created_at, bool(scheduled)) for job, status, created_at, scheduled in rows
    ]


def scheduled_failures_in_a_row(runs: Iterable[Run]) -> int:
    """How many of the newest scheduled runs failed one after the other. `runs` is newest first."""
    count = 0
    for run in runs:
        if not run.scheduled:
            continue
        if not run.failed:
            break
        count += 1
    return count


def _rate_of(runs: Iterable[Run]) -> float | None:
    counted = list(runs)
    return sum(run.failed for run in counted) / len(counted) if counted else None


def _red_reasons(window: list[Run], in_a_row: int, others_rate: float | None) -> tuple[str, ...]:
    """The rules, for the runs of one job in the window and its scheduled failures in a row."""
    failures = sum(run.failed for run in window)
    low, high = wilson(failures, len(window))
    reasons: list[str] = []
    if in_a_row >= 2:
        reasons.append(f"last {in_a_row} scheduled runs failed")
    if others_rate is not None and len(window) >= MIN_RUNS and failures >= MIN_FAILURES and low > others_rate:
        reasons.append(f"{failures} of {len(window)} runs failed, {low:.0%} to {high:.0%} against {others_rate:.0%}")
    return tuple(reasons)


def judge(runs: Iterable[Run], *, now: datetime) -> Judgement:
    """The verdict over `runs` (finished runs, newest first) as of `now`."""
    baseline_start = now - timedelta(days=BASELINE_DAYS)
    window_start = now - timedelta(days=WINDOW_DAYS)
    kept = sorted(
        (run for run in runs if baseline_start <= run.created_at <= now), key=lambda r: r.created_at, reverse=True
    )
    jobs: list[JobHealth] = []
    for job in sorted({run.job for run in kept}):
        history = [run for run in kept if run.job == job]
        window = [run for run in history if run.created_at >= window_start]
        if not window:
            continue
        others_rate = _rate_of(run for run in kept if run.job != job)
        in_a_row = scheduled_failures_in_a_row(history)
        failures = sum(run.failed for run in window)
        jobs.append(
            JobHealth(
                job=job,
                runs=len(window),
                failures=failures,
                interval=wilson(failures, len(window)),
                scheduled_in_a_row=in_a_row,
                others_rate=others_rate,
                red=_red_reasons(window, in_a_row, others_rate),
            )
        )
    return Judgement(
        tuple(jobs),
        runs=sum(job.runs for job in jobs),
        failures=sum(job.failures for job in jobs),
    )
