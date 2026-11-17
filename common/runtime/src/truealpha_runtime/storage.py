from __future__ import annotations

import hashlib
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from infra2_sdk.runtime.s3 import create_s3_client, is_not_found, read_object_bytes
from infra2_sdk.runtime.s3 import ensure_bucket as ensure_bucket_sdk
from truealpha_contracts.models import RawCapture, RawIngestionEnvelope, RawObjectRef

from truealpha_runtime.config import RuntimeSettings


def _unconfigured(name: str) -> RuntimeError:
    return RuntimeError(f"{name} is not configured; object storage refuses to dial a default")


def build_raw_envelope(capture: RawCapture, *, bucket: str, prefix: str = "raw") -> RawIngestionEnvelope:
    """Build SHA-256 content envelope for `capture`."""
    digest = hashlib.sha256(capture.body).hexdigest()
    key = f"{prefix}/{capture.source.value}/{digest[:2]}/{digest}"
    return RawIngestionEnvelope(
        source=capture.source,
        source_record_id=capture.source_record_id,
        object=RawObjectRef(
            bucket=bucket,
            key=key,
            sha256=digest,
            byte_length=len(capture.body),
            content_type=capture.content_type,
        ),
        fetched_at=capture.fetched_at,
        source_published_at=capture.source_published_at,
        metadata=capture.metadata,
    )


class StorageError(Exception):
    pass


class S3RawObjectStore:
    def __init__(self, settings: RuntimeSettings | None = None, *, client: Any | None = None) -> None:
        self.settings = settings or RuntimeSettings()
        self.bucket = self.settings.s3_bucket
        self.s3_settings = self.settings.to_s3_settings()
        self._has_injected_client = client is not None
        self.client = client or create_s3_client(self.s3_settings)

    def _require_endpoint(self) -> None:
        if self._has_injected_client:
            return
        if not (self.settings.s3_endpoint or "").strip():
            raise _unconfigured("S3_ENDPOINT")
        if not self.settings.s3_access_key.strip() or not self.settings.s3_secret_key.get_secret_value().strip():
            raise _unconfigured("S3_ACCESS_KEY / S3_SECRET_KEY")

    def ensure_bucket(self, *, create: bool | None = None) -> None:
        self._require_endpoint()
        allow_create = self.settings.may_create_bucket if create is None else create
        try:
            ensure_bucket_sdk(self.s3_settings, client=self.client, allow_create=allow_create)
        except Exception as exc:
            raise StorageError(f"cannot access or create bucket {self.bucket}: {exc}") from exc

    def store(self, capture: RawCapture) -> RawIngestionEnvelope:
        self._require_endpoint()
        envelope = build_raw_envelope(capture, bucket=self.bucket, prefix=self.settings.s3_raw_prefix)
        ref = envelope.object
        self.ensure_bucket()

        existing_length: int | None = None
        try:
            existing = self.client.head_object(Bucket=self.bucket, Key=ref.key)
            existing_length = int(existing.get("ContentLength", -1))
        except Exception as exc:
            if not is_not_found(exc):
                raise StorageError(f"cannot inspect {ref.key}") from exc

        if existing_length is not None and existing_length != ref.byte_length:
            raise StorageError(f"content-address collision for {ref.key}")

        if existing_length is None:
            try:
                self.client.put_object(
                    Bucket=self.bucket,
                    Key=ref.key,
                    Body=capture.body,
                    ContentType=capture.content_type,
                    Metadata={"sha256": ref.sha256, "source": capture.source.value},
                )
            except (BotoCoreError, ClientError) as exc:
                raise StorageError(f"cannot store {ref.key}") from exc
        return envelope

    def get(self, ref: RawObjectRef) -> bytes:
        if ref.bucket != self.bucket:
            raise StorageError(f"object belongs to unexpected bucket {ref.bucket}")
        self._require_endpoint()
        try:
            content = read_object_bytes(self.client, bucket=ref.bucket, key=ref.key)
        except Exception as exc:
            raise StorageError(f"cannot read {ref.key}: {exc}") from exc
        if hashlib.sha256(content).hexdigest() != ref.sha256:
            raise StorageError(f"checksum mismatch for {ref.key}")
        return content
