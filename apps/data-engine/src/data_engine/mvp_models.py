"""Typed filing-document payload for the D1 normalization handoff."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field


class FilingDocumentPayload(BaseModel):
    """Source-neutral fields exposed by the normalized filing repository."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    accession: str = Field(pattern=r"^[0-9]{10}-[0-9]{2}-[0-9]{6}$")
    form: str = Field(pattern=r"^[0-9A-Z-]+(?:/A)?$")
    filing_date: date
    report_period: date
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_type: str = Field(min_length=1)


__all__ = ["FilingDocumentPayload"]
