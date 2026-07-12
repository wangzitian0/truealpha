"""Postgres persistence for immutable capture scopes and manifests."""

from __future__ import annotations

import json

from truealpha_contracts import CaptureManifest, CaptureScope


class CaptureRepositoryError(RuntimeError):
    pass


def _payload(model) -> dict:
    return json.loads(model.model_dump_json())


def put_scope(conn, scope: CaptureScope) -> bool:
    payload = _payload(scope)
    row = conn.execute(
        """
        insert into staging.capture_scopes
            (capture_scope_id, environment, universe_id, universe_version,
             universe_membership_sha256, as_of, payload)
        values (%s, %s, %s, %s, %s, %s, %s::jsonb)
        on conflict (capture_scope_id) do nothing
        returning capture_scope_id
        """,
        (
            scope.capture_scope_id,
            scope.environment.value,
            scope.universe_id,
            scope.universe_version,
            scope.universe_membership_sha256,
            scope.as_of,
            json.dumps(payload),
        ),
    ).fetchone()
    if row is not None:
        return True
    existing = conn.execute(
        "select payload from staging.capture_scopes where capture_scope_id = %s",
        (scope.capture_scope_id,),
    ).fetchone()
    if existing is None or existing[0] != payload:
        raise CaptureRepositoryError(f"capture scope collision for {scope.capture_scope_id}")
    return False


def get_scope(conn, capture_scope_id: str) -> CaptureScope:
    row = conn.execute(
        "select payload from staging.capture_scopes where capture_scope_id = %s",
        (capture_scope_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"capture scope {capture_scope_id} does not exist")
    return CaptureScope.model_validate(row[0])


def put_manifest(conn, manifest: CaptureManifest) -> bool:
    put_scope(conn, manifest.scope)
    payload = _payload(manifest)
    row = conn.execute(
        """
        insert into staging.capture_manifests
            (capture_manifest_id, capture_scope_id, run_id, image_digest, evaluated_as_of, status,
             started_at, completed_at, blockers, payload)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
        on conflict (capture_manifest_id) do nothing
        returning capture_manifest_id
        """,
        (
            manifest.capture_manifest_id,
            manifest.scope.capture_scope_id,
            manifest.run_id,
            manifest.image_digest,
            manifest.as_of,
            manifest.status.value,
            manifest.started_at,
            manifest.completed_at,
            json.dumps(manifest.blockers),
            json.dumps(payload),
        ),
    ).fetchone()
    if row is None:
        existing = conn.execute(
            "select payload from staging.capture_manifests where capture_manifest_id = %s",
            (manifest.capture_manifest_id,),
        ).fetchone()
        if existing is None or existing[0] != payload:
            raise CaptureRepositoryError(f"capture manifest collision for {manifest.capture_manifest_id}")
        return False

    requirements = manifest.scope.requirement_map()
    for cell in manifest.cells:
        requirement = requirements[cell.key]
        conn.execute(
            """
            insert into staging.capture_manifest_cells
                (capture_manifest_id, subject_id, domain, partition_key,
                 requirement_level, status, source, record_count, raw_refs,
                 normalized_record_ids, content_sha256, min_knowable_at,
                 max_knowable_at, source_recorded_at, observed_at, confidence,
                 mapping_version, detail)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb,
                    %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                manifest.capture_manifest_id,
                cell.subject_id,
                cell.domain.value,
                cell.partition_key,
                requirement.level.value,
                cell.status.value,
                cell.source.value if cell.source is not None else None,
                cell.record_count,
                json.dumps(cell.raw_refs),
                json.dumps(cell.normalized_record_ids),
                cell.content_sha256,
                cell.min_knowable_at,
                cell.max_knowable_at,
                cell.recorded_at,
                cell.observed_at,
                cell.confidence,
                cell.mapping_version,
                cell.detail,
            ),
        )
    return True


def get_manifest(conn, capture_manifest_id: str) -> CaptureManifest:
    row = conn.execute(
        "select payload from staging.capture_manifests where capture_manifest_id = %s",
        (capture_manifest_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"capture manifest {capture_manifest_id} does not exist")
    return CaptureManifest.model_validate(row[0])
