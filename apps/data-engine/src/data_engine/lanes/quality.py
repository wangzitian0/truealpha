"""Data-quality standing checks that run where the data lives (#674, #725 item A, #581).

`tools/output_invariants.py` is the only check that asks whether a published NUMBER is
possible. Until 2026-09-07 its only scheduled host was a GitHub runner that cannot reach
the loopback Postgres, so the step announced itself unconfigured and exited 0 every day;
run by hand against production that day it found two stale invariants and no new data
defect. This lane runs the same suite, from the copy baked into the image, against the
environment's own database after the nightly ticks — a red Dagster run, in the daemon's
log and in `dagster.runs`, is the verdict.
"""

import contextlib
import io
import runpy
from pathlib import Path

import dagster as dg

from data_engine.config import settings

OUTPUT_INVARIANTS_JOB_NAME = "output_invariants_check"
# 00:15 UTC: after the canary (23:47) has published, before anything reads the day's head.
OUTPUT_INVARIANTS_CRON = "15 0 * * *"

#: The suite as the image carries it (Dockerfile), or the repository copy for local runs.
_IMAGE_COPY = Path("/app/tools/output_invariants.py")
_REPO_COPY = Path(__file__).resolve().parents[5] / "tools" / "output_invariants.py"


def _suite_path() -> Path:
    for candidate in (_IMAGE_COPY, _REPO_COPY):
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"output_invariants.py is neither at {_IMAGE_COPY} nor {_REPO_COPY}")


@dg.op
def run_output_invariants(context: dg.OpExecutionContext) -> str:
    """Run the suite against this environment's database; fail the run on any verdict
    the suite fails on (a violation, an expired exemption, a stale exemption, missing
    coverage). The suite's own report is the run log."""
    module = runpy.run_path(str(_suite_path()), run_name="output_invariants")
    report = io.StringIO()
    with contextlib.redirect_stdout(report), contextlib.redirect_stderr(report):
        exit_code = module["main"](["--database-url", settings.database_url, "--require-coverage"])
    for line in report.getvalue().splitlines():
        context.log.info(line)
    if exit_code != 0:
        raise dg.Failure(f"output invariants: exit {exit_code} — the report above names what failed (#581)")
    return report.getvalue()


@dg.job(name=OUTPUT_INVARIANTS_JOB_NAME)
def output_invariants_job() -> None:
    run_output_invariants()


@dg.schedule(
    job=output_invariants_job,
    cron_schedule=OUTPUT_INVARIANTS_CRON,
    execution_timezone="UTC",
    default_status=dg.DefaultScheduleStatus.RUNNING,
)
def output_invariants_schedule(context: dg.ScheduleEvaluationContext) -> dg.RunRequest:
    return dg.RunRequest(run_key=context.scheduled_execution_time.isoformat())


defs = dg.Definitions(jobs=[output_invariants_job], schedules=[output_invariants_schedule])
