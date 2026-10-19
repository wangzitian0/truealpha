from __future__ import annotations

import hashlib
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from infra2_sdk.runtime.s3 import (
    create_s3_client,
    is_not_found,
    read_object_bytes,
)
from infra2_sdk.runtime.s3 import (
    ensure_bucket as ensure_bucket_sdk,
)
from truealpha_contracts.models import RawCapture, RawIngestionEnvelope, RawObjectRef

from truealpha_runtime.config import RuntimeSettings, runtime_settings


def _unconfigured(name: str) -> RuntimeError:
    return RuntimeError(f"{name} is not configured; object storage refuses to dial a default")


class StorageError(Exception):
    pass


class S3RawObjectStore:
    """Immutable raw payload storage over the S3 API.

    MinIO is only a runtime backend. Callers use the same boto3 adapter in local,
    CI, infra2, and production environments.
    """

    def __init__(self, settings: RuntimeSettings | None = None, *, client: Any | None = None) -> None:
        self.settings = settings or runtime_settings
        self.bucket = self.settings.s3_bucket
        self.s3_settings = self.settings.to_s3_settings()
        self.client = client or create_s3_client(self.s3_settings)

    def _require_endpoint(self) -> str:
        """The configured endpoint, or a named configuration error.

        Never a default. An unconfigured object store used to dial `localhost:9000` with
        placeholder credentials, so "nobody configured S3 here" arrived as a connection
        failure against the container's own loopback (#531). Refusing names the cause.
        """
        if not self.settings.s3_endpoint:
            raise _unconfigured("S3_ENDPOINT")
        if not self.settings.s3_access_key or not self.settings.s3_secret_key.get_secret_value():
            raise _unconfigured("S3_ACCESS_KEY / S3_SECRET_KEY")
        return self.settings.s3_endpoint

    def ensure_bucket(self, *, create: bool | None = None) -> None:
        allow_create = self.settings.may_create_bucket if create is None else create
        try:
            ensure_bucket_sdk(self.s3_settings, client=self.client, allow_create=allow_create)
        except Exception as exc:
            raise StorageError(f"cannot access or create bucket {self.bucket}: {exc}") from exc

    def store(self, capture: RawCapture) -> RawIngestionEnvelope:
        digest = hashlib.sha256(capture.body).hexdigest()
        key = f"{self.settings.s3_raw_prefix}/{capture.source.value}/{digest[:2]}/{digest}"
        ref = RawObjectRef(
            bucket=self.bucket,
            key=key,
            sha256=digest,
            byte_length=len(capture.body),
            content_type=capture.content_type,
        )
        self.ensure_bucket()

        existing_length: int | None = None
        try:
            existing = self.client.head_object(Bucket=self.bucket, Key=key)
            existing_length = int(existing.get("ContentLength", -1))
        except Exception as exc:
            if not is_not_found(exc):
                raise StorageError(f"cannot inspect {key}") from exc

        # Compare outside the try scope: the broad except must not replace this message.
        if existing_length is not None and existing_length != len(capture.body):
            raise StorageError(f"content-address collision for {key}")

        if existing_length is None:
            try:
                self.client.put_object(
                    Bucket=self.bucket,
                    Key=key,
                    Body=capture.body,
                    ContentType=capture.content_type,
                    Metadata={
                        "sha256": digest,
                        "source": capture.source.value,
                    },
                )
            except (BotoCoreError, ClientError) as exc:
                raise StorageError(f"cannot store {key}") from exc

        return RawIngestionEnvelope(
            source=capture.source,
            source_record_id=capture.source_record_id,
            object=ref,
            fetched_at=capture.fetched_at,
            source_published_at=capture.source_published_at,
            metadata=capture.metadata,
        )

    def get(self, ref: RawObjectRef) -> bytes:
        if ref.bucket != self.bucket:
            raise StorageError(f"object belongs to unexpected bucket {ref.bucket}")
        try:
            content = read_object_bytes(self.client, bucket=ref.bucket, key=ref.key)
        except Exception as exc:
            raise StorageError(f"cannot read {ref.key}: {exc}") from exc
        if hashlib.sha256(content).hexdigest() != ref.sha256:
            raise StorageError(f"checksum mismatch for {ref.key}")
        return content
