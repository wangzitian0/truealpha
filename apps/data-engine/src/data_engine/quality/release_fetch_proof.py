"""Did the deployed release fetch from every registered origin itself? (`release_fetch_proof`)

Production's boot canary does not force a fetch (`lanes.triggers.boot_canary_forces_fetch`).
It reuses the observations an earlier build committed in the last twelve hours (#635), so a
production deploy never runs the new release's fetch path. A release could reach production
with a broken adapter while every deploy check stayed green, and the first sign would be a
frozen pointer days later.

This check reads the first live-pipeline run the current deployment made that had to fetch:
a scheduled tick (`dagster/schedule_name`) or a forced one (`force_fetch`, #874).

* **ok**: that run succeeded, and its capture (`mart.data_engine_identity`, stamped with
  this deployment's image digest) holds at least one freshly fetched observation from every
  origin registered and enabled here. Reused observations do not count.
* **pending** (`ok` is null): no such run exists yet, or it has not finished.
* **red**: that run failed, or it fetched nothing from an origin (the origin was served only
  by reuse, or it was lost), or its capture names another build.

The deployment starts at its boot canary run. `boot_canary_sensor` launches exactly one per
deployment (image digest plus configuration) and tags it with the digest. A rollback and a
configuration change are new deployments, and each must prove itself again.

`tools/deploy_freshness.py` turns a production release older than 26 hours without an ok
verdict into a red leg.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import dagster as dg
import psycopg

from data_engine.config import settings
from data_engine.datahub.production_topt.source_registrations import REGISTRATIONS, SourceRegistration
from data_engine.lanes.capture import TICK_BY_JOB
from data_engine.lanes.triggers import BOOT_CANARY_TAG

#: Dagster's own tag on every run a schedule launched.
SCHEDULE_TAG = "dagster/schedule_name"
#: The tick's output metadata key naming the capture run it wrote (`lanes.capture._run_tick`).
CAPTURE_RUN_METADATA = "capture_run_id"

OK, PENDING, RED = "ok", "pending", "red"

#: Whether a corroborating origin is switched on in this deployment, keyed by its
#: `OriginRegistration.origin_source`. A registered origin missing here fails
#: `test_release_fetch_proof.py`, so a new origin declares its switch before it ships.
#: A registration's own primary is always on.
ORIGIN_ENABLED: Mapping[str, Callable[[], bool]] = {
    "yahoo-chart:v1": lambda: True,
    "twelve-data:v1": lambda: bool(settings.twelve_data_api_key),
    "moomoo-kline:v1": lambda: settings.moomoo_kline_origin_enabled,
    "moomoo-financials:v1": lambda: settings.moomoo_financials_origin_enabled,
}

_FINISHED_RED = (dg.DagsterRunStatus.FAILURE, dg.DagsterRunStatus.CANCELED)


@dataclass(frozen=True)
class Proof:
    state: str
    summary: str

    @property
    def ok(self) -> bool | None:
        """The verdict row's `ok`: null while pending."""
        return None if self.state == PENDING else self.state == OK


def _vendor_registrations() -> Iterable[SourceRegistration]:
    # Release-derived semantics come from the release's own corpus: no vendor, no fetch path.
    return (registration for registration in REGISTRATIONS if registration.corroboration_class != "release")


def _primary(registration: SourceRegistration) -> str:
    return f"{registration.source_id}:{registration.version}"


def origin_of(semantic_type: str, parser_version: str) -> str | None:
    """The registered origin that wrote an observation, or None for a release-derived one.

    Keyed by semantic AND parser vintage: the SEC primary writes the shared primary
    vintage that the Yahoo origin also lists, and only the semantic tells them apart."""
    for registration in _vendor_registrations():
        if semantic_type not in registration.semantic_types:
            continue
        for origin in registration.origins:
            if parser_version in origin.parser_versions:
                return origin.origin_source
        return _primary(registration)
    return None


def expected_origins() -> frozenset[str]:
    """Every origin this deployment fetches from: each vendor registration's primary, and
    each corroborating origin its switch turns on."""
    expected: set[str] = set()
    for registration in _vendor_registrations():
        expected.add(_primary(registration))
        for origin in registration.origins:
            enabled = ORIGIN_ENABLED.get(origin.origin_source)
            if enabled is None:
                raise LookupError(f"origin {origin.origin_source} declares no switch in ORIGIN_ENABLED")
            if enabled():
                expected.add(origin.origin_source)
    return frozenset(expected)


#: Observations bound to the capture's obligations whose final attempt fetched (a source
#: vintage of its own). An obligation #635 satisfied by reuse ends `unchanged` with a
#: reused vintage and contributes nothing, even though it binds the earlier run's
#: observations. A corroborating origin's observation rides its obligation's binding.
FETCHED_SQL = """
select observation.semantic_type, observation.parser_version, count(*)
from raw.capture_obligations obligation
join raw.capture_obligation_results result on result.capture_obligation_id = obligation.obligation_id
join raw.capture_attempt_results attempt on attempt.attempt_id = result.final_attempt_id
join staging.capture_observation_obligations link on link.capture_obligation_id = obligation.obligation_id
join staging.capture_normalized_observations observation on observation.observation_id = link.observation_id
where obligation.run_id = %s
  and attempt.source_vintage_id is not null
group by observation.semantic_type, observation.parser_version
"""

IDENTITY_SQL = "select image_digest from mart.data_engine_identity where run_id = %s"


def fetched_by_origin(connection: psycopg.Connection[Any], capture_run_id: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for semantic_type, parser_version, count in connection.execute(FETCHED_SQL, (capture_run_id,)).fetchall():
        origin = origin_of(str(semantic_type), str(parser_version))
        if origin is not None:
            counts[origin] = counts.get(origin, 0) + int(count)
    return counts


def deployment_started_at(instance: dg.DagsterInstance, digest: str) -> datetime | None:
    """When the current deployment's boot canary run was created (the newest one for this
    digest: a rollback or a configuration change launched a new one)."""
    records = instance.get_run_records(filters=dg.RunsFilter(tags={BOOT_CANARY_TAG: digest}), limit=1)
    return records[0].create_timestamp if records else None


def forced(run: dg.DagsterRun) -> bool:
    tick = TICK_BY_JOB.get(run.job_name)
    if tick is None:
        return False
    op_config = ((run.run_config or {}).get("ops") or {}).get(tick.op_name) or {}
    return bool((op_config.get("config") or {}).get("force_fetch"))


def first_fetching_run(instance: dg.DagsterInstance, since: datetime) -> dg.RunRecord | None:
    """The earliest live-pipeline run created at or after `since` that had to fetch."""
    candidates: list[dg.RunRecord] = []
    for job_name in TICK_BY_JOB:
        records = instance.get_run_records(
            # `created_after` is exclusive; a forced boot canary is created at `since` itself.
            filters=dg.RunsFilter(job_name=job_name, created_after=since - timedelta(seconds=1))
        )
        candidates.extend(
            record
            for record in records
            if record.create_timestamp >= since
            and (record.dagster_run.tags.get(SCHEDULE_TAG) or forced(record.dagster_run))
        )
    return min(candidates, key=lambda record: record.create_timestamp, default=None)


def capture_run_of(instance: dg.DagsterInstance, run_id: str) -> str | None:
    """The capture run a successful tick wrote, from its output metadata."""
    records = instance.get_records_for_run(run_id=run_id, of_type=dg.DagsterEventType.STEP_OUTPUT).records
    for record in records:
        event = record.event_log_entry.dagster_event
        output = event.step_output_data if event is not None else None
        value = (output.metadata if output is not None else {}).get(CAPTURE_RUN_METADATA)
        text = getattr(value, "value", value)
        if text:
            return str(text)
    return None


def _label(record: dg.RunRecord) -> str:
    run = record.dagster_run
    how = "forced" if forced(run) else "scheduled"
    return f"{how} {run.job_name} run {run.run_id[:8]}"


def evaluate(connection: psycopg.Connection[Any], instance: dg.DagsterInstance, *, digest: str) -> Proof:
    digest = digest.strip()
    if not digest.startswith("sha256:"):
        return Proof(PENDING, "no data-engine image digest in this environment (local/CI)")
    short = digest[:19]
    since = deployment_started_at(instance, digest)
    if since is None:
        return Proof(PENDING, f"no boot canary run for {short}… yet; the deployment has not started its proof")
    record = first_fetching_run(instance, since)
    if record is None:
        return Proof(
            PENDING,
            f"no scheduled or forced live-pipeline run since the deployment of {short}… at "
            f"{since.astimezone(UTC).isoformat(timespec='minutes')}",
        )
    label = _label(record)
    status = record.dagster_run.status
    if status in _FINISHED_RED:
        return Proof(RED, f"{label} ended {status.value.lower()} on {short}…")
    if status != dg.DagsterRunStatus.SUCCESS:
        return Proof(PENDING, f"{label} is {status.value.lower().replace('_', ' ')}")
    capture_run_id = capture_run_of(instance, record.dagster_run.run_id)
    if capture_run_id is None:
        return Proof(RED, f"{label} succeeded but names no capture run")
    row = connection.execute(IDENTITY_SQL, (capture_run_id,)).fetchone()
    if row is None or str(row[0]) != digest:
        stamped = "no build" if row is None else str(row[0])[:19] + "…"
        return Proof(RED, f"{label}: its capture is stamped by {stamped}, not {short}…")
    fetched = fetched_by_origin(connection, capture_run_id)
    expected = expected_origins()
    counts = ", ".join(f"{origin} {fetched.get(origin, 0)}" for origin in sorted(expected))
    missing = sorted(origin for origin in expected if fetched.get(origin, 0) <= 0)
    if missing:
        return Proof(RED, f"{label} fetched nothing from {', '.join(missing)} (reuse only or lost); fetched {counts}")
    return Proof(OK, f"{label} on {short}… fetched {counts}")
