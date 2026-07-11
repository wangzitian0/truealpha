from __future__ import annotations

import contextlib
import hashlib
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from truealpha_contracts import RawCapture, RawIngestionEnvelope, RawObjectRef

from truealpha_runtime.config import RuntimeSettings, runtime_settings


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
        self.client = client or boto3.client(
            "s3",
            endpoint_url=self.settings.s3_endpoint,
            aws_access_key_id=self.settings.s3_access_key,
            aws_secret_access_key=self.settings.s3_secret_key.get_secret_value(),
            region_name=self.settings.s3_region,
            config=Config(
                signature_version="s3v4",
                connect_timeout=self.settings.s3_connect_timeout_seconds,
                read_timeout=self.settings.s3_connect_timeout_seconds,
                s3={"addressing_style": "path"},
            ),
        )

    def ensure_bucket(self, *, create: bool | None = None) -> None:
        allow_create = self.settings.may_create_bucket if create is None else create
        try:
            self.client.head_bucket(Bucket=self.bucket)
            return
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if not allow_create or code not in {"404", "NoSuchBucket", "NotFound"}:
                raise StorageError(f"cannot access bucket {self.bucket}") from exc
        except BotoCoreError as exc:
            raise StorageError(f"cannot access bucket {self.bucket}") from exc

        try:
            kwargs: dict[str, Any] = {"Bucket": self.bucket}
            if self.settings.s3_region != "us-east-1":
                kwargs["CreateBucketConfiguration"] = {"LocationConstraint": self.settings.s3_region}
            self.client.create_bucket(**kwargs)
        except (BotoCoreError, ClientError) as exc:
            raise StorageError(f"cannot create bucket {self.bucket}") from exc

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

        exists = False
        try:
            existing = self.client.head_object(Bucket=self.bucket, Key=key)
            exists = True
            if int(existing.get("ContentLength", -1)) != len(capture.body):
                raise StorageError(f"content-address collision for {key}")
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code not in {"404", "NoSuchKey", "NotFound"}:
                raise StorageError(f"cannot inspect {key}") from exc
        except BotoCoreError as exc:
            raise StorageError(f"cannot inspect {key}") from exc

        if not exists:
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
            response = self.client.get_object(Bucket=ref.bucket, Key=ref.key)
            with contextlib.closing(response["Body"]) as body:
                content = body.read()
        except (BotoCoreError, ClientError) as exc:
            raise StorageError(f"cannot read {ref.key}") from exc
        if hashlib.sha256(content).hexdigest() != ref.sha256:
            raise StorageError(f"checksum mismatch for {ref.key}")
        return content
