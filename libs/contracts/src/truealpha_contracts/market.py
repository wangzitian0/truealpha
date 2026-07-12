"""Point-in-time market, currency, valuation-path, and return contracts.

The contracts in this module keep prices on explicit listings, share counts on
explicit securities/share classes, and lifecycle events on an exact simulation
clock.  They deliberately encode the V1 convention instead of offering a choice
between adjusted prices and separately applied corporate actions.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.models import _require_aware
from truealpha_contracts.universe import (
    IssuerSecurityLink,
    SecurityKind,
    SecurityListingLink,
)

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_CURRENCY_PATTERN = r"^[A-Z]{3}$"
_STABLE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$")
_MUTABLE_REFERENCE_MARKERS = frozenset({"current", "head", "latest"})


def _require_stable_id(value: str, field_name: str) -> str:
    if value != value.strip() or not _STABLE_ID_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} must be a non-empty stable identifier")
    tokens = {token for token in re.split(r"[^a-z0-9]+", value.lower()) if token}
    mutable_markers = tokens & _MUTABLE_REFERENCE_MARKERS
    if mutable_markers:
        raise ValueError(f"{field_name} cannot use a mutable reference marker: {sorted(mutable_markers)}")
    return value


def _require_currency(value: str, field_name: str) -> str:
    if re.fullmatch(_CURRENCY_PATTERN, value) is None:
        raise ValueError(f"{field_name} must be an uppercase ISO-4217-style currency code")
    return value


def _hash_payload(schema: str, payload: dict[str, Any]) -> str:
    return canonical_sha256({"schema": schema, **payload})


def _date_in_interval(value: date, valid_from: date, valid_to: date | None) -> bool:
    return value >= valid_from and (valid_to is None or value <= valid_to)


class CurrencyPair(BaseModel):
    """One directed FX pair: quote-currency units per base-currency unit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    base_currency: str = Field(pattern=_CURRENCY_PATTERN)
    quote_currency: str = Field(pattern=_CURRENCY_PATTERN)

    @model_validator(mode="after")
    def reject_identity_pair(self) -> CurrencyPair:
        if self.base_currency == self.quote_currency:
            raise ValueError("an FX pair must contain two different currencies")
        return self


class FxRate(BaseModel):
    """An immutable PIT FX observation in one explicit direction."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_id: str
    pair: CurrencyPair
    quote_per_base: Decimal = Field(gt=0)
    valid_at: datetime
    knowable_at: datetime
    recorded_at: datetime
    confidence: Decimal = Field(ge=0, le=1)
    raw_ref: str = Field(min_length=1)

    @field_validator("input_id")
    @classmethod
    def validate_input_id(cls, value: str) -> str:
        return _require_stable_id(value, "input_id")

    @field_validator("valid_at", "knowable_at", "recorded_at")
    @classmethod
    def validate_datetimes(cls, value: datetime, info) -> datetime:
        return _require_aware(value, info.field_name)

    @model_validator(mode="after")
    def validate_time_order(self) -> FxRate:
        if self.knowable_at < self.valid_at:
            raise ValueError("knowable_at must not precede the FX valid_at time")
        if self.recorded_at < self.knowable_at:
            raise ValueError("recorded_at must not precede knowable_at")
        return self


class CurrencyConversionPolicy(BaseModel):
    """Content-addressed compatible-currency and directed-FX policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_id: str
    policy_version: str
    valuation_currency: str = Field(pattern=_CURRENCY_PATTERN)
    compatible_currencies: tuple[str, ...] = Field(min_length=1)
    permitted_fx_pairs: tuple[CurrencyPair, ...] = ()
    content_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("policy_id", "policy_version")
    @classmethod
    def validate_stable_identity(cls, value: str, info) -> str:
        return _require_stable_id(value, info.field_name)

    @field_validator("compatible_currencies")
    @classmethod
    def validate_compatible_currencies(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        currencies = tuple(sorted(_require_currency(value, "compatible_currency") for value in values))
        if len(currencies) != len(set(currencies)):
            raise ValueError("compatible_currencies must be unique")
        return currencies

    @field_validator("permitted_fx_pairs")
    @classmethod
    def normalize_pairs(cls, values: tuple[CurrencyPair, ...]) -> tuple[CurrencyPair, ...]:
        pairs = tuple(sorted(values, key=lambda pair: (pair.base_currency, pair.quote_currency)))
        pair_keys = [(pair.base_currency, pair.quote_currency) for pair in pairs]
        if len(pair_keys) != len(set(pair_keys)):
            raise ValueError("permitted_fx_pairs must be unique")
        return pairs

    @classmethod
    def compute_content_sha256(
        cls,
        *,
        policy_id: str,
        policy_version: str,
        valuation_currency: str,
        compatible_currencies: tuple[str, ...],
        permitted_fx_pairs: tuple[CurrencyPair, ...] = (),
    ) -> str:
        pairs = sorted(
            (pair.model_dump(mode="json") for pair in permitted_fx_pairs),
            key=lambda pair: (pair["base_currency"], pair["quote_currency"]),
        )
        return _hash_payload(
            "truealpha.currency-conversion-policy.v1",
            {
                "policy_id": policy_id,
                "policy_version": policy_version,
                "valuation_currency": valuation_currency,
                "compatible_currencies": sorted(compatible_currencies),
                "permitted_fx_pairs": pairs,
            },
        )

    @classmethod
    def create(
        cls,
        *,
        policy_id: str,
        policy_version: str,
        valuation_currency: str,
        compatible_currencies: tuple[str, ...],
        permitted_fx_pairs: tuple[CurrencyPair, ...] = (),
    ) -> CurrencyConversionPolicy:
        return cls(
            policy_id=policy_id,
            policy_version=policy_version,
            valuation_currency=valuation_currency,
            compatible_currencies=compatible_currencies,
            permitted_fx_pairs=permitted_fx_pairs,
            content_sha256=cls.compute_content_sha256(
                policy_id=policy_id,
                policy_version=policy_version,
                valuation_currency=valuation_currency,
                compatible_currencies=compatible_currencies,
                permitted_fx_pairs=permitted_fx_pairs,
            ),
        )

    @model_validator(mode="after")
    def validate_scope_and_hash(self) -> CurrencyConversionPolicy:
        compatible = set(self.compatible_currencies)
        if self.valuation_currency not in compatible:
            raise ValueError("valuation_currency must be in compatible_currencies")
        for pair in self.permitted_fx_pairs:
            if pair.base_currency not in compatible or pair.quote_currency not in compatible:
                raise ValueError("permitted FX pairs must remain inside compatible_currencies")
            if pair.quote_currency != self.valuation_currency:
                raise ValueError("V1 FX pairs must quote directly into valuation_currency")
        expected = self.compute_content_sha256(
            policy_id=self.policy_id,
            policy_version=self.policy_version,
            valuation_currency=self.valuation_currency,
            compatible_currencies=self.compatible_currencies,
            permitted_fx_pairs=self.permitted_fx_pairs,
        )
        if self.content_sha256 != expected:
            raise ValueError("currency policy content_sha256 does not match canonical content")
        return self

    def permits(self, pair: CurrencyPair) -> bool:
        return pair in self.permitted_fx_pairs


class CurrencyConversionBinding(BaseModel):
    """Binds a valuation to either identity conversion or one exact PIT FX row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    as_of: datetime
    source_currency: str = Field(pattern=_CURRENCY_PATTERN)
    target_currency: str = Field(pattern=_CURRENCY_PATTERN)
    policy: CurrencyConversionPolicy
    fx_rate: FxRate | None = None

    @field_validator("as_of")
    @classmethod
    def validate_as_of(cls, value: datetime) -> datetime:
        return _require_aware(value, "as_of")

    @model_validator(mode="after")
    def fail_closed_without_exact_fx(self) -> CurrencyConversionBinding:
        if self.target_currency != self.policy.valuation_currency:
            raise ValueError("target_currency must equal the policy valuation_currency")
        compatible = set(self.policy.compatible_currencies)
        if self.source_currency not in compatible or self.target_currency not in compatible:
            raise ValueError("currency is outside the accepted compatible-currency scope")
        if self.source_currency == self.target_currency:
            if self.fx_rate is not None:
                raise ValueError("same-currency valuation must not bind an FX row")
            return self
        expected_pair = CurrencyPair(
            base_currency=self.source_currency,
            quote_currency=self.target_currency,
        )
        if not self.policy.permits(expected_pair):
            raise ValueError("cross-currency pair is not permitted by the bound policy")
        if self.fx_rate is None:
            raise ValueError("cross-currency valuation requires an explicit PIT FX input")
        if self.fx_rate.pair != expected_pair:
            raise ValueError("FX input direction does not match the requested conversion")
        if self.fx_rate.valid_at > self.as_of or self.fx_rate.knowable_at > self.as_of:
            raise ValueError("FX input was not valid and knowable at the valuation cutoff")
        return self


class MarketSessionKind(StrEnum):
    REGULAR = "regular"
    EARLY_CLOSE = "early_close"


class MarketSession(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    session_date: date
    opens_at: datetime
    closes_at: datetime
    kind: MarketSessionKind = MarketSessionKind.REGULAR

    @field_validator("opens_at", "closes_at")
    @classmethod
    def validate_datetimes(cls, value: datetime, info) -> datetime:
        return _require_aware(value, info.field_name)

    @model_validator(mode="after")
    def validate_session(self) -> MarketSession:
        if self.closes_at <= self.opens_at:
            raise ValueError("closes_at must follow opens_at")
        return self


class MarketHoliday(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    holiday_date: date
    name: str = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("holiday name cannot have surrounding whitespace")
        return value


class ExchangeCalendar(BaseModel):
    """Content-addressed exchange sessions and explicit holidays."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    calendar_id: str
    calendar_version: str
    exchange_mic: str = Field(pattern=r"^[A-Z0-9]{4}$")
    timezone: str
    valid_from: date
    valid_to: date
    sessions: tuple[MarketSession, ...] = Field(min_length=1)
    holidays: tuple[MarketHoliday, ...] = ()
    content_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("calendar_id", "calendar_version")
    @classmethod
    def validate_stable_identity(cls, value: str, info) -> str:
        return _require_stable_id(value, info.field_name)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError) as error:
            raise ValueError("timezone must be a valid IANA timezone") from error
        return value

    @field_validator("sessions")
    @classmethod
    def normalize_sessions(cls, values: tuple[MarketSession, ...]) -> tuple[MarketSession, ...]:
        sessions = tuple(sorted(values, key=lambda session: session.session_date))
        dates = [session.session_date for session in sessions]
        if len(dates) != len(set(dates)):
            raise ValueError("calendar sessions must have unique dates")
        return sessions

    @field_validator("holidays")
    @classmethod
    def normalize_holidays(cls, values: tuple[MarketHoliday, ...]) -> tuple[MarketHoliday, ...]:
        holidays = tuple(sorted(values, key=lambda holiday: holiday.holiday_date))
        dates = [holiday.holiday_date for holiday in holidays]
        if len(dates) != len(set(dates)):
            raise ValueError("calendar holidays must have unique dates")
        return holidays

    @classmethod
    def compute_content_sha256(
        cls,
        *,
        calendar_id: str,
        calendar_version: str,
        exchange_mic: str,
        timezone: str,
        valid_from: date,
        valid_to: date,
        sessions: tuple[MarketSession, ...],
        holidays: tuple[MarketHoliday, ...] = (),
    ) -> str:
        return _hash_payload(
            "truealpha.exchange-calendar.v1",
            {
                "calendar_id": calendar_id,
                "calendar_version": calendar_version,
                "exchange_mic": exchange_mic,
                "timezone": timezone,
                "valid_from": valid_from.isoformat(),
                "valid_to": valid_to.isoformat(),
                "sessions": [
                    session.model_dump(mode="json")
                    for session in sorted(sessions, key=lambda value: value.session_date)
                ],
                "holidays": [
                    holiday.model_dump(mode="json")
                    for holiday in sorted(holidays, key=lambda value: value.holiday_date)
                ],
            },
        )

    @classmethod
    def create(
        cls,
        *,
        calendar_id: str,
        calendar_version: str,
        exchange_mic: str,
        timezone: str,
        valid_from: date,
        valid_to: date,
        sessions: tuple[MarketSession, ...],
        holidays: tuple[MarketHoliday, ...] = (),
    ) -> ExchangeCalendar:
        return cls(
            calendar_id=calendar_id,
            calendar_version=calendar_version,
            exchange_mic=exchange_mic,
            timezone=timezone,
            valid_from=valid_from,
            valid_to=valid_to,
            sessions=sessions,
            holidays=holidays,
            content_sha256=cls.compute_content_sha256(
                calendar_id=calendar_id,
                calendar_version=calendar_version,
                exchange_mic=exchange_mic,
                timezone=timezone,
                valid_from=valid_from,
                valid_to=valid_to,
                sessions=sessions,
                holidays=holidays,
            ),
        )

    @model_validator(mode="after")
    def validate_calendar_and_hash(self) -> ExchangeCalendar:
        if self.valid_to < self.valid_from:
            raise ValueError("valid_to must not precede valid_from")
        session_dates = {session.session_date for session in self.sessions}
        holiday_dates = {holiday.holiday_date for holiday in self.holidays}
        if overlap := session_dates & holiday_dates:
            raise ValueError(f"holidays cannot also be market sessions: {sorted(overlap)}")
        timezone = ZoneInfo(self.timezone)
        for session in self.sessions:
            if not _date_in_interval(session.session_date, self.valid_from, self.valid_to):
                raise ValueError("all sessions must be inside the calendar validity interval")
            if session.opens_at.astimezone(timezone).date() != session.session_date:
                raise ValueError("session_date must equal the exchange-local open date")
        for holiday in self.holidays:
            if not _date_in_interval(holiday.holiday_date, self.valid_from, self.valid_to):
                raise ValueError("all holidays must be inside the calendar validity interval")
        expected = self.compute_content_sha256(
            calendar_id=self.calendar_id,
            calendar_version=self.calendar_version,
            exchange_mic=self.exchange_mic,
            timezone=self.timezone,
            valid_from=self.valid_from,
            valid_to=self.valid_to,
            sessions=self.sessions,
            holidays=self.holidays,
        )
        if self.content_sha256 != expected:
            raise ValueError("exchange calendar content_sha256 does not match canonical content")
        return self

    def require_session(self, session_date: date) -> MarketSession:
        holiday = next((item for item in self.holidays if item.holiday_date == session_date), None)
        if holiday is not None:
            raise ValueError(f"{session_date.isoformat()} is a market holiday: {holiday.name}")
        session = next((item for item in self.sessions if item.session_date == session_date), None)
        if session is None:
            raise ValueError(f"{session_date.isoformat()} has no exchange session in the bound calendar")
        return session


class PriceBasis(StrEnum):
    UNADJUSTED = "unadjusted"
    ADJUSTED_RECONCILIATION_ONLY = "adjusted_reconciliation_only"


class ListingPriceBar(BaseModel):
    """One listing-level daily bar; V1 execution accepts unadjusted values only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_id: str
    listing_id: str
    calendar_id: str
    calendar_version: str
    trading_date: date
    session_close_at: datetime
    open: Decimal = Field(gt=0)
    high: Decimal = Field(gt=0)
    low: Decimal = Field(gt=0)
    close: Decimal = Field(gt=0)
    volume: int = Field(ge=0)
    currency: str = Field(pattern=_CURRENCY_PATTERN)
    price_basis: PriceBasis
    knowable_at: datetime
    recorded_at: datetime
    confidence: Decimal = Field(ge=0, le=1)
    raw_ref: str = Field(min_length=1)

    @field_validator("input_id", "listing_id", "calendar_id", "calendar_version")
    @classmethod
    def validate_stable_identity(cls, value: str, info) -> str:
        return _require_stable_id(value, info.field_name)

    @field_validator("session_close_at", "knowable_at", "recorded_at")
    @classmethod
    def validate_datetimes(cls, value: datetime, info) -> datetime:
        return _require_aware(value, info.field_name)

    @model_validator(mode="after")
    def validate_bar(self) -> ListingPriceBar:
        if self.price_basis is not PriceBasis.UNADJUSTED:
            raise ValueError("V1 listing price bars must be explicitly unadjusted")
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high is below another OHLC value")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low is above another OHLC value")
        if self.knowable_at < self.session_close_at:
            raise ValueError("daily bar knowable_at must not precede session close")
        if self.recorded_at < self.knowable_at:
            raise ValueError("recorded_at must not precede knowable_at")
        return self


class ShareCountBasis(StrEnum):
    POINT_IN_TIME_OUTSTANDING = "point_in_time_outstanding"


class SharesOutstanding(BaseModel):
    """Actual shares keyed to one exact security and share class."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_id: str
    security_id: str
    share_class: str
    basis: ShareCountBasis
    shares: Decimal = Field(gt=0)
    valid_at: date
    knowable_at: datetime
    recorded_at: datetime
    confidence: Decimal = Field(ge=0, le=1)
    raw_ref: str = Field(min_length=1)

    @field_validator("input_id", "security_id")
    @classmethod
    def validate_stable_identity(cls, value: str, info) -> str:
        return _require_stable_id(value, info.field_name)

    @field_validator("share_class")
    @classmethod
    def validate_share_class(cls, value: str) -> str:
        if not value or value != value.strip():
            raise ValueError("share_class must be non-empty and cannot have surrounding whitespace")
        return value

    @field_validator("knowable_at", "recorded_at")
    @classmethod
    def validate_datetimes(cls, value: datetime, info) -> datetime:
        return _require_aware(value, info.field_name)

    @model_validator(mode="after")
    def validate_time_order(self) -> SharesOutstanding:
        if self.recorded_at < self.knowable_at:
            raise ValueError("recorded_at must not precede knowable_at")
        return self


class CorporateActionType(StrEnum):
    SPLIT = "split"
    CASH_DIVIDEND = "cash_dividend"
    DELISTING = "delisting"
    SYMBOL_CHANGE = "symbol_change"
    PRIMARY_LISTING_CHANGE = "primary_listing_change"


class CorporateAction(BaseModel):
    """PIT corporate-action lifecycle keyed to a security/share class."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action_id: str
    action_type: CorporateActionType
    security_id: str
    share_class: str
    source_instrument_ids: tuple[str, ...] = Field(min_length=1)
    resulting_instrument_ids: tuple[str, ...] = ()
    source_listing_id: str | None = None
    resulting_listing_id: str | None = None
    declared_at: datetime
    knowable_at: datetime
    ex_at: datetime | None = None
    effective_at: datetime | None = None
    record_at: datetime | None = None
    pay_at: datetime | None = None
    split_ratio_after_per_before: Decimal | None = Field(default=None, gt=0)
    cash_amount_per_share: Decimal | None = Field(default=None, gt=0)
    cash_currency: str | None = Field(default=None, pattern=_CURRENCY_PATTERN)
    old_symbol: str | None = None
    new_symbol: str | None = None
    delisting_reason: str | None = None
    recorded_at: datetime
    confidence: Decimal = Field(ge=0, le=1)
    raw_ref: str = Field(min_length=1)

    @field_validator(
        "action_id",
        "security_id",
        "source_listing_id",
        "resulting_listing_id",
    )
    @classmethod
    def validate_stable_identity(cls, value: str | None, info) -> str | None:
        return None if value is None else _require_stable_id(value, info.field_name)

    @field_validator("source_instrument_ids", "resulting_instrument_ids")
    @classmethod
    def validate_instrument_ids(cls, values: tuple[str, ...], info) -> tuple[str, ...]:
        normalized = tuple(sorted(_require_stable_id(value, info.field_name) for value in values))
        if len(normalized) != len(set(normalized)):
            raise ValueError(f"{info.field_name} must be unique")
        return normalized

    @field_validator("share_class", "old_symbol", "new_symbol", "delisting_reason")
    @classmethod
    def validate_text(cls, value: str | None, info) -> str | None:
        if value is not None and (not value or value != value.strip()):
            raise ValueError(f"{info.field_name} must be non-empty and cannot have surrounding whitespace")
        return value

    @field_validator("declared_at", "knowable_at", "ex_at", "effective_at", "record_at", "pay_at", "recorded_at")
    @classmethod
    def validate_datetimes(cls, value: datetime | None, info) -> datetime | None:
        return None if value is None else _require_aware(value, info.field_name)

    @model_validator(mode="after")
    def validate_action_semantics(self) -> CorporateAction:
        if self.security_id not in self.source_instrument_ids:
            raise ValueError("source_instrument_ids must include the keyed security_id")
        if self.knowable_at < self.declared_at:
            raise ValueError("knowable_at must not precede declared_at")
        if self.recorded_at < self.knowable_at:
            raise ValueError("recorded_at must not precede knowable_at")

        if self.action_type is CorporateActionType.SPLIT:
            if self.ex_at is None or self.effective_at is None:
                raise ValueError("splits require ex_at and effective_at")
            if self.split_ratio_after_per_before is None or self.split_ratio_after_per_before == Decimal("1"):
                raise ValueError("splits require a non-unit split ratio")
            if not self.resulting_instrument_ids:
                raise ValueError("splits require resulting_instrument_ids")
            self._reject_fields(
                "cash_amount_per_share", "cash_currency", "old_symbol", "new_symbol", "delisting_reason"
            )
        elif self.action_type is CorporateActionType.CASH_DIVIDEND:
            if self.ex_at is None or self.record_at is None or self.pay_at is None:
                raise ValueError("cash dividends require ex_at, record_at, and pay_at")
            if self.cash_amount_per_share is None or self.cash_currency is None:
                raise ValueError("cash dividends require amount and currency")
            if not self.ex_at <= self.record_at <= self.pay_at:
                raise ValueError("dividend lifecycle must satisfy ex_at <= record_at <= pay_at")
            if self.resulting_instrument_ids:
                raise ValueError("cash dividends cannot create resulting instruments")
            self._reject_fields("split_ratio_after_per_before", "old_symbol", "new_symbol", "delisting_reason")
        elif self.action_type is CorporateActionType.DELISTING:
            if self.effective_at is None or self.source_listing_id is None or self.delisting_reason is None:
                raise ValueError("delistings require effective_at, source_listing_id, and reason")
            if self.resulting_instrument_ids or self.resulting_listing_id is not None:
                raise ValueError("a V1 delisting cannot imply a successor instrument or listing")
            self._reject_fields(
                "split_ratio_after_per_before",
                "cash_amount_per_share",
                "cash_currency",
                "old_symbol",
                "new_symbol",
            )
        elif self.action_type is CorporateActionType.SYMBOL_CHANGE:
            if self.effective_at is None or self.source_listing_id is None or self.resulting_listing_id is None:
                raise ValueError("symbol changes require effective_at and explicit source/resulting listings")
            if self.old_symbol is None or self.new_symbol is None or self.old_symbol == self.new_symbol:
                raise ValueError("symbol changes require distinct old_symbol and new_symbol")
            if not self.resulting_instrument_ids:
                raise ValueError("symbol changes require resulting_instrument_ids")
            self._reject_fields(
                "split_ratio_after_per_before",
                "cash_amount_per_share",
                "cash_currency",
                "delisting_reason",
            )
        else:
            if self.effective_at is None or self.source_listing_id is None or self.resulting_listing_id is None:
                raise ValueError("primary-listing changes require effective_at and source/resulting listings")
            if self.source_listing_id == self.resulting_listing_id:
                raise ValueError("primary-listing changes require distinct listings")
            if not self.resulting_instrument_ids:
                raise ValueError("primary-listing changes require resulting_instrument_ids")
            self._reject_fields(
                "split_ratio_after_per_before",
                "cash_amount_per_share",
                "cash_currency",
                "old_symbol",
                "new_symbol",
                "delisting_reason",
            )
        return self

    def _reject_fields(self, *field_names: str) -> None:
        populated = [field_name for field_name in field_names if getattr(self, field_name) is not None]
        if populated:
            raise ValueError(f"{self.action_type.value} cannot populate fields: {populated}")

    def lifecycle_times(self) -> dict[CorporateActionPhase, datetime]:
        values: tuple[tuple[CorporateActionPhase, datetime | None], ...] = (
            (CorporateActionPhase.DECLARATION, self.declared_at),
            (CorporateActionPhase.KNOWABLE, self.knowable_at),
            (CorporateActionPhase.EX, self.ex_at),
            (CorporateActionPhase.EFFECTIVE, self.effective_at),
            (CorporateActionPhase.RECORD, self.record_at),
            (CorporateActionPhase.PAY, self.pay_at),
        )
        return {phase: value for phase, value in values if value is not None}


class CorporateActionPhase(StrEnum):
    DECLARATION = "declaration"
    KNOWABLE = "knowable"
    EX = "ex"
    EFFECTIVE = "effective"
    RECORD = "record"
    PAY = "pay"


class CorporateActionClockTick(BaseModel):
    """One exact-once transition on the monotonic simulation clock."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tick_id: str
    action_id: str
    phase: CorporateActionPhase
    occurred_at: datetime
    applied_at: datetime
    sequence: int = Field(gt=0)

    @field_validator("tick_id", "action_id")
    @classmethod
    def validate_stable_identity(cls, value: str, info) -> str:
        return _require_stable_id(value, info.field_name)

    @field_validator("occurred_at", "applied_at")
    @classmethod
    def validate_datetimes(cls, value: datetime, info) -> datetime:
        return _require_aware(value, info.field_name)

    @model_validator(mode="after")
    def validate_application_time(self) -> CorporateActionClockTick:
        if self.applied_at < self.occurred_at:
            raise ValueError("applied_at must not precede the lifecycle occurrence")
        return self


class ReturnConvention(StrEnum):
    V1_UNADJUSTED_WITH_EXPLICIT_ACTIONS = "v1_unadjusted_with_explicit_actions"


class V1ReturnReplay(BaseModel):
    """Content-addressed V1 replay input with exact listing and clock bindings."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    replay_id: str
    convention: ReturnConvention
    security_id: str
    share_class: str
    listing_id: str
    as_of: datetime
    calendar: ExchangeCalendar
    price_bars: tuple[ListingPriceBar, ...] = Field(min_length=1)
    corporate_actions: tuple[CorporateAction, ...] = ()
    action_clock: tuple[CorporateActionClockTick, ...] = ()
    content_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("replay_id", "security_id", "listing_id")
    @classmethod
    def validate_stable_identity(cls, value: str, info) -> str:
        return _require_stable_id(value, info.field_name)

    @field_validator("share_class")
    @classmethod
    def validate_share_class(cls, value: str) -> str:
        if not value or value != value.strip():
            raise ValueError("share_class must be non-empty and cannot have surrounding whitespace")
        return value

    @field_validator("as_of")
    @classmethod
    def validate_as_of(cls, value: datetime) -> datetime:
        return _require_aware(value, "as_of")

    @classmethod
    def compute_content_sha256(
        cls,
        *,
        replay_id: str,
        convention: ReturnConvention,
        security_id: str,
        share_class: str,
        listing_id: str,
        as_of: datetime,
        calendar: ExchangeCalendar,
        price_bars: tuple[ListingPriceBar, ...],
        corporate_actions: tuple[CorporateAction, ...] = (),
        action_clock: tuple[CorporateActionClockTick, ...] = (),
    ) -> str:
        return _hash_payload(
            "truealpha.v1-return-replay.v1",
            {
                "replay_id": replay_id,
                "convention": ReturnConvention(convention).value,
                "security_id": security_id,
                "share_class": share_class,
                "listing_id": listing_id,
                "as_of": as_of.isoformat(),
                "calendar": calendar.model_dump(mode="json"),
                "price_bars": [
                    bar.model_dump(mode="json")
                    for bar in sorted(price_bars, key=lambda value: (value.trading_date, value.input_id))
                ],
                "corporate_actions": [
                    action.model_dump(mode="json")
                    for action in sorted(corporate_actions, key=lambda value: value.action_id)
                ],
                "action_clock": [
                    tick.model_dump(mode="json") for tick in sorted(action_clock, key=lambda value: value.sequence)
                ],
            },
        )

    @classmethod
    def create(
        cls,
        *,
        replay_id: str,
        security_id: str,
        share_class: str,
        listing_id: str,
        as_of: datetime,
        calendar: ExchangeCalendar,
        price_bars: tuple[ListingPriceBar, ...],
        corporate_actions: tuple[CorporateAction, ...] = (),
        action_clock: tuple[CorporateActionClockTick, ...] = (),
    ) -> V1ReturnReplay:
        convention = ReturnConvention.V1_UNADJUSTED_WITH_EXPLICIT_ACTIONS
        return cls(
            replay_id=replay_id,
            convention=convention,
            security_id=security_id,
            share_class=share_class,
            listing_id=listing_id,
            as_of=as_of,
            calendar=calendar,
            price_bars=price_bars,
            corporate_actions=corporate_actions,
            action_clock=action_clock,
            content_sha256=cls.compute_content_sha256(
                replay_id=replay_id,
                convention=convention,
                security_id=security_id,
                share_class=share_class,
                listing_id=listing_id,
                as_of=as_of,
                calendar=calendar,
                price_bars=price_bars,
                corporate_actions=corporate_actions,
                action_clock=action_clock,
            ),
        )

    @model_validator(mode="after")
    def validate_replay(self) -> V1ReturnReplay:
        if self.convention is not ReturnConvention.V1_UNADJUSTED_WITH_EXPLICIT_ACTIONS:
            raise ValueError("V1 replay has one return convention")

        bars = tuple(sorted(self.price_bars, key=lambda bar: (bar.trading_date, bar.input_id)))
        bar_ids = [bar.input_id for bar in bars]
        if len(bar_ids) != len(set(bar_ids)):
            raise ValueError("price-bar input_ids must be unique")
        if len({bar.trading_date for bar in bars}) != len(bars):
            raise ValueError("V1 replay accepts one price bar per listing session")
        for bar in bars:
            if bar.listing_id != self.listing_id:
                raise ValueError("price bars must bind the replay's explicit listing_id")
            if (bar.calendar_id, bar.calendar_version) != (
                self.calendar.calendar_id,
                self.calendar.calendar_version,
            ):
                raise ValueError("price bars must bind the exact exchange calendar version")
            session = self.calendar.require_session(bar.trading_date)
            if bar.session_close_at != session.closes_at:
                raise ValueError("price bar session_close_at must match the bound calendar session")
            if bar.knowable_at > self.as_of:
                raise ValueError("price bar was not knowable at the replay cutoff")

        actions = tuple(sorted(self.corporate_actions, key=lambda action: action.action_id))
        action_ids = [action.action_id for action in actions]
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("corporate-action IDs must be unique")
        action_by_id = {action.action_id: action for action in actions}
        for action in actions:
            if action.security_id != self.security_id or action.share_class != self.share_class:
                raise ValueError("corporate actions must match the replay security and share class")
            if action.knowable_at > self.as_of:
                raise ValueError("corporate action was not knowable at the replay cutoff")
            listing_ids = {action.source_listing_id, action.resulting_listing_id} - {None}
            if listing_ids and self.listing_id not in listing_ids:
                raise ValueError("listing lifecycle action does not include the explicit replay listing")

        ticks = tuple(sorted(self.action_clock, key=lambda tick: tick.sequence))
        if [tick.sequence for tick in ticks] != list(range(1, len(ticks) + 1)):
            raise ValueError("action-clock sequence must be contiguous and start at one")
        tick_ids = [tick.tick_id for tick in ticks]
        if len(tick_ids) != len(set(tick_ids)):
            raise ValueError("action-clock tick_ids must be unique")
        tick_keys = [(tick.action_id, tick.phase) for tick in ticks]
        if len(tick_keys) != len(set(tick_keys)):
            raise ValueError("each corporate-action lifecycle phase must be applied exactly once")
        if any(left.applied_at > right.applied_at for left, right in zip(ticks, ticks[1:], strict=False)):
            raise ValueError("action-clock applied_at values must be monotonic")

        expected_ticks: dict[tuple[str, CorporateActionPhase], datetime] = {}
        for action in actions:
            for phase, occurred_at in action.lifecycle_times().items():
                if occurred_at <= self.as_of:
                    expected_ticks[(action.action_id, phase)] = occurred_at
        if set(tick_keys) != set(expected_ticks):
            missing = sorted(f"{action_id}:{phase.value}" for action_id, phase in set(expected_ticks) - set(tick_keys))
            extra = sorted(f"{action_id}:{phase.value}" for action_id, phase in set(tick_keys) - set(expected_ticks))
            raise ValueError(
                f"action clock must cover every due lifecycle phase exactly once; missing={missing}, extra={extra}"
            )
        for tick in ticks:
            tick_action = action_by_id.get(tick.action_id)
            if tick_action is None:
                raise ValueError("action clock references an unknown corporate action")
            if tick.occurred_at != expected_ticks[(tick.action_id, tick.phase)]:
                raise ValueError("action-clock occurred_at must equal the corporate-action lifecycle time")
            if tick.applied_at < tick_action.knowable_at or tick.applied_at > self.as_of:
                raise ValueError("action-clock application must be knowable and inside the replay cutoff")

        object.__setattr__(self, "price_bars", bars)
        object.__setattr__(self, "corporate_actions", actions)
        object.__setattr__(self, "action_clock", ticks)
        expected_hash = self.compute_content_sha256(
            replay_id=self.replay_id,
            convention=self.convention,
            security_id=self.security_id,
            share_class=self.share_class,
            listing_id=self.listing_id,
            as_of=self.as_of,
            calendar=self.calendar,
            price_bars=bars,
            corporate_actions=actions,
            action_clock=ticks,
        )
        if self.content_sha256 != expected_hash:
            raise ValueError("return replay content_sha256 does not match canonical content")
        return self


class IssuerListingValuationPath(BaseModel):
    """Exact PIT issuer -> security/share class -> listing valuation binding."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path_id: str
    issuer_id: str
    as_of: datetime
    issuer_security_link: IssuerSecurityLink
    security_listing_link: SecurityListingLink
    shares_outstanding: SharesOutstanding
    price_bar: ListingPriceBar
    underlying_shares_per_listed_unit: Decimal = Field(gt=0)
    currency_binding: CurrencyConversionBinding
    content_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("path_id", "issuer_id")
    @classmethod
    def validate_stable_identity(cls, value: str, info) -> str:
        return _require_stable_id(value, info.field_name)

    @field_validator("as_of")
    @classmethod
    def validate_as_of(cls, value: datetime) -> datetime:
        return _require_aware(value, "as_of")

    @classmethod
    def compute_content_sha256(
        cls,
        *,
        path_id: str,
        issuer_id: str,
        as_of: datetime,
        issuer_security_link: IssuerSecurityLink,
        security_listing_link: SecurityListingLink,
        shares_outstanding: SharesOutstanding,
        price_bar: ListingPriceBar,
        underlying_shares_per_listed_unit: Decimal,
        currency_binding: CurrencyConversionBinding,
    ) -> str:
        return _hash_payload(
            "truealpha.issuer-listing-valuation-path.v1",
            {
                "path_id": path_id,
                "issuer_id": issuer_id,
                "as_of": as_of.isoformat(),
                "issuer_security_link": issuer_security_link.model_dump(mode="json"),
                "security_listing_link": security_listing_link.model_dump(mode="json"),
                "shares_outstanding": shares_outstanding.model_dump(mode="json"),
                "price_bar": price_bar.model_dump(mode="json"),
                "underlying_shares_per_listed_unit": str(underlying_shares_per_listed_unit),
                "currency_binding": currency_binding.model_dump(mode="json"),
            },
        )

    @classmethod
    def create(
        cls,
        *,
        path_id: str,
        issuer_id: str,
        as_of: datetime,
        issuer_security_link: IssuerSecurityLink,
        security_listing_link: SecurityListingLink,
        shares_outstanding: SharesOutstanding,
        price_bar: ListingPriceBar,
        underlying_shares_per_listed_unit: Decimal,
        currency_binding: CurrencyConversionBinding,
    ) -> IssuerListingValuationPath:
        return cls(
            path_id=path_id,
            issuer_id=issuer_id,
            as_of=as_of,
            issuer_security_link=issuer_security_link,
            security_listing_link=security_listing_link,
            shares_outstanding=shares_outstanding,
            price_bar=price_bar,
            underlying_shares_per_listed_unit=underlying_shares_per_listed_unit,
            currency_binding=currency_binding,
            content_sha256=cls.compute_content_sha256(
                path_id=path_id,
                issuer_id=issuer_id,
                as_of=as_of,
                issuer_security_link=issuer_security_link,
                security_listing_link=security_listing_link,
                shares_outstanding=shares_outstanding,
                price_bar=price_bar,
                underlying_shares_per_listed_unit=underlying_shares_per_listed_unit,
                currency_binding=currency_binding,
            ),
        )

    @model_validator(mode="after")
    def validate_explicit_path(self) -> IssuerListingValuationPath:
        issuer_link = self.issuer_security_link
        listing_link = self.security_listing_link
        shares = self.shares_outstanding
        bar = self.price_bar
        valuation_date = bar.trading_date

        if issuer_link.issuer_id != self.issuer_id:
            raise ValueError("issuer-security link must match the explicit issuer_id")
        if listing_link.security_id != issuer_link.security_id:
            raise ValueError("security-listing link must continue the exact issuer-security path")
        if bar.listing_id != listing_link.listing_id:
            raise ValueError("price bar must belong to the exact explicit listing")
        if (bar.calendar_id, bar.calendar_version) != (
            listing_link.trading_calendar_id,
            listing_link.trading_calendar_version,
        ):
            raise ValueError("price bar and listing link must bind the same calendar version")
        if not _date_in_interval(valuation_date, issuer_link.valid_from, issuer_link.valid_to):
            raise ValueError("issuer-security link is not valid on the valuation date")
        if not _date_in_interval(valuation_date, listing_link.valid_from, listing_link.valid_to):
            raise ValueError("security-listing link is not valid on the valuation date")
        if shares.valid_at > valuation_date:
            raise ValueError("shares outstanding cannot be valid after the valuation date")

        if issuer_link.security_kind is SecurityKind.COMMON_STOCK:
            if shares.security_id != issuer_link.security_id or shares.share_class != issuer_link.share_class:
                raise ValueError("common-stock valuation cannot substitute another security or share class")
            if self.underlying_shares_per_listed_unit != Decimal("1"):
                raise ValueError("common-stock valuation requires a one-to-one share multiplier")
        elif issuer_link.security_kind is SecurityKind.ADR:
            if shares.security_id != issuer_link.underlying_security_id:
                raise ValueError("ADR valuation shares must belong to the explicit underlying security")
            if self.underlying_shares_per_listed_unit != issuer_link.underlying_shares_per_security_unit:
                raise ValueError("ADR valuation multiplier must equal the identity-link ADR ratio")
        else:
            raise ValueError("V1 issuer valuation paths support common stock and ADR securities only")

        pit_inputs = (
            issuer_link.knowable_at,
            listing_link.knowable_at,
            shares.knowable_at,
            bar.knowable_at,
        )
        if any(knowable_at > self.as_of for knowable_at in pit_inputs):
            raise ValueError("valuation path contains an input not knowable at as_of")
        if self.currency_binding.as_of != self.as_of:
            raise ValueError("currency binding must use the exact valuation as_of")
        if self.currency_binding.source_currency != bar.currency:
            raise ValueError("currency binding source must equal the listing price currency")

        expected = self.compute_content_sha256(
            path_id=self.path_id,
            issuer_id=self.issuer_id,
            as_of=self.as_of,
            issuer_security_link=issuer_link,
            security_listing_link=listing_link,
            shares_outstanding=shares,
            price_bar=bar,
            underlying_shares_per_listed_unit=self.underlying_shares_per_listed_unit,
            currency_binding=self.currency_binding,
        )
        if self.content_sha256 != expected:
            raise ValueError("valuation path content_sha256 does not match canonical content")
        return self
