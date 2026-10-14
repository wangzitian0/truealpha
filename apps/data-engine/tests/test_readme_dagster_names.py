"""The README names real Dagster objects (#1286).

A review of #1286 found `production_topt_tick_job` in the README. No such job exists: the
scheduled job is `topt_live_pipeline`. An unresolved review thread then blocked two releases.
This test reads the "Production TOPT orchestration" section and requires each job, schedule and
sensor name in it to exist in the deployed definitions.
"""

from __future__ import annotations

import re
from pathlib import Path

from data_engine.dagster_defs import defs

README = Path(__file__).resolve().parents[3] / "README.md"
SECTION = "### Production TOPT orchestration"
DAGSTER_NAME = re.compile(r"`([a-z][a-z0-9_]*_(?:pipeline|job|schedule|sensor))`")


def _section_text() -> str:
    text = README.read_text(encoding="utf-8")
    assert SECTION in text, f"README.md has no section {SECTION!r}"
    return text.split(SECTION, 1)[1].split("\n## ", 1)[0]


def _deployed_names() -> set[str]:
    return (
        {job.name for job in defs.jobs or ()}
        | {schedule.name for schedule in defs.schedules or ()}
        | {sensor.name for sensor in defs.sensors or ()}
    )


def test_the_readme_orchestration_section_names_real_dagster_objects() -> None:
    named = set(DAGSTER_NAME.findall(_section_text()))

    assert named, "the section names no Dagster object, so this test would prove nothing"
    assert named <= _deployed_names(), (
        f"README.md names objects that the deployed definitions lack: {sorted(named - _deployed_names())}"
    )


def test_the_readme_names_the_scheduled_job_and_its_schedule() -> None:
    named = set(DAGSTER_NAME.findall(_section_text()))

    assert {"topt_live_pipeline", "topt_live_schedule"} <= named
