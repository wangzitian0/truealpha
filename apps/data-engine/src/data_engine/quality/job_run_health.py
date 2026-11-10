# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from __future__ import annotations; import math; from collections.abc import Iterable; from dataclasses import dataclass; from datetime import datetime, timedelta; from typing import Any; WINDOW_DAYS = 7; BASELINE_DAYS = 30; MIN_RUNS = 3; MIN_FAILURES = 2; Z95 = 1.959963984540054; FAILURE = "FAILURE"; RUNS_SQL = "select r.pipeline_name, r.status, r.create_timestamp at time zone'UTC', exists (select 1 from dagster.run_tags t where t.run_id = r.run_id and t.key ='dagster/schedule_name') as scheduled from dagster.runs r where r.status in ('SUCCESS','FAILURE') and r.create_timestamp >= (%s::timestamptz at time zone'UTC') order by r.create_timestamp desc"
@dataclass(frozen=True)
class Run: job: str; failed: bool; created_at: datetime; scheduled: bool
@dataclass(frozen=True)
class JobHealth: job: str; runs: int; failures: int; interval: tuple[float, float]; scheduled_in_a_row: int; others_rate: float | None; red: tuple[str, ...]
@dataclass(frozen=True)
class Judgement:
    jobs: tuple[JobHealth, ...]; runs: int; failures: int
    @property
    def red(self) -> tuple[JobHealth, ...]: return tuple(job for job in self.jobs if job.red)
    @property
    def summary(self) -> str:
        head = f"{len(self.jobs)} jobs, {self.failures} of {self.runs} runs failed in {WINDOW_DAYS} days"
        if not self.red: return f"ok: {head}"
        return "; ".join(f"{job.job} ({', '.join(job.red)})" for job in self.red) + f". {head}"
def wilson(failures: int, n: int, z: float = Z95) -> tuple[float, float]:
    if n < 1: raise ValueError("a Wilson interval needs at least one run")
    p = failures / n; denominator = 1 + z * z / n; centre = (p + z * z / (2 * n)) / denominator; margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator; return max(0.0, centre - margin), min(1.0, centre + margin)
def read_runs(connection: Any, *, now: datetime) -> list[Run]: rows = connection.execute(RUNS_SQL, (now - timedelta(days=BASELINE_DAYS),)).fetchall(); return [ Run(str(job), status == FAILURE, created_at, bool(scheduled)) for job, status, created_at, scheduled in rows ]
def scheduled_failures_in_a_row(runs: Iterable[Run]) -> int:
    count = 0
    for run in runs:
        if not run.scheduled: continue
        if not run.failed: break
        count += 1
    return count
def _rate_of(runs: Iterable[Run]) -> float | None: counted = list(runs); return sum(run.failed for run in counted) / len(counted) if counted else None
def _red_reasons(window: list[Run], in_a_row: int, newest_failed: bool, others_rate: float | None) -> tuple[str, ...]:
    failures = sum(run.failed for run in window); low, high = wilson(failures, len(window)); reasons: list[str] = []
    if in_a_row >= 2 and newest_failed: reasons.append(f"last {in_a_row} scheduled runs failed")
    if others_rate is not None and len(window) >= MIN_RUNS and failures >= MIN_FAILURES and low > others_rate: reasons.append(f"{failures} of {len(window)} runs failed, {low:.0%} to {high:.0%} against {others_rate:.0%}")
    return tuple(reasons)
def judge(runs: Iterable[Run], *, now: datetime) -> Judgement:
    baseline_start = now - timedelta(days=BASELINE_DAYS); window_start = now - timedelta(days=WINDOW_DAYS); kept = sorted( (run for run in runs if baseline_start <= run.created_at <= now), key=lambda r: r.created_at, reverse=True ); jobs: list[JobHealth] = []
    for job in sorted({run.job for run in kept}):
        history = [run for run in kept if run.job == job]; window = [run for run in history if run.created_at >= window_start]
        if not window: continue
        others_rate = _rate_of(run for run in kept if run.job != job); in_a_row = scheduled_failures_in_a_row(history); failures = sum(run.failed for run in window); jobs.append( JobHealth( job=job, runs=len(window), failures=failures, interval=wilson(failures, len(window)), scheduled_in_a_row=in_a_row, others_rate=others_rate, red=_red_reasons(window, in_a_row, history[0].failed, others_rate), ) )
    return Judgement(tuple(jobs), runs=sum(job.runs for job in jobs), failures=sum(job.failures for job in jobs))
