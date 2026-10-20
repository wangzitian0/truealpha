"""Every deployed Dagster job has a maximum runtime from measured run times (#1442).

The environment contract (`run_time_bounds`) holds the measured run times. The run launcher
writes the derived bound to each run (`data_engine.run_launcher`). These tests hold the catalog
equal to the deployed jobs in both directions, so a new job or schedule without a basis fails
here, and a retired job leaves no stale entry.
"""

from __future__ import annotations

import pytest
from dagster._core.storage.tags import MAX_RUNTIME_SECONDS_TAG
from data_engine.dagster_defs import build_definitions
from data_engine.run_launcher import max_runtime_tags
from truealpha_contracts.environment_contract import load_environment_contract

#: The environments that run the deployed definitions. Their schedules differ in status only.
DEPLOYED_ENVIRONMENTS = ("production", "staging")


def _repositories():
    return [build_definitions(environment).get_repository_def() for environment in DEPLOYED_ENVIRONMENTS]


def _deployed_job_names() -> set[str]:
    return {job.name for repository in _repositories() for job in repository.get_all_jobs()}


def test_every_schedule_runs_a_job_that_has_a_run_time_basis() -> None:
    catalog = load_environment_contract().run_time_bounds.jobs
    schedules = {(schedule.name, schedule.job_name) for repo in _repositories() for schedule in repo.schedule_defs}
    assert schedules, "the deployed definitions declare no schedule"

    missing = sorted(f"{name} -> {job}" for name, job in schedules if job not in catalog)

    assert missing == [], (
        "these schedules run a job without a run_time_bounds entry in environment_contract.v1.json; "
        f"measure the job and add its entry: {missing}"
    )


def test_every_sensor_target_has_a_run_time_basis() -> None:
    catalog = load_environment_contract().run_time_bounds.jobs
    targets = {
        (sensor.name, target.job_name)
        for repository in _repositories()
        for sensor in repository.sensor_defs
        for target in sensor.targets
    }
    assert targets, "the deployed definitions declare no sensor target"

    missing = sorted(f"{name} -> {job}" for name, job in targets if job not in catalog)

    assert missing == [], f"these sensors launch a job without a run_time_bounds entry: {missing}"


def test_the_catalog_names_exactly_the_deployed_jobs() -> None:
    catalog = set(load_environment_contract().run_time_bounds.jobs)
    deployed = _deployed_job_names()

    assert sorted(deployed - catalog) == [], "add a run_time_bounds entry for each new job"
    assert sorted(catalog - deployed) == [], "delete the run_time_bounds entry of each retired job"


@pytest.mark.parametrize("job_name", sorted(load_environment_contract().run_time_bounds.jobs))
def test_no_measured_success_reaches_the_bound_of_its_job(job_name: str) -> None:
    """A bound below a real success would end a healthy run. Each recorded success must fit."""
    contract = load_environment_contract()
    basis = contract.run_time_bounds.jobs[job_name]
    bound = contract.max_runtime_seconds(job_name)
    if basis.unmeasured is not None:
        assert bound is None
        assert max_runtime_tags(job_name) == {}
        return

    assert bound is not None
    assert max_runtime_tags(job_name) == {MAX_RUNTIME_SECONDS_TAG: str(bound)}
    for environment, times in (basis.measured or {}).items():
        assert bound > times.longest_minutes * 60, f"{job_name}: a {environment} success reaches the bound"
