# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from __future__ import annotations; from collections.abc import Callable, Iterable, Mapping; from dataclasses import dataclass; from datetime import UTC, datetime, timedelta; from typing import Any; import dagster as dg; import psycopg; from data_engine.config import settings; from data_engine.datahub.production_topt.source_registrations import REGISTRATIONS, SourceRegistration; from data_engine.datahub.production_topt.universe_plane import UNIVERSE_SOURCES; from data_engine.lanes.capture import TICK_BY_JOB; from data_engine.lanes.quality import UNFINISHED_RUN_STATUSES; from data_engine.lanes.triggers import BOOT_CANARY_TAG; SCHEDULE_TAG = "dagster/schedule_name"; CAPTURE_RUN_METADATA = "capture_run_id"; OK, PENDING, RED = "ok", "pending", "red"; MAX_PROVING_WINDOW_HOURS = 26.0; ORIGIN_ENABLED: Mapping[str, Callable[[], bool]] = { "yahoo-chart:v1": lambda: True, "twelve-data:v1": lambda: bool(settings.twelve_data_api_key), "moomoo-kline:v1": lambda: settings.moomoo_kline_origin_enabled, "moomoo-financials:v1": lambda: settings.moomoo_financials_origin_enabled, }; _FINISHED_RED = (dg.DagsterRunStatus.FAILURE, dg.DagsterRunStatus.CANCELED)
@dataclass(frozen=True)
class Proof:
    state: str; summary: str
    @property
    def ok(self) -> bool | None: return None if self.state == PENDING else self.state == OK
def _vendor_registrations() -> Iterable[SourceRegistration]: return (registration for registration in REGISTRATIONS if registration.corroboration_class != "release")
def _primary(registration: SourceRegistration) -> str: return f"{registration.source_id}:{registration.version}"
def origin_of(semantic_type: str, parser_version: str) -> str | None:
    for registration in _vendor_registrations():
        if semantic_type not in registration.semantic_types: continue
        for origin in registration.origins:
            if parser_version in origin.parser_versions: return origin.origin_source
        return _primary(registration)
    return None
def expected_origins() -> frozenset[str]:
    expected: set[str] = set()
    for registration in _vendor_registrations():
        expected.add(_primary(registration))
        for origin in registration.origins:
            enabled = ORIGIN_ENABLED.get(origin.origin_source)
            if enabled is None: raise LookupError(f"origin {origin.origin_source} declares no switch in ORIGIN_ENABLED")
            if enabled(): expected.add(origin.origin_source)
    return frozenset(expected)
FETCHED_SQL = "select observation.semantic_type, observation.parser_version, count(*) from raw.capture_obligations obligation join raw.capture_obligation_results result on result.capture_obligation_id = obligation.obligation_id join raw.capture_attempt_results attempt on attempt.attempt_id = result.final_attempt_id join staging.capture_observation_obligations link on link.capture_obligation_id = obligation.obligation_id join staging.capture_normalized_observations observation on observation.observation_id = link.observation_id where obligation.run_id = %s and attempt.source_vintage_id is not null group by observation.semantic_type, observation.parser_version"; UNIVERSE_CONSTITUENTS_SQL = "select etf_symbol, count(*) from staging.etf_constituent_facts where (etf_symbol, as_of, knowable_at) in ( select etf_symbol, as_of, max(knowable_at) from staging.etf_constituent_facts where (etf_symbol, as_of) in ( select etf_symbol, max(as_of) from staging.etf_constituent_facts group by etf_symbol ) group by etf_symbol, as_of ) group by etf_symbol"; IDENTITY_SQL = "select image_digest from mart.data_engine_identity where run_id = %s"
def expected_universe_sources() -> frozenset[str]: return frozenset(UNIVERSE_SOURCES)
def fetched_by_universe_source(connection: psycopg.Connection[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for etf_symbol, count in connection.execute(UNIVERSE_CONSTITUENTS_SQL, ()).fetchall(): counts[str(etf_symbol)] = int(count)
    return counts
def fetched_by_origin(connection: psycopg.Connection[Any], capture_run_id: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for semantic_type, parser_version, count in connection.execute(FETCHED_SQL, (capture_run_id,)).fetchall():
        origin = origin_of(str(semantic_type), str(parser_version))
        if origin is not None: counts[origin] = counts.get(origin, 0) + int(count)
    return counts
def _as_utc(stamp: datetime | float | int) -> datetime:
    if isinstance(stamp, (int, float)): return datetime.fromtimestamp(stamp, tz=UTC)
    if isinstance(stamp, datetime): return stamp.astimezone(UTC) if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)
    raise TypeError(f"expected datetime or numeric timestamp, got {type(stamp)}")
def deployment_started_at(instance: dg.DagsterInstance, digest: str) -> datetime | None: records = instance.get_run_records(filters=dg.RunsFilter(tags={BOOT_CANARY_TAG: digest}), limit=1); return _as_utc(records[0].create_timestamp) if records else None
def forced(run: dg.DagsterRun) -> bool:
    tick = TICK_BY_JOB.get(run.job_name)
    if tick is None: return False
    run_config = run.run_config if isinstance(run.run_config, dict) else {}; ops = run_config.get("ops"); op_config = ops.get(tick.op_name) if isinstance(ops, dict) else {}; config = op_config.get("config") if isinstance(op_config, dict) else {}; return bool(config.get("force_fetch") if isinstance(config, dict) else False)
def fetching_runs(instance: dg.DagsterInstance, since: datetime) -> list[dg.RunRecord]:
    candidates: list[dg.RunRecord] = []
    for job_name in TICK_BY_JOB: records = instance.get_run_records( filters=dg.RunsFilter(job_name=job_name, created_after=since - timedelta(seconds=1)) ); candidates.extend( record for record in records if _as_utc(record.create_timestamp) >= since and (record.dagster_run.tags.get(SCHEDULE_TAG) or forced(record.dagster_run)) )
    candidates.sort(key=lambda record: _as_utc(record.create_timestamp)); return candidates
def capture_run_of(instance: dg.DagsterInstance, run_id: str) -> str | None:
    records = instance.get_records_for_run(run_id=run_id, of_type=dg.DagsterEventType.STEP_OUTPUT).records
    for record in records:
        event = record.event_log_entry.dagster_event; output = event.step_output_data if event is not None else None; metadata = getattr(output, "metadata", None) or {}; value = metadata.get(CAPTURE_RUN_METADATA); text = getattr(value, "value", value)
        if text: return str(text)
    return None
def _label(record: dg.RunRecord) -> str: run = record.dagster_run; how = "forced" if forced(run) else "scheduled"; return f"{how} {run.job_name} run {run.run_id[:8]}"
def _evaluate_run( connection: psycopg.Connection[Any], instance: dg.DagsterInstance, record: dg.RunRecord, *, digest: str, short: str ) -> Proof:
    label = _label(record); status = record.dagster_run.status
    if status in _FINISHED_RED: return Proof(RED, f"{label} ended {status.value.lower()} on {short}…")
    if status in UNFINISHED_RUN_STATUSES: return Proof(PENDING, f"{label} is {status.value.lower().replace('_', ' ')}")
    if status != dg.DagsterRunStatus.SUCCESS: return Proof(PENDING, f"{label} is {status.value.lower().replace('_', ' ')}")
    capture_run_id = capture_run_of(instance, record.dagster_run.run_id)
    if capture_run_id is None: return Proof(RED, f"{label} succeeded but names no capture run")
    row = connection.execute(IDENTITY_SQL, (capture_run_id,)).fetchone()
    if row is None or str(row[0]) != digest: stamped = "no build" if row is None else str(row[0])[:19] + "…"; return Proof(RED, f"{label}: its capture is stamped by {stamped}, not {short}…")
    fetched = fetched_by_origin(connection, capture_run_id); expected = expected_origins()
    if not expected: return Proof(RED, f"{label}: no expected origins configured")
    counts = ", ".join(f"{origin} {fetched.get(origin, 0)}" for origin in sorted(expected)); missing = sorted(origin for origin in expected if fetched.get(origin, 0) <= 0)
    if missing: return Proof(RED, f"{label} fetched nothing from {', '.join(missing)} (reuse only or lost); fetched {counts}")
    expected_universes = expected_universe_sources()
    if expected_universes:
        universe_counts = fetched_by_universe_source(connection); missing_universes = sorted(f"universe:{etf}" for etf in expected_universes if universe_counts.get(etf, 0) <= 0)
        if missing_universes: u_counts = ", ".join(f"universe:{etf} {universe_counts.get(etf, 0)}" for etf in sorted(expected_universes)); return Proof( RED, f"{label} fetched nothing from {', '.join(missing_universes)} (0 constituent rows); " f"fetched {counts}; universes {u_counts}", )
    return Proof(OK, f"{label} on {short}… fetched {counts}")
def evaluate( connection: psycopg.Connection[Any], instance: dg.DagsterInstance, *, digest: str, now: datetime | None = None ) -> Proof:
    digest = digest.strip()
    if not digest.startswith("sha256:"): return Proof(PENDING, "no data-engine image digest in this environment (local/CI)")
    short = digest[:19]; since = deployment_started_at(instance, digest)
    if since is None: return Proof(PENDING, f"no boot canary run for {short}… yet; the deployment has not started its proof")
    current_time = _as_utc(now) if now is not None else datetime.now(UTC); deployment_age = (current_time - since.astimezone(UTC)).total_seconds() / 3600.0; candidates = fetching_runs(instance, since)
    for record in reversed(candidates):
        if record.dagster_run.status == dg.DagsterRunStatus.SUCCESS:
            proof = _evaluate_run(connection, instance, record, digest=digest, short=short)
            if proof.state == OK: return proof
    if not candidates:
        if deployment_age > MAX_PROVING_WINDOW_HOURS: return Proof( RED, f"{short}… awaiting its first tick for {deployment_age:.1f}h (limit {MAX_PROVING_WINDOW_HOURS:g}h); proof timed out", )
        return Proof( PENDING, f"no scheduled or forced live-pipeline run since the deployment of {short}… at " f"{since.astimezone(UTC).isoformat(timespec='minutes')}", )
    in_progress = [c for c in candidates if c.dagster_run.status in UNFINISHED_RUN_STATUSES]
    if in_progress:
        earliest_in_prog = min(in_progress, key=lambda r: _as_utc(r.create_timestamp)); run_age = (current_time - _as_utc(earliest_in_prog.create_timestamp)).total_seconds() / 3600.0
        if run_age > MAX_PROVING_WINDOW_HOURS: return Proof( RED, f"{short}… in-progress run hung for {run_age:.1f}h (limit {MAX_PROVING_WINDOW_HOURS:g}h); proof timed out", )
        label = _label(earliest_in_prog); status = earliest_in_prog.dagster_run.status; return Proof(PENDING, f"{label} is {status.value.lower().replace('_', ' ')}")
    return _evaluate_run(connection, instance, candidates[0], digest=digest, short=short)
