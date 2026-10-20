"""The runtime shape of the Dagster code server: did its memory cgroup hold the load? (#1467, #28 V5)

Every Dagster run executes inside the code server container, in the same memory cgroup as the
gRPC server. On 2026-10-20 the kernel killed processes of that cgroup five times. Each kill left
a run `STARTED` for ever, and no check had read the cgroup. This module reads three facts from
inside the container and judges them against the capacity that `code_server_capacity` declares in
`environment_contract.v1.json`:

* `memory.events` `oom_kill`: the kernel killed a process of the cgroup;
* `memory.peak`: the most memory the cgroup held since the container started;
* `memory.max`: the ceiling that the container really has.

A verdict is red on any of these:

* a fact is unreadable. Unknown is red, never OK (#28 V3);
* `memory.max` is `max` (no ceiling) or below the declared ceiling. The run limit of `dagster.yaml`
  derives from the declared ceiling, so a lower real ceiling breaks the premise of the limit;
* `oom_kill` rose since the last check. The first check counts every kill of the container;
* the peak is above `utilization_limit` of `memory.max` (70%, rule of #1418);
* the container restarted since the last check and the release did not change. The kernel may
  kill the server process itself. The container then restarts with a new cgroup, and the counter
  of the new cgroup does not show the kill. A deploy restarts the container too, but it changes
  the release.

The last check's facts are the baseline. They live in the key-value table of the Dagster run
storage, not in the verdict summary, which is published on `/health` and carries counts only.
`memory.peak` counts page cache that the cgroup holds, as the kernel does when it decides on a kill.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path

import dagster as dg
from truealpha_contracts.environment_contract import CodeServerCapacity

log = logging.getLogger(__name__)

#: The memory cgroup of the container, as the container sees it (cgroup v2).
CGROUP_ROOT = Path("/sys/fs/cgroup")
#: The process table of the container. Its first process is the code server.
PROC_ROOT = Path("/proc")
#: The key of the baseline in the key-value table of the Dagster run storage.
BASELINE_KEY = "truealpha/runtime_shape/baseline"

MIB = 1024 * 1024


@dataclass(frozen=True)
class Reading:
    """What the cgroup says now. A field is None when it could not be read.

    `memory_max` is None with `unlimited` True when the file says `max` (no ceiling). `started` is the
    start second of the first process of the container, in whole seconds since the epoch."""

    memory_max: int | None = None
    unlimited: bool = False
    memory_peak: int | None = None
    oom_kill: int | None = None
    started: int | None = None

    @property
    def unreadable(self) -> tuple[str, ...]:
        """The names of the facts that are missing. A reading with a missing fact is never green."""
        missing = (
            ("memory.max", self.memory_max is None and not self.unlimited),
            ("memory.peak", self.memory_peak is None),
            ("memory.events", self.oom_kill is None),
            ("process start time", self.started is None),
        )
        return tuple(name for name, is_missing in missing if is_missing)


@dataclass(frozen=True)
class Baseline:
    """The facts of the last check that the next check compares with."""

    oom_kill: int
    started: int
    release: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> Baseline | None:
        """None for a value that is not a baseline. The check then treats the container as never checked."""
        try:
            data = json.loads(raw)
            return cls(oom_kill=int(data["oom_kill"]), started=int(data["started"]), release=str(data["release"]))
        except (ValueError, KeyError, TypeError):
            log.warning("the runtime shape baseline is not readable; treating the container as never checked")
            return None


@dataclass(frozen=True)
class Judgement:
    """The verdict of one check: `red` lists every reason; `summary` is the public line."""

    red: tuple[str, ...]
    summary: str
    #: What the next check compares with. None when this check could not read the facts it needs.
    baseline: Baseline | None = None
    facts: Mapping[str, int | float] = field(default_factory=dict)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def _events(text: str) -> dict[str, int]:
    """Parse the flat keyed file `memory.events`: one `name value` pair per line."""
    pairs = (line.split() for line in text.splitlines() if line.strip())
    return {name: int(value) for name, value in pairs}


def _start_second(proc_root: Path) -> int:
    """Start second of the first process: boot time plus the `starttime` ticks of `/proc/1/stat`.

    The process name in `stat` may hold spaces and brackets, so the fields count from the last `)`."""
    stat = _read_text(proc_root / "1" / "stat")
    fields_after_name = stat[stat.rindex(")") + 2 :].split()
    start_ticks = int(fields_after_name[19])  # field 22 of the line, counting the pid as field 1
    boot_time = next(
        int(line.split()[1]) for line in _read_text(proc_root / "stat").splitlines() if line.startswith("btime ")
    )
    return boot_time + start_ticks // os.sysconf("SC_CLK_TCK")


def read(cgroup_root: Path = CGROUP_ROOT, proc_root: Path = PROC_ROOT) -> Reading:
    """Read the cgroup and the process table. A file that fails to read leaves its field None."""
    memory_max: int | None = None
    unlimited = False
    memory_peak: int | None = None
    oom_kill: int | None = None
    started: int | None = None

    try:
        raw_max = _read_text(cgroup_root / "memory.max")
        unlimited = raw_max == "max"
        memory_max = None if unlimited else int(raw_max)
    except (OSError, ValueError):
        pass
    try:
        memory_peak = int(_read_text(cgroup_root / "memory.peak"))
    except (OSError, ValueError):
        pass
    try:
        oom_kill = _events(_read_text(cgroup_root / "memory.events"))["oom_kill"]
    except (OSError, ValueError, KeyError):
        pass
    try:
        started = _start_second(proc_root)
    except (OSError, ValueError, IndexError, StopIteration):
        pass

    return Reading(
        memory_max=memory_max, unlimited=unlimited, memory_peak=memory_peak, oom_kill=oom_kill, started=started
    )


def _mib(value: int) -> int:
    return round(value / MIB)


def judge(reading: Reading, *, baseline: Baseline | None, release: str, capacity: CodeServerCapacity) -> Judgement:
    """Judge one reading against the last check and the declared capacity."""
    red: list[str] = []
    facts: dict[str, int | float] = {}
    if reading.unreadable:
        red.append(f"unreadable: {', '.join(reading.unreadable)}")
    if reading.unlimited:
        red.append("memory.max is max: the container has no memory ceiling")
    elif reading.memory_max is not None and reading.memory_max < capacity.ceiling_mib * MIB:
        red.append(
            f"memory.max {_mib(reading.memory_max)} MiB is below the declared ceiling {capacity.ceiling_mib} MiB"
        )

    same_container = baseline is not None and reading.started is not None and baseline.started == reading.started
    restarted = baseline is not None and reading.started is not None and not same_container
    if restarted and baseline is not None and baseline.release == release:
        red.append("the code server restarted without a release change; its OOM counter restarted too")

    if reading.oom_kill is not None:
        # A new container starts a new counter, so all of its kills came after the last check.
        counted_before = baseline.oom_kill if baseline is not None and same_container else 0
        kills = max(reading.oom_kill - counted_before, 0)
        facts["oom_kill_since_last_check"] = kills
        if kills > 0:
            red.append(f"{kills} OOM kill(s) since the last check")

    peak = ""
    if reading.memory_peak is not None and reading.memory_max is not None:
        share = reading.memory_peak / reading.memory_max
        facts["peak_share"] = round(share, 3)
        peak = f"peak {_mib(reading.memory_peak)} of {_mib(reading.memory_max)} MiB ({share:.0%})"
        if share > capacity.utilization_limit:
            red.append(f"{peak}; the limit is {capacity.utilization_limit:.0%}")

    new_baseline = (
        Baseline(oom_kill=reading.oom_kill, started=reading.started, release=release)
        if reading.oom_kill is not None and reading.started is not None
        else None
    )
    # The verdict is published on /health: counts and sizes only, never a path or an error text.
    summary = "; ".join(red) if red else f"no OOM kill since the last check; {peak}"
    if restarted and not red:
        summary += "; the container restarted with a new release"
    return Judgement(red=tuple(red), summary=summary, baseline=new_baseline, facts=facts)


def load_baseline(instance: dg.DagsterInstance) -> Baseline | None:
    """The baseline that the last check saved, or None when no check ran or the value is unreadable."""
    raw = instance.run_storage.get_cursor_values({BASELINE_KEY}).get(BASELINE_KEY)
    return None if raw is None else Baseline.from_json(raw)


def save_baseline(instance: dg.DagsterInstance, baseline: Baseline) -> None:
    instance.run_storage.set_cursor_values({BASELINE_KEY: baseline.to_json()})
