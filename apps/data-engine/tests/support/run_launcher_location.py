"""A code location for the run launcher tests (#1442).

`test_run_launcher.py` serves this file from a real `dagster api grpc` process. Its one job waits,
so the test can kill the code server while the run is `STARTED`.
"""

import time

import dagster as dg

WAITING_JOB_NAME = "run_launcher_waiting_job"


@dg.op
def wait_for_the_kill() -> None:
    time.sleep(600)


@dg.job(name=WAITING_JOB_NAME)
def run_launcher_waiting_job() -> None:
    wait_for_the_kill()


defs = dg.Definitions(jobs=[run_launcher_waiting_job])
