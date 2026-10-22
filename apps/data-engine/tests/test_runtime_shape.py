"""The runtime-shape verdict reads the memory cgroup of the code server (#1467, #28 V5).

Each test builds a cgroup tree and a process table in `tmp_path`, so no test depends on the host. The
reading, the folding of readings into totals, and the judgement are pure functions. The scenario tests
run the real `sample` and `check` on a Dagster instance across several nights, because the failures that
matter happen between two checks: a deploy that replaces the container, a peak that stays in the
counter, a re-run of a tick, a lost state.

A test fails under its mutation: a comparison turned around, a kill count that ignores the totals, an
unreadable file that reads as zero, a baseline that a manual run moves, or a sampler that does not save.
"""

from __future__ import annotations

import dataclasses
import functools
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import dagster as dg
import pytest
from data_engine.lanes import quality, standards
from data_engine.quality import nightly_verdicts, runtime_shape
from data_engine.quality.runtime_shape import MIB, Baseline, Reading, Tracker
from truealpha_contracts.environment_contract import UTILIZATION_CEILING, CodeServerCapacity, load_environment_contract

CAPACITY: CodeServerCapacity = load_environment_contract().code_server_capacity
CEILING = CAPACITY.ceiling_mib * MIB
BOOT_ID = "6f1c2c7e-0a5e-4b8e-9d11-3c2f5a7b9e01"
#: A quarter of the declared ceiling: far below the limit, whatever the contract declares.
QUIET_PEAK_MIB = CAPACITY.ceiling_mib // 4
#: The largest whole MiB at or below UTILIZATION_CEILING of the declared ceiling.
LIMIT_MIB = math.floor(CAPACITY.ceiling_mib * UTILIZATION_CEILING)
#: A peak above the limit.
HIGH_PEAK_MIB = LIMIT_MIB + 1
#: The budget of one run, a process above it, and the declared size of the server, in bytes.
RUN_BYTES = CAPACITY.run_mib * MIB
BIG_PROCESS = RUN_BYTES + MIB
SERVER_BYTES = CAPACITY.server_mib * MIB
QUIET_SERVER = SERVER_BYTES - 10 * MIB
#: A synthetic capacity for the exact-boundary test: UTILIZATION_CEILING of 2000 MiB is exactly 1400 MiB.
SYNTHETIC = CAPACITY.model_copy(update={"ceiling_mib": 2000})
TICK_1 = "2026-10-21T06:30:00+00:00"
TICK_2 = "2026-10-22T06:30:00+00:00"
TICK_3 = "2026-10-23T06:30:00+00:00"
#: The moment of every check and sample in the scenario tests.
NOW = datetime(2026, 10, 23, 6, 29, tzinfo=UTC)
#: The reader as the deployed code has it. A test patches the module attribute, never this name.
REAL_READ = runtime_shape.read
#: The totals of a check that counted nothing.
NOTHING = Baseline(
    kills=0, restarts_same_deployment=0, restarts_new_deployment=0, peaks_over_limit=0, tick=TICK_1, red=(), summary=""
)


def _tree(
    root: Path,
    *,
    memory_max: str | None = str(CEILING),
    peak: int | None = QUIET_PEAK_MIB * MIB,
    oom_kill: int | None = 0,
    start_ticks: int | None = 12345,
    boot_id: str | None = BOOT_ID,
    command: str = "dagster api grpc",
    processes: dict[int, int] | None = None,
    server: int | None = QUIET_SERVER,
    ticks: dict[int, int] | None = None,
    server_command: bytes = b"/app/.venv/bin/python\x00/app/.venv/bin/dagster\x00api\x00grpc\x00",
) -> tuple[Path, Path]:
    """Write a cgroup tree and a process table. A None argument leaves the file out.

    `processes` maps a pid to its `VmHWM` in kB, `ticks` a pid to its start ticks (default: the pid). The
    code server (pid 1) holds `server` bytes."""
    cgroup, proc = root / "cgroup", root / "proc"
    (proc / "1").mkdir(parents=True)
    (proc / "sys" / "kernel" / "random").mkdir(parents=True)
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
    if boot_id is not None:
        (proc / "sys" / "kernel" / "random" / "boot_id").write_text(f"{boot_id}\n")
    (proc / "1" / "cmdline").write_bytes(server_command)
    hwm_line = "" if server is None else f"VmHWM:\t{server // 1024} kB\n"
    (proc / "1" / "status").write_text(f"Name:\tdagster\n{hwm_line}")
    for pid, kilobytes in (processes or {}).items():
        (proc / str(pid)).mkdir()
        (proc / str(pid) / "status").write_text(f"Name:\tpython\nVmPeak:\t{kilobytes * 2} kB\nVmHWM:\t{kilobytes} kB\n")
        after_name = ["S", *["0"] * 18, str((ticks or {}).get(pid, pid)), *["0"] * 30]
        (proc / str(pid) / "stat").write_text(f"{pid} (python) {' '.join(after_name)}\n")
    return cgroup, proc


def _reading(tmp_path: Path, **tree: Any) -> Reading:
    cgroup, proc = _tree(tmp_path, **tree)
    return runtime_shape.read(cgroup, proc)


def _container(
    tmp_path: Path,
    name: str,
    *,
    ticks: int,
    oom_kill: int = 0,
    peak_mib: int = QUIET_PEAK_MIB,
    process_bytes: int = 0,
    pid: int = 2,
    server: int = QUIET_SERVER,
) -> Reading:
    """The reading of the container that started at `ticks`, in its own tree. A process is `pid:pid`."""
    processes = {pid: process_bytes // 1024} if process_bytes else None
    return _reading(
        tmp_path / name,
        start_ticks=ticks,
        oom_kill=oom_kill,
        peak=peak_mib * MIB,
        processes=processes,
        server=server,
    )


def _fold(tracker: Tracker, reading: Reading, deployment: str = "A") -> Tracker:
    return runtime_shape.fold(tracker, reading, deployment=deployment, capacity=CAPACITY)


# --- reading the files -----------------------------------------------------------------


def test_the_reading_holds_the_facts_of_the_files(tmp_path: Path) -> None:
    reading = _reading(tmp_path, memory_max="1610612736", peak=123456789, oom_kill=7, start_ticks=424242)

    assert reading == Reading(
        memory_max=1610612736,
        unlimited=False,
        memory_peak=123456789,
        oom_kill=7,
        container=f"{BOOT_ID}:424242",
        largest_process=0,
        largest_process_id="",
        server_process=QUIET_SERVER,
    )
    assert reading.unreadable == ()


def test_the_container_is_named_by_the_boot_id_and_the_start_ticks_never_by_the_wall_clock(tmp_path: Path) -> None:
    """A step of the clock moves a derived epoch second. The ticks and the boot id do not move."""
    assert _reading(tmp_path / "a", start_ticks=1).container != _reading(tmp_path / "b", start_ticks=2).container
    assert _reading(tmp_path / "c", boot_id="other").container != _reading(tmp_path / "d").container
    assert not (tmp_path / "a" / "proc" / "stat").exists(), "the reader needs no /proc/stat"


def test_the_start_ticks_read_past_a_process_name_with_spaces_and_brackets(tmp_path: Path) -> None:
    assert _reading(tmp_path, command="a b) (c", start_ticks=99).container == f"{BOOT_ID}:99"


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
        ("start_ticks", "container identity"),
        ("boot_id", "container identity"),
    ],
)
def test_a_file_that_cannot_be_read_is_named_and_never_read_as_zero(tmp_path: Path, missing: str, name: str) -> None:
    assert _reading(tmp_path, **{missing: None}).unreadable == (name,)


def test_a_events_file_without_oom_kill_is_unreadable(tmp_path: Path) -> None:
    cgroup, proc = _tree(tmp_path)
    (cgroup / "memory.events").write_text("low 0\nhigh 0\nmax 0\noom 0\n")

    assert runtime_shape.read(cgroup, proc).unreadable == ("memory.events",)


def test_a_file_with_text_that_is_not_a_number_is_unreadable(tmp_path: Path) -> None:
    cgroup, proc = _tree(tmp_path)
    (cgroup / "memory.peak").write_text("lots\n")

    assert runtime_shape.read(cgroup, proc).unreadable == ("memory.peak",)


def test_an_empty_reading_misses_every_fact() -> None:
    assert Reading().unreadable == (
        "memory.max",
        "memory.peak",
        "memory.events",
        "container identity",
        "process sizes",
    )


def test_the_sensor_samples_often_enough_to_see_a_run_of_a_few_minutes_and_the_stale_limit_allows_slow_ticks() -> None:
    """A run of two minutes (head_reports) needs a sample inside its life; one slow tick must not look silent."""
    assert runtime_shape.SAMPLE_INTERVAL_SECONDS <= 60
    assert runtime_shape.STALE_AFTER_SECONDS >= 10 * runtime_shape.SAMPLE_INTERVAL_SECONDS
    assert runtime_shape.STALE_AFTER_SECONDS <= 3600, "an hour of silence is not a nightly check"


def test_the_deployed_paths_are_the_container_cgroup_and_process_table() -> None:
    assert runtime_shape.CGROUP_ROOT == Path("/sys/fs/cgroup")
    assert runtime_shape.PROC_ROOT == Path("/proc")


def test_the_largest_process_is_the_largest_vmhwm_of_any_process_but_the_code_server(tmp_path: Path) -> None:
    reading = _reading(
        tmp_path, processes={7: 300 * 1024, 8: 330 * 1024, 9: 120 * 1024}, server=9999 * MIB, ticks={8: 777}
    )

    assert reading.largest_process == 330 * MIB
    assert reading.largest_process_id == "8:777", "the process is named by pid and start ticks"
    assert reading.server_process == 9999 * MIB, "pid 1 is the server, never a run"


def test_no_process_but_the_code_server_is_a_largest_process_of_zero_not_a_missing_fact(tmp_path: Path) -> None:
    reading = _reading(tmp_path)

    assert reading.largest_process == 0
    assert reading.unreadable == ()


def test_a_process_that_exited_or_hides_its_status_is_skipped(tmp_path: Path) -> None:
    cgroup, proc = _tree(tmp_path, processes={7: 250 * 1024})
    (proc / "8").mkdir()  # exited between the listing and the read: no status file
    (proc / "cpuinfo").write_text("x")
    (proc / "9").mkdir()  # a kernel thread: a status file without VmHWM
    (proc / "9" / "status").write_text("Name:\tkthreadd\n")

    reading = runtime_shape.read(cgroup, proc)

    assert reading.largest_process == 250 * MIB
    assert reading.unreadable == (), "one process that cannot be read does not void the reading"


def test_a_process_name_that_is_not_utf8_does_not_hide_a_large_process(tmp_path: Path) -> None:
    cgroup, proc = _tree(tmp_path, processes={7: 250 * 1024})
    (proc / "10").mkdir()
    (proc / "10" / "status").write_bytes(b"Name:\t\xff\xfe\nVmHWM:\t" + str(976 * 1024).encode() + b" kB\n")
    (proc / "10" / "stat").write_bytes(b"10 (\xff\xfe) S " + b"0 " * 18 + b"4242 " + b"0 " * 30 + b"\n")

    reading = runtime_shape.read(cgroup, proc)

    assert (reading.largest_process, reading.largest_process_id) == (976 * MIB, "10:4242")


def test_a_first_process_that_is_not_the_code_server_makes_the_sizes_unreadable(tmp_path: Path) -> None:
    """With `init: true` pid 1 is docker-init. The server would be judged as a run and pid 1 as the server."""
    reading = _reading(tmp_path, server_command=b"/sbin/docker-init\x00--\x00dagster-entrypoint\x00"[:18])

    assert reading.server_process is None
    assert reading.unreadable == ("process sizes",)


def test_a_first_process_without_a_vmhwm_makes_the_sizes_unreadable(tmp_path: Path) -> None:
    assert _reading(tmp_path, server=None).unreadable == ("process sizes",)


def test_proc_self_is_not_a_process_even_when_it_carries_a_large_status(tmp_path: Path) -> None:
    """On a real /proc, `self` and `thread-self` point at the reader. They are not process ids."""
    cgroup, proc = _tree(tmp_path, processes={7: 100 * 1024})
    (proc / "self").mkdir()
    (proc / "self" / "status").write_text(f"VmHWM:\t{5000 * 1024} kB\n")
    (proc / "self" / "stat").write_text(f"1 (python) {' '.join(['S', *['0'] * 18, '5', *['0'] * 30])}\n")

    assert runtime_shape.read(cgroup, proc).largest_process == 100 * MIB


def test_a_process_table_that_cannot_be_listed_is_an_unreadable_fact(tmp_path: Path) -> None:
    cgroup, _ = _tree(tmp_path)

    assert runtime_shape.read(cgroup, tmp_path / "no-proc").unreadable == (
        "container identity",
        "process sizes",
    )


# --- folding readings into totals --------------------------------------------------------


def test_the_first_reading_counts_every_kill_of_the_container(tmp_path: Path) -> None:
    tracker = _fold(Tracker(), _container(tmp_path, "c1", ticks=1, oom_kill=3))

    assert (tracker.kills, tracker.restarts_same_deployment, tracker.restarts_new_deployment) == (3, 0, 0)
    assert tracker.container is not None
    assert tracker.container.oom_kill == 3


def test_the_same_container_adds_only_the_kills_since_the_last_reading(tmp_path: Path) -> None:
    tracker = _fold(Tracker(), _container(tmp_path, "a", ticks=1, oom_kill=1))
    tracker = _fold(tracker, _container(tmp_path, "b", ticks=1, oom_kill=1))
    assert tracker.kills == 1, "a kill that was counted is not counted again"

    tracker = _fold(tracker, _container(tmp_path, "c", ticks=1, oom_kill=4))
    assert tracker.kills == 4


def test_a_new_container_adds_all_of_its_kills_and_counts_the_restart_by_the_deployment(tmp_path: Path) -> None:
    tracker = _fold(Tracker(), _container(tmp_path, "c1", ticks=1, oom_kill=2))

    same = _fold(tracker, _container(tmp_path, "c2", ticks=2, oom_kill=1), deployment="A")
    new = _fold(tracker, _container(tmp_path, "c3", ticks=2, oom_kill=1), deployment="B")

    assert (same.kills, same.restarts_same_deployment, same.restarts_new_deployment) == (3, 1, 0)
    assert (new.kills, new.restarts_same_deployment, new.restarts_new_deployment) == (3, 0, 1)


def test_a_peak_above_the_limit_counts_once_while_it_stays_the_same(tmp_path: Path) -> None:
    """`memory.peak` is a high-water mark of the container life: the same value is the same peak."""
    tracker = _fold(Tracker(), _container(tmp_path, "a", ticks=1, peak_mib=HIGH_PEAK_MIB))
    tracker = _fold(tracker, _container(tmp_path, "b", ticks=1, peak_mib=HIGH_PEAK_MIB))
    assert tracker.peaks_over_limit == 1

    tracker = _fold(tracker, _container(tmp_path, "c", ticks=1, peak_mib=HIGH_PEAK_MIB + 5))
    assert tracker.peaks_over_limit == 2, "a higher peak is a new peak"


def test_a_peak_at_the_limit_does_not_count(tmp_path: Path) -> None:
    assert _fold(Tracker(), _container(tmp_path, "a", ticks=1, peak_mib=LIMIT_MIB)).peaks_over_limit == 0


def test_a_new_container_with_a_high_peak_counts_even_when_the_old_peak_was_higher(tmp_path: Path) -> None:
    tracker = _fold(Tracker(), _container(tmp_path, "c1", ticks=1, peak_mib=CAPACITY.ceiling_mib))

    tracker = _fold(tracker, _container(tmp_path, "c2", ticks=2, peak_mib=HIGH_PEAK_MIB))

    assert tracker.peaks_over_limit == 2


def test_a_reading_with_a_missing_fact_adds_nothing(tmp_path: Path) -> None:
    before = _fold(Tracker(), _container(tmp_path, "a", ticks=1, oom_kill=1))

    assert _fold(before, _reading(tmp_path / "b", peak=None)) == before
    assert _fold(Tracker(), Reading()) == Tracker()


def test_one_process_that_grows_past_the_budget_counts_once(tmp_path: Path) -> None:
    """A process of the same pid and start ticks, sampled three times while it grows."""
    first = _fold(Tracker(), _container(tmp_path, "a", ticks=1, process_bytes=BIG_PROCESS))
    second = _fold(first, _container(tmp_path, "b", ticks=1, process_bytes=BIG_PROCESS + MIB))
    third = _fold(second, _container(tmp_path, "c", ticks=1, process_bytes=BIG_PROCESS + 2 * MIB))

    assert (first.processes_over_size, second.processes_over_size, third.processes_over_size) == (1, 1, 1)


def test_a_process_that_keeps_growing_over_the_budget_raises_the_record_without_a_new_count(tmp_path: Path) -> None:
    first = _fold(Tracker(), _container(tmp_path, "a", ticks=1, process_bytes=BIG_PROCESS))
    grown = _fold(first, _container(tmp_path, "b", ticks=1, process_bytes=BIG_PROCESS + 30 * MIB))

    assert (first.worst_process, grown.worst_process) == (BIG_PROCESS, BIG_PROCESS + 30 * MIB)
    assert grown.processes_over_size == 1


def test_a_process_that_crosses_the_budget_while_it_runs_counts_when_it_crosses(tmp_path: Path) -> None:
    below = _fold(Tracker(), _container(tmp_path, "a", ticks=1, process_bytes=RUN_BYTES))
    crossed = _fold(below, _container(tmp_path, "b", ticks=1, process_bytes=BIG_PROCESS))

    assert (below.processes_over_size, crossed.processes_over_size) == (0, 1)


def test_a_new_process_over_the_budget_counts_even_when_an_earlier_one_was_larger(tmp_path: Path) -> None:
    """A recurring over-size run must not hide behind one record: a smaller process of another pid counts."""
    big = _fold(Tracker(), _container(tmp_path, "a", ticks=1, process_bytes=BIG_PROCESS + 100 * MIB, pid=2))
    other = _fold(big, _container(tmp_path, "b", ticks=1, process_bytes=BIG_PROCESS, pid=3))

    assert other.processes_over_size == 2
    assert other.largest_process == BIG_PROCESS + 100 * MIB, "the record is the largest of the container so far"


def test_the_same_pid_with_other_start_ticks_is_another_process(tmp_path: Path) -> None:
    """A pid is reused. The start ticks tell the new process from the old one."""
    first = _fold(Tracker(), _container(tmp_path, "a", ticks=1, process_bytes=BIG_PROCESS))
    reading = _container(tmp_path, "b", ticks=1, process_bytes=BIG_PROCESS)
    reading = dataclasses.replace(reading, largest_process_id="2:99999")

    assert _fold(first, reading).processes_over_size == 2


def test_an_over_size_process_counts_again_in_a_new_container_even_with_the_same_pid_and_ticks(tmp_path: Path) -> None:
    """`_container` names the process `2:2` in every container, as a restart that reuses pid and ticks would.

    It runs through `sample`, the path of production: the event of the first container is kept, and the
    process of the new container must still count and get an event of its own."""
    instance = dg.DagsterInstance.ephemeral()
    first = _sample(instance, _container(tmp_path, "a", ticks=1, process_bytes=BIG_PROCESS))
    second = _sample(instance, _container(tmp_path, "b", ticks=2, process_bytes=BIG_PROCESS + 7 * MIB), deployment="B")

    assert (first.processes_over_size, second.processes_over_size) == (1, 2)
    assert [event.size for event in second.over_events] == [BIG_PROCESS, BIG_PROCESS + 7 * MIB]

    grown = _sample(instance, _container(tmp_path, "c", ticks=2, process_bytes=BIG_PROCESS + 20 * MIB), deployment="B")

    assert [event.size for event in grown.over_events] == [BIG_PROCESS, BIG_PROCESS + 20 * MIB], "the newest copy grows"


def test_a_process_that_grows_past_the_budget_in_a_new_container_counts_even_when_an_old_event_has_its_id(
    tmp_path: Path,
) -> None:
    """The common shape of an id collision: the process starts small and grows in the later samples.

    Container A holds the event `2:2`. Container B has its own process `2:2`, first under the budget, then
    over it. It must count and get an event of its own; the event of A must not change."""
    instance = dg.DagsterInstance.ephemeral()
    first = _sample(instance, _container(tmp_path, "a", ticks=1, process_bytes=BIG_PROCESS, pid=2))
    small = _sample(instance, _container(tmp_path, "b", ticks=2, process_bytes=RUN_BYTES - MIB, pid=2), deployment="B")
    assert small.processes_over_size == first.processes_over_size == 1, "under the budget: no count yet"

    grown = _sample(
        instance, _container(tmp_path, "c", ticks=2, process_bytes=BIG_PROCESS + 20 * MIB, pid=2), deployment="B"
    )

    assert grown.processes_over_size == 2
    assert [(event.container, event.size) for event in grown.over_events] == [
        (first.container.id, BIG_PROCESS),  # type: ignore[union-attr]
        (grown.container.id, BIG_PROCESS + 20 * MIB),  # type: ignore[union-attr]
    ]


def test_the_event_of_an_old_container_is_not_updated_or_renamed_by_a_process_of_a_new_one(tmp_path: Path) -> None:
    """Container A has the event `2:2` with an unknown job. In container B a process `2:2` runs under the
    budget, for the job `head_reports_pipeline`. The event of A must stay as it was."""
    instance = dg.DagsterInstance.ephemeral()
    first = _sample(instance, _container(tmp_path, "a", ticks=1, process_bytes=BIG_PROCESS, pid=2))
    assert [event.job for event in first.over_events] == [runtime_shape.UNKNOWN_JOB]
    _run(instance, "head_reports_pipeline", 2)

    after = _sample(instance, _container(tmp_path, "b", ticks=2, process_bytes=RUN_BYTES - MIB, pid=2), deployment="B")

    assert after.over_events == first.over_events


def test_an_event_without_a_container_in_old_state_loads_and_never_matches_a_new_process() -> None:
    state = Tracker.from_json(
        _tracker_json(over_events=[{"process_id": "2:2", "job": "topt_live_pipeline", "size": 412 * MIB}])
    )

    assert state.over_events == (runtime_shape.OverEvent("2:2", "topt_live_pipeline", 412 * MIB, container=""),)
    assert Tracker.from_json(state.to_json()) == state


def test_a_process_whose_event_was_dropped_by_newer_ones_counts_again(tmp_path: Path) -> None:
    """The documented cap: an event is kept while it is among the newest `MAX_OVER_EVENTS`."""
    instance = dg.DagsterInstance.ephemeral()
    _sample(instance, _container(tmp_path, "first", ticks=1, process_bytes=BIG_PROCESS, pid=50))
    for pid in range(60, 60 + runtime_shape.MAX_OVER_EVENTS):
        _sample(instance, _container(tmp_path, f"p{pid}", ticks=1, process_bytes=BIG_PROCESS, pid=pid))

    again = _sample(instance, _container(tmp_path, "again", ticks=1, process_bytes=BIG_PROCESS + 30 * MIB, pid=50))

    assert again.processes_over_size == 1 + runtime_shape.MAX_OVER_EVENTS + 1
    assert len(again.over_events) == runtime_shape.MAX_OVER_EVENTS
    assert again.over_events[-1].process_id == "50:50"


def test_the_largest_process_record_starts_again_in_a_new_container(tmp_path: Path) -> None:
    old = _fold(Tracker(), _container(tmp_path, "a", ticks=1, process_bytes=BIG_PROCESS + 50 * MIB))

    new = _fold(old, _container(tmp_path, "b", ticks=2, process_bytes=RUN_BYTES - MIB), deployment="B")

    assert new.largest_process == RUN_BYTES - MIB
    assert new.processes_over_size == 1


def test_a_process_at_the_budget_does_not_count(tmp_path: Path) -> None:
    tracker = _fold(Tracker(), _container(tmp_path, "a", ticks=1, process_bytes=RUN_BYTES))

    assert (tracker.processes_over_size, tracker.largest_process) == (0, RUN_BYTES)


def test_the_code_server_over_its_declared_size_counts_once_per_crossing(tmp_path: Path) -> None:
    quiet = _fold(Tracker(), _container(tmp_path, "a", ticks=1))
    over = _fold(quiet, _container(tmp_path, "b", ticks=1, server=SERVER_BYTES + MIB))
    still = _fold(over, _container(tmp_path, "c", ticks=1, server=SERVER_BYTES + 2 * MIB))
    replaced = _fold(still, _container(tmp_path, "d", ticks=2, server=SERVER_BYTES + MIB), deployment="B")

    assert [quiet.servers_over_size, over.servers_over_size, still.servers_over_size] == [0, 1, 1]
    assert replaced.servers_over_size == 2, "a new container that starts over the size counts"


def test_a_tracker_with_sizes_survives_a_round_trip(tmp_path: Path) -> None:
    tracker = _fold(Tracker(), _container(tmp_path, "a", ticks=1, process_bytes=BIG_PROCESS, server=SERVER_BYTES + MIB))

    again = Tracker.from_json(tracker.to_json())

    assert again == tracker
    assert (again.process_id, again.process_size, again.server_size) == (
        "2:2",
        BIG_PROCESS,
        SERVER_BYTES + MIB,
    )
    assert (again.processes_over_size, again.servers_over_size, again.largest_process) == (1, 1, BIG_PROCESS)
    assert (again.worst_process, again.worst_server) == (BIG_PROCESS, SERVER_BYTES + MIB)


# --- the judgement of one reading ------------------------------------------------------------


def _judge(
    reading: Reading,
    *,
    tracker: Tracker | None = None,
    baseline: Baseline | None = None,
    capacity: CodeServerCapacity = CAPACITY,
    problems: tuple[str, ...] = (),
) -> runtime_shape.Judgement:
    folded = (
        tracker if tracker is not None else runtime_shape.fold(Tracker(), reading, deployment="A", capacity=capacity)
    )
    return runtime_shape.judge(reading, tracker=folded, baseline=baseline, capacity=capacity, problems=problems)


def test_a_quiet_reading_is_green(tmp_path: Path) -> None:
    verdict = _judge(_reading(tmp_path))

    assert verdict.red == ()
    assert verdict.summary == (
        f"no OOM kill and no unplanned restart since the last check; peak {QUIET_PEAK_MIB} of {CAPACITY.ceiling_mib} MiB"
        f" ({QUIET_PEAK_MIB / CAPACITY.ceiling_mib:.0%})"
    )


def test_an_unreadable_fact_is_red_even_when_every_other_fact_is_good(tmp_path: Path) -> None:
    assert _judge(_reading(tmp_path, peak=None)).red == ("unreadable: memory.peak",)


def test_an_empty_reading_is_red_for_every_fact() -> None:
    assert _judge(Reading()).red == (
        "unreadable: memory.max, memory.peak, memory.events, container identity, process sizes",
    )


@pytest.mark.parametrize(
    ("peak_mib", "red"),
    [(LIMIT_MIB, False), (HIGH_PEAK_MIB, True), (CAPACITY.ceiling_mib, True), (0, False)],
    ids=["at-the-limit", "one-mib-above", "at-the-ceiling", "nothing"],
)
def test_a_new_peak_above_the_utilization_limit_of_the_ceiling_is_red(tmp_path: Path, peak_mib: int, red: bool) -> None:
    verdict = _judge(_container(tmp_path, "a", ticks=1, peak_mib=peak_mib))

    assert bool(verdict.red) is red
    if red:
        assert verdict.red[0].startswith(f"the peak rose above {UTILIZATION_CEILING:.0%} of memory.max 1 time(s)")
        assert f"peak {peak_mib} of {CAPACITY.ceiling_mib} MiB" in verdict.red[0]


def test_a_peak_exactly_at_the_limit_of_the_real_ceiling_is_green_and_one_byte_above_is_red(tmp_path: Path) -> None:
    """The rule is `above` the limit. UTILIZATION_CEILING of 2000 MiB is exactly 1400 MiB (a synthetic capacity)."""
    at_the_limit = _judge(_reading(tmp_path / "a", memory_max=str(2000 * MIB), peak=1400 * MIB), capacity=SYNTHETIC)
    one_byte_above = _judge(
        _reading(tmp_path / "b", memory_max=str(2000 * MIB), peak=1400 * MIB + 1), capacity=SYNTHETIC
    )

    assert at_the_limit.red == ()
    assert len(one_byte_above.red) == 1
    assert f"rose above {UTILIZATION_CEILING:.0%}" in one_byte_above.red[0]


def test_a_process_larger_than_the_declared_run_size_is_red_and_does_not_invent_a_size_or_a_job(tmp_path: Path) -> None:
    verdict = _judge(_container(tmp_path, "a", ticks=1, process_bytes=BIG_PROCESS))

    assert verdict.red == (
        f"1 run process(es) passed the run budget {CAPACITY.run_mib} MiB (size and job not recorded)",
    )
    assert verdict.facts["processes_over_size"] == 1
    assert verdict.facts["largest_process_mib"] == CAPACITY.run_mib + 1


def test_the_facts_keep_the_record_of_the_largest_size_across_containers(tmp_path: Path) -> None:
    """A run of 480 MiB in one container, a deploy, then a small process in the next: the record keeps 480.

    The red text names a size only from an event, never from this record (see the tests of the events)."""
    big = BIG_PROCESS + 60 * MIB
    tracker = _fold(Tracker(), _container(tmp_path, "a", ticks=1, process_bytes=big, server=SERVER_BYTES + 7 * MIB))
    reading = _container(tmp_path, "b", ticks=2, process_bytes=RUN_BYTES // 2)
    tracker = _fold(tracker, reading, deployment="B")

    verdict = _judge(reading, tracker=tracker, baseline=NOTHING)

    assert any(f"the code server held {SERVER_BYTES // MIB + 7} MiB" in reason for reason in verdict.red)
    assert (tracker.worst_process, tracker.worst_server) == (big, SERVER_BYTES + 7 * MIB)
    assert verdict.facts["worst_process_mib"] == big // MIB
    run_process_reasons = [reason for reason in verdict.red if "run process" in reason]
    assert len(run_process_reasons) == 1, "the finding exists, so the negative check below has something to check"
    assert f"{big // MIB} MiB" not in run_process_reasons[0]


def test_a_code_server_larger_than_its_declared_size_is_red(tmp_path: Path) -> None:
    verdict = _judge(_container(tmp_path, "a", ticks=1, server=SERVER_BYTES + 3 * MIB))

    assert verdict.red == (
        f"the code server held {CAPACITY.server_mib + 3} MiB, above the declared {CAPACITY.server_mib} MiB (1 time(s))",
    )


def test_the_peak_is_judged_against_the_real_ceiling_not_the_declared_one(tmp_path: Path) -> None:
    """A peak above the limit of the declared ceiling is under the limit of a real ceiling twice as large."""
    peak = HIGH_PEAK_MIB * MIB
    assert _judge(_reading(tmp_path / "a", memory_max=str(2 * CEILING), peak=peak)).red == ()
    assert _judge(_reading(tmp_path / "b", memory_max=str(CEILING), peak=peak)).red != ()


def test_a_real_ceiling_below_the_declared_one_is_red_for_that_reason(tmp_path: Path) -> None:
    """Staging held half of the declared ceiling while the run limit assumed the whole."""
    half = CAPACITY.ceiling_mib // 2
    verdict = _judge(_reading(tmp_path, memory_max=str(half * MIB), peak=10 * MIB))

    assert verdict.red == (f"memory.max {half} MiB is below the declared ceiling {CAPACITY.ceiling_mib} MiB",)


def test_no_ceiling_at_all_is_red(tmp_path: Path) -> None:
    assert _judge(_reading(tmp_path, memory_max="max")).red == (
        "memory.max is max: the container has no memory ceiling",
    )


def test_the_growth_of_the_totals_since_the_baseline_is_judged_not_the_totals(tmp_path: Path) -> None:
    reading = _container(tmp_path, "a", ticks=1, oom_kill=3)
    tracker = _fold(Tracker(), reading)
    counted = dataclasses.replace(NOTHING, kills=3)
    older = dataclasses.replace(NOTHING, kills=1)

    assert _judge(reading, tracker=tracker, baseline=counted).red == ()
    assert _judge(reading, tracker=tracker, baseline=None).red == ("3 OOM kill(s) since the last check",)
    assert _judge(reading, tracker=tracker, baseline=older).red == ("2 OOM kill(s) since the last check",)


def test_a_restart_by_a_new_deployment_is_green_and_named(tmp_path: Path) -> None:
    tracker = _fold(Tracker(), _container(tmp_path, "c1", ticks=1))
    reading = _container(tmp_path, "c2", ticks=2)
    tracker = _fold(tracker, reading, deployment="B")

    verdict = _judge(reading, tracker=tracker, baseline=NOTHING)

    assert verdict.red == ()
    assert verdict.summary.endswith("; replaced 1 time(s) by a new deployment")


def test_a_restart_without_a_new_deployment_is_red_because_the_kill_of_the_server_leaves_no_counter(
    tmp_path: Path,
) -> None:
    tracker = _fold(Tracker(), _container(tmp_path, "c1", ticks=1))
    reading = _container(tmp_path, "c2", ticks=2)
    tracker = _fold(tracker, reading, deployment="A")

    verdict = _judge(reading, tracker=tracker, baseline=None)

    assert verdict.red == (
        "the code server was replaced 1 time(s) without a deployment change (a crash, a host reboot or a restart by hand)",
    )


def test_totals_below_the_baseline_mean_the_state_was_reset_and_are_red(tmp_path: Path) -> None:
    reading = _container(tmp_path, "a", ticks=1)

    verdict = _judge(reading, baseline=dataclasses.replace(NOTHING, kills=5))

    assert verdict.red == (
        "the runtime shape state is lower than at the last check: it was reset, so kills may be unseen",
    )


def test_every_reason_is_reported_not_only_the_first(tmp_path: Path) -> None:
    reading = _container(tmp_path, "a", ticks=1, oom_kill=2, peak_mib=HIGH_PEAK_MIB)
    reading = dataclasses.replace(reading, memory_max=CEILING // 2)

    verdict = _judge(reading, problems=("a problem of the caller",))

    assert len(verdict.red) == 4
    assert verdict.summary == "; ".join(verdict.red)


def test_the_summary_holds_counts_sizes_and_job_names_only(tmp_path: Path) -> None:
    """The summary goes to /health: counts, sizes and job names (code identifiers). No path, no error text,
    no host, no deployment, no process id."""
    tracker = _tracker_with_events(runtime_shape.OverEvent("4242:99", "topt_live_pipeline", 412 * MIB))
    reading = _reading(tmp_path, peak=None, oom_kill=2)

    summary = _judge(reading, tracker=tracker, baseline=NOTHING).summary

    assert "(job topt_live_pipeline)" in summary
    assert str(tmp_path) not in summary
    assert "/sys/fs/cgroup" not in summary
    assert BOOT_ID not in summary
    assert "4242" not in summary


# --- the nights: sample and check on one instance -------------------------------------------


def _sample(instance: dg.DagsterInstance, reading: Reading, deployment: str = "A", at: datetime = NOW) -> Tracker:
    return runtime_shape.sample(instance, deployment=deployment, capacity=CAPACITY, reading=reading, now=at)


def _check(instance: dg.DagsterInstance, reading: Reading, tick: str | None, at: datetime = NOW):
    return runtime_shape.check(instance, tick=tick, capacity=CAPACITY, reading=reading, now=at)


def _night(instance: dg.DagsterInstance, reading: Reading, tick: str | None, deployment: str = "A"):
    """The sensor runs, then the schedule: the order of a normal night."""
    _sample(instance, reading, deployment)
    return _check(instance, reading, tick)


def _baseline(instance: dg.DagsterInstance) -> Baseline:
    baseline = runtime_shape.load_baseline(instance)
    assert baseline is not None
    return baseline


def test_a_kill_before_a_deploy_is_seen_because_the_sensor_sampled_the_old_container(tmp_path: Path) -> None:
    """The kernel kills two processes at night, a deploy replaces the container at 03:00, and the check at
    06:30 reads a new cgroup that shows zero kills and a small peak."""
    instance = dg.DagsterInstance.ephemeral()
    assert _night(instance, _container(tmp_path, "n1", ticks=1), TICK_1).red == ()

    _sample(instance, _container(tmp_path, "killed", ticks=1, oom_kill=2, peak_mib=CAPACITY.ceiling_mib))
    after_deploy = _container(tmp_path, "deployed", ticks=9)
    verdict = _night(instance, after_deploy, TICK_2, deployment="B")

    assert any(reason.startswith("2 OOM kill(s)") for reason in verdict.red)
    assert any("rose above" in reason for reason in verdict.red)
    assert _night(instance, after_deploy, TICK_3, deployment="B").red == (), "the next night has nothing new"


def test_a_check_without_a_recent_sample_is_red_not_green(tmp_path: Path) -> None:
    """Why the check depends on the sensor: a kill in a container that a deploy erased is unseen without it."""
    instance = dg.DagsterInstance.ephemeral()
    reading = _container(tmp_path, "n1", ticks=1)

    never = _check(instance, reading, TICK_1)
    _sample(instance, reading, at=NOW - timedelta(seconds=runtime_shape.STALE_AFTER_SECONDS + 60))
    old = _check(instance, reading, TICK_2)
    _sample(instance, reading, at=NOW - timedelta(seconds=runtime_shape.STALE_AFTER_SECONDS - 60))
    recent = _check(instance, reading, TICK_3)

    assert never.red == ("the sampler has recorded nothing, so kills and restarts may be unseen",)
    minutes = round((runtime_shape.STALE_AFTER_SECONDS + 60) / 60)
    assert old.red == (f"the last sample is {minutes} minutes old, so kills and restarts may be unseen",)
    assert recent.red == ()


def test_a_peak_burst_is_red_once_and_the_quiet_nights_after_it_are_green(tmp_path: Path) -> None:
    """`memory.peak` stays high for the container life. The nights after the burst judge only a new peak."""
    instance = dg.DagsterInstance.ephemeral()
    burst = _container(tmp_path, "burst", ticks=1, peak_mib=HIGH_PEAK_MIB)

    first = _night(instance, burst, TICK_1)
    second = _night(instance, burst, TICK_2)
    third = _night(instance, burst, TICK_3)

    assert first.red != ()
    assert (second.red, third.red) == ((), ())


def test_a_restart_without_a_deployment_change_between_two_samples_is_red(tmp_path: Path) -> None:
    instance = dg.DagsterInstance.ephemeral()
    _night(instance, _container(tmp_path, "a", ticks=1), TICK_1)

    verdict = _night(instance, _container(tmp_path, "c", ticks=7), TICK_2)

    assert verdict.red == (
        "the code server was replaced 1 time(s) without a deployment change"
        " (a crash, a host reboot or a restart by hand)",
    )


def test_a_crash_after_a_deploy_is_an_unplanned_restart_not_a_second_deployment(tmp_path: Path) -> None:
    """Two replacements in a row, each sampled: a deploy (A to B) and then a crash of B."""
    instance = dg.DagsterInstance.ephemeral()
    _night(instance, _container(tmp_path, "a", ticks=1), TICK_1, deployment="A")

    _sample(instance, _container(tmp_path, "b", ticks=2), deployment="B")
    verdict = _night(instance, _container(tmp_path, "c", ticks=3), TICK_2, deployment="B")

    assert [reason for reason in verdict.red if "without a deployment change" in reason]


def test_an_over_size_process_is_red_on_its_night_only_and_a_new_one_is_red_again(tmp_path: Path) -> None:
    """Three nights: a process over the budget, the same process still alive, then another process over it."""
    instance = dg.DagsterInstance.ephemeral()
    big = _container(tmp_path, "a", ticks=1, process_bytes=BIG_PROCESS, pid=2)
    other = _container(tmp_path, "b", ticks=1, process_bytes=BIG_PROCESS, pid=3)

    first = _night(instance, big, TICK_1)
    second = _night(instance, big, TICK_2)
    third = _night(instance, other, TICK_3)

    assert [reason for reason in first.red if reason.startswith("a run process held")]
    assert second.red == ()
    assert [reason for reason in third.red if reason.startswith("a run process held")]
    assert _baseline(instance).processes_over_size == 2, "the baseline keeps the count, so a later night is not red"


def test_a_code_server_over_its_size_is_red_on_its_night_only(tmp_path: Path) -> None:
    instance = dg.DagsterInstance.ephemeral()
    big_server = _container(tmp_path, "a", ticks=1, server=SERVER_BYTES + MIB)

    first = _night(instance, big_server, TICK_1)
    second = _night(instance, big_server, TICK_2)

    assert [reason for reason in first.red if reason.startswith("the code server held")]
    assert second.red == ()


def test_a_repeat_of_a_tick_repeats_its_verdict_instead_of_clearing_it(tmp_path: Path) -> None:
    """A re-execution copies the tags of its parent, tick included. It must not turn a red night green."""
    instance = dg.DagsterInstance.ephemeral()
    killed = _container(tmp_path, "a", ticks=1, oom_kill=2)

    first = _night(instance, killed, TICK_1)
    again = _check(instance, killed, TICK_1)

    assert first.red != ()
    assert (again.red, again.summary) == (first.red, first.summary)
    assert _night(instance, killed, TICK_2).red == (), "the next night counts from the first run, once"


def test_a_re_execution_of_an_older_tick_judges_and_saves_nothing(tmp_path: Path) -> None:
    """Night 1 is red, night 2 is green. An operator re-executes night 1: the baseline must stay at night 2."""
    instance = dg.DagsterInstance.ephemeral()
    killed = _container(tmp_path, "a", ticks=1, oom_kill=2)
    _night(instance, killed, TICK_1)
    _night(instance, killed, TICK_2)

    _check(instance, killed, TICK_1)

    assert _baseline(instance).tick == TICK_2


def test_a_manual_run_shows_the_growth_and_consumes_nothing(tmp_path: Path) -> None:
    instance = dg.DagsterInstance.ephemeral()
    _night(instance, _container(tmp_path, "a", ticks=1), TICK_1)
    killed = _container(tmp_path, "b", ticks=1, oom_kill=2)
    _sample(instance, killed)

    manual = _check(instance, killed, None)
    assert _baseline(instance).tick == TICK_1, "a manual run does not move the baseline"
    scheduled = _check(instance, killed, TICK_2)

    assert manual.red == scheduled.red != ()
    assert _baseline(instance).tick == TICK_2


def test_the_baseline_holds_exactly_the_totals_the_sensor_stored(tmp_path: Path) -> None:
    """The check does not fold its own reading, so a count in the baseline is always a count in the tracker."""
    instance = dg.DagsterInstance.ephemeral()
    _sample(instance, _container(tmp_path, "a", ticks=1, oom_kill=1))

    _check(instance, _container(tmp_path, "b", ticks=1, oom_kill=5), TICK_1)

    assert _baseline(instance).kills == 1
    tracker = runtime_shape.load_tracker(instance)
    assert tracker is not None
    assert tracker.kills == 1


def test_an_unreadable_baseline_is_red_not_silently_a_first_check(tmp_path: Path) -> None:
    instance = dg.DagsterInstance.ephemeral()
    instance.run_storage.set_cursor_values({runtime_shape.BASELINE_KEY: "{broken"})

    verdict = _night(instance, _container(tmp_path, "a", ticks=1), TICK_1)

    assert "the runtime shape baseline is not readable" in verdict.red
    assert _baseline(instance).tick == TICK_1, "the check replaces the broken value, so the next night is judged"


def test_an_unreadable_tracker_is_red_at_the_check_and_restarts_at_the_sample(tmp_path: Path) -> None:
    instance = dg.DagsterInstance.ephemeral()
    instance.run_storage.set_cursor_values({runtime_shape.TRACKER_KEY: "[]"})

    verdict = _check(instance, _container(tmp_path, "a", ticks=1), None)
    sampled = _sample(instance, _container(tmp_path, "b", ticks=1))

    assert "the runtime shape tracker is not readable" in verdict.red
    assert sampled.container is not None
    assert runtime_shape.load_tracker(instance) == sampled


def test_a_tracker_that_lost_the_kills_of_a_replaced_container_turns_the_next_check_red(tmp_path: Path) -> None:
    """The totals hold kills of a container that is gone. A reset tracker cannot rebuild them from the new one."""
    instance = dg.DagsterInstance.ephemeral()
    _sample(instance, _container(tmp_path, "old", ticks=1, oom_kill=3))
    replaced = _container(tmp_path, "new", ticks=2)
    assert _night(instance, replaced, TICK_1, deployment="B").red != ()
    instance.run_storage.set_cursor_values({runtime_shape.TRACKER_KEY: Tracker().to_json()})

    verdict = _night(instance, replaced, TICK_2, deployment="B")

    assert (
        "the runtime shape state is lower than at the last check: it was reset, so kills may be unseen" in verdict.red
    )


@pytest.mark.parametrize(
    "field",
    [
        "kills",
        "restarts_same_deployment",
        "restarts_new_deployment",
        "peaks_over_limit",
        "processes_over_size",
        "servers_over_size",
    ],
)
def test_each_total_that_falls_below_the_baseline_is_a_reset(tmp_path: Path, field: str) -> None:
    reading = _container(tmp_path, "a", ticks=1)

    verdict = _judge(reading, baseline=dataclasses.replace(NOTHING, **{field: 1}))

    assert verdict.red == (
        "the runtime shape state is lower than at the last check: it was reset, so kills may be unseen",
    )


def test_the_sampler_saves_what_it_folds_and_the_time(tmp_path: Path) -> None:
    instance = dg.DagsterInstance.ephemeral()

    sampled = _sample(instance, _container(tmp_path, "a", ticks=1, oom_kill=1))

    assert runtime_shape.load_tracker(instance) == sampled
    assert (sampled.kills, sampled.sampled_at) == (1, NOW.isoformat())


def test_a_reading_with_one_missing_fact_adds_nothing_but_stamps_the_time(tmp_path: Path) -> None:
    instance = dg.DagsterInstance.ephemeral()
    before = _sample(instance, _container(tmp_path, "a", ticks=1, oom_kill=1))

    after = _sample(instance, _reading(tmp_path / "b", memory_max=None), at=NOW + timedelta(minutes=5))

    assert (after.kills, after.container) == (before.kills, before.container)
    assert after.sampled_at == (NOW + timedelta(minutes=5)).isoformat()


def _tracker_json(**changes: Any) -> str:
    import json

    state: dict[str, Any] = {
        "container": {"id": "x", "deployment": "A", "oom_kill": 0, "peak": 0},
        "kills": 0,
        "restarts_same_deployment": 0,
        "restarts_new_deployment": 0,
        "peaks_over_limit": 0,
        "sampled_at": NOW.isoformat(),
    }
    return json.dumps({**state, **changes})


def test_state_saved_before_the_process_sizes_existed_still_loads_with_zero_sizes() -> None:
    """The sensor of the first release stored no process size. Its tracker and baseline must not read as broken."""
    import json

    tracker = Tracker.from_json(_tracker_json())
    baseline = Baseline.from_json(
        json.dumps(
            {
                "kills": 0,
                "restarts_same_deployment": 0,
                "restarts_new_deployment": 0,
                "peaks_over_limit": 1,
                "tick": TICK_1,
                "red": ["x"],
                "summary": "x",
            }
        )
    )

    assert (tracker.largest_process, tracker.processes_over_size) == (0, 0)
    assert baseline.processes_over_size == 0


@pytest.mark.parametrize(
    "stored",
    [
        "",
        "not json",
        "[1, 2]",
        '{"kills": "x"}',
        "{}",
        _tracker_json(kills=-1),
        _tracker_json(kills=True),
        _tracker_json(kills=1.5),
        _tracker_json(kills="3"),
        _tracker_json(sampled_at=5),
        _tracker_json(container={"id": "x", "deployment": "A", "oom_kill": 0, "peak": -1}),
        '{"kills": Infinity}',
    ],
)
def test_a_stored_value_that_is_not_a_tracker_raises_state_unreadable(stored: str) -> None:
    with pytest.raises(runtime_shape.StateUnreadable):
        Tracker.from_json(stored)


@pytest.mark.parametrize(
    "changes",
    [{"kills": -1}, {"kills": True}, {"kills": 1.5}, {"red": "abc"}, {"red": [1]}, {"summary": None}, {"tick": 3}],
)
def test_a_stored_value_that_is_not_a_baseline_raises_state_unreadable(changes: dict[str, Any]) -> None:
    import json

    good = {
        "kills": 0,
        "restarts_same_deployment": 0,
        "restarts_new_deployment": 0,
        "peaks_over_limit": 0,
        "tick": TICK_1,
        "red": [],
        "summary": "ok",
    }
    assert Baseline.from_json(json.dumps(good)).red == ()
    with pytest.raises(runtime_shape.StateUnreadable):
        Baseline.from_json(json.dumps({**good, **changes}))


@pytest.mark.parametrize("stored", ["", "not json", "[1, 2]", "{}", '{"kills": Infinity}'])
def test_a_stored_value_that_is_not_a_baseline_at_all_raises_state_unreadable(stored: str) -> None:
    with pytest.raises(runtime_shape.StateUnreadable):
        Baseline.from_json(stored)


# --- the job that holds the largest process (#1467) --------------------------------------------


@dg.op
def _probe_op() -> None:
    return None


def _probe_job(name: str) -> dg.JobDefinition:
    @dg.job(name=name)
    def probe() -> None:
        _probe_op()

    return probe


def _run(instance: dg.DagsterInstance, job_name: str, pid: int, *, started: bool = True) -> str:
    """A run of `job_name` whose process has the id `pid`: the engine event the in-process executor writes.

    With `started`, the run is STARTED; otherwise it is finished. The message is the one of Dagster 1.13;
    `test_the_job_of_the_process_of_a_real_in_process_run_is_found` runs a real job to prove it."""
    from dagster import DagsterEventType
    from dagster._core.events import DagsterEvent

    run = instance.create_run_for_job(job_def=_probe_job(job_name))
    instance.report_engine_event(f"Executing steps in process (pid: {pid})", dagster_run=run)
    start = DagsterEvent(event_type_value=DagsterEventType.PIPELINE_START.value, job_name=job_name, message="start")
    instance.report_dagster_event(start, run_id=run.run_id)
    if not started:
        end = DagsterEvent(event_type_value=DagsterEventType.PIPELINE_SUCCESS.value, job_name=job_name, message="end")
        instance.report_dagster_event(end, run_id=run.run_id)
    return run.run_id


def test_the_job_of_the_process_of_a_real_in_process_run_is_found() -> None:
    """Dagster writes the engine event itself here. If an upgrade changes its text, this test fails."""
    import os

    found: list[str] = []

    @dg.op
    def look(context) -> None:  # unannotated: the module delays annotations, which Dagster refuses
        found.append(runtime_shape.job_of_process(context.instance, f"{os.getpid()}:0"))

    @dg.job(name="a_real_job_that_looks_at_itself")
    def real_job() -> None:
        look()

    result = real_job.execute_in_process(instance=dg.DagsterInstance.ephemeral())

    assert result.success
    assert found == ["a_real_job_that_looks_at_itself"]


def test_the_job_of_a_running_process_is_found_from_the_engine_event_of_its_run() -> None:
    instance = dg.DagsterInstance.ephemeral()
    _run(instance, "head_reports_pipeline", 4242)
    _run(instance, "topt_live_pipeline", 5151)

    assert runtime_shape.job_of_process(instance, "5151:99") == "topt_live_pipeline"
    assert runtime_shape.job_of_process(instance, "4242:7") == "head_reports_pipeline"


def test_a_process_that_belongs_to_no_started_run_has_the_job_unknown() -> None:
    instance = dg.DagsterInstance.ephemeral()
    _run(instance, "topt_live_pipeline", 5151, started=False)

    assert runtime_shape.job_of_process(instance, "5151:99") == runtime_shape.UNKNOWN_JOB, "the run is finished"
    assert runtime_shape.job_of_process(instance, "9999:1") == runtime_shape.UNKNOWN_JOB, "no run has this pid"
    assert runtime_shape.job_of_process(instance, "") == runtime_shape.UNKNOWN_JOB


def test_a_pid_that_is_a_prefix_of_another_pid_is_not_a_match() -> None:
    instance = dg.DagsterInstance.ephemeral()
    _run(instance, "topt_live_pipeline", 51510)

    assert runtime_shape.job_of_process(instance, "5151:1") == runtime_shape.UNKNOWN_JOB


def _over(job: str, size_mib: int, pid: int = 2) -> runtime_shape.OverEvent:
    return runtime_shape.OverEvent(f"{pid}:{pid}", job, size_mib * MIB)


def test_the_sampler_records_an_event_with_the_job_of_the_process_that_passed_the_budget(tmp_path: Path) -> None:
    instance = dg.DagsterInstance.ephemeral()
    _run(instance, "topt_live_pipeline", 4242)

    sampled = _sample(instance, _container(tmp_path, "a", ticks=1, process_bytes=BIG_PROCESS + 5 * MIB, pid=4242))

    assert sampled.processes_over_size == 1
    assert sampled.over_events == (
        runtime_shape.OverEvent("4242:4242", "topt_live_pipeline", BIG_PROCESS + 5 * MIB, f"{BOOT_ID}:1"),
    )
    assert runtime_shape.load_tracker(instance) == sampled, "the events are stored with the tracker"


def test_the_sampler_does_not_look_for_a_job_while_no_process_passes_the_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = dg.DagsterInstance.ephemeral()
    looked: list[str] = []
    monkeypatch.setattr(runtime_shape, "job_of_process", lambda _instance, process_id: looked.append(process_id) or "x")

    sampled = _sample(instance, _container(tmp_path, "a", ticks=1, process_bytes=RUN_BYTES, pid=4242))

    assert (looked, sampled.over_events) == ([], ())


def test_the_sampler_looks_for_a_job_once_per_event_and_not_at_every_sample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = dg.DagsterInstance.ephemeral()
    _run(instance, "topt_live_pipeline", 4242)
    looked: list[str] = []
    real = runtime_shape.job_of_process
    monkeypatch.setattr(
        runtime_shape, "job_of_process", lambda i, process_id: looked.append(process_id) or real(i, process_id)
    )

    for name in ("a", "b", "c"):
        _sample(instance, _container(tmp_path, name, ticks=1, process_bytes=BIG_PROCESS, pid=4242))

    assert looked == ["4242:4242"]


def test_a_process_that_keeps_growing_raises_the_size_of_its_event_and_keeps_its_job(tmp_path: Path) -> None:
    instance = dg.DagsterInstance.ephemeral()
    _run(instance, "topt_live_pipeline", 4242)
    _sample(instance, _container(tmp_path, "a", ticks=1, process_bytes=BIG_PROCESS, pid=4242))

    grown = _sample(instance, _container(tmp_path, "b", ticks=1, process_bytes=BIG_PROCESS + 40 * MIB, pid=4242))

    assert grown.over_events == (
        runtime_shape.OverEvent("4242:4242", "topt_live_pipeline", BIG_PROCESS + 40 * MIB, f"{BOOT_ID}:1"),
    )
    assert grown.processes_over_size == 1


def test_a_process_of_another_job_is_a_second_event_with_its_own_job(tmp_path: Path) -> None:
    instance = dg.DagsterInstance.ephemeral()
    _run(instance, "topt_live_pipeline", 4242)
    _run(instance, "market_data_refresh_pipeline", 5151)
    _sample(instance, _container(tmp_path, "a", ticks=1, process_bytes=BIG_PROCESS + 20 * MIB, pid=4242))

    second = _sample(instance, _container(tmp_path, "b", ticks=1, process_bytes=BIG_PROCESS, pid=5151))

    assert [(event.job, event.size) for event in second.over_events] == [
        ("topt_live_pipeline", BIG_PROCESS + 20 * MIB),
        ("market_data_refresh_pipeline", BIG_PROCESS),
    ]


def test_only_the_newest_events_stay(tmp_path: Path) -> None:
    instance = dg.DagsterInstance.ephemeral()
    tracker = Tracker()
    for pid in range(10, 10 + runtime_shape.MAX_OVER_EVENTS + 3):
        _run(instance, f"job_{pid}", pid)
        tracker = _sample(instance, _container(tmp_path, f"c{pid}", ticks=1, process_bytes=BIG_PROCESS, pid=pid))

    assert len(tracker.over_events) == runtime_shape.MAX_OVER_EVENTS
    assert tracker.over_events[-1].job == f"job_{10 + runtime_shape.MAX_OVER_EVENTS + 2}"
    assert tracker.processes_over_size == runtime_shape.MAX_OVER_EVENTS + 3


def test_a_job_that_was_unknown_at_the_first_sample_is_looked_up_again(tmp_path: Path) -> None:
    """The run row may not be STARTED yet, or the read failed once. The process is still the largest."""
    instance = dg.DagsterInstance.ephemeral()
    first = _sample(instance, _container(tmp_path, "a", ticks=1, process_bytes=BIG_PROCESS, pid=4242))
    assert first.over_events[-1].job == runtime_shape.UNKNOWN_JOB

    _run(instance, "topt_live_pipeline", 4242)
    second = _sample(instance, _container(tmp_path, "b", ticks=1, process_bytes=BIG_PROCESS, pid=4242))

    assert second.over_events[-1].job == "topt_live_pipeline"
    assert second.processes_over_size == 1


def test_a_failing_job_lookup_does_not_fail_the_sample_and_names_the_job_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = dg.DagsterInstance.ephemeral()

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("the event log cannot be read")

    monkeypatch.setattr(instance, "get_run_records", broken)

    sampled = _sample(instance, _container(tmp_path, "a", ticks=1, process_bytes=BIG_PROCESS, pid=4242))

    assert sampled.processes_over_size == 1
    assert [event.job for event in sampled.over_events] == [runtime_shape.UNKNOWN_JOB]
    assert runtime_shape.load_tracker(instance) == sampled


def test_two_processes_that_overtake_each_other_count_once_each_and_keep_their_own_event(tmp_path: Path) -> None:
    instance = dg.DagsterInstance.ephemeral()
    _run(instance, "topt_live_pipeline", 4242)
    _run(instance, "head_reports_pipeline", 5151)
    _sample(instance, _container(tmp_path, "a", ticks=1, process_bytes=BIG_PROCESS + 3 * MIB, pid=4242))
    _sample(instance, _container(tmp_path, "b", ticks=1, process_bytes=BIG_PROCESS + 8 * MIB, pid=5151))

    overtaken = _sample(instance, _container(tmp_path, "c", ticks=1, process_bytes=BIG_PROCESS + 13 * MIB, pid=4242))

    assert overtaken.processes_over_size == 2, "the first process is not counted again when it is the largest again"
    assert [(event.job, event.size) for event in overtaken.over_events] == [
        ("topt_live_pipeline", BIG_PROCESS + 13 * MIB),
        ("head_reports_pipeline", BIG_PROCESS + 8 * MIB),
    ]


def test_a_known_job_is_not_looked_up_again_when_the_process_grows_even_after_its_run_ended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = dg.DagsterInstance.ephemeral()
    run_id = _run(instance, "topt_live_pipeline", 4242)
    _sample(instance, _container(tmp_path, "a", ticks=1, process_bytes=BIG_PROCESS, pid=4242))
    from dagster import DagsterEventType
    from dagster._core.events import DagsterEvent

    end = DagsterEvent(
        event_type_value=DagsterEventType.PIPELINE_SUCCESS.value, job_name="topt_live_pipeline", message="end"
    )
    instance.report_dagster_event(end, run_id=run_id)
    looked: list[str] = []
    real = runtime_shape.job_of_process
    monkeypatch.setattr(
        runtime_shape, "job_of_process", lambda i, process_id: looked.append(process_id) or real(i, process_id)
    )

    grown = _sample(instance, _container(tmp_path, "b", ticks=1, process_bytes=BIG_PROCESS + 40 * MIB, pid=4242))

    assert looked == [], "a known job costs no lookup"
    assert grown.over_events == (
        runtime_shape.OverEvent("4242:4242", "topt_live_pipeline", BIG_PROCESS + 40 * MIB, f"{BOOT_ID}:1"),
    )


def test_an_event_with_an_unknown_job_is_looked_up_again_while_its_process_grows(tmp_path: Path) -> None:
    instance = dg.DagsterInstance.ephemeral()
    first = _sample(instance, _container(tmp_path, "a", ticks=1, process_bytes=BIG_PROCESS, pid=4242))
    assert first.over_events[-1].job == runtime_shape.UNKNOWN_JOB
    _run(instance, "topt_live_pipeline", 4242)

    grown = _sample(instance, _container(tmp_path, "b", ticks=1, process_bytes=BIG_PROCESS + 40 * MIB, pid=4242))

    assert grown.over_events == (
        runtime_shape.OverEvent("4242:4242", "topt_live_pipeline", BIG_PROCESS + 40 * MIB, f"{BOOT_ID}:1"),
    )


def test_an_unknown_event_is_not_looked_up_for_another_process_that_is_under_the_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = dg.DagsterInstance.ephemeral()
    _sample(instance, _container(tmp_path, "a", ticks=1, process_bytes=BIG_PROCESS, pid=4242))
    looked: list[str] = []
    monkeypatch.setattr(runtime_shape, "job_of_process", lambda _i, process_id: looked.append(process_id) or "x")

    after = _sample(instance, _container(tmp_path, "b", ticks=1, process_bytes=RUN_BYTES, pid=5151))

    assert looked == []
    assert [event.job for event in after.over_events] == [runtime_shape.UNKNOWN_JOB]


def test_the_job_of_a_canceling_run_is_found() -> None:
    instance = dg.DagsterInstance.ephemeral()
    run_id = _run(instance, "topt_live_pipeline", 4242)
    run = instance.get_run_by_id(run_id)
    assert run is not None
    instance.report_run_canceling(run)

    assert runtime_shape.job_of_process(instance, "4242:1") == "topt_live_pipeline"


def test_the_red_text_names_the_job_of_each_process_of_the_night_and_not_an_older_one(tmp_path: Path) -> None:
    """Night 1: topt at 600 MiB. Night 2: head_reports at 450 MiB. The text of night 2 names head_reports."""
    instance = dg.DagsterInstance.ephemeral()
    _run(instance, "topt_live_pipeline", 4242)
    _run(instance, "head_reports_pipeline", 5151)
    first = _container(tmp_path, "a", ticks=1, process_bytes=600 * MIB, pid=4242)
    second = _container(tmp_path, "b", ticks=1, process_bytes=450 * MIB, pid=5151)

    night_one = _night(instance, first, TICK_1)
    night_two = _night(instance, second, TICK_2)

    assert [reason for reason in night_one.red if "(job topt_live_pipeline)" in reason]
    text = " ".join(night_two.red)
    assert "450 MiB (job head_reports_pipeline)" in text
    assert "topt_live_pipeline" not in text and "600 MiB" not in text


def test_the_red_text_groups_the_processes_of_the_night_by_job_largest_first(tmp_path: Path) -> None:
    instance = dg.DagsterInstance.ephemeral()
    _run(instance, "topt_live_pipeline", 4242)
    _run(instance, "head_reports_pipeline", 5151)
    _sample(instance, _container(tmp_path, "a", ticks=1, process_bytes=BIG_PROCESS + 5 * MIB, pid=4242))
    both = _container(tmp_path, "b", ticks=1, process_bytes=BIG_PROCESS + 9 * MIB, pid=5151)

    verdict = _night(instance, both, TICK_1)

    size_a, size_b = (BIG_PROCESS + 5 * MIB) // MIB, (BIG_PROCESS + 9 * MIB) // MIB
    assert verdict.red[0] == (
        f"run processes passed the run budget {CAPACITY.run_mib} MiB (2 time(s)): "
        f"head_reports_pipeline (1x, up to {size_b} MiB), topt_live_pipeline (1x, up to {size_a} MiB)"
    )


def _tracker_with_events(*events: runtime_shape.OverEvent, count: int | None = None) -> Tracker:
    return Tracker(over_events=events, processes_over_size=len(events) if count is None else count)


def test_a_night_with_eight_events_of_one_job_fits_the_published_summary(tmp_path: Path) -> None:
    """The row of mart.nightly_verdicts is cut at SUMMARY_LIMIT. The count and the budget must stay in it."""
    events = [_over("topt_live_pipeline", 412, pid=pid) for pid in range(10, 18)]
    tracker = _tracker_with_events(*events)

    verdict = _judge(_container(tmp_path, "a", ticks=1), tracker=tracker, baseline=NOTHING)

    published = nightly_verdicts.bounded("failed: " + "; ".join(verdict.red))
    assert published == "failed: " + "; ".join(verdict.red), "the text is not cut"
    assert f"{CAPACITY.run_mib} MiB (8 time(s))" in published
    assert "topt_live_pipeline (8x, up to 412 MiB)" in published


def test_the_three_texts_of_the_finding_end_the_same_way_with_the_count_and_no_sentence_of_their_own(
    tmp_path: Path,
) -> None:
    """One event, no event and several events: each ends with its count or its names, none with a sentence
    that the others lack."""
    reading = _container(tmp_path, "a", ticks=1)
    one = _judge(reading, tracker=_tracker_with_events(_over("topt_live_pipeline", 412)), baseline=NOTHING).red[0]
    none = _judge(reading, tracker=Tracker(processes_over_size=1), baseline=NOTHING).red[0]
    several = _judge(
        reading, tracker=_tracker_with_events(_over("a_job", 412), _over("b_job", 420, pid=3)), baseline=NOTHING
    ).red[0]

    budget = CAPACITY.run_mib
    assert one == f"a run process held 412 MiB (job topt_live_pipeline), above the run budget {budget} MiB (1 time(s))"
    assert none == f"1 run process(es) passed the run budget {budget} MiB (size and job not recorded)"
    assert several.endswith("b_job (1x, up to 420 MiB), a_job (1x, up to 412 MiB)")


def test_the_worst_case_text_fits_the_published_summary_with_the_longest_deployed_job_names(tmp_path: Path) -> None:
    """The five longest names of the deployed jobs, 4-digit sizes, a 4-digit count and counts without an event:
    the longest finding alone. It must pass `bounded` unchanged, with the `failed: ` prefix of the row."""
    from data_engine.dagster_defs import defs

    names = sorted({job.name for job in defs.resolve_all_job_defs()}, key=len, reverse=True)[:5]
    assert len(names) == 5 and len(names[0]) >= 36, "the deployed job names are the input of this test"
    events = [_over(names[index % 5], 1530 + index, pid=10 + index) for index in range(8)]
    tracker = _tracker_with_events(*events, count=9999)

    text = _judge(_container(tmp_path, "a", ticks=1), tracker=tracker, baseline=NOTHING).red[0]

    published = nightly_verdicts.bounded("failed: " + text)
    assert published == "failed: " + text, f"the finding alone is cut at {nightly_verdicts.SUMMARY_LIMIT} characters"
    assert f"{CAPACITY.run_mib} MiB (9999 time(s))" in published and published.endswith("9991 more not named")


def test_the_text_names_the_three_largest_jobs_and_counts_the_others(tmp_path: Path) -> None:
    events = [_over(f"job_{size}", size, pid=size) for size in (410, 420, 430, 440, 450)]
    tracker = _tracker_with_events(*events)

    text = _judge(_container(tmp_path, "a", ticks=1), tracker=tracker, baseline=NOTHING).red[0]

    assert (
        "job_450 (1x, up to 450 MiB), job_440 (1x, up to 440 MiB), job_430 (1x, up to 430 MiB), and 2 other job(s)"
        in text
    )
    assert text.endswith("and 2 other job(s)")


def test_the_text_says_how_many_counts_have_no_event(tmp_path: Path) -> None:
    """More counts than events: a rollback window, legacy state, or more than MAX_OVER_EVENTS counts."""
    tracker = _tracker_with_events(_over("topt_live_pipeline", 450), count=3)

    text = _judge(_container(tmp_path, "a", ticks=1), tracker=tracker, baseline=NOTHING).red[0]

    assert text.endswith("(3 time(s)): topt_live_pipeline (1x, up to 450 MiB); 2 more not named")
    assert text.startswith("run processes passed the run budget")


def test_the_red_text_does_not_name_an_old_size_when_state_has_no_event(tmp_path: Path) -> None:
    """State saved before the jobs were recorded counts a process and has no event for it. The all-time
    record of the largest size may belong to another night, so the text names no size."""
    tracker = _fold(Tracker(), _container(tmp_path, "a", ticks=1, process_bytes=BIG_PROCESS + 5 * MIB))

    verdict = _judge(_container(tmp_path, "b", ticks=1), tracker=tracker, baseline=NOTHING)

    assert verdict.red == (
        f"1 run process(es) passed the run budget {CAPACITY.run_mib} MiB (size and job not recorded)",
    )


def test_state_saved_before_the_jobs_were_recorded_still_loads_with_no_event() -> None:
    tracker = Tracker.from_json(_tracker_json())

    assert tracker.over_events == ()
    saved = dataclasses.replace(tracker, over_events=(_over("topt_live_pipeline", 412),))
    assert Tracker.from_json(saved.to_json()) == saved


@pytest.mark.parametrize(
    "events",
    [
        {},
        "x",
        [1],
        [{"process_id": "1:1", "job": "j"}],
        [{"process_id": "1:1", "job": 5, "size": 1}],
        [{"process_id": "1:1", "job": "j", "size": -1}],
    ],
)
def test_events_that_are_not_events_make_the_state_unreadable(events: Any) -> None:
    with pytest.raises(runtime_shape.StateUnreadable):
        Tracker.from_json(_tracker_json(over_events=events))


# --- the deployed job and sensor ---------------------------------------------------------------


@pytest.fixture
def written(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every verdict the job records, instead of a database row."""
    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(nightly_verdicts, "record", lambda name, **kwargs: rows.append({"check": name, **kwargs}))
    return rows


def _point_the_code_at(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **tree: Any) -> None:
    cgroup, proc = _tree(tmp_path, **tree)
    monkeypatch.setattr(runtime_shape, "read", functools.partial(REAL_READ, cgroup, proc))
    monkeypatch.setattr(quality, "boot_deployment", lambda: "A")


def test_the_check_is_a_declared_nightly_verdict_of_a_scheduled_job() -> None:
    assert quality.RUNTIME_SHAPE_VERDICT == "runtime_shape"
    assert "runtime_shape" in quality.NIGHTLY_VERDICTS
    assert "runtime_shape" not in standards.NIGHTLY_VERDICTS
    assert quality.runtime_shape_schedule.job_name == quality.RUNTIME_SHAPE_JOB_NAME == "runtime_shape_check"
    request = quality.runtime_shape_schedule(
        dg.build_schedule_context(scheduled_execution_time=datetime(2026, 10, 21, 6, 30, tzinfo=UTC))
    )
    assert request.tags[nightly_verdicts.TICK_TAG] == TICK_1


def test_the_job_records_green_then_red_after_a_kill_then_green_again(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, written: list[dict[str, Any]]
) -> None:
    """One instance holds the state across three nights. The sensor runs before each check."""
    instance = dg.DagsterInstance.ephemeral()

    def night(name: str, tick: str, **tree: Any):
        _point_the_code_at(monkeypatch, tmp_path / name, **tree)
        list(quality.runtime_shape_sampler_sensor(dg.build_sensor_context(instance=instance)))
        return quality.runtime_shape_job.execute_in_process(
            instance=instance, tags={nightly_verdicts.TICK_TAG: tick}, raise_on_error=False
        )

    first = night("n1", TICK_1, oom_kill=0)
    second = night("n2", TICK_2, oom_kill=2)
    third = night("n3", TICK_3, oom_kill=2)

    assert (first.success, second.success, third.success) == (True, False, True), (
        "the run is red when the verdict is red, and the kill is counted once"
    )
    assert [(row["check"], row["ok"]) for row in written] == [
        ("runtime_shape", True),
        ("runtime_shape", False),
        ("runtime_shape", True),
    ]
    assert written[1]["summary"] == "failed: 2 OOM kill(s) since the last check"
    assert [row["ran_at"] for row in written] == [datetime.fromisoformat(tick) for tick in (TICK_1, TICK_2, TICK_3)]


def test_the_job_is_red_when_the_sensor_never_ran(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, written: list[dict[str, Any]]
) -> None:
    _point_the_code_at(monkeypatch, tmp_path)

    result = quality.runtime_shape_job.execute_in_process(
        tags={nightly_verdicts.TICK_TAG: TICK_1}, raise_on_error=False
    )

    assert result.success is False
    assert written[0]["summary"] == "failed: the sampler has recorded nothing, so kills and restarts may be unseen"


def test_the_job_is_red_and_records_red_when_a_file_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, written: list[dict[str, Any]]
) -> None:
    instance = dg.DagsterInstance.ephemeral()
    _point_the_code_at(monkeypatch, tmp_path, peak=None)
    list(quality.runtime_shape_sampler_sensor(dg.build_sensor_context(instance=instance)))

    result = quality.runtime_shape_job.execute_in_process(
        instance=instance, tags={nightly_verdicts.TICK_TAG: TICK_1}, raise_on_error=False
    )

    assert result.success is False
    assert [(row["check"], row["ok"], row["summary"]) for row in written] == [
        ("runtime_shape", False, "failed: unreadable: memory.peak")
    ]


def test_the_sampler_sensor_folds_the_reading_with_the_boot_deployment_and_launches_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    instance = dg.DagsterInstance.ephemeral()
    _point_the_code_at(monkeypatch, tmp_path, oom_kill=2)

    result = list(quality.runtime_shape_sampler_sensor(dg.build_sensor_context(instance=instance)))

    assert [type(item) for item in result] == [dg.SkipReason]
    tracker = runtime_shape.load_tracker(instance)
    assert tracker is not None
    assert tracker.kills == 2
    assert tracker.container is not None
    assert tracker.container.deployment == "A", "the sensor passes boot_deployment(), the identity of the deploy"
    assert tracker.sampled_at != ""
    assert quality.runtime_shape_sampler_sensor.minimum_interval_seconds == runtime_shape.SAMPLE_INTERVAL_SECONDS
    assert quality.runtime_shape_sampler_sensor.default_status == dg.DefaultSensorStatus.RUNNING


# -- the order of the nightly night (#1484) ----------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]
DELIVERY_REPORT_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "delivery-report.yml"
NIGHTLY_CHECKS = REPO_ROOT / "tools" / "nightly_verdicts.json"


def _minutes_of_day(expression: str) -> list[int]:
    """Every time of day (minutes after 00:00 UTC) at which the cron fires."""
    from truealpha_runtime.testing import load_tool

    cron = load_tool("scheduler_liveness").Cron.parse(expression)
    return sorted(hour * 60 + minute for hour in cron.hours for minute in cron.minutes)


def order_violations(shape: str, report: str, checks: dict[str, list[str]]) -> list[str]:
    """What breaks the order of the night: the other nightly checks, then the runtime shape, then the report.

    The shape verdict reads state that the other checks and the ticks leave. A check that fires in the
    window from the shape verdict to the report is read after the shape verdict. The shape verdict must
    also come before the report, or the report reads the verdict of the night before.
    """
    (shape_time,) = _minutes_of_day(shape)
    (report_time,) = _minutes_of_day(report)
    found = []
    if not shape_time < report_time:
        found.append(f"the shape check {shape!r} does not precede the report {report!r}")
    for job, expressions in sorted(checks.items()):
        for expression in expressions:
            late = [t for t in _minutes_of_day(expression) if shape_time <= t < report_time]
            if late:
                found.append(f"{job} {expression!r} does not precede the shape check {shape!r}")
    return found


def _deployed_check_crons() -> dict[str, list[str]]:
    """The cron of every job that tools/nightly_verdicts.json names, from the deployed root, without the shape job."""
    import json

    from data_engine.dagster_defs import defs

    jobs = {entry["job"] for entry in json.loads(NIGHTLY_CHECKS.read_text())["checks"].values()}
    jobs.discard(quality.RUNTIME_SHAPE_JOB_NAME)
    crons: dict[str, list[str]] = {}
    for schedule in defs.schedules or ():
        if schedule.job_name in jobs:
            expressions = (
                [schedule.cron_schedule] if isinstance(schedule.cron_schedule, str) else schedule.cron_schedule
            )
            crons.setdefault(schedule.job_name, []).extend(expressions)
    assert set(crons) == jobs, f"a nightly check job has no deployed schedule: {sorted(jobs - set(crons))}"
    return crons


def _deployed_shape_cron() -> str:
    from data_engine.dagster_defs import defs

    (schedule,) = [s for s in defs.schedules or () if s.job_name == quality.RUNTIME_SHAPE_JOB_NAME]
    assert isinstance(schedule.cron_schedule, str)
    return schedule.cron_schedule


def _report_cron() -> str:
    import re

    found = re.findall(r'^\s*-?\s*cron:\s*"([^"]+)"', DELIVERY_REPORT_WORKFLOW.read_text(), flags=re.MULTILINE)
    assert len(found) == 1, f"the delivery report has {len(found)} schedules"
    return found[0]


def test_the_runtime_shape_check_follows_every_nightly_check_and_precedes_the_report() -> None:
    """The deployed schedules: no other check fires between the shape check and the report, and the shape
    check fires before the report. The shape verdict then reads a finished night."""
    checks = _deployed_check_crons()
    assert "release_fetch_proof" in checks, "the last check before the shape check is in the scan"
    assert _deployed_shape_cron() == quality.RUNTIME_SHAPE_CRON

    assert order_violations(_deployed_shape_cron(), _report_cron(), checks) == []


@pytest.mark.parametrize(
    ("shape", "expected"),
    [
        ("30 8 * * *", "does not precede the report"),
        ("30 5 * * *", "release_fetch_proof"),
        ("0 6 * * *", "release_fetch_proof"),
        ("0 7 * * *", "does not precede the report"),
    ],
    ids=[
        "after-the-report",
        "before-the-release-fetch-proof",
        "before-the-release-fetch-proof-at-six",
        "at-the-report",
    ],
)
def test_a_shape_cron_out_of_order_with_the_night_is_a_violation(shape: str, expected: str) -> None:
    """The mutation of #1484: `30 8 * * *` kept every test green before this one."""
    violations = order_violations(shape, _report_cron(), _deployed_check_crons())

    assert any(expected in violation for violation in violations), violations


def test_the_report_cron_is_seven_in_the_morning_so_the_order_is_read_from_the_workflow() -> None:
    assert _minutes_of_day(_report_cron()) == [7 * 60]
