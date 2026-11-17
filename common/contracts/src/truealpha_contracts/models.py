"""Raw ingestion contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from truealpha_contracts.common import AwareDatetime, Sha256


class DataSource(StrEnum):
    SEC = "sec"
    MOOMOO = "moomoo"
    NPORT = "nport"
    YAHOO = "yahoo"
    TWELVE_DATA = "twelvedata"
    OPENFIGI = "openfigi"
    NASDAQ_INDEX = "nasdaq-index"
    US_TREASURY = "us-treasury"
    RELEASE = "release"


class RawObjectRef(BaseModel):
    bucket: str = Field(min_length=3)
    key: str = Field(min_length=1)
    sha256: Sha256
    byte_length: int = Field(ge=0)
    content_type: str = Field(min_length=1)

    @property
    def uri(self) -> str:
        return f"s3://{self.bucket}/{self.key}"


class _RawEnvelopeBase(BaseModel):
    source: DataSource
    source_record_id: str = Field(min_length=1)
    fetched_at: AwareDatetime
    source_published_at: AwareDatetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RawCapture(_RawEnvelopeBase):
    body: bytes
    content_type: str = Field(min_length=1)


class RawIngestionEnvelope(_RawEnvelopeBase):
    object: RawObjectRef
