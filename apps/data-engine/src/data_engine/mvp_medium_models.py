"""Typed payload and normalization envelopes for the D2 shared MVP boundary."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from truealpha_contracts.market import PriceBasis
from truealpha_contracts.universe import SubjectRef


class MarketPricePayload(BaseModel):
    """Source-neutral unadjusted price payload persisted by the E0 slice."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$")
    issuer_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$")
    security_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$")
    listing_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$")
    share_class: str = Field(min_length=1)
    exchange_mic: str = Field(pattern=r"^[A-Z0-9]{4}$")
    ticker: str = Field(pattern=r"^[A-Z.]+$")
    calendar_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$")
    calendar_version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$")
    trading_date: date
    session_close_at: datetime
    open: Decimal = Field(gt=0)
    high: Decimal = Field(gt=0)
    low: Decimal = Field(gt=0)
    close: Decimal = Field(gt=0)
    volume: int = Field(ge=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    price_basis: Literal[PriceBasis.UNADJUSTED] = PriceBasis.UNADJUSTED
    knowable_at: datetime
    produced_at: datetime
    recorded_at: datetime
    confidence: Decimal = Field(ge=0, le=1)
    confidence_policy_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$")
    price_policy_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$")

    @field_validator("open", "high", "low", "close", "confidence", mode="before")
    @classmethod
    def reject_binary_float(cls, value: Any) -> Any:
        if isinstance(value, (float, bool)):
            raise ValueError("price and confidence inputs must not use binary floats")
        return value

    @field_validator("session_close_at", "knowable_at", "produced_at", "recorded_at")
    @classmethod
    def validate_datetimes(cls, value: datetime, info) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{info.field_name} must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_price(self) -> MarketPricePayload:
        decimals = (self.open, self.high, self.low, self.close, self.confidence)
        if any(not value.is_finite() for value in decimals):
            raise ValueError("price and confidence values must be finite Decimals")
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high is below another OHLC value")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low is above another OHLC value")
        if self.knowable_at < self.session_close_at:
            raise ValueError("price cannot be knowable before the bound session closes")
        if not (self.knowable_at <= self.produced_at <= self.recorded_at):
            raise ValueError("price normalization timestamps are out of order")
        return self


class MvpNormalizationDraft(BaseModel):
    """Source-owned typed payload before generic raw and registry lineage is attached."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    semantic_type_id: str = Field(pattern=r"^semantic\.[a-z0-9]+(?:[._-][a-z0-9]+)*$")
    payload: BaseModel
    subject: SubjectRef
    valid_from: date
    valid_to: date
    knowable_at: datetime
    produced_at: datetime
    recorded_at: datetime
    document_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$")
    confidence: Decimal = Field(ge=0, le=1)
    raw_ref: str = Field(pattern=r"^raw\.fetches:[1-9][0-9]*$")
    is_restatement: bool = False
    supersedes_record_id: str | None = Field(
        default=None,
        pattern=r"^normalized-record:[0-9a-f]{64}$",
    )
    supersedes_document_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$",
    )

    @field_validator("knowable_at", "produced_at", "recorded_at")
    @classmethod
    def validate_datetimes(cls, value: datetime, info) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{info.field_name} must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_draft(self) -> MvpNormalizationDraft:
        if self.valid_to < self.valid_from:
            raise ValueError("valid_to must not precede valid_from")
        if not (self.knowable_at <= self.produced_at <= self.recorded_at):
            raise ValueError("normalization timestamps are out of order")
        predecessor_count = sum(value is not None for value in (self.supersedes_record_id, self.supersedes_document_id))
        if self.is_restatement != (predecessor_count == 1):
            raise ValueError("restatements must name exactly one predecessor")
        return self


__all__ = ["MarketPricePayload", "MvpNormalizationDraft"]
