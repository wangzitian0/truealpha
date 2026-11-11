"""Point-in-time DTOs separating valid time, knowable-at transaction time, and ingestion recorded time."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


def _require_aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


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


class _PitRecordBase(BaseModel):
    knowable_at: datetime
    recorded_at: datetime

    @field_validator("knowable_at", "recorded_at")
    @classmethod
    def validate_datetimes(cls, value: datetime, info) -> datetime:
        return _require_aware(value, info.field_name)


class EntityIdentifier(_PitRecordBase):
    entity_id: str = Field(min_length=1)
    source: DataSource
    value: str = Field(min_length=1)
    identifier_type: str = Field(min_length=1)
    valid_from: date
    valid_to: date
    confidence: Decimal = Field(ge=0, le=1)
    raw_ref: str

    @model_validator(mode="after")
    def validate_time_order(self) -> EntityIdentifier:
        if self.valid_to < self.valid_from:
            raise ValueError("valid_to must not precede valid_from")
        if self.recorded_at < self.knowable_at:
            raise ValueError("recorded_at must not precede knowable_at")
        return self


class RawObjectRef(BaseModel):
    bucket: str = Field(min_length=3)
    key: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_length: int = Field(ge=0)
    content_type: str = Field(min_length=1)

    @property
    def uri(self) -> str:
        return f"s3://{self.bucket}/{self.key}"


class _RawEnvelopeBase(BaseModel):
    source: DataSource
    source_record_id: str = Field(min_length=1)
    fetched_at: datetime
    source_published_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("fetched_at", "source_published_at")
    @classmethod
    def validate_datetimes(cls, value: datetime | None, info) -> datetime | None:
        return None if value is None else _require_aware(value, info.field_name)


class RawCapture(_RawEnvelopeBase):
    body: bytes
    content_type: str = Field(min_length=1)


class RawIngestionEnvelope(_RawEnvelopeBase):
    object: RawObjectRef


class AnalystRatingEvent(BaseModel):
    analyst_id: str
    company_id: str
    recommendation_at: datetime
    knowable_at: datetime
    recorded_at: datetime
    vendor_updated_at: datetime | None = None
    rating: int = Field(ge=1, le=5)
    target_price: Decimal | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    source_url: str | None = None
    confidence: Decimal = Field(ge=0, le=1)
    raw_ref: str

    @field_validator("recommendation_at", "knowable_at", "recorded_at", "vendor_updated_at")
    @classmethod
    def validate_datetimes(cls, value: datetime | None, info) -> datetime | None:
        return None if value is None else _require_aware(value, info.field_name)

    @model_validator(mode="after")
    def validate_time_order(self) -> AnalystRatingEvent:
        if self.knowable_at < self.recommendation_at:
            raise ValueError("knowable_at must not precede recommendation_at")
        if self.recorded_at < self.knowable_at:
            raise ValueError("recorded_at must not precede knowable_at")
        return self


class FundHolding(_PitRecordBase):
    fund_id: str
    holding_id: str | None = None
    holding_name: str
    report_period: date
    cusip: str | None = None
    isin: str | None = None
    lei: str | None = None
    balance: Decimal | None = None
    value_usd: Decimal
    percent_of_net_assets: Decimal
    confidence: Decimal = Field(ge=0, le=1)
    raw_ref: str

    @model_validator(mode="after")
    def validate_time_order(self) -> FundHolding:
        if self.recorded_at < self.knowable_at:
            raise ValueError("recorded_at must not precede knowable_at")
        return self


class PriceBar(_PitRecordBase):
    entity_id: str
    symbol: str
    trading_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    adjusted_close: Decimal
    volume: int = Field(ge=0)
    source: DataSource
    raw_ref: str

    @model_validator(mode="after")
    def validate_bar(self) -> PriceBar:
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high is below another OHLC value")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low is above another OHLC value")
        if self.recorded_at < self.knowable_at:
            raise ValueError("recorded_at must not precede knowable_at")
        return self


class GraphEdge(_PitRecordBase):
    from_id: str
    to_id: str
    relation_type: str
    valid_from: date
    valid_to: date
    confidence: Decimal = Field(ge=0, le=1)
    raw_ref: str

    @model_validator(mode="after")
    def validate_time_order(self) -> GraphEdge:
        if self.valid_to < self.valid_from:
            raise ValueError("valid_to must not precede valid_from")
        if self.recorded_at < self.knowable_at:
            raise ValueError("recorded_at must not precede knowable_at")
        return self


class AsOfQuery(BaseModel):
    entity_ids: tuple[str, ...] = Field(min_length=1)
    as_of: datetime
    valid_on: date | None = None

    @field_validator("as_of")
    @classmethod
    def validate_as_of(cls, value: datetime) -> datetime:
        return _require_aware(value, "as_of")


class BacktestDataset(BaseModel):
    query: AsOfQuery
    graph_edges: tuple[GraphEdge, ...] = ()
    analyst_ratings: tuple[AnalystRatingEvent, ...] = ()
    fund_holdings: tuple[FundHolding, ...] = ()
    price_bars: tuple[PriceBar, ...] = ()

    @model_validator(mode="after")
    def reject_lookahead(self) -> BacktestDataset:
        collections = (self.graph_edges, self.analyst_ratings, self.fund_holdings, self.price_bars)
        if any(item.knowable_at > self.query.as_of for items in collections for item in items):
            raise ValueError("dataset contains a record that was not knowable at query.as_of")
        return self
