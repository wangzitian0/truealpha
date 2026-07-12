"""Build a row-complete capture manifest from persisted observations."""

from __future__ import annotations

from datetime import datetime

from truealpha_contracts import (
    CaptureCellStatus,
    CaptureManifest,
    CaptureManifestCell,
    CaptureScope,
    canonical_sha256,
)

from data_engine.capture import observations


def _raw_evidence(conn, raw_refs: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    ids = [int(raw_ref.removeprefix("raw.fetches:")) for raw_ref in raw_refs]
    rows = conn.execute(
        "select id, payload_sha256 from raw.fetches where id = any(%s) order by id",
        (ids,),
    ).fetchall()
    if len(rows) != len(ids):
        found = {row[0] for row in rows}
        raise LookupError(f"capture observation references missing raw rows: {sorted(set(ids) - found)}")
    return tuple((f"raw.fetches:{raw_id}", payload_sha256) for raw_id, payload_sha256 in rows)


def build(
    conn,
    *,
    scope: CaptureScope,
    run_id: str,
    image_digest: str,
    as_of: datetime,
    started_at: datetime,
    completed_at: datetime,
) -> CaptureManifest:
    by_key = {
        observation.key: (observation_id, observation)
        for observation_id, observation in observations.for_run(conn, run_id)
    }
    cells: list[CaptureManifestCell] = []
    for requirement in scope.requirements:
        evidence = by_key.get(requirement.key)
        if evidence is None:
            cells.append(
                CaptureManifestCell(
                    subject_id=requirement.subject_id,
                    domain=requirement.domain,
                    partition_key=requirement.partition_key,
                    status=CaptureCellStatus.MISSING,
                    detail="No persisted capture observation exists for the frozen cell.",
                )
            )
            continue
        observation_id, observation = evidence
        if observation.outcome is observations.ObservationOutcome.FAILED:
            cells.append(
                CaptureManifestCell(
                    subject_id=requirement.subject_id,
                    domain=requirement.domain,
                    partition_key=requirement.partition_key,
                    status=CaptureCellStatus.FAILED,
                    source=observation.source,
                    raw_refs=observation.raw_refs,
                    detail=observation.detail or "Capture observation failed.",
                )
            )
            continue

        normalized_ids = (
            f"staging.capture_observations:{observation_id}",
            *observation.domain_record_ids,
        )
        raw_hashes = _raw_evidence(conn, observation.raw_refs)
        recorded_at = conn.execute(
            "select recorded_at from staging.capture_observations where id = %s",
            (observation_id,),
        ).fetchone()[0]
        # For successful empty event/result sets, the query observation is the
        # fact that became knowable. Domain tables remain empty by design.
        min_knowable_at = observation.min_knowable_at or observation.observed_at
        max_knowable_at = observation.max_knowable_at or observation.observed_at
        cells.append(
            CaptureManifestCell(
                subject_id=requirement.subject_id,
                domain=requirement.domain,
                partition_key=requirement.partition_key,
                status=CaptureCellStatus.COMPLETE,
                source=observation.source,
                raw_refs=observation.raw_refs,
                normalized_record_ids=normalized_ids,
                record_count=len(normalized_ids),
                content_sha256=canonical_sha256(
                    {
                        "raw": raw_hashes,
                        "normalized": normalized_ids,
                        "outcome": observation.outcome.value,
                        "mapping_version": observation.mapping_version,
                    }
                ),
                min_knowable_at=min_knowable_at,
                max_knowable_at=max_knowable_at,
                recorded_at=recorded_at,
                observed_at=observation.observed_at,
                confidence=observation.confidence,
                mapping_version=observation.mapping_version,
                detail=observation.detail,
            )
        )
    return CaptureManifest(
        scope=scope,
        run_id=run_id,
        image_digest=image_digest,
        as_of=as_of,
        started_at=started_at,
        completed_at=completed_at,
        cells=tuple(cells),
    )
