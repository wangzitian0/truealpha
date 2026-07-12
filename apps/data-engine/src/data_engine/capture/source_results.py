"""Source-specific capture evidence and deterministic requirement fusion."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from truealpha_contracts import CaptureCellRequirement, DataDomain, DataSource, canonical_sha256

from data_engine.capture import observations


class SourceResultOutcome(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"


@dataclass(frozen=True)
class CaptureSourceResult:
    run_id: str
    subject_id: str
    domain: DataDomain
    partition_key: str
    source: DataSource
    outcome: SourceResultOutcome
    raw_refs: tuple[str, ...]
    domain_record_ids: tuple[str, ...]
    observed_fields: tuple[str, ...]
    min_knowable_at: datetime | None
    max_knowable_at: datetime | None
    observed_at: datetime
    confidence: Decimal
    mapping_version: str
    attempt: int = 0
    detail: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "raw_refs", tuple(sorted(set(self.raw_refs))))
        object.__setattr__(self, "domain_record_ids", tuple(sorted(set(self.domain_record_ids))))
        object.__setattr__(self, "observed_fields", tuple(sorted(set(self.observed_fields))))
        if not self.raw_refs or any(not raw_ref.startswith("raw.fetches:") for raw_ref in self.raw_refs):
            raise ValueError("source result requires raw.fetches lineage")
        if not Decimal("0") <= self.confidence <= Decimal("1"):
            raise ValueError("source result confidence must be between 0 and 1")
        if self.attempt < 0:
            raise ValueError("source result attempt must be non-negative")
        if self.max_knowable_at is not None and self.max_knowable_at > self.observed_at:
            raise ValueError("source result cannot contain future knowledge")
        if (
            self.min_knowable_at is not None
            and self.max_knowable_at is not None
            and self.min_knowable_at > self.max_knowable_at
        ):
            raise ValueError("source result knowable range is inverted")
        if self.outcome is SourceResultOutcome.FAILED and not self.detail:
            raise ValueError("failed source result requires detail")

    @property
    def key(self) -> tuple[str, DataDomain, str, DataSource, int]:
        return self.subject_id, self.domain, self.partition_key, self.source, self.attempt


def put(conn, result: CaptureSourceResult) -> int:
    transaction_time = result.max_knowable_at or result.observed_at
    # PostgreSQL now() is fixed at transaction start, which can predate a late
    # source result in a long-running capture transaction.
    recorded_at = datetime.now(UTC)
    row = conn.execute(
        """
        insert into staging.capture_source_results
            (run_id, subject_id, domain, partition_key, source, outcome,
             raw_refs, domain_record_ids, observed_fields, min_knowable_at,
             max_knowable_at, observed_at, valid_time, transaction_time,
             recorded_at, confidence, mapping_version, attempt, detail)
        values (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb,
                %s, %s, %s, daterange(%s::date, (%s::date + 1), '[)'), %s,
                %s, %s, %s, %s, %s)
        on conflict do nothing returning id
        """,
        (
            result.run_id,
            result.subject_id,
            result.domain.value,
            result.partition_key,
            result.source.value,
            result.outcome.value,
            json.dumps(result.raw_refs),
            json.dumps(result.domain_record_ids),
            json.dumps(result.observed_fields),
            result.min_knowable_at,
            result.max_knowable_at,
            result.observed_at,
            result.observed_at.date(),
            result.observed_at.date(),
            transaction_time,
            recorded_at,
            result.confidence,
            result.mapping_version,
            result.attempt,
            result.detail,
        ),
    ).fetchone()
    if row is not None:
        return row[0]
    existing = get(conn, result.run_id, *result.key)
    if existing is None:
        raise RuntimeError("source result conflict did not expose an existing row")
    existing_id, existing_result = existing
    if existing_result != result:
        raise ValueError("source result identity collision with different evidence")
    return existing_id


def get(
    conn,
    run_id: str,
    subject_id: str,
    domain: DataDomain,
    partition_key: str,
    source: DataSource,
    attempt: int = 0,
) -> tuple[int, CaptureSourceResult] | None:
    row = conn.execute(
        """
        select id, outcome, raw_refs, domain_record_ids, observed_fields,
               min_knowable_at, max_knowable_at, observed_at, confidence,
               mapping_version, detail
        from staging.capture_source_results
        where run_id = %s and subject_id = %s and domain = %s
          and partition_key = %s and source = %s and attempt = %s
        """,
        (run_id, subject_id, domain.value, partition_key, source.value, attempt),
    ).fetchone()
    if row is None:
        return None
    return (
        row[0],
        CaptureSourceResult(
            run_id=run_id,
            subject_id=subject_id,
            domain=domain,
            partition_key=partition_key,
            source=source,
            outcome=SourceResultOutcome(row[1]),
            raw_refs=tuple(row[2]),
            domain_record_ids=tuple(row[3]),
            observed_fields=tuple(row[4]),
            min_knowable_at=row[5],
            max_knowable_at=row[6],
            observed_at=row[7],
            confidence=Decimal(row[8]),
            mapping_version=row[9],
            attempt=attempt,
            detail=row[10],
        ),
    )


def for_cell(
    conn,
    run_id: str,
    requirement: CaptureCellRequirement,
) -> tuple[tuple[int, CaptureSourceResult], ...]:
    rows = conn.execute(
        """
        select distinct on (source) source, attempt
        from staging.capture_source_results
        where run_id = %s and subject_id = %s and domain = %s and partition_key = %s
        order by source, attempt desc
        """,
        (run_id, requirement.subject_id, requirement.domain.value, requirement.partition_key),
    ).fetchall()
    results = [
        get(
            conn,
            run_id,
            requirement.subject_id,
            requirement.domain,
            requirement.partition_key,
            DataSource(row[0]),
            row[1],
        )
        for row in rows
    ]
    return tuple(result for result in results if result is not None)


def evidence_digest(conn, result_ids: tuple[int, ...]) -> str:
    """Hash semantic evidence, excluding run/attempt/ingestion clock identity."""
    ids = sorted(set(result_ids))
    if not ids:
        raise ValueError("source asset produced no result rows")
    rows = conn.execute(
        """
        select id, subject_id, domain, partition_key, source, outcome,
               raw_refs, domain_record_ids, observed_fields, confidence,
               mapping_version, detail
        from staging.capture_source_results where id = any(%s) order by id
        """,
        (ids,),
    ).fetchall()
    if len(rows) != len(ids):
        found = {row[0] for row in rows}
        raise LookupError(f"missing capture source results: {sorted(set(ids) - found)}")
    raw_ids = {int(raw_ref.removeprefix("raw.fetches:")) for row in rows for raw_ref in row[6]}
    raw_rows = conn.execute(
        "select id, payload_sha256 from raw.fetches where id = any(%s)",
        (sorted(raw_ids),),
    ).fetchall()
    raw_hashes = {raw_id: payload_sha256 for raw_id, payload_sha256 in raw_rows}
    if set(raw_hashes) != raw_ids:
        raise LookupError(f"missing raw evidence: {sorted(raw_ids - set(raw_hashes))}")
    payload = [
        {
            "subject_id": row[1],
            "domain": row[2],
            "partition_key": row[3],
            "source": row[4],
            "outcome": row[5],
            "raw_sha256": sorted(raw_hashes[int(ref.removeprefix("raw.fetches:"))] for ref in row[6]),
            "domain_record_ids": sorted(row[7]),
            "observed_fields": sorted(row[8]),
            "confidence": str(row[9]),
            "mapping_version": row[10],
            "detail": row[11],
        }
        for row in rows
    ]
    return canonical_sha256(payload)


EMPTY_COMPLETE_DOMAINS = {
    DataDomain.CORPORATE_ACTIONS,
    DataDomain.COMPANY_GUIDANCE,
    DataDomain.KNOWLEDGE_GRAPH,
}


def finalize(conn, *, run_id: str, requirement: CaptureCellRequirement) -> int:
    if requirement.primary_source is None:
        raise ValueError(f"cannot finalize source-less requirement {requirement.key}")
    allowed = (requirement.primary_source, *requirement.fallback_sources)
    candidates = [
        result
        for _result_id, result in for_cell(conn, run_id, requirement)
        if result.source in allowed and result.outcome is SourceResultOutcome.SUCCESS
    ]
    if not candidates:
        failures = [result.detail for _id, result in for_cell(conn, run_id, requirement) if result.detail]
        detail = "; ".join(failures) or "No approved source asset produced evidence for the frozen cell."
        failed_results = for_cell(conn, run_id, requirement)
        if not failed_results:
            raise ValueError(f"cannot finalize {requirement.key}: no source result carries raw lineage")
        first = failed_results[0][1]
        observation = observations.CaptureObservation(
            run_id=run_id,
            subject_id=requirement.subject_id,
            domain=requirement.domain,
            partition_key=requirement.partition_key,
            outcome=observations.ObservationOutcome.FAILED,
            raw_refs=tuple(raw_ref for _id, result in failed_results for raw_ref in result.raw_refs),
            domain_record_ids=(),
            required_fields=requirement.required_fields,
            observed_fields=(),
            min_knowable_at=None,
            max_knowable_at=None,
            observed_at=max(result.observed_at for _id, result in failed_results),
            confidence=first.confidence,
            source=first.source,
            mapping_version="capture-fusion:1",
            detail=detail,
        )
        return observations.put(conn, observation)

    raw_refs = tuple(raw_ref for result in candidates for raw_ref in result.raw_refs)
    record_ids = tuple(record_id for result in candidates for record_id in result.domain_record_ids)
    observed_fields = tuple(field for result in candidates for field in result.observed_fields)
    missing = set(requirement.required_fields) - set(observed_fields)
    empty_allowed = requirement.domain in EMPTY_COMPLETE_DOMAINS and not record_ids
    outcome = (
        observations.ObservationOutcome.COMPLETE_EMPTY
        if empty_allowed
        else observations.ObservationOutcome.COMPLETE_RECORDS
        if not missing and record_ids
        else observations.ObservationOutcome.FAILED
    )
    observation_detail: str | None = None
    if outcome is observations.ObservationOutcome.COMPLETE_EMPTY:
        observation_detail = "All approved source queries succeeded and returned no domain events or assertions."
    elif outcome is observations.ObservationOutcome.FAILED:
        observation_detail = f"Approved sources did not cover frozen required fields: {sorted(missing)}"
    chosen_source = next(source for source in allowed if any(result.source is source for result in candidates))
    min_times = [result.min_knowable_at for result in candidates if result.min_knowable_at is not None]
    max_times = [result.max_knowable_at for result in candidates if result.max_knowable_at is not None]
    mapping_version = "capture-fusion:" + canonical_sha256(
        sorted((result.source.value, result.mapping_version) for result in candidates)
    )
    observation = observations.CaptureObservation(
        run_id=run_id,
        subject_id=requirement.subject_id,
        domain=requirement.domain,
        partition_key=requirement.partition_key,
        outcome=outcome,
        raw_refs=raw_refs,
        domain_record_ids=record_ids,
        required_fields=requirement.required_fields,
        observed_fields=observed_fields,
        min_knowable_at=min(min_times) if min_times else None,
        max_knowable_at=max(max_times) if max_times else None,
        observed_at=max(result.observed_at for result in candidates),
        confidence=min(result.confidence for result in candidates),
        source=chosen_source,
        mapping_version=mapping_version,
        detail=observation_detail,
    )
    return observations.put(conn, observation)
