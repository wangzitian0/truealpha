"""The deployed Dagster instance config, `apps/data-engine/dagster.yaml` (#1072).

The image copies this file to `/opt/dagster/dagster.yaml`. The daemon, the webserver and
the code server read it through `DAGSTER_HOME`. No test opens a database here. The tests
check three facts:

1. The installed Dagster accepts the file. An unknown key must fail the load.
2. The effective tick retention keeps every tick, except skipped sensor ticks.
3. Run monitoring ends a run that outlives every real run (#1271, #1442).

Three sensors poll every 30 seconds. Each poll that does no work writes one `SKIPPED` tick
to `dagster.job_ticks`. The retention block limits that table.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from dagster import DagsterInstance
from dagster._core.definitions.run_request import InstigatorType
from dagster._core.errors import DagsterInvalidConfigError
from dagster._core.instance.config import dagster_instance_config
from dagster._core.instance.ref import InstanceRef
from dagster._core.scheduler.instigation import TickStatus
from truealpha_contracts.environment_contract import load_environment_contract

DAGSTER_YAML = Path(__file__).resolve().parents[1] / "dagster.yaml"

#: Days after which the daemon deletes a tick. A value of -1 keeps the tick for ever.
KEEP = -1
SKIPPED_SENSOR_TICK_DAYS = 3

#: The limit that `dagster.yaml` sets for a run without a `dagster/max_runtime` tag: 3 hours.
MAX_RUNTIME_SECONDS = 3 * 60 * 60
#: The fewest run-monitor polls that the shortest bound (contract `floor_minutes`) must span.
MONITOR_POLLS_PER_FLOOR = 5


def _instance_from_the_real_file() -> DagsterInstance:
    """Build the instance from the real file, as the daemon does."""
    instance_ref = InstanceRef.from_dir(str(DAGSTER_YAML.parent))
    return DagsterInstance.ephemeral(settings=instance_ref.settings)


def _effective_retention(instigator_type: InstigatorType) -> dict[TickStatus, int]:
    """Ask the instance for its tick retention."""
    return dict(_instance_from_the_real_file().get_tick_retention_settings(instigator_type))


def test_dagster_yaml_passes_the_installed_instance_config_schema() -> None:
    """The load validates every key of the file against the installed Dagster schema."""
    config, _custom_instance_class = dagster_instance_config(str(DAGSTER_YAML.parent))

    assert "retention" in config, "dagster.yaml has no retention block (#1072)"
    assert set(config["retention"]) == {"sensor"}, "only the sensor tick retention is set"


def test_sensor_retention_deletes_only_skipped_ticks() -> None:
    """Skipped sensor ticks expire. Started, successful and failed ticks stay."""
    assert _effective_retention(InstigatorType.SENSOR) == {
        TickStatus.SKIPPED: SKIPPED_SENSOR_TICK_DAYS,
        TickStatus.STARTED: KEEP,
        TickStatus.SUCCESS: KEEP,
        TickStatus.FAILURE: KEEP,
    }


def test_schedule_retention_keeps_every_tick() -> None:
    """Schedule ticks are few and record real work. No status expires."""
    assert _effective_retention(InstigatorType.SCHEDULE) == {
        TickStatus.SKIPPED: KEEP,
        TickStatus.STARTED: KEEP,
        TickStatus.SUCCESS: KEEP,
        TickStatus.FAILURE: KEEP,
    }


def _rename_key(mapping: dict[str, Any], old: str, new: str) -> None:
    """Rename one key and keep its value. The key order stays the same."""
    items = [(new if key == old else key, value) for key, value in mapping.items()]
    mapping.clear()
    mapping.update(items)


def _misspell_purge_after_days(retention: dict[str, Any]) -> None:
    _rename_key(retention["sensor"], "purge_after_days", "purge_after_day")


def _misspell_skipped(retention: dict[str, Any]) -> None:
    _rename_key(retention["sensor"]["purge_after_days"], "skipped", "skiped")


def _misspell_sensor(retention: dict[str, Any]) -> None:
    _rename_key(retention, "sensor", "sensors")


@pytest.mark.parametrize(
    "mutate",
    [_misspell_purge_after_days, _misspell_skipped, _misspell_sensor],
    ids=lambda mutate: mutate.__name__,
)
def test_an_unknown_retention_key_fails_the_instance_config_load(
    tmp_path: Path, mutate: Callable[[dict[str, Any]], None]
) -> None:
    """The schema check must be able to fail. A misspelled key must raise an error.

    Without this test, a loader that ignores unknown keys would keep every other test green.
    """
    config = yaml.safe_load(DAGSTER_YAML.read_text(encoding="utf-8"))
    assert "retention" in config, "dagster.yaml has no retention block (#1072)"
    mutate(config["retention"])
    (tmp_path / "dagster.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")

    with pytest.raises(DagsterInvalidConfigError):
        dagster_instance_config(str(tmp_path))


def test_the_run_launcher_is_the_class_that_the_composition_root_imports() -> None:
    """`dagster.yaml` names the launcher by string. The composition root imports the class (#1442)."""
    from data_engine.dagster_defs import MonitoredRunLauncher

    launcher = yaml.safe_load(DAGSTER_YAML.read_text(encoding="utf-8"))["run_launcher"]

    assert launcher == {"module": MonitoredRunLauncher.__module__, "class": MonitoredRunLauncher.__name__}


def test_run_monitoring_is_on_and_ends_a_run_after_the_limit() -> None:
    """A run whose process died with its container stays `STARTED` until the limit ends it (#1271)."""
    instance = _instance_from_the_real_file()

    assert instance.run_monitoring_enabled is True
    assert instance.run_monitoring_max_runtime_seconds == MAX_RUNTIME_SECONDS


def test_the_runtime_limit_leaves_room_for_the_longest_real_run_and_has_a_ceiling() -> None:
    """The limit must not end a real run. It must still end an orphan within one day.

    The environment contract holds the measured run times (#1442). The limit bounds only a run
    without a bound of its own, so it must exceed the margin times every measured success.
    """
    bounds = load_environment_contract().run_time_bounds
    longest_success_seconds = 60 * max(
        times.longest_minutes for basis in bounds.jobs.values() for times in (basis.measured or {}).values()
    )
    limit = _instance_from_the_real_file().run_monitoring_max_runtime_seconds

    assert limit >= bounds.margin * longest_success_seconds
    assert limit <= 24 * 60 * 60


def test_the_monitor_polls_often_enough_for_the_shortest_bound() -> None:
    """A lost run fails at the next poll. The shortest bound spans several polls (#1442)."""
    floor_seconds = 60 * load_environment_contract().run_time_bounds.floor_minutes
    poll_seconds = _instance_from_the_real_file().run_monitoring_poll_interval_seconds

    assert poll_seconds * MONITOR_POLLS_PER_FLOOR <= floor_seconds


def _misspell_max_runtime_seconds(run_monitoring: dict[str, Any]) -> None:
    _rename_key(run_monitoring, "max_runtime_seconds", "max_runtime_second")


def _misspell_enabled(run_monitoring: dict[str, Any]) -> None:
    _rename_key(run_monitoring, "enabled", "enable")


def _misspell_poll_interval_seconds(run_monitoring: dict[str, Any]) -> None:
    _rename_key(run_monitoring, "poll_interval_seconds", "poll_interval_second")


@pytest.mark.parametrize(
    "mutate",
    [_misspell_max_runtime_seconds, _misspell_enabled, _misspell_poll_interval_seconds],
    ids=lambda mutate: mutate.__name__,
)
def test_an_unknown_run_monitoring_key_fails_the_instance_config_load(
    tmp_path: Path, mutate: Callable[[dict[str, Any]], None]
) -> None:
    """A misspelled key must raise an error, or the limit could be silently absent."""
    config = yaml.safe_load(DAGSTER_YAML.read_text(encoding="utf-8"))
    assert "run_monitoring" in config, "dagster.yaml has no run_monitoring block (#1271)"
    mutate(config["run_monitoring"])
    (tmp_path / "dagster.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")

    with pytest.raises(DagsterInvalidConfigError):
        dagster_instance_config(str(tmp_path))
