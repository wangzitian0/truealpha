"""The runtime shape of the Dagster code server: did its memory cgroup hold the load? (#1467, #28 V5)

Every Dagster run executes inside the code server container, in the same memory cgroup as the
gRPC server. On 2026-10-20 the kernel killed processes of that cgroup five times. Each kill left
a run `STARTED` for ever, and no check had read the cgroup. This module reads these facts from
inside the container and judges them against the capacity that `code_server_capacity` declares in
`environment_contract.v1.json`:

* `memory.events` `oom_kill`: the kernel killed a process of the cgroup;
* `memory.peak`: the most memory the cgroup held since the container started;
* `memory.max`: the ceiling that the container really has;
* the identity of the container: the boot id of the host and the start ticks of its first process;
* `VmHWM` of the code server and of the largest other process (a run), judged against `server_mib`
  and the run budget `run_mib` of the contract.

The kernel counters belong to the container. A deploy or a crash replaces the container, and the
new cgroup starts at zero. A check that reads the counters once a night loses every kill and every
peak that a replaced container held. So a sensor (`sample`, every `SAMPLE_INTERVAL_SECONDS`) folds
each reading into a `Tracker`: the totals of kills, unplanned restarts and new peaks above the limit
over the life of all containers. The nightly check judges the growth of the stored totals since the
last scheduled check (`Baseline`). It does not fold its own reading in: the sensor and the check each
write one key of the run storage, and the baseline never holds a count that the tracker lacks. A kill
in the last minutes before a check shows at the next check.

The check depends on the sensor, so a silent sensor is red: no tracker, or a last sample older than
`STALE_AFTER_SECONDS`. A container that lives for less than one interval (a deploy followed at once by
a crash) is not seen by any sample; this is the blind window of the design.

The nightly verdict is red on any of these:

* a fact is unreadable, the sensor is silent, or the stored state is unreadable or lower than the
  last check saw. Unknown is red, never OK (#28 V3);
* `memory.max` is `max` (no ceiling) or below the declared ceiling. The run limit of `dagster.yaml`
  derives from the declared ceiling, so a lower real ceiling breaks the premise of the limit;
* `oom_kill` rose in any container since the last check. The first check counts every kill of
  the container that the sensor saw;
* the peak of a container rose above `UTILIZATION_CEILING` of `memory.max` since the last check. A
  peak that the last check already judged does not count again: `memory.peak` is a high-water mark
  of the whole container life;
* a run process held more than the budget `run_mib`, or the code server more than `server_mib`. A
  process counts once: when it first passes the limit, and again for each new process that does. The
  budget is what is left inside `UTILIZATION_CEILING` of the ceiling when the queue is full, so a larger run
  breaks the premise of the limit. A run that ends between two samples after its peak is not seen,
  and the cgroup peak shows it only when the whole passes the limit. The count is by the largest
  process at each sample, and each counted process keeps an event (the newest `MAX_OVER_EVENTS`). A
  smaller over-size process beside a larger one is not seen until it is the largest. A process whose
  event was dropped by `MAX_OVER_EVENTS` newer ones, and that is the largest again, counts again;
* the container was replaced without a change of the deployment. The kernel may kill the server
  process itself. The container then restarts with a new cgroup, and the counter of the new cgroup
  does not show the kill. A deploy replaces the container too, but it changes the deployment.

Only a tick-tagged run (the schedule) moves the baseline, and only forward. A manual run shows the
growth since the last scheduled check and consumes nothing. A re-execution of the latest tick returns
the verdict that the tick already has; a re-execution of an older tick judges and saves nothing.

`memory.peak` counts page cache that the cgroup holds. The summary shows counts, sizes and job names
(code identifiers) only, because the verdict is published on `/health`.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

import dagster as dg
from truealpha_contracts.environment_contract import UTILIZATION_CEILING, CodeServerCapacity

log = logging.getLogger(__name__)

#: The memory cgroup of the container, as the container sees it (cgroup v2).
CGROUP_ROOT = Path("/sys/fs/cgroup")
#: The process table of the container. Its first process is the code server.
PROC_ROOT = Path("/proc")
#: The keys in the key-value table of the Dagster run storage. Each has one writer.
TRACKER_KEY = "truealpha/runtime_shape/tracker"
BASELINE_KEY = "truealpha/runtime_shape/baseline"

MIB = 1024 * 1024
#: How often the sensor samples, and how old the last sample may be before the check calls the sensor silent.
SAMPLE_INTERVAL_SECONDS = 30
STALE_AFTER_SECONDS = 600


class StateUnreadable(Exception):
    """A value in the run storage is not the state that this module wrote."""


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f"not a text: {value!r}")
    return value


def _texts(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"not a list: {value!r}")
    return tuple(_text(item) for item in value)


def _over_events(value: object) -> tuple[OverEvent, ...]:
    if not isinstance(value, list):
        raise ValueError(f"not a list: {value!r}")
    return tuple(
        OverEvent(
            process_id=_text(item["process_id"]),
            job=_text(item["job"]),
            size=_count(item["size"]),
            container=_text(item.get("container", "")),
        )
        for item in value
    )


def _count(value: object) -> int:
    """A count is an integer of at least zero. A bool, a float or a string is not one."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"not a count: {value!r}")
    return value


@dataclass(frozen=True)
class Reading:
    """What the cgroup says now. A field is None when it could not be read.

    `memory_max` is None with `unlimited` True when the file says `max` (no ceiling).
    `container` names the container: the boot id of the host and the start ticks of its first process."""

    memory_max: int | None = None
    unlimited: bool = False
    memory_peak: int | None = None
    oom_kill: int | None = None
    container: str | None = None
    #: The largest `VmHWM` in bytes of any process except the first (the code server), and that process
    #: (pid and start ticks). 0 and empty when none runs.
    largest_process: int | None = None
    largest_process_id: str = ""
    #: The `VmHWM` in bytes of the first process, the code server.
    server_process: int | None = None

    @property
    def unreadable(self) -> tuple[str, ...]:
        """The names of the facts that are missing. A reading with a missing fact is never green."""
        missing = (
            ("memory.max", self.memory_max is None and not self.unlimited),
            ("memory.peak", self.memory_peak is None),
            ("memory.events", self.oom_kill is None),
            ("container identity", self.container is None),
            ("process sizes", self.largest_process is None or self.server_process is None),
        )
        return tuple(name for name, is_missing in missing if is_missing)


@dataclass(frozen=True)
class Container:
    """The container that the last sample saw, with the counters it showed."""

    id: str
    deployment: str
    oom_kill: int
    peak: int


#: How many over-budget processes the tracker names. The newest stay.
MAX_OVER_EVENTS = 8


@dataclass(frozen=True)
class OverEvent:
    """A run process that passed the run budget: its id (`pid:start ticks`), the job of its run and its size (bytes).

    `container` names the container it ran in. A process id repeats only across containers (after a host
    reboot), so the pair is the key of an event. State from before the container was recorded has ""."""

    process_id: str
    job: str
    size: int
    container: str = ""


@dataclass(frozen=True)
class Tracker:
    """Totals over the life of all containers. Written by the sampler only."""

    container: Container | None = None
    kills: int = 0
    restarts_same_deployment: int = 0
    restarts_new_deployment: int = 0
    peaks_over_limit: int = 0
    #: The process that was the largest at the last sample and its size (bytes). A process counts when it
    #: first passes the run budget; a new process that passes it counts again.
    process_id: str = ""
    process_size: int = 0
    processes_over_size: int = 0
    #: The code server at the last sample (bytes), and how often it passed `server_mib`.
    server_size: int = 0
    servers_over_size: int = 0
    #: The largest process of this container so far (bytes). A record to read, not a rule.
    largest_process: int = 0
    #: The largest run process and the largest code server seen over their limit (bytes).
    #: They never restart with a container, so a red verdict names the size that made it red.
    worst_process: int = 0
    worst_server: int = 0
    #: The processes that passed the run budget, oldest first, each with the job that runs in it. The
    #: newest `processes_over_size - baseline` of them are the ones of a night. State from before the
    #: jobs were recorded has none.
    over_events: tuple[OverEvent, ...] = ()
    #: When the sensor last ran (ISO 8601, UTC). Empty before the first sample.
    sampled_at: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> Tracker:
        try:
            data = json.loads(raw)
            container = data["container"]
            return cls(
                container=None
                if container is None
                else Container(
                    id=_text(container["id"]),
                    deployment=_text(container["deployment"]),
                    oom_kill=_count(container["oom_kill"]),
                    peak=_count(container["peak"]),
                ),
                kills=_count(data["kills"]),
                restarts_same_deployment=_count(data["restarts_same_deployment"]),
                restarts_new_deployment=_count(data["restarts_new_deployment"]),
                peaks_over_limit=_count(data["peaks_over_limit"]),
                process_id=_text(data.get("process_id", "")),
                process_size=_count(data.get("process_size", 0)),
                processes_over_size=_count(data.get("processes_over_size", 0)),
                server_size=_count(data.get("server_size", 0)),
                servers_over_size=_count(data.get("servers_over_size", 0)),
                largest_process=_count(data.get("largest_process", 0)),
                worst_process=_count(data.get("worst_process", 0)),
                worst_server=_count(data.get("worst_server", 0)),
                over_events=_over_events(data.get("over_events", [])),
                sampled_at=_text(data["sampled_at"]),
            )
        except (ValueError, KeyError, TypeError, OverflowError) as exc:
            raise StateUnreadable("the runtime shape tracker is not readable") from exc


@dataclass(frozen=True)
class Baseline:
    """The totals that the last scheduled check saw, and the verdict it gave. Written by that check only."""

    kills: int
    restarts_same_deployment: int
    restarts_new_deployment: int
    peaks_over_limit: int
    tick: str
    red: tuple[str, ...]
    summary: str
    processes_over_size: int = 0
    servers_over_size: int = 0

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> Baseline:
        try:
            data = json.loads(raw)
            return cls(
                kills=_count(data["kills"]),
                restarts_same_deployment=_count(data["restarts_same_deployment"]),
                restarts_new_deployment=_count(data["restarts_new_deployment"]),
                peaks_over_limit=_count(data["peaks_over_limit"]),
                tick=_text(data["tick"]),
                red=_texts(data["red"]),
                summary=_text(data["summary"]),
                processes_over_size=_count(data.get("processes_over_size", 0)),
                servers_over_size=_count(data.get("servers_over_size", 0)),
            )
        except (ValueError, KeyError, TypeError, OverflowError) as exc:
            raise StateUnreadable("the runtime shape baseline is not readable") from exc


#: The totals before the first scheduled check: nothing was counted yet.
_NO_CHECK_YET = Baseline(
    kills=0, restarts_same_deployment=0, restarts_new_deployment=0, peaks_over_limit=0, tick="", red=(), summary=""
)


@dataclass(frozen=True)
class Judgement:
    """The verdict of one check: `red` lists every reason; `summary` is the public line."""

    red: tuple[str, ...]
    summary: str
    facts: Mapping[str, int | float] = field(default_factory=dict)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def _events(text: str) -> dict[str, int]:
    """Parse the flat keyed file `memory.events`: one `name value` pair per line."""
    pairs = (line.split() for line in text.splitlines() if line.strip())
    return {name: int(value) for name, value in pairs}


def _start_ticks(stat: str) -> int:
    """Field 22 of `/proc/<pid>/stat`, the start of the process in ticks since boot.

    The process name in `stat` may hold spaces and brackets, so the fields count from the last `)`."""
    fields_after_name = stat[stat.rindex(")") + 2 :].split()
    return int(fields_after_name[19])  # field 22 of the line, counting the pid as field 1


def _container_id(proc_root: Path) -> str:
    """Boot id of the host plus the start ticks of the first process: neither depends on the wall clock."""
    boot_id = _read_text(proc_root / "sys" / "kernel" / "random" / "boot_id")
    if not boot_id:
        raise ValueError("empty boot id")
    return f"{boot_id}:{_start_ticks(_read_text(proc_root / '1' / 'stat'))}"


def _hwm_bytes(status: str) -> int | None:
    """`VmHWM` of a `/proc/<pid>/status` text in bytes. None for a process without one (a kernel thread)."""
    for line in status.splitlines():
        if line.startswith("VmHWM:"):
            return int(line.split()[1]) * 1024
    return None


def _largest_process(proc_root: Path) -> tuple[int, str]:
    """The largest `VmHWM` in bytes of any process except the first, and that process as `pid:start ticks`.

    A process that exits between the listing and the read, or that cannot be read, is skipped. A listing
    that cannot be read raises."""
    largest, identity = 0, ""
    for entry in proc_root.iterdir():
        if not entry.name.isdigit() or entry.name == "1":
            continue
        try:
            size = _hwm_bytes((entry / "status").read_bytes().decode("utf-8", errors="replace"))
            ticks = _start_ticks((entry / "stat").read_bytes().decode("utf-8", errors="replace")) if size else 0
        except (OSError, ValueError, IndexError):
            continue
        if size is not None and size > largest:
            largest, identity = size, f"{entry.name}:{ticks}"
    return largest, identity


def _server_process(proc_root: Path) -> int:
    """`VmHWM` in bytes of the first process, the code server."""
    command = (proc_root / "1" / "cmdline").read_bytes()
    if b"dagster" not in command:
        raise ValueError("the first process is not the code server")
    size = _hwm_bytes((proc_root / "1" / "status").read_bytes().decode("utf-8", errors="replace"))
    if size is None:
        raise ValueError("the first process has no VmHWM")
    return size


def read(cgroup_root: Path = CGROUP_ROOT, proc_root: Path = PROC_ROOT) -> Reading:
    """Read the cgroup and the process table. A file that fails to read leaves its field None."""
    memory_max: int | None = None
    unlimited = False
    memory_peak: int | None = None
    oom_kill: int | None = None
    container: str | None = None
    largest_process: int | None = None
    largest_process_id = ""
    server_process: int | None = None

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
        container = _container_id(proc_root)
    except (OSError, ValueError, IndexError):
        pass
    try:
        largest_process, largest_process_id = _largest_process(proc_root)
    except (OSError, ValueError, IndexError):
        pass
    try:
        server_process = _server_process(proc_root)
    except (OSError, ValueError):
        pass

    return Reading(
        memory_max=memory_max,
        unlimited=unlimited,
        memory_peak=memory_peak,
        oom_kill=oom_kill,
        container=container,
        largest_process=largest_process,
        largest_process_id=largest_process_id,
        server_process=server_process,
    )


def _fold_sizes(
    tracker: Tracker, folded: Tracker, reading: Reading, *, fresh_container: bool, capacity: CodeServerCapacity
) -> Tracker:
    """`folded` with the size fields of one reading.

    A process counts when it first passes the run budget. While its event is kept (the newest `MAX_OVER_EVENTS`),
    it does not count again, so two processes that overtake each other as the largest count once each."""
    run_limit, server_limit = capacity.run_mib * MIB, capacity.server_mib * MIB
    size, server = reading.largest_process or 0, reading.server_process or 0
    before_id = "" if fresh_container else tracker.process_id
    before_size = 0 if fresh_container else tracker.process_size
    over, worst_process = tracker.processes_over_size, tracker.worst_process
    if size > run_limit:
        worst_process = max(worst_process, size)  # a process that grows after it passed the budget raises the record
        named = any(
            event.container == reading.container and event.process_id == reading.largest_process_id
            for event in tracker.over_events
        )
        if not named and (reading.largest_process_id != before_id or before_size <= run_limit):
            over += 1
    server_over, worst_server = tracker.servers_over_size, tracker.worst_server
    if server > server_limit and (fresh_container or tracker.server_size <= server_limit):
        server_over, worst_server = server_over + 1, max(worst_server, server)
    return replace(
        folded,
        process_id=reading.largest_process_id,
        process_size=size,
        processes_over_size=over,
        server_size=server,
        servers_over_size=server_over,
        worst_process=worst_process,
        worst_server=worst_server,
        over_events=tracker.over_events,
        largest_process=size if fresh_container else max(tracker.largest_process, size),
    )


def fold(tracker: Tracker, reading: Reading, *, deployment: str, capacity: CodeServerCapacity) -> Tracker:
    """Add one reading to the totals. A reading with a missing fact returns `tracker` itself, unchanged."""
    if reading.oom_kill is None or reading.memory_peak is None or reading.container is None or reading.unreadable:
        return tracker
    previous = tracker.container
    fresh_container = previous is None or previous.id != reading.container
    kills, same, new = tracker.kills, tracker.restarts_same_deployment, tracker.restarts_new_deployment
    if previous is not None and not fresh_container:
        kills += max(reading.oom_kill - previous.oom_kill, 0)
        peak_is_new = reading.memory_peak > previous.peak
    else:
        # A new container starts new counters, so all of its kills and its whole peak came after the last sample.
        kills += reading.oom_kill
        peak_is_new = True
        if previous is not None and previous.deployment == deployment:
            same += 1
        elif previous is not None:
            new += 1
    over = tracker.peaks_over_limit
    if (
        peak_is_new
        and reading.memory_max is not None
        and reading.memory_peak > UTILIZATION_CEILING * reading.memory_max
    ):
        over += 1
    folded = Tracker(
        container=Container(
            id=reading.container, deployment=deployment, oom_kill=reading.oom_kill, peak=reading.memory_peak
        ),
        kills=kills,
        restarts_same_deployment=same,
        restarts_new_deployment=new,
        peaks_over_limit=over,
        sampled_at=tracker.sampled_at,
    )
    return _fold_sizes(tracker, folded, reading, fresh_container=fresh_container, capacity=capacity)


def _mib(value: int) -> int:
    return round(value / MIB)


def _ceiling_findings(reading: Reading, capacity: CodeServerCapacity) -> list[str]:
    """The real ceiling must exist and must not be below the ceiling that the run limit assumed."""
    if reading.unlimited:
        return ["memory.max is max: the container has no memory ceiling"]
    if reading.memory_max is not None and reading.memory_max < capacity.ceiling_mib * MIB:
        return [f"memory.max {_mib(reading.memory_max)} MiB is below the declared ceiling {capacity.ceiling_mib} MiB"]
    return []


def _state_findings(tracker: Tracker, baseline: Baseline | None) -> list[str]:
    """Totals only grow. A total below the last check's means the state was lost or reset."""
    if baseline is None:
        return []
    shrunk = (
        tracker.kills < baseline.kills
        or tracker.restarts_same_deployment < baseline.restarts_same_deployment
        or tracker.restarts_new_deployment < baseline.restarts_new_deployment
        or tracker.peaks_over_limit < baseline.peaks_over_limit
        or tracker.processes_over_size < baseline.processes_over_size
        or tracker.servers_over_size < baseline.servers_over_size
    )
    if shrunk:
        return ["the runtime shape state is lower than at the last check: it was reset, so kills may be unseen"]
    return []


#: The most jobs that the finding names. The row is cut at `nightly_verdicts.SUMMARY_LIMIT`.
MAX_NAMED_JOBS = 3


def _over_size_text(tracker: Tracker, count: int, capacity: CodeServerCapacity) -> str:
    """The finding for `count` run processes over the budget since the last check.

    The newest events name them. The count and the budget come first, the jobs follow grouped, so that a
    night with many events still fits the published summary. State from before the jobs were recorded, or a
    night with more counts than events, names fewer processes than it counts, and the text says so."""
    budget = capacity.run_mib
    events = tracker.over_events[-count:]
    unnamed = count - len(events)
    if not events:
        return f"{count} run process(es) passed the run budget {budget} MiB (size and job not recorded)"
    if len(events) == 1 and not unnamed:
        event = events[0]
        return (
            f"a run process held {round(event.size / MIB)} MiB (job {event.job}), above the run budget"
            f" {budget} MiB (1 time(s))"
        )
    by_job: dict[str, list[OverEvent]] = {}
    for event in events:
        by_job.setdefault(event.job, []).append(event)
    largest_first = sorted(by_job.items(), key=lambda item: -max(event.size for event in item[1]))
    named = ", ".join(
        f"{job} ({len(group)}x, up to {round(max(event.size for event in group) / MIB)} MiB)"
        for job, group in largest_first[:MAX_NAMED_JOBS]
    )
    rest = len(largest_first) - MAX_NAMED_JOBS
    more_jobs = f", and {rest} other job(s)" if rest > 0 else ""
    more_counts = f"; {unnamed} more not named" if unnamed else ""
    return f"run processes passed the run budget {budget} MiB ({count} time(s)): {named}{more_jobs}{more_counts}"


def judge(
    reading: Reading,
    *,
    tracker: Tracker,
    baseline: Baseline | None,
    capacity: CodeServerCapacity,
    problems: tuple[str, ...] = (),
) -> Judgement:
    """Judge the growth of the totals since the last check, and the facts of this reading.

    `tracker` already holds this reading. `problems` are findings of the caller (an unreadable state)."""
    red: list[str] = list(problems)
    if reading.unreadable:
        red.append(f"unreadable: {', '.join(reading.unreadable)}")
    red += _ceiling_findings(reading, capacity)
    red += _state_findings(tracker, baseline)

    before = baseline or _NO_CHECK_YET
    kills = max(tracker.kills - before.kills, 0)
    same = max(tracker.restarts_same_deployment - before.restarts_same_deployment, 0)
    new = max(tracker.restarts_new_deployment - before.restarts_new_deployment, 0)
    peaks = max(tracker.peaks_over_limit - before.peaks_over_limit, 0)
    too_big = max(tracker.processes_over_size - before.processes_over_size, 0)
    server_big = max(tracker.servers_over_size - before.servers_over_size, 0)
    facts: dict[str, int | float] = {
        "oom_kills": kills,
        "unplanned_restarts": same,
        "peaks_over_limit": peaks,
        "processes_over_size": too_big,
        "servers_over_size": server_big,
        "largest_process_mib": round(tracker.largest_process / MIB),
        "worst_process_mib": round(tracker.worst_process / MIB),
        "server_size_mib": round(tracker.server_size / MIB),
        "worst_server_mib": round(tracker.worst_server / MIB),
    }

    peak = ""
    if reading.memory_peak is not None and reading.memory_max is not None:
        share = reading.memory_peak / reading.memory_max
        facts["peak_share"] = round(share, 3)
        peak = f"peak {_mib(reading.memory_peak)} of {_mib(reading.memory_max)} MiB ({share:.0%})"
    if kills:
        red.append(f"{kills} OOM kill(s) since the last check")
    if same:
        red.append(
            f"the code server was replaced {same} time(s) without a deployment change (a crash, a host reboot or a restart by hand)"
        )
    if peaks:
        red.append(f"the peak rose above {UTILIZATION_CEILING:.0%} of memory.max {peaks} time(s); {peak}")
    if too_big:
        red.append(_over_size_text(tracker, too_big, capacity))
    if server_big:
        red.append(
            f"the code server held {round(tracker.worst_server / MIB)} MiB, above the declared"
            f" {capacity.server_mib} MiB ({server_big} time(s))"
        )

    # The verdict is published on /health: counts, sizes and job names only, never a path or an error text.
    if red:
        summary = "; ".join(red)
    else:
        summary = f"no OOM kill and no unplanned restart since the last check; {peak}"
        if new:
            summary += f"; replaced {new} time(s) by a new deployment"
    return Judgement(red=tuple(red), summary=summary, facts=facts)


def _load(instance: dg.DagsterInstance, key: str) -> str | None:
    return instance.run_storage.get_cursor_values({key}).get(key)


def load_tracker(instance: dg.DagsterInstance) -> Tracker | None:
    """The totals, or None when no sample was taken. Raises `StateUnreadable` for a value that is not a tracker."""
    raw = _load(instance, TRACKER_KEY)
    return None if raw is None else Tracker.from_json(raw)


def load_baseline(instance: dg.DagsterInstance) -> Baseline | None:
    """The last scheduled check, or None when none ran. Raises `StateUnreadable` for a value that is not a baseline."""
    raw = _load(instance, BASELINE_KEY)
    return None if raw is None else Baseline.from_json(raw)


#: The job of a process that no started run claims: a lookup that found nothing, or failed.
UNKNOWN_JOB = "unknown"
#: The engine event that the in-process executor writes into the event log of its run.
_ENGINE_PID = re.compile(r"\(pid: (\d+)\)")


def job_of_process(instance: dg.DagsterInstance, process_id: str) -> str:
    """The job whose started run executes in the process `process_id` (`pid:start ticks`), or `UNKNOWN_JOB`.

    The in-process executor logs `Executing steps in process (pid: N)` as an engine event of its run, so
    the pid of a run process names its run (a run that is started or canceling). The run monitor fails a
    run that a kill left started within about two minutes, so only a few runs are candidates. An event
    whose job is unknown is looked up again at every sample (30 s) while its process is the largest.
    A lookup that fails is logged and answers `UNKNOWN_JOB`: the sensor must not fail on it."""
    pid = process_id.split(":", 1)[0]
    if not pid.isdigit():
        return UNKNOWN_JOB
    try:
        records = instance.get_run_records(
            filters=dg.RunsFilter(statuses=[dg.DagsterRunStatus.STARTED, dg.DagsterRunStatus.CANCELING])
        )
        for record in records:
            run = record.dagster_run
            for entry in instance.all_logs(run.run_id, of_type=dg.DagsterEventType.ENGINE_EVENT):
                found = _ENGINE_PID.search(entry.dagster_event.message or "") if entry.dagster_event else None
                if found is not None and found.group(1) == pid:
                    return run.job_name
    except Exception:  # noqa: BLE001 - the sensor must not fail on a lookup that only adds a name
        log.exception("could not find the job of process %s", process_id)
    return UNKNOWN_JOB


def _name_over_events(instance: dg.DagsterInstance, before: Tracker, folded: Tracker, reading: Reading) -> Tracker:
    """`folded` with the job of each run process that passed the budget (one event per count).

    A new count adds an event with the job found now. The largest process, when it already has an event,
    raises the size of that event as it grows. An event with an unknown job is looked up again while its
    process is the largest, because the run may not have been started yet or the read may have failed once.
    A known job is never looked up again."""
    if folded is before or folded.container is None:
        return folded  # the fold took nothing from this reading, so the names must not change either
    events, process_id, size = folded.over_events, reading.largest_process_id, reading.largest_process or 0
    container = folded.container.id
    if folded.processes_over_size > before.processes_over_size:
        job = job_of_process(instance, process_id)
        return replace(folded, over_events=(*events, OverEvent(process_id, job, size, container))[-MAX_OVER_EVENTS:])
    index = next(
        (
            i
            for i in range(len(events) - 1, -1, -1)
            if (events[i].container, events[i].process_id) == (container, process_id)
        ),
        None,
    )
    if index is None:
        return folded
    event = events[index]
    job = event.job if event.job != UNKNOWN_JOB else job_of_process(instance, process_id)
    grown = max(event.size, size)
    if job == event.job and grown == event.size:
        return folded
    return replace(
        folded, over_events=(*events[:index], OverEvent(process_id, job, grown, container), *events[index + 1 :])
    )


def sample(
    instance: dg.DagsterInstance,
    *,
    deployment: str,
    capacity: CodeServerCapacity,
    reading: Reading | None = None,
    now: datetime | None = None,
) -> Tracker:
    """Fold the current reading into the stored totals and stamp the time. The only writer of the tracker.

    A stored value that is not a tracker restarts the totals; the next scheduled check sees totals below
    its baseline and goes red, so the loss is not hidden. A reading with a missing fact adds nothing but
    still stamps the time: the sensor ran."""
    try:
        tracker = load_tracker(instance) or Tracker()
    except StateUnreadable:
        log.warning("the runtime shape tracker is not readable; the totals restart")
        tracker = Tracker()
    current = read() if reading is None else reading
    folded = _name_over_events(
        instance, tracker, fold(tracker, current, deployment=deployment, capacity=capacity), current
    )
    folded = replace(folded, sampled_at=(now or datetime.now(UTC)).isoformat())
    instance.run_storage.set_cursor_values({TRACKER_KEY: folded.to_json()})
    return folded


def _sensor_findings(tracker: Tracker | None, now: datetime) -> list[str]:
    """The check depends on the sensor: no tracker, or an old last sample, means kills may be unseen."""
    if tracker is None or not tracker.sampled_at:
        return ["the sampler has recorded nothing, so kills and restarts may be unseen"]
    try:
        age = (now - datetime.fromisoformat(tracker.sampled_at)).total_seconds()
    except ValueError:
        return ["the time of the last sample is not readable, so kills and restarts may be unseen"]
    if age > STALE_AFTER_SECONDS:
        return [f"the last sample is {round(age / 60)} minutes old, so kills and restarts may be unseen"]
    return []


def _later_than(tick: str, baseline_tick: str) -> bool:
    """Whether `tick` is a later time than `baseline_tick`. An unreadable baseline tick is older than any tick."""
    try:
        return datetime.fromisoformat(tick) > datetime.fromisoformat(baseline_tick)
    except ValueError:
        return True


def check(
    instance: dg.DagsterInstance,
    *,
    tick: str | None,
    capacity: CodeServerCapacity,
    reading: Reading | None = None,
    now: datetime | None = None,
) -> Judgement:
    """The nightly judgement of the stored totals. The only writer of the baseline.

    Only a run with a `tick` moves the baseline, and only to a later tick. A repeat of the latest tick repeats
    its verdict; an older tick is judged and saves nothing."""
    reading = read() if reading is None else reading
    now = now or datetime.now(UTC)
    problems: list[str] = []
    try:
        tracker = load_tracker(instance)
    except StateUnreadable:
        problems.append("the runtime shape tracker is not readable")
        tracker = None
    try:
        baseline = load_baseline(instance)
    except StateUnreadable:
        problems.append("the runtime shape baseline is not readable")
        baseline = None
    if tick is not None and baseline is not None and baseline.tick == tick:
        return Judgement(red=baseline.red, summary=baseline.summary)

    problems += _sensor_findings(tracker, now)
    totals = tracker or Tracker()
    judgement = judge(reading, tracker=totals, baseline=baseline, capacity=capacity, problems=tuple(problems))
    if tick is not None and (baseline is None or _later_than(tick, baseline.tick)):
        saved = Baseline(
            kills=totals.kills,
            restarts_same_deployment=totals.restarts_same_deployment,
            restarts_new_deployment=totals.restarts_new_deployment,
            peaks_over_limit=totals.peaks_over_limit,
            processes_over_size=totals.processes_over_size,
            servers_over_size=totals.servers_over_size,
            tick=tick,
            red=judgement.red,
            summary=judgement.summary,
        )
        instance.run_storage.set_cursor_values({BASELINE_KEY: saved.to_json()})
    return judgement
