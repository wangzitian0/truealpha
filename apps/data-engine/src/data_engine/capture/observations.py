"""Append-only evidence that one capture cell was actually queried."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from truealpha_contracts import DataDomain, DataSource


class ObservationOutcome(StrEnum):
    COMPLETE_RECORDS = "complete_records"
    COMPLETE_EMPTY = "complete_empty"
    FAILED = "failed"


@dataclass(frozen=True)
class CaptureObservation:
    run_id: str
    subject_id: str
    domain: DataDomain
    partition_key: str
    outcome: ObservationOutcome
    raw_refs: tuple[str, ...]
    domain_record_ids: tuple[str, ...]
    required_fields: tuple[str, ...]
    observed_fields: tuple[str, ...]
    min_knowable_at: datetime | None
    max_knowable_at: datetime | None
    observed_at: datetime
    confidence: Decimal
    source: DataSource
    mapping_version: str
    detail: str | None = None

    @property
    def key(self) -> tuple[str, DataDomain, str]:
        return self.subject_id, self.domain, self.partition_key

    def __post_init__(self) -> None:
        object.__setattr__(self, "raw_refs", tuple(sorted(set(self.raw_refs))))
        object.__setattr__(self, "domain_record_ids", tuple(sorted(set(self.domain_record_ids))))
        object.__setattr__(self, "required_fields", tuple(sorted(set(self.required_fields))))
        object.__setattr__(self, "observed_fields", tuple(sorted(set(self.observed_fields))))
        if not self.run_id or not self.subject_id or not self.partition_key or not self.mapping_version:
            raise ValueError("capture observation identity/version fields cannot be empty")
        if not self.raw_refs or any(not ref.startswith("raw.fetches:") for ref in self.raw_refs):
            raise ValueError("capture observation requires raw.fetches lineage")
        if not Decimal("0") <= self.confidence <= Decimal("1"):
            raise ValueError("capture observation confidence must be between 0 and 1")
        if self.max_knowable_at is not None and self.max_knowable_at > self.observed_at:
            raise ValueError("capture observation cannot contain future knowledge")
        if (
            self.min_knowable_at is not None
            and self.max_knowable_at is not None
            and self.min_knowable_at > self.max_knowable_at
        ):
            raise ValueError("capture observation knowable range is inverted")
        if self.outcome is ObservationOutcome.COMPLETE_RECORDS:
            if not self.domain_record_ids:
                raise ValueError("complete_records requires domain record IDs")
            missing = set(self.required_fields) - set(self.observed_fields)
            if missing:
                raise ValueError(f"complete_records missing required fields: {sorted(missing)}")
        elif self.outcome is ObservationOutcome.COMPLETE_EMPTY:
            if self.domain_record_ids:
                raise ValueError("complete_empty cannot carry fabricated domain records")
        elif not self.detail:
            raise ValueError("failed observations require detail")


def put(conn, observation: CaptureObservation) -> int:
    row = conn.execute(
        """
        insert into staging.capture_observations
            (run_id, subject_id, domain, partition_key, outcome, raw_refs,
             domain_record_ids, required_fields, observed_fields,
             min_knowable_at, max_knowable_at, observed_at, confidence, source,
             mapping_version, detail, valid_time, transaction_time)
        values (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb,
                %s::jsonb, %s, %s, %s, %s, %s, %s, %s,
                daterange(%s::date, (%s::date + 1), '[)'), %s)
        on conflict do nothing returning id
        """,
        (
            observation.run_id,
            observation.subject_id,
            observation.domain.value,
            observation.partition_key,
            observation.outcome.value,
            json.dumps(observation.raw_refs),
            json.dumps(observation.domain_record_ids),
            json.dumps(observation.required_fields),
            json.dumps(observation.observed_fields),
            observation.min_knowable_at,
            observation.max_knowable_at,
            observation.observed_at,
            observation.confidence,
            observation.source.value,
            observation.mapping_version,
            observation.detail,
            observation.observed_at.date(),
            observation.observed_at.date(),
            observation.max_knowable_at or observation.observed_at,
        ),
    ).fetchone()
    if row is not None:
        return row[0]
    existing = get(conn, observation.run_id, observation.subject_id, observation.domain, observation.partition_key)
    if existing is None:
        raise RuntimeError("capture observation conflict did not expose an existing row")
    existing_id, existing_observation = existing
    if existing_observation != observation:
        raise ValueError("capture observation identity collision with different evidence")
    return existing_id


def get(
    conn,
    run_id: str,
    subject_id: str,
    domain: DataDomain,
    partition_key: str,
) -> tuple[int, CaptureObservation] | None:
    row = conn.execute(
        """
        select id, outcome, raw_refs, domain_record_ids, required_fields,
               observed_fields, min_knowable_at, max_knowable_at, observed_at,
               confidence, source, mapping_version, detail
        from staging.capture_observations
        where run_id = %s and subject_id = %s and domain = %s and partition_key = %s
        """,
        (run_id, subject_id, domain.value, partition_key),
    ).fetchone()
    if row is None:
        return None
    return (
        row[0],
        CaptureObservation(
            run_id=run_id,
            subject_id=subject_id,
            domain=domain,
            partition_key=partition_key,
            outcome=ObservationOutcome(row[1]),
            raw_refs=tuple(row[2]),
            domain_record_ids=tuple(row[3]),
            required_fields=tuple(row[4]),
            observed_fields=tuple(row[5]),
            min_knowable_at=row[6],
            max_knowable_at=row[7],
            observed_at=row[8],
            confidence=Decimal(row[9]),
            source=DataSource(row[10]),
            mapping_version=row[11],
            detail=row[12],
        ),
    )


def for_run(conn, run_id: str) -> tuple[tuple[int, CaptureObservation], ...]:
    keys = conn.execute(
        """
        select subject_id, domain, partition_key
        from staging.capture_observations where run_id = %s
        order by subject_id, domain, partition_key
        """,
        (run_id,),
    ).fetchall()
    observations = [
        get(conn, run_id, subject_id, DataDomain(domain), partition) for subject_id, domain, partition in keys
    ]
    return tuple(observation for observation in observations if observation is not None)
