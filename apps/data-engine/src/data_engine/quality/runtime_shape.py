"""The runtime shape of the Dagster code server: did its memory cgroup hold the load? (#1467, #28 V5)

Every Dagster run executes inside the code server container, in the same memory cgroup as the
gRPC server. On 2026-10-20 the kernel killed processes of that cgroup five times. Each kill left
a run `STARTED` for ever, and no check had read the cgroup. This module reads these facts from
inside the container and judges them against the capacity that `code_server_capacity` declares in
`environment_contract.v1.json`:

* `memory.events` `oom_kill`: the kernel killed a process of the cgroup;
* `memory.peak`: the most memory the cgroup held since the container started;
* `memory.max`: the ceiling that the container really has;
* the identity of the container: the boot id of the host and the start ticks of its first process.

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
* the peak of a container rose above `utilization_limit` of `memory.max` since the last check. A
  peak that the last check already judged does not count again: `memory.peak` is a high-water mark
  of the whole container life;
* the container was replaced without a change of the deployment. The kernel may kill the server
  process itself. The container then restarts with a new cgroup, and the counter of the new cgroup
  does not show the kill. A deploy replaces the container too, but it changes the deployment.

Only a tick-tagged run (the schedule) moves the baseline, and only forward. A manual run shows the
growth since the last scheduled check and consumes nothing. A re-execution of the latest tick returns
the verdict that the tick already has; a re-execution of an older tick judges and saves nothing.

`memory.peak` counts page cache that the cgroup holds. The summary shows counts and sizes only,
because the verdict is published on `/health`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

import dagster as dg
from truealpha_contracts.environment_contract import CodeServerCapacity

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
SAMPLE_INTERVAL_SECONDS = 300
STALE_AFTER_SECONDS = 3 * SAMPLE_INTERVAL_SECONDS


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

    @property
    def unreadable(self) -> tuple[str, ...]:
        """The names of the facts that are missing. A reading with a missing fact is never green."""
        missing = (
            ("memory.max", self.memory_max is None and not self.unlimited),
            ("memory.peak", self.memory_peak is None),
            ("memory.events", self.oom_kill is None),
            ("container identity", self.container is None),
        )
        return tuple(name for name, is_missing in missing if is_missing)


@dataclass(frozen=True)
class Container:
    """The container that the last sample saw, with the counters it showed."""

    id: str
    deployment: str
    oom_kill: int
    peak: int


@dataclass(frozen=True)
class Tracker:
    """Totals over the life of all containers. Written by the sampler only."""

    container: Container | None = None
    kills: int = 0
    restarts_same_deployment: int = 0
    restarts_new_deployment: int = 0
    peaks_over_limit: int = 0
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


def _container_id(proc_root: Path) -> str:
    """Boot id of the host plus the start ticks of the first process: neither depends on the wall clock.

    The process name in `stat` may hold spaces and brackets, so the fields count from the last `)`."""
    boot_id = _read_text(proc_root / "sys" / "kernel" / "random" / "boot_id")
    stat = _read_text(proc_root / "1" / "stat")
    fields_after_name = stat[stat.rindex(")") + 2 :].split()
    start_ticks = int(fields_after_name[19])  # field 22 of the line, counting the pid as field 1
    if not boot_id:
        raise ValueError("empty boot id")
    return f"{boot_id}:{start_ticks}"


def read(cgroup_root: Path = CGROUP_ROOT, proc_root: Path = PROC_ROOT) -> Reading:
    """Read the cgroup and the process table. A file that fails to read leaves its field None."""
    memory_max: int | None = None
    unlimited = False
    memory_peak: int | None = None
    oom_kill: int | None = None
    container: str | None = None

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

    return Reading(
        memory_max=memory_max, unlimited=unlimited, memory_peak=memory_peak, oom_kill=oom_kill, container=container
    )


def fold(tracker: Tracker, reading: Reading, *, deployment: str, capacity: CodeServerCapacity) -> Tracker:
    """Add one reading to the totals. A reading with a missing fact adds nothing."""
    if reading.oom_kill is None or reading.memory_peak is None or reading.container is None or reading.unreadable:
        return tracker
    previous = tracker.container
    kills, same, new = tracker.kills, tracker.restarts_same_deployment, tracker.restarts_new_deployment
    if previous is not None and previous.id == reading.container:
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
        and reading.memory_peak > capacity.utilization_limit * reading.memory_max
    ):
        over += 1
    return Tracker(
        container=Container(
            id=reading.container, deployment=deployment, oom_kill=reading.oom_kill, peak=reading.memory_peak
        ),
        kills=kills,
        restarts_same_deployment=same,
        restarts_new_deployment=new,
        peaks_over_limit=over,
    )


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
    )
    if shrunk:
        return ["the runtime shape state is lower than at the last check: it was reset, so kills may be unseen"]
    return []


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
    facts: dict[str, int | float] = {"oom_kills": kills, "unplanned_restarts": same, "peaks_over_limit": peaks}

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
        red.append(f"the peak rose above {capacity.utilization_limit:.0%} of memory.max {peaks} time(s); {peak}")

    # The verdict is published on /health: counts and sizes only, never a path or an error text.
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
    folded = fold(tracker, read() if reading is None else reading, deployment=deployment, capacity=capacity)
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
            tick=tick,
            red=judgement.red,
            summary=judgement.summary,
        )
        instance.run_storage.set_cursor_values({BASELINE_KEY: saved.to_json()})
    return judgement
