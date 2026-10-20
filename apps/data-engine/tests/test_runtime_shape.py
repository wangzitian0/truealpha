"""The runtime-shape verdict reads the memory cgroup of the code server (#1467, #28 V5).

Each test builds a cgroup tree and a process table in `tmp_path`, so no test depends on the host. The
judgement is a pure function of a reading, a baseline, a release and the declared capacity. The job
test runs the deployed job on a Dagster instance and holds the baseline across two nights.

A test fails under its mutation: a comparison turned around, a kill count that ignores the baseline,
an unreadable file that reads as zero, or a baseline that is not saved.
"""

from __future__ import annotations

import functools
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import dagster as dg
import pytest
from data_engine.lanes import quality, standards
from data_engine.quality import nightly_verdicts, runtime_shape
from data_engine.quality.runtime_shape import MIB, Baseline, Reading
from truealpha_contracts.environment_contract import CodeServerCapacity, load_environment_contract

CAPACITY: CodeServerCapacity = load_environment_contract().code_server_capacity
CEILING = CAPACITY.ceiling_mib * MIB
BOOT = 1_760_000_000
TICK = "2026-10-21T06:30:00+00:00"
TICKS_PER_SECOND = os.sysconf("SC_CLK_TCK")
#: The reader as the deployed code has it. A test patches the module attribute, never this name.
REAL_READ = runtime_shape.read


def _tree(
    root: Path,
    *,
    memory_max: str | None = str(CEILING),
    peak: int | None = 400 * MIB,
    oom_kill: int | None = 0,
    start_ticks: int | None = 100 * TICKS_PER_SECOND,
    command: str = "dagster api grpc",
) -> tuple[Path, Path]:
    """Write a cgroup tree and a process table. A None argument leaves the file out."""
    cgroup, proc = root / "cgroup", root / "proc"
    (proc / "1").mkdir(parents=True)
    cgroup.mkdir()
    if memory_max is not None:
        (cgroup / "memory.max").write_text(f"{memory_max}\n")
    if peak is not None:
        (cgroup / "memory.peak").write_text(f"{peak}\n")
    if oom_kill is not None:
        (cgroup / "memory.events").write_text(f"low 0\nhigh 0\nmax 870\noom 1\noom_kill {oom_kill}\noom_group_kill 0\n")
    if start_ticks is not None:
        after_name = ["S", *["0"] * 18, str(start_ticks), *["0"] * 30]
        (proc / "1" / "stat").write_text(f"1 ({command}) {' '.join(after_name)}\n")
        (proc / "stat").write_text(f"cpu  1 2 3\nbtime {BOOT}\nprocesses 9\n")
    return cgroup, proc


def _reading(tmp_path: Path, **tree: Any) -> Reading:
    cgroup, proc = _tree(tmp_path, **tree)
    return runtime_shape.read(cgroup, proc)


def _judge(reading: Reading, *, baseline: Baseline | None = None, release: str = "sha256:a") -> runtime_shape.Judgement:
    return runtime_shape.judge(reading, baseline=baseline, release=release, capacity=CAPACITY)


STARTED = BOOT + 100


# --- reading the files -----------------------------------------------------------------


def test_the_reading_holds_the_four_facts_of_the_files(tmp_path: Path) -> None:
    reading = _reading(tmp_path, memory_max="1610612736", peak=123456789, oom_kill=7)

    assert reading == Reading(
        memory_max=1610612736, unlimited=False, memory_peak=123456789, oom_kill=7, started=STARTED
    )
    assert reading.unreadable == ()


def test_the_start_second_reads_past_a_process_name_with_spaces_and_brackets(tmp_path: Path) -> None:
    assert _reading(tmp_path, command="a b) (c").started == STARTED


def test_a_memory_max_of_max_is_a_ceiling_that_is_not_there_not_an_unreadable_file(tmp_path: Path) -> None:
    reading = _reading(tmp_path, memory_max="max")

    assert reading.unlimited is True
    assert reading.memory_max is None
    assert reading.unreadable == ()


@pytest.mark.parametrize(
    ("missing", "name"),
    [
        ("memory_max", "memory.max"),
        ("peak", "memory.peak"),
        ("oom_kill", "memory.events"),
        ("start_ticks", "process start time"),
    ],
)
def test_a_file_that_cannot_be_read_is_named_and_never_read_as_zero(tmp_path: Path, missing: str, name: str) -> None:
    reading = _reading(tmp_path, **{missing: None})

    assert reading.unreadable == (name,)


def test_a_events_file_without_oom_kill_is_unreadable(tmp_path: Path) -> None:
    cgroup, proc = _tree(tmp_path)
    (cgroup / "memory.events").write_text("low 0\nhigh 0\nmax 0\noom 0\n")

    assert runtime_shape.read(cgroup, proc).unreadable == ("memory.events",)


def test_a_file_with_text_that_is_not_a_number_is_unreadable(tmp_path: Path) -> None:
    cgroup, proc = _tree(tmp_path)
    (cgroup / "memory.peak").write_text("lots\n")

    assert runtime_shape.read(cgroup, proc).unreadable == ("memory.peak",)


def test_a_reading_with_a_missing_fact_is_red_even_when_every_other_fact_is_good(tmp_path: Path) -> None:
    verdict = _judge(_reading(tmp_path, peak=None))

    assert verdict.red == ("unreadable: memory.peak",)
    assert "memory.peak" in verdict.summary


def test_an_empty_reading_is_red_for_all_four_facts() -> None:
    verdict = _judge(Reading())

    assert verdict.red == ("unreadable: memory.max, memory.peak, memory.events, process start time",)
    assert verdict.baseline is None, "a reading that lacks a fact cannot become the baseline"


def test_the_deployed_paths_are_the_container_cgroup_and_process_table() -> None:
    assert runtime_shape.CGROUP_ROOT == Path("/sys/fs/cgroup")
    assert runtime_shape.PROC_ROOT == Path("/proc")


# --- the judgement ---------------------------------------------------------------------


def test_a_quiet_night_is_green_and_leaves_the_baseline_of_the_next_check(tmp_path: Path) -> None:
    verdict = _judge(_reading(tmp_path, peak=400 * MIB, oom_kill=0))

    assert verdict.red == ()
    assert verdict.summary == "no OOM kill since the last check; peak 400 of 1536 MiB (26%)"
    assert verdict.baseline == Baseline(oom_kill=0, started=STARTED, release="sha256:a")


def test_an_oom_kill_since_the_last_check_is_red(tmp_path: Path) -> None:
    baseline = Baseline(oom_kill=1, started=STARTED, release="sha256:a")

    verdict = _judge(_reading(tmp_path, oom_kill=3), baseline=baseline)

    assert verdict.red == ("2 OOM kill(s) since the last check",)
    assert verdict.baseline == Baseline(oom_kill=3, started=STARTED, release="sha256:a"), (
        "the next check counts from here"
    )


def test_a_kill_that_the_last_check_already_counted_is_not_red_again(tmp_path: Path) -> None:
    baseline = Baseline(oom_kill=3, started=STARTED, release="sha256:a")

    assert _judge(_reading(tmp_path, oom_kill=3), baseline=baseline).red == ()


def test_the_first_check_counts_every_kill_of_the_container(tmp_path: Path) -> None:
    assert _judge(_reading(tmp_path, oom_kill=2), baseline=None).red == ("2 OOM kill(s) since the last check",)


def test_a_new_container_counts_its_kills_from_zero(tmp_path: Path) -> None:
    """The baseline of the old container says 5 kills. The new container shows 2: all 2 are new."""
    baseline = Baseline(oom_kill=5, started=STARTED - 3600, release="sha256:old")

    verdict = _judge(_reading(tmp_path, oom_kill=2), baseline=baseline, release="sha256:new")

    assert verdict.red == ("2 OOM kill(s) since the last check",)


def test_a_restart_with_a_new_release_is_a_deploy_and_is_green(tmp_path: Path) -> None:
    baseline = Baseline(oom_kill=0, started=STARTED - 3600, release="sha256:old")

    verdict = _judge(_reading(tmp_path), baseline=baseline, release="sha256:new")

    assert verdict.red == ()
    assert verdict.summary.endswith("; the container restarted with a new release")


def test_a_restart_without_a_new_release_is_red_because_the_kill_of_the_server_leaves_no_counter(
    tmp_path: Path,
) -> None:
    """The kernel killed the server process: the container restarted and its counter began at 0."""
    baseline = Baseline(oom_kill=0, started=STARTED - 3600, release="sha256:a")

    verdict = _judge(_reading(tmp_path, oom_kill=0), baseline=baseline, release="sha256:a")

    assert verdict.red == ("the code server restarted without a release change; its OOM counter restarted too",)


@pytest.mark.parametrize(
    ("peak_mib", "red"),
    [(1075, False), (1076, True), (1536, True), (0, False)],
)
def test_a_peak_above_seventy_percent_of_the_ceiling_is_red(tmp_path: Path, peak_mib: int, red: bool) -> None:
    """70% of 1536 MiB is 1075.2 MiB. A peak at the limit is green; one MiB above it is red."""
    verdict = _judge(_reading(tmp_path, peak=peak_mib * MIB))

    assert bool(verdict.red) is red
    if red:
        assert verdict.red[0].startswith(f"peak {peak_mib} of 1536 MiB")
        assert verdict.red[0].endswith("the limit is 70%")


def test_a_peak_exactly_at_seventy_percent_of_the_real_ceiling_is_green_and_one_byte_above_is_red(
    tmp_path: Path,
) -> None:
    """The rule is `above 70%`. 1400 MiB of 2000 MiB is exactly 70%."""
    at_the_limit = _judge(_reading(tmp_path / "a", memory_max=str(2000 * MIB), peak=1400 * MIB))
    one_byte_above = _judge(_reading(tmp_path / "b", memory_max=str(2000 * MIB), peak=1400 * MIB + 1))

    assert at_the_limit.red == ()
    assert len(one_byte_above.red) == 1
    assert one_byte_above.red[0].endswith("the limit is 70%")


def test_the_peak_is_judged_against_the_real_ceiling_not_the_declared_one(tmp_path: Path) -> None:
    """A real ceiling above the declared one makes 1100 MiB 56%: green. 1100 MiB of 1536 MiB is 72%: red."""
    assert _judge(_reading(tmp_path / "a", memory_max=str(2048 * MIB), peak=1100 * MIB)).red == ()
    assert _judge(_reading(tmp_path / "b", memory_max=str(1536 * MIB), peak=1100 * MIB)).red != ()


def test_a_real_ceiling_below_the_declared_one_is_red_for_that_reason(tmp_path: Path) -> None:
    """Staging held 768 MiB while the run limit assumed 1536 MiB."""
    verdict = _judge(_reading(tmp_path, memory_max=str(768 * MIB), peak=100 * MIB))

    assert verdict.red == ("memory.max 768 MiB is below the declared ceiling 1536 MiB",)


def test_no_ceiling_at_all_is_red(tmp_path: Path) -> None:
    verdict = _judge(_reading(tmp_path, memory_max="max"))

    assert verdict.red == ("memory.max is max: the container has no memory ceiling",)


def test_every_reason_is_reported_not_only_the_first(tmp_path: Path) -> None:
    baseline = Baseline(oom_kill=0, started=STARTED, release="sha256:a")

    verdict = _judge(
        _reading(tmp_path, memory_max=str(768 * MIB), peak=768 * MIB, oom_kill=2),
        baseline=baseline,
    )

    assert len(verdict.red) == 3
    assert verdict.summary == "; ".join(verdict.red)


def test_the_summary_is_counts_and_sizes_only(tmp_path: Path) -> None:
    """The summary goes to /health: no path, no error text, no host."""
    summary = _judge(_reading(tmp_path, peak=None, oom_kill=2)).summary

    assert str(tmp_path) not in summary
    assert "/sys/fs/cgroup" not in summary


# --- the baseline in the instance --------------------------------------------------------


def test_the_baseline_survives_a_round_trip_through_the_instance() -> None:
    instance = dg.DagsterInstance.ephemeral()
    baseline = Baseline(oom_kill=4, started=STARTED, release="sha256:a")

    assert runtime_shape.load_baseline(instance) is None
    runtime_shape.save_baseline(instance, baseline)

    assert runtime_shape.load_baseline(instance) == baseline


@pytest.mark.parametrize("stored", ["", "not json", "[1, 2]", '{"oom_kill": "x", "started": 1, "release": "a"}', "{}"])
def test_a_stored_value_that_is_not_a_baseline_reads_as_no_baseline(stored: str) -> None:
    instance = dg.DagsterInstance.ephemeral()
    instance.run_storage.set_cursor_values({runtime_shape.BASELINE_KEY: stored})

    assert runtime_shape.load_baseline(instance) is None


# --- the deployed job ------------------------------------------------------------------


@pytest.fixture
def written(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every verdict the job records, instead of a database row."""
    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(nightly_verdicts, "record", lambda name, **kwargs: rows.append({"check": name, **kwargs}))
    return rows


def _point_the_job_at(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **tree: Any) -> None:
    cgroup, proc = _tree(tmp_path, **tree)
    monkeypatch.setattr(runtime_shape, "read", functools.partial(REAL_READ, cgroup, proc))


def test_the_check_is_a_declared_nightly_verdict_of_a_scheduled_job() -> None:
    assert quality.RUNTIME_SHAPE_VERDICT == "runtime_shape"
    assert "runtime_shape" in quality.NIGHTLY_VERDICTS
    assert "runtime_shape" not in standards.NIGHTLY_VERDICTS
    assert quality.runtime_shape_schedule.job_name == quality.RUNTIME_SHAPE_JOB_NAME == "runtime_shape_check"
    request = quality.runtime_shape_schedule(
        dg.build_schedule_context(scheduled_execution_time=datetime(2026, 10, 21, 6, 30, tzinfo=UTC))
    )
    assert request.tags[nightly_verdicts.TICK_TAG] == TICK


def test_the_job_records_green_then_red_after_a_kill_then_green_again(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, written: list[dict[str, Any]]
) -> None:
    """One instance holds the baseline across three nights. Each night reads a different cgroup."""
    instance = dg.DagsterInstance.ephemeral()
    tags = {nightly_verdicts.TICK_TAG: TICK}

    _point_the_job_at(monkeypatch, tmp_path / "night1", oom_kill=0)
    first = quality.runtime_shape_job.execute_in_process(instance=instance, tags=tags)

    _point_the_job_at(monkeypatch, tmp_path / "night2", oom_kill=2)
    second = quality.runtime_shape_job.execute_in_process(instance=instance, tags=tags, raise_on_error=False)

    _point_the_job_at(monkeypatch, tmp_path / "night3", oom_kill=2)
    third = quality.runtime_shape_job.execute_in_process(instance=instance, tags=tags)

    assert (first.success, second.success, third.success) == (True, False, True), (
        "the run is red when the verdict is red, and the kill is counted once"
    )
    assert [(row["check"], row["ok"]) for row in written] == [("runtime_shape", True)] * 1 + [
        ("runtime_shape", False),
        ("runtime_shape", True),
    ]
    assert written[1]["summary"] == "failed: 2 OOM kill(s) since the last check"
    assert all(row["ran_at"] == datetime.fromisoformat(TICK) for row in written)


def test_the_job_is_red_and_records_red_when_a_file_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, written: list[dict[str, Any]]
) -> None:
    _point_the_job_at(monkeypatch, tmp_path, peak=None)

    result = quality.runtime_shape_job.execute_in_process(raise_on_error=False)

    assert result.success is False
    assert [(row["check"], row["ok"], row["summary"]) for row in written] == [
        ("runtime_shape", False, "failed: unreadable: memory.peak")
    ]


def test_the_job_saves_the_baseline_before_it_goes_red(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, written: list[dict[str, Any]]
) -> None:
    instance = dg.DagsterInstance.ephemeral()
    _point_the_job_at(monkeypatch, tmp_path, oom_kill=3)

    quality.runtime_shape_job.execute_in_process(instance=instance, raise_on_error=False)

    baseline = runtime_shape.load_baseline(instance)
    assert baseline is not None
    assert baseline.oom_kill == 3
