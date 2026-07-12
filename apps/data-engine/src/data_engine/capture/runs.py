"""Immutable bindings between a scheduled run and its promoted deployment."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from truealpha_contracts import CaptureScope

_RELEASE_ID = re.compile(r"^release-manifest:[0-9a-f]{64}$")
_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class CaptureRunBinding:
    run_id: str
    capture_scope_id: str
    release_manifest_id: str
    image_digest: str
    configuration_sha256: str
    schedule_name: str
    started_at: datetime

    def __post_init__(self) -> None:
        if not self.run_id or not self.schedule_name:
            raise ValueError("capture run identity fields cannot be empty")
        if not self.capture_scope_id.startswith("capture-scope:"):
            raise ValueError("capture run requires a canonical scope ID")
        if not _RELEASE_ID.fullmatch(self.release_manifest_id):
            raise ValueError("release_manifest_id must be a content-addressed release manifest")
        if not _IMAGE_DIGEST.fullmatch(self.image_digest):
            raise ValueError("image_digest must be a sha256 OCI digest")
        if not _SHA256.fullmatch(self.configuration_sha256):
            raise ValueError("configuration_sha256 must be lowercase sha256")
        if self.started_at.tzinfo is None or self.started_at.utcoffset() is None:
            raise ValueError("started_at must be timezone-aware")


def put(conn, *, scope: CaptureScope, binding: CaptureRunBinding) -> bool:
    if binding.capture_scope_id != scope.capture_scope_id:
        raise ValueError("run binding scope does not match the persisted scope")
    row = conn.execute(
        """
        insert into staging.capture_run_bindings
            (run_id, capture_scope_id, release_manifest_id, image_digest,
             configuration_sha256, schedule_name, started_at, recorded_at)
        values (%s, %s, %s, %s, %s, %s, %s, %s)
        on conflict (run_id) do nothing
        returning run_id
        """,
        (
            binding.run_id,
            binding.capture_scope_id,
            binding.release_manifest_id,
            binding.image_digest,
            binding.configuration_sha256,
            binding.schedule_name,
            binding.started_at,
            datetime.now(UTC),
        ),
    ).fetchone()
    if row is not None:
        return True
    existing = get(conn, binding.run_id)
    if existing != binding:
        raise ValueError(f"capture run {binding.run_id} is already bound to different immutable inputs")
    return False


def get(conn, run_id: str) -> CaptureRunBinding | None:
    row = conn.execute(
        """
        select capture_scope_id, release_manifest_id, image_digest,
               configuration_sha256, schedule_name, started_at
        from staging.capture_run_bindings where run_id = %s
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    return CaptureRunBinding(
        run_id=run_id,
        capture_scope_id=row[0],
        release_manifest_id=row[1],
        image_digest=row[2],
        configuration_sha256=row[3],
        schedule_name=row[4],
        started_at=row[5],
    )
