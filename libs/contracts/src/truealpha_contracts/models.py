"""Point-in-time DTOs derived from the Phase -1 sample payloads.

`valid_*` describes the real-world period. `knowable_at` is the transaction-time
cutoff used by backtests. `recorded_at` is merely when TrueAlpha ingested it.
"""

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
    # Identifier-resolution vendor (ISIN -> listings), not a fundamental-data
    # source — its responses land in raw like any other so KG assertions can
    # carry a raw_ref.
    OPENFIGI = "openfigi"


class EntityIdentifier(BaseModel):
    entity_id: str = Field(min_length=1)
    source: DataSource
    value: str = Field(min_length=1)
    identifier_type: str = Field(min_length=1)
    valid_from: date
    valid_to: date
    knowable_at: datetime
    recorded_at: datetime
    confidence: Decimal = Field(ge=0, le=1)
    raw_ref: str

    @field_validator("knowable_at", "recorded_at")
    @classmethod
    def validate_datetimes(cls, value: datetime, info) -> datetime:
        return _require_aware(value, info.field_name)

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


class RawCapture(BaseModel):
    """Bytes returned by a source before runtime persists them immutably."""

    source: DataSource
    source_record_id: str = Field(min_length=1)
    body: bytes
    content_type: str = Field(min_length=1)
    fetched_at: datetime
    source_published_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("fetched_at", "source_published_at")
    @classmethod
    def validate_datetimes(cls, value: datetime | None, info) -> datetime | None:
        return None if value is None else _require_aware(value, info.field_name)


class RawIngestionEnvelope(BaseModel):
    source: DataSource
    source_record_id: str = Field(min_length=1)
    object: RawObjectRef
    fetched_at: datetime
    source_published_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("fetched_at", "source_published_at")
    @classmethod
    def validate_datetimes(cls, value: datetime | None, info) -> datetime | None:
        return None if value is None else _require_aware(value, info.field_name)


class FinancialFact(BaseModel):
    """Normalized SEC/moomoo fact; source tags remain evidence, not factor logic."""

    entity_id: str
    metric: str
    value: Decimal | None
    unit: str
    fiscal_period: str
    valid_from: date
    valid_to: date
    knowable_at: datetime
    recorded_at: datetime
    confidence: Decimal = Field(ge=0, le=1)
    raw_ref: str
    source_metric: str
    # "<parser-id>:<schema-version>" (see truealpha_contracts.metrics) — keeps a
    # reparse under revised mapping logic distinguishable from a restatement.
    mapping_version: str = Field(min_length=1)
    accession: str | None = None
    form: str | None = None
    is_restatement: bool = False

    @field_validator("knowable_at", "recorded_at")
    @classmethod
    def validate_datetimes(cls, value: datetime, info) -> datetime:
        return _require_aware(value, info.field_name)

    @model_validator(mode="after")
    def validate_time_order(self) -> FinancialFact:
        if self.valid_to < self.valid_from:
            raise ValueError("valid_to must not precede valid_from")
        if self.recorded_at < self.knowable_at:
            raise ValueError("recorded_at must not precede knowable_at")
        return self


class AnalystRatingEvent(BaseModel):
    """A rating event with recommendation time separated from knowability.

    The sample contains both recommendation_date and vendor update_time. A
    backtest must supply a corroborated `knowable_at`; it may not silently use
    the recommendation date for a record obtained through a later backfill.
    """

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


class FundHolding(BaseModel):
    fund_id: str
    holding_id: str | None = None
    holding_name: str
    report_period: date
    knowable_at: datetime
    recorded_at: datetime
    cusip: str | None = None
    isin: str | None = None
    lei: str | None = None
    balance: Decimal | None = None
    value_usd: Decimal
    percent_of_net_assets: Decimal
    confidence: Decimal = Field(ge=0, le=1)
    raw_ref: str

    @field_validator("knowable_at", "recorded_at")
    @classmethod
    def validate_datetimes(cls, value: datetime, info) -> datetime:
        return _require_aware(value, info.field_name)

    @model_validator(mode="after")
    def validate_time_order(self) -> FundHolding:
        if self.recorded_at < self.knowable_at:
            raise ValueError("recorded_at must not precede knowable_at")
        return self


class PriceBar(BaseModel):
    entity_id: str
    symbol: str
    trading_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    adjusted_close: Decimal
    volume: int = Field(ge=0)
    knowable_at: datetime
    recorded_at: datetime
    source: DataSource
    raw_ref: str

    @field_validator("knowable_at", "recorded_at")
    @classmethod
    def validate_datetimes(cls, value: datetime, info) -> datetime:
        return _require_aware(value, info.field_name)

    @model_validator(mode="after")
    def validate_bar(self) -> PriceBar:
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high is below another OHLC value")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low is above another OHLC value")
        if self.recorded_at < self.knowable_at:
            raise ValueError("recorded_at must not precede knowable_at")
        return self


class GraphEdge(BaseModel):
    from_id: str
    to_id: str
    relation_type: str
    valid_from: date
    valid_to: date
    knowable_at: datetime
    recorded_at: datetime
    confidence: Decimal = Field(ge=0, le=1)
    raw_ref: str

    @field_validator("knowable_at", "recorded_at")
    @classmethod
    def validate_datetimes(cls, value: datetime, info) -> datetime:
        return _require_aware(value, info.field_name)

    @model_validator(mode="after")
    def validate_time_order(self) -> GraphEdge:
        if self.valid_to < self.valid_from:
            raise ValueError("valid_to must not precede valid_from")
        if self.recorded_at < self.knowable_at:
            raise ValueError("recorded_at must not precede knowable_at")
        return self


class AsOfQuery(BaseModel):
    """Mandatory replay boundary for every factor/backtest repository read."""

    entity_ids: tuple[str, ...] = Field(min_length=1)
    as_of: datetime
    valid_on: date | None = None

    @field_validator("as_of")
    @classmethod
    def validate_as_of(cls, value: datetime) -> datetime:
        return _require_aware(value, "as_of")


class BacktestDataset(BaseModel):
    """One immutable dataset resolved at a single transaction-time cutoff."""

    query: AsOfQuery
    financial_facts: tuple[FinancialFact, ...] = ()
    graph_edges: tuple[GraphEdge, ...] = ()
    analyst_ratings: tuple[AnalystRatingEvent, ...] = ()
    fund_holdings: tuple[FundHolding, ...] = ()
    price_bars: tuple[PriceBar, ...] = ()

    @model_validator(mode="after")
    def reject_lookahead(self) -> BacktestDataset:
        collections = (
            self.financial_facts,
            self.graph_edges,
            self.analyst_ratings,
            self.fund_holdings,
            self.price_bars,
        )
        if any(item.knowable_at > self.query.as_of for items in collections for item in items):
            raise ValueError("dataset contains a record that was not knowable at query.as_of")
        return self
