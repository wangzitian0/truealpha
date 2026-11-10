# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from __future__ import annotations; import json; import logging; import re; from collections.abc import Callable, Mapping; from dataclasses import asdict, dataclass, field, replace; from datetime import UTC, datetime; from pathlib import Path; import dagster as dg; from truealpha_contracts.environment_contract import UTILIZATION_CEILING, CodeServerCapacity; log = logging.getLogger(__name__); CGROUP_ROOT, PROC_ROOT = Path("/sys/fs/cgroup"), Path("/proc"); TRACKER_KEY, BASELINE_KEY = "truealpha/runtime_shape/tracker", "truealpha/runtime_shape/baseline"; MIB, SAMPLE_INTERVAL_SECONDS, STALE_AFTER_SECONDS = 1024 * 1024, 30, 600; MAX_OVER_EVENTS, MAX_NAMED_JOBS, UNKNOWN_JOB = 8, 3, "unknown"; _ENGINE_PID = re.compile(r"\(pid: (\d+)\)")
class StateUnreadable(Exception): pass
def _text(value: object) -> str:
    if not isinstance(value, str): raise ValueError(f"not a text: {value!r}")
    return value
def _texts(value: object) -> tuple[str, ...]:
    if not isinstance(value, list): raise ValueError(f"not a list: {value!r}")
    return tuple(_text(item) for item in value)
def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0: raise ValueError(f"not a count: {value!r}")
    return value
def _over_events(value: object) -> tuple[OverEvent, ...]:
    if not isinstance(value, list): raise ValueError(f"not a list: {value!r}")
    return tuple( OverEvent(_text(i["process_id"]), _text(i["job"]), _count(i["size"]), _text(i.get("container", ""))) for i in value )
@dataclass(frozen=True)
class Reading:
    memory_max: int | None = None; unlimited: bool = False; memory_peak: int | None = None; swap_peak: int | None = None; oom_kill: int | None = None; max_events: int | None = None; container: str | None = None; largest_process: int | None = None; largest_process_id: str = ""; server_process: int | None = None
    @property
    def unreadable(self) -> tuple[str, ...]: missing = ( ("memory.max", self.memory_max is None and not self.unlimited), ("memory.peak", self.memory_peak is None), ("memory.swap.peak", self.swap_peak is None), ("memory.events", self.oom_kill is None or self.max_events is None), ("container identity", self.container is None), ("process sizes", self.largest_process is None or self.server_process is None), ); return tuple(name for name, is_missing in missing if is_missing)
@dataclass(frozen=True)
class Container: id: str; deployment: str; oom_kill: int; peak: int; swap_peak: int = 0; max_events: int = 0
@dataclass(frozen=True)
class OverEvent: process_id: str; job: str; size: int; container: str = ""
_TRACKER_REQ_COUNTS = ("kills", "restarts_same_deployment", "restarts_new_deployment", "peaks_over_limit"); _TRACKER_OPT_COUNTS = ( "swap_peaks", "worst_swap", "process_size", "processes_over_size", "server_size", "servers_over_size", "largest_process", "worst_process", "worst_server", )
@dataclass(frozen=True)
class Tracker:
    container: Container | None = None; kills: int = 0; restarts_same_deployment: int = 0; restarts_new_deployment: int = 0; peaks_over_limit: int = 0; swap_peaks: int = 0; worst_swap: int = 0; process_id: str = ""; process_size: int = 0; processes_over_size: int = 0; server_size: int = 0; servers_over_size: int = 0; largest_process: int = 0; worst_process: int = 0; worst_server: int = 0; over_events: tuple[OverEvent, ...] = (); sampled_at: str = ""
    def to_json(self) -> str: return json.dumps(asdict(self), sort_keys=True)
    @classmethod
    def from_json(cls, raw: str) -> Tracker:
        try: d = json.loads(raw); c = d["container"]; container = ( None if c is None else Container( _text(c["id"]), _text(c["deployment"]), _count(c["oom_kill"]), _count(c["peak"]), _count(c.get("swap_peak", 0)), _count(c.get("max_events", 0)), ) ); counts = {k: _count(d[k]) for k in _TRACKER_REQ_COUNTS} | { k: _count(d.get(k, 0)) for k in _TRACKER_OPT_COUNTS }; return cls( container=container, process_id=_text(d.get("process_id", "")), over_events=_over_events(d.get("over_events", [])), sampled_at=_text(d["sampled_at"]), **counts, )
        except (ValueError, KeyError, TypeError, OverflowError) as exc: raise StateUnreadable("the runtime shape tracker is not readable") from exc
@dataclass(frozen=True)
class Baseline:
    kills: int; restarts_same_deployment: int; restarts_new_deployment: int; peaks_over_limit: int; tick: str; red: tuple[str, ...]; summary: str; processes_over_size: int = 0; servers_over_size: int = 0; swap_peaks: int = 0
    def to_json(self) -> str: return json.dumps(asdict(self), sort_keys=True)
    @classmethod
    def from_json(cls, raw: str) -> Baseline:
        try:
            d = json.loads(raw); counts = {k: _count(d[k]) for k in _TRACKER_REQ_COUNTS}
            for k in ("processes_over_size", "servers_over_size", "swap_peaks"): counts[k] = _count(d.get(k, 0))
            return cls(tick=_text(d["tick"]), red=_texts(d["red"]), summary=_text(d["summary"]), **counts)
        except (ValueError, KeyError, TypeError, OverflowError) as exc: raise StateUnreadable("the runtime shape baseline is not readable") from exc
_NO_CHECK_YET = Baseline(0, 0, 0, 0, "", (), "")
@dataclass(frozen=True)
class Judgement: red: tuple[str, ...]; summary: str; facts: Mapping[str, int | float] = field(default_factory=dict)
def _read_text(path: Path) -> str: return path.read_text(encoding="utf-8").strip()
def _events(text: str) -> dict[str, int]: return {name: int(val) for name, val in (line.split() for line in text.splitlines() if line.strip())}
def _start_ticks(stat: str) -> int: return int(stat[stat.rindex(")") + 2 :].split()[19])
def _container_id(proc_root: Path) -> str:
    if not (boot_id := _read_text(proc_root / "sys" / "kernel" / "random" / "boot_id")): raise ValueError("empty boot id")
    return f"{boot_id}:{_start_ticks(_read_text(proc_root / '1' / 'stat'))}"
def _hwm_bytes(status: str) -> int | None: return next((int(ln.split()[1]) * 1024 for ln in status.splitlines() if ln.startswith("VmHWM:")), None)
def _largest_process(proc_root: Path) -> tuple[int, str]:
    largest, identity = 0, ""
    for entry in proc_root.iterdir():
        if not entry.name.isdigit() or entry.name == "1": continue
        try: size = _hwm_bytes((entry / "status").read_bytes().decode("utf-8", errors="replace")); ticks = _start_ticks((entry / "stat").read_bytes().decode("utf-8", errors="replace")) if size else 0
        except (OSError, ValueError, IndexError): continue
        if size is not None and size > largest: largest, identity = size, f"{entry.name}:{ticks}"
    return largest, identity
def _server_process(proc_root: Path) -> int:
    if b"dagster" not in (proc_root / "1" / "cmdline").read_bytes(): raise ValueError("the first process is not the code server")
    if (size := _hwm_bytes((proc_root / "1" / "status").read_bytes().decode("utf-8", errors="replace"))) is None: raise ValueError("the first process has no VmHWM")
    return size
def _try[T, D](fn: Callable[[], T], default: D) -> T | D:
    try: return fn()
    except (OSError, ValueError, KeyError, IndexError): return default
def read(cgroup_root: Path = CGROUP_ROOT, proc_root: Path = PROC_ROOT) -> Reading: raw_max = _try(lambda: _read_text(cgroup_root / "memory.max"), None); unlimited = raw_max == "max"; memory_max = None if raw_max is None or unlimited else _try(lambda: int(raw_max or ""), None); oom_kill, max_events = _try( lambda: (_events(_read_text(cgroup_root / "memory.events"))["oom_kill"], _events(_read_text(cgroup_root / "memory.events"))["max"]), (None, None), ); largest_process, largest_process_id = _try(lambda: _largest_process(proc_root), (None, "")); return Reading( memory_max=memory_max, unlimited=unlimited, memory_peak=_try(lambda: int(_read_text(cgroup_root / "memory.peak")), None), swap_peak=_try(lambda: int(_read_text(cgroup_root / "memory.swap.peak")), None), oom_kill=oom_kill, max_events=max_events, container=_try(lambda: _container_id(proc_root), None), largest_process=largest_process, largest_process_id=largest_process_id, server_process=_try(lambda: _server_process(proc_root), None), )
def _fold_sizes( tracker: Tracker, folded: Tracker, reading: Reading, *, fresh_container: bool, capacity: CodeServerCapacity ) -> Tracker:
    run_limit, server_limit = capacity.run_mib * MIB, capacity.server_mib * MIB; size, server = reading.largest_process or 0, reading.server_process or 0; before_id, before_size = ("", 0) if fresh_container else (tracker.process_id, tracker.process_size); over, worst_process = tracker.processes_over_size, tracker.worst_process
    if size > run_limit:
        worst_process = max(worst_process, size); named = any( (e.container, e.process_id) == (reading.container, reading.largest_process_id) for e in tracker.over_events )
        if not named and (reading.largest_process_id != before_id or before_size <= run_limit): over += 1
    server_over, worst_server = tracker.servers_over_size, tracker.worst_server
    if server > server_limit and (fresh_container or tracker.server_size <= server_limit): server_over, worst_server = server_over + 1, max(worst_server, server)
    return replace( folded, process_id=reading.largest_process_id, process_size=size, processes_over_size=over, server_size=server, servers_over_size=server_over, worst_process=worst_process, worst_server=worst_server, over_events=tracker.over_events, largest_process=size if fresh_container else max(tracker.largest_process, size), )
def fold(tracker: Tracker, reading: Reading, *, deployment: str, capacity: CodeServerCapacity) -> Tracker:
    if ( reading.oom_kill is None or reading.memory_peak is None or reading.swap_peak is None or reading.max_events is None or reading.container is None or reading.unreadable ): return tracker
    previous = tracker.container; fresh_container = previous is None or previous.id != reading.container; kills, same, new = tracker.kills, tracker.restarts_same_deployment, tracker.restarts_new_deployment
    if previous is not None and not fresh_container: kills += max(reading.oom_kill - previous.oom_kill, 0); peak_is_new = reading.memory_peak > previous.peak; swap_before, max_before = previous.swap_peak, previous.max_events
    else:
        kills += reading.oom_kill; peak_is_new, swap_before, max_before = True, 0, 0
        if previous is not None: same, new = (same + 1, new) if previous.deployment == deployment else (same, new + 1)
    over = tracker.peaks_over_limit + int( bool( peak_is_new and reading.memory_max is not None and reading.memory_peak > UTILIZATION_CEILING * reading.memory_max ) ); swaps, worst_swap = tracker.swap_peaks, tracker.worst_swap
    if reading.swap_peak > swap_before and reading.max_events > max_before: swaps, worst_swap = swaps + 1, max(worst_swap, reading.swap_peak)
    folded = Tracker( container=Container( reading.container, deployment, reading.oom_kill, reading.memory_peak, reading.swap_peak, reading.max_events ), kills=kills, restarts_same_deployment=same, restarts_new_deployment=new, peaks_over_limit=over, swap_peaks=swaps, worst_swap=worst_swap, sampled_at=tracker.sampled_at, ); return _fold_sizes(tracker, folded, reading, fresh_container=fresh_container, capacity=capacity)
def _mib(value: int) -> int: return round(value / MIB)
def _ceiling_findings(reading: Reading, capacity: CodeServerCapacity) -> list[str]:
    if reading.unlimited: return ["memory.max is max: the container has no memory ceiling"]
    if reading.memory_max is not None and reading.memory_max < capacity.ceiling_mib * MIB: return [f"memory.max {_mib(reading.memory_max)} MiB is below the declared ceiling {capacity.ceiling_mib} MiB"]
    return []
def _state_findings(tracker: Tracker, baseline: Baseline | None) -> list[str]:
    if baseline is None: return []
    fields = (*_TRACKER_REQ_COUNTS, "swap_peaks", "processes_over_size", "servers_over_size")
    if any(getattr(tracker, f) < getattr(baseline, f) for f in fields): return ["the runtime shape state is lower than at the last check: it was reset, so kills may be unseen"]
    return []
def _over_size_text(tracker: Tracker, count: int, capacity: CodeServerCapacity) -> str:
    budget, events = capacity.run_mib, tracker.over_events[-count:]; unnamed = count - len(events)
    if not events: return f"{count} run process(es) passed the run budget {budget} MiB (size and job not recorded)"
    if len(events) == 1 and not unnamed: ev = events[0]; return f"a run process held {round(ev.size / MIB)} MiB (job {ev.job}), above the run budget {budget} MiB (1 time(s))"
    by_job: dict[str, list[OverEvent]] = {}
    for ev in events: by_job.setdefault(ev.job, []).append(ev)
    largest_first = sorted(by_job.items(), key=lambda item: -max(e.size for e in item[1])); named = ", ".join( f"{job} ({len(group)}x, up to {round(max(e.size for e in group) / MIB)} MiB)" for job, group in largest_first[:MAX_NAMED_JOBS] ); rest = len(largest_first) - MAX_NAMED_JOBS; more_jobs = f", and {rest} other job(s)" if rest > 0 else ""; more_counts = f"; {unnamed} more not named" if unnamed else ""; return f"run processes passed the run budget {budget} MiB ({count} time(s)): {named}{more_jobs}{more_counts}"
def judge( reading: Reading, *, tracker: Tracker, baseline: Baseline | None, capacity: CodeServerCapacity, problems: tuple[str, ...] = (), ) -> Judgement:
    red: list[str] = list(problems)
    if reading.unreadable: red.append(f"unreadable: {', '.join(reading.unreadable)}")
    red += _ceiling_findings(reading, capacity) + _state_findings(tracker, baseline); before = baseline or _NO_CHECK_YET; kills = max(tracker.kills - before.kills, 0); same = max(tracker.restarts_same_deployment - before.restarts_same_deployment, 0); new = max(tracker.restarts_new_deployment - before.restarts_new_deployment, 0); peaks = max(tracker.peaks_over_limit - before.peaks_over_limit, 0); swaps = max(tracker.swap_peaks - before.swap_peaks, 0); too_big = max(tracker.processes_over_size - before.processes_over_size, 0); server_big = max(tracker.servers_over_size - before.servers_over_size, 0); facts: dict[str, int | float] = { "oom_kills": kills, "unplanned_restarts": same, "peaks_over_limit": peaks, "swap_peaks": swaps, "worst_swap_mib": _mib(tracker.worst_swap), "processes_over_size": too_big, "servers_over_size": server_big, "largest_process_mib": _mib(tracker.largest_process), "worst_process_mib": _mib(tracker.worst_process), "server_size_mib": _mib(tracker.server_size), "worst_server_mib": _mib(tracker.worst_server), }; peak = ""
    if reading.memory_peak is not None and reading.memory_max is not None: share = reading.memory_peak / reading.memory_max; facts["peak_share"] = round(share, 3); peak = f"peak {_mib(reading.memory_peak)} of {_mib(reading.memory_max)} MiB ({share:.0%})"
    if kills: red.append(f"{kills} OOM kill(s) since the last check")
    if same: red.append( f"the code server was replaced {same} time(s) without a deployment change (a crash, a host reboot or a restart by hand)" )
    if peaks: red.append(f"the peak rose above {UTILIZATION_CEILING:.0%} of memory.max {peaks} time(s); {peak}")
    if swaps: red.append( f"swap peak rose at memory.max {swaps} time(s); largest swap peak {_mib(tracker.worst_swap)} MiB: the processes needed more than memory.max" )
    if too_big: red.append(_over_size_text(tracker, too_big, capacity))
    if server_big: red.append( f"the code server held {_mib(tracker.worst_server)} MiB, above the declared {capacity.server_mib} MiB ({server_big} time(s))" )
    if red: summary = "; ".join(red)
    else: summary = f"no OOM kill and no unplanned restart since the last check; {peak}" + ( f"; replaced {new} time(s) by a new deployment" if new else "" )
    return Judgement(red=tuple(red), summary=summary, facts=facts)
def _load(instance: dg.DagsterInstance, key: str) -> str | None: return instance.run_storage.get_cursor_values({key}).get(key)
def load_tracker(instance: dg.DagsterInstance) -> Tracker | None: return None if (raw := _load(instance, TRACKER_KEY)) is None else Tracker.from_json(raw)
def load_baseline(instance: dg.DagsterInstance) -> Baseline | None: return None if (raw := _load(instance, BASELINE_KEY)) is None else Baseline.from_json(raw)
def job_of_process(instance: dg.DagsterInstance, process_id: str) -> str:
    if not (pid := process_id.split(":", 1)[0]).isdigit(): return UNKNOWN_JOB
    try:
        records = instance.get_run_records( filters=dg.RunsFilter(statuses=[dg.DagsterRunStatus.STARTED, dg.DagsterRunStatus.CANCELING]) )
        for record in records:
            run = record.dagster_run
            for entry in instance.all_logs(run.run_id, of_type=dg.DagsterEventType.ENGINE_EVENT):
                found = _ENGINE_PID.search(entry.dagster_event.message or "") if entry.dagster_event else None
                if found is not None and found.group(1) == pid: return run.job_name
    except Exception: log.exception("could not find the job of process %s", process_id)
    return UNKNOWN_JOB
def _name_over_events(instance: dg.DagsterInstance, before: Tracker, folded: Tracker, reading: Reading) -> Tracker:
    if folded is before or folded.container is None: return folded
    events, process_id, size, container = ( folded.over_events, reading.largest_process_id, reading.largest_process or 0, folded.container.id, )
    if folded.processes_over_size > before.processes_over_size: return replace( folded, over_events=(*events, OverEvent(process_id, job_of_process(instance, process_id), size, container))[ -MAX_OVER_EVENTS: ], )
    index = next( ( i for i in range(len(events) - 1, -1, -1) if (events[i].container, events[i].process_id) == (container, process_id) ), None, )
    if index is None: return folded
    ev = events[index]; job, grown = ev.job if ev.job != UNKNOWN_JOB else job_of_process(instance, process_id), max(ev.size, size); return ( folded if (job, grown) == (ev.job, ev.size) else replace( folded, over_events=(*events[:index], OverEvent(process_id, job, grown, container), *events[index + 1 :]) ) )
def sample( instance: dg.DagsterInstance, *, deployment: str, capacity: CodeServerCapacity, reading: Reading | None = None, now: datetime | None = None, ) -> Tracker:
    try: tracker = load_tracker(instance) or Tracker()
    except StateUnreadable: log.warning("the runtime shape tracker is not readable; the totals restart"); tracker = Tracker()
    current = read() if reading is None else reading; folded = _name_over_events( instance, tracker, fold(tracker, current, deployment=deployment, capacity=capacity), current ); folded = replace(folded, sampled_at=(now or datetime.now(UTC)).isoformat()); instance.run_storage.set_cursor_values({TRACKER_KEY: folded.to_json()}); return folded
def _sensor_findings(tracker: Tracker | None, now: datetime) -> list[str]:
    if tracker is None or not tracker.sampled_at: return ["the sampler has recorded nothing, so kills and restarts may be unseen"]
    try: age = (now - datetime.fromisoformat(tracker.sampled_at)).total_seconds()
    except ValueError: return ["the time of the last sample is not readable, so kills and restarts may be unseen"]
    return ( [f"the last sample is {round(age / 60)} minutes old, so kills and restarts may be unseen"] if age > STALE_AFTER_SECONDS else [] )
def _later_than(tick: str, baseline_tick: str) -> bool:
    try: return datetime.fromisoformat(tick) > datetime.fromisoformat(baseline_tick)
    except ValueError: return True
def check( instance: dg.DagsterInstance, *, tick: str | None, capacity: CodeServerCapacity, reading: Reading | None = None, now: datetime | None = None, ) -> Judgement:
    reading, now, problems = read() if reading is None else reading, now or datetime.now(UTC), []
    try: tracker = load_tracker(instance)
    except StateUnreadable: problems.append("the runtime shape tracker is not readable"); tracker = None
    try: baseline = load_baseline(instance)
    except StateUnreadable: problems.append("the runtime shape baseline is not readable"); baseline = None
    if tick is not None and baseline is not None and baseline.tick == tick: return Judgement(red=baseline.red, summary=baseline.summary)
    problems += _sensor_findings(tracker, now); totals = tracker or Tracker(); judgement = judge(reading, tracker=totals, baseline=baseline, capacity=capacity, problems=tuple(problems))
    if tick is not None and (baseline is None or _later_than(tick, baseline.tick)): saved = Baseline( totals.kills, totals.restarts_same_deployment, totals.restarts_new_deployment, totals.peaks_over_limit, tick, judgement.red, judgement.summary, totals.processes_over_size, totals.servers_over_size, totals.swap_peaks, ); instance.run_storage.set_cursor_values({BASELINE_KEY: saved.to_json()})
    return judgement
