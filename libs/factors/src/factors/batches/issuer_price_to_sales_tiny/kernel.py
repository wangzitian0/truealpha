"""Provisional issuer-level P/S computation for the isolated S5 corpus."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.research import IssuerStrategyCandidate, LargeModelValueV0Binding

S4_MANIFEST_SHA256 = "3a72c29f0c965e3aa8eaa4464726d183622aa930a7780f9a2d3b51b9e25afa16"
S5_PREPARED_MANIFEST_SHA256 = "8df71e96351af78fa1a20e2ef3f98b98ddb6685be38b6ec75cd268c39bf880b5"
FROZEN_CORPUS_SHA256 = "7cf5e3d4e76eba73d749035ec27540c8529b8921fbf4510812e2e59f7a312d52"


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _require_aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _normalize_decimal(value: Decimal) -> Decimal:
    if value == 0:
        return Decimal("0")
    return value.normalize()


class InputAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class RevenuePeriodKind(StrEnum):
    FISCAL_QUARTER = "fiscal_quarter"
    FISCAL_YEAR = "fiscal_year"


class RevenueBasis(StrEnum):
    LATEST_FOUR_COMPLETE_QUARTERS = "latest_four_complete_quarters"
    LATEST_COMPLETE_FISCAL_YEAR = "latest_complete_fiscal_year"


class ResultAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class ReasonCode(StrEnum):
    BINDING_IDENTITY_MISMATCH = "binding_identity_mismatch"
    CANDIDATE_UNIVERSE_IDENTITY_MISMATCH = "candidate_universe_identity_mismatch"
    PRICE_TO_SALES_POLICY_IDENTITY_MISMATCH = "price_to_sales_policy_identity_mismatch"
    ISSUER_NOT_IN_CANDIDATE_UNIVERSE = "issuer_not_in_candidate_universe"
    UNEXPECTED_PRICE_LISTING = "unexpected_price_listing"
    UNEXPECTED_SHARES_SECURITY = "unexpected_shares_security"
    UNEXPECTED_REVENUE_ISSUER = "unexpected_revenue_issuer"
    UNEXPECTED_FX_RATE = "unexpected_fx_rate"
    MISSING_COMPONENT_PRICE = "missing_component_price"
    DUPLICATE_COMPONENT_PRICE = "duplicate_component_price"
    MISSING_ELIGIBLE_COMPONENT_SHARES = "missing_eligible_component_shares"
    DUPLICATE_COMPONENT_SHARES = "duplicate_component_shares"
    FUTURE_KNOWN_PRICE = "future_known_price"
    FUTURE_KNOWN_SHARES = "future_known_shares"
    FUTURE_KNOWN_REVENUE = "future_known_revenue"
    FUTURE_REVENUE_PERIOD = "future_revenue_period"
    FUTURE_KNOWN_FX = "future_known_fx"
    MISSING_PRICE_SESSION_FX = "missing_price_session_fx"
    DUPLICATE_PRICE_SESSION_FX = "duplicate_price_session_fx"
    MISSING_COMPLETE_REVENUE_WINDOW = "missing_complete_revenue_window"
    REVENUE_CURRENCY_MISMATCH = "revenue_currency_mismatch"
    NONPOSITIVE_REVENUE = "nonpositive_revenue"
    UNAVAILABLE_REQUIRED_INPUT = "unavailable_required_input"


class IssuerPriceToSalesTinyActivation(_StrictFrozenModel):
    """Exact development pins that cannot activate a release or schedule."""

    batch_id: Literal["S5-issuer-price-sales-kernel"] = "S5-issuer-price-sales-kernel"
    environment: Literal["local", "ci"]
    s4_manifest_sha256: str = Field(default=S4_MANIFEST_SHA256, pattern=r"^[0-9a-f]{64}$")
    s5_prepared_manifest_sha256: str = Field(
        default=S5_PREPARED_MANIFEST_SHA256,
        pattern=r"^[0-9a-f]{64}$",
    )
    frozen_corpus_sha256: str = Field(default=FROZEN_CORPUS_SHA256, pattern=r"^[0-9a-f]{64}$")
    semantic_policy_state: Literal["candidate_unapproved"] = "candidate_unapproved"
    live_source_allowed: Literal[False] = False
    staging_allowed: Literal[False] = False
    schedule_allowed: Literal[False] = False
    release_allowed: Literal[False] = False

    @model_validator(mode="after")
    def bind_exact_artifacts(self) -> Self:
        actual = (
            self.s4_manifest_sha256,
            self.s5_prepared_manifest_sha256,
            self.frozen_corpus_sha256,
        )
        expected = (S4_MANIFEST_SHA256, S5_PREPARED_MANIFEST_SHA256, FROZEN_CORPUS_SHA256)
        if actual != expected:
            raise ValueError("S5 activation artifact identity drifted")
        return self


class PriceObservation(_StrictFrozenModel):
    input_id: str = Field(min_length=1)
    listing_id: str = Field(min_length=1)
    value: Decimal | None = Field(default=None, gt=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    unit: Literal["currency_per_share"]
    session_close_at: datetime
    knowable_at: datetime
    confidence: Decimal = Field(ge=0, le=1)
    availability: InputAvailability
    price_basis: Literal["unadjusted_close"]

    @field_validator("session_close_at", "knowable_at")
    @classmethod
    def require_aware_time(cls, value: datetime, info) -> datetime:
        return _require_aware(value, info.field_name)

    @model_validator(mode="after")
    def validate_availability(self) -> Self:
        if (self.availability is InputAvailability.AVAILABLE) != (self.value is not None):
            raise ValueError("price value must exist exactly when the input is available")
        return self


class SharesObservation(_StrictFrozenModel):
    input_id: str = Field(min_length=1)
    security_id: str = Field(min_length=1)
    value: Decimal | None = Field(default=None, gt=0)
    unit: Literal["shares"]
    effective_on: date
    knowable_at: datetime
    confidence: Decimal = Field(ge=0, le=1)
    availability: InputAvailability
    corporate_action_basis: Literal["effective_shares"]

    @field_validator("knowable_at")
    @classmethod
    def require_aware_knowable_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "knowable_at")

    @model_validator(mode="after")
    def validate_availability(self) -> Self:
        if (self.availability is InputAvailability.AVAILABLE) != (self.value is not None):
            raise ValueError("shares value must exist exactly when the input is available")
        return self


class RevenueObservation(_StrictFrozenModel):
    input_id: str = Field(min_length=1)
    issuer_id: str = Field(min_length=1)
    value: Decimal | None = None
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    unit: Literal["currency"]
    period_kind: RevenuePeriodKind
    period_start: date
    period_end: date
    knowable_at: datetime
    confidence: Decimal = Field(ge=0, le=1)
    availability: InputAvailability
    complete: bool

    @field_validator("knowable_at")
    @classmethod
    def require_aware_knowable_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "knowable_at")

    @model_validator(mode="after")
    def validate_period_and_availability(self) -> Self:
        if self.period_start > self.period_end:
            raise ValueError("revenue period start must not follow its end")
        if (self.availability is InputAvailability.AVAILABLE) != (self.value is not None):
            raise ValueError("revenue value must exist exactly when the input is available")
        return self


class FxRateObservation(_StrictFrozenModel):
    input_id: str = Field(min_length=1)
    from_currency: str = Field(pattern=r"^[A-Z]{3}$")
    to_currency: str = Field(pattern=r"^[A-Z]{3}$")
    value: Decimal | None = Field(default=None, gt=0)
    unit: Literal["quote_per_base"]
    session_close_at: datetime
    knowable_at: datetime
    confidence: Decimal = Field(ge=0, le=1)
    availability: InputAvailability

    @field_validator("session_close_at", "knowable_at")
    @classmethod
    def require_aware_time(cls, value: datetime, info) -> datetime:
        return _require_aware(value, info.field_name)

    @model_validator(mode="after")
    def validate_pair_and_availability(self) -> Self:
        if self.from_currency == self.to_currency:
            raise ValueError("identity FX rates must not be supplied")
        if (self.availability is InputAvailability.AVAILABLE) != (self.value is not None):
            raise ValueError("FX value must exist exactly when the input is available")
        return self


class IssuerPriceToSalesRequest(_StrictFrozenModel):
    activation: IssuerPriceToSalesTinyActivation
    strategy_binding_id: str = Field(min_length=1)
    candidate_universe_id: str = Field(min_length=1)
    price_to_sales_policy_id: str = Field(min_length=1)
    issuer_id: str = Field(min_length=1)
    cutoff: datetime
    reporting_currency: str = Field(pattern=r"^[A-Z]{3}$")
    revenue_basis: RevenueBasis
    prices: tuple[PriceObservation, ...]
    shares: tuple[SharesObservation, ...]
    revenues: tuple[RevenueObservation, ...]
    fx_rates: tuple[FxRateObservation, ...]

    @field_validator("cutoff")
    @classmethod
    def require_aware_cutoff(cls, value: datetime) -> datetime:
        return _require_aware(value, "cutoff")

    @model_validator(mode="after")
    def canonicalize_inputs(self) -> Self:
        object.__setattr__(
            self,
            "prices",
            tuple(sorted(self.prices, key=lambda item: (item.listing_id, item.session_close_at, item.input_id))),
        )
        object.__setattr__(
            self,
            "shares",
            tuple(sorted(self.shares, key=lambda item: (item.security_id, item.effective_on, item.input_id))),
        )
        object.__setattr__(
            self,
            "revenues",
            tuple(
                sorted(
                    self.revenues,
                    key=lambda item: (item.period_start, item.period_end, item.input_id),
                )
            ),
        )
        object.__setattr__(
            self,
            "fx_rates",
            tuple(
                sorted(
                    self.fx_rates,
                    key=lambda item: (
                        item.from_currency,
                        item.to_currency,
                        item.session_close_at,
                        item.input_id,
                    ),
                )
            ),
        )
        input_ids = tuple(
            item.input_id for group in (self.prices, self.shares, self.revenues, self.fx_rates) for item in group
        )
        if len(input_ids) != len(set(input_ids)):
            raise ValueError("factor-visible input IDs must be unique")
        return self


class IssuerPriceToSalesTinyResult(_StrictFrozenModel):
    issuer_price_to_sales_id: str = Field(
        default="",
        pattern=r"^(?:|issuer-price-to-sales:[0-9a-f]{64})$",
    )
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    strategy_binding_id: str
    candidate_universe_id: str
    price_to_sales_policy_id: str
    issuer_id: str
    as_of: datetime
    reporting_currency: str = Field(pattern=r"^[A-Z]{3}$")
    availability: ResultAvailability
    price_to_sales: Decimal | None = None
    market_cap: Decimal | None = None
    revenue: Decimal | None = None
    confidence: Decimal = Field(ge=0, le=1)
    revenue_basis: RevenueBasis
    component_count: int = Field(ge=0)
    reason_codes: tuple[ReasonCode, ...] = ()
    stable_handoff: Literal[False] = False
    release_eligible: Literal[False] = False

    @field_validator("as_of")
    @classmethod
    def require_aware_as_of(cls, value: datetime) -> datetime:
        return _require_aware(value, "as_of")

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        values = (self.price_to_sales, self.market_cap, self.revenue)
        if self.availability is ResultAvailability.AVAILABLE:
            if any(value is None for value in values) or self.reason_codes:
                raise ValueError("available P/S output requires values and no reason codes")
        elif any(value is not None for value in values) or not self.reason_codes:
            raise ValueError("unavailable P/S output requires no values and at least one reason code")
        for field_name in ("price_to_sales", "market_cap", "revenue"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, _normalize_decimal(value))
        payload = self.model_dump(
            mode="json",
            exclude={"issuer_price_to_sales_id", "content_sha256"},
        )
        digest = canonical_sha256(payload)
        expected_id = f"issuer-price-to-sales:{digest}"
        if self.content_sha256 not in {"", digest} or self.issuer_price_to_sales_id not in {"", expected_id}:
            raise ValueError("issuer P/S result identity mismatch")
        object.__setattr__(self, "content_sha256", digest)
        object.__setattr__(self, "issuer_price_to_sales_id", expected_id)
        return self


def _candidate(binding: LargeModelValueV0Binding, issuer_id: str) -> IssuerStrategyCandidate | None:
    return next(
        (candidate for candidate in binding.candidate_universe.candidates if candidate.issuer.id == issuer_id),
        None,
    )


def _unavailable(
    binding: LargeModelValueV0Binding,
    request: IssuerPriceToSalesRequest,
    reason: ReasonCode,
    *,
    component_count: int,
) -> IssuerPriceToSalesTinyResult:
    return IssuerPriceToSalesTinyResult(
        strategy_binding_id=binding.strategy_binding_id,
        candidate_universe_id=binding.candidate_universe.candidate_universe_id,
        price_to_sales_policy_id=binding.price_to_sales_policy.price_to_sales_policy_id,
        issuer_id=request.issuer_id,
        as_of=request.cutoff.astimezone(UTC),
        reporting_currency=request.reporting_currency,
        availability=ResultAvailability.UNAVAILABLE,
        confidence=Decimal("0"),
        revenue_basis=request.revenue_basis,
        component_count=component_count,
        reason_codes=(reason,),
    )


def _revenue_window_is_complete(request: IssuerPriceToSalesRequest) -> bool:
    revenues = request.revenues
    if request.revenue_basis is RevenueBasis.LATEST_COMPLETE_FISCAL_YEAR:
        return len(revenues) == 1 and revenues[0].period_kind is RevenuePeriodKind.FISCAL_YEAR and revenues[0].complete
    if len(revenues) != 4 or any(
        item.period_kind is not RevenuePeriodKind.FISCAL_QUARTER or not item.complete for item in revenues
    ):
        return False
    return all(right.period_start == left.period_end + timedelta(days=1) for left, right in zip(revenues, revenues[1:]))


def compute_issuer_price_to_sales(
    binding: LargeModelValueV0Binding,
    request: IssuerPriceToSalesRequest,
) -> IssuerPriceToSalesTinyResult:
    """Validate one runner-selected PIT input set and compute issuer P/S."""

    candidate = _candidate(binding, request.issuer_id)
    component_count = len(candidate.market_value_components) if candidate is not None else 0
    identity_checks = (
        (
            request.strategy_binding_id == binding.strategy_binding_id,
            ReasonCode.BINDING_IDENTITY_MISMATCH,
        ),
        (
            request.candidate_universe_id == binding.candidate_universe.candidate_universe_id,
            ReasonCode.CANDIDATE_UNIVERSE_IDENTITY_MISMATCH,
        ),
        (
            request.price_to_sales_policy_id == binding.price_to_sales_policy.price_to_sales_policy_id,
            ReasonCode.PRICE_TO_SALES_POLICY_IDENTITY_MISMATCH,
        ),
    )
    for matches, reason in identity_checks:
        if not matches:
            return _unavailable(binding, request, reason, component_count=component_count)
    if candidate is None:
        return _unavailable(
            binding,
            request,
            ReasonCode.ISSUER_NOT_IN_CANDIDATE_UNIVERSE,
            component_count=0,
        )

    components = candidate.market_value_components
    expected_listings = {component.price_listing.id for component in components}
    expected_securities = {component.security.id for component in components}
    if any(item.listing_id not in expected_listings for item in request.prices):
        return _unavailable(binding, request, ReasonCode.UNEXPECTED_PRICE_LISTING, component_count=component_count)
    if any(item.security_id not in expected_securities for item in request.shares):
        return _unavailable(binding, request, ReasonCode.UNEXPECTED_SHARES_SECURITY, component_count=component_count)
    if any(item.issuer_id != request.issuer_id for item in request.revenues):
        return _unavailable(binding, request, ReasonCode.UNEXPECTED_REVENUE_ISSUER, component_count=component_count)
    all_inputs: tuple[PriceObservation | SharesObservation | RevenueObservation | FxRateObservation, ...] = (
        *request.prices,
        *request.shares,
        *request.revenues,
        *request.fx_rates,
    )
    if any(item.availability is not InputAvailability.AVAILABLE for item in all_inputs):
        return _unavailable(binding, request, ReasonCode.UNAVAILABLE_REQUIRED_INPUT, component_count=component_count)
    if any(item.knowable_at > request.cutoff or item.session_close_at > request.cutoff for item in request.prices):
        return _unavailable(binding, request, ReasonCode.FUTURE_KNOWN_PRICE, component_count=component_count)
    if any(item.knowable_at > request.cutoff for item in request.shares):
        return _unavailable(binding, request, ReasonCode.FUTURE_KNOWN_SHARES, component_count=component_count)
    if any(item.knowable_at > request.cutoff for item in request.revenues):
        return _unavailable(binding, request, ReasonCode.FUTURE_KNOWN_REVENUE, component_count=component_count)
    if any(item.period_end > request.cutoff.astimezone(UTC).date() for item in request.revenues):
        return _unavailable(binding, request, ReasonCode.FUTURE_REVENUE_PERIOD, component_count=component_count)
    if any(item.knowable_at > request.cutoff or item.session_close_at > request.cutoff for item in request.fx_rates):
        return _unavailable(binding, request, ReasonCode.FUTURE_KNOWN_FX, component_count=component_count)

    selected: list[PriceObservation | SharesObservation | RevenueObservation | FxRateObservation] = []
    market_cap = Decimal("0")
    for component in components:
        prices = [item for item in request.prices if item.listing_id == component.price_listing.id]
        if not prices:
            return _unavailable(binding, request, ReasonCode.MISSING_COMPONENT_PRICE, component_count=component_count)
        if len(prices) > 1:
            return _unavailable(binding, request, ReasonCode.DUPLICATE_COMPONENT_PRICE, component_count=component_count)
        price = prices[0]
        shares = [item for item in request.shares if item.security_id == component.security.id]
        if not shares or shares[0].effective_on > price.session_close_at.astimezone(UTC).date():
            return _unavailable(
                binding,
                request,
                ReasonCode.MISSING_ELIGIBLE_COMPONENT_SHARES,
                component_count=component_count,
            )
        if len(shares) > 1:
            return _unavailable(
                binding, request, ReasonCode.DUPLICATE_COMPONENT_SHARES, component_count=component_count
            )
        share = shares[0]
        assert price.value is not None and share.value is not None
        component_value = price.value * share.value
        selected.extend((price, share))
        if price.currency != request.reporting_currency:
            rates = [
                item
                for item in request.fx_rates
                if item.from_currency == price.currency
                and item.to_currency == request.reporting_currency
                and item.session_close_at == price.session_close_at
            ]
            if not rates:
                return _unavailable(
                    binding, request, ReasonCode.MISSING_PRICE_SESSION_FX, component_count=component_count
                )
            if len(rates) > 1:
                return _unavailable(
                    binding,
                    request,
                    ReasonCode.DUPLICATE_PRICE_SESSION_FX,
                    component_count=component_count,
                )
            rate = rates[0]
            assert rate.value is not None
            component_value *= rate.value
            selected.append(rate)
        market_cap += component_value

    if not _revenue_window_is_complete(request):
        return _unavailable(
            binding,
            request,
            ReasonCode.MISSING_COMPLETE_REVENUE_WINDOW,
            component_count=component_count,
        )
    if any(item.currency != request.reporting_currency for item in request.revenues):
        return _unavailable(binding, request, ReasonCode.REVENUE_CURRENCY_MISMATCH, component_count=component_count)
    revenue = sum((item.value or Decimal("0") for item in request.revenues), start=Decimal("0"))
    if revenue <= 0:
        return _unavailable(binding, request, ReasonCode.NONPOSITIVE_REVENUE, component_count=component_count)
    selected.extend(request.revenues)
    selected_fx_ids = {item.input_id for item in selected if isinstance(item, FxRateObservation)}
    if any(item.input_id not in selected_fx_ids for item in request.fx_rates):
        return _unavailable(binding, request, ReasonCode.UNEXPECTED_FX_RATE, component_count=component_count)
    confidence = min(item.confidence for item in selected)
    return IssuerPriceToSalesTinyResult(
        strategy_binding_id=binding.strategy_binding_id,
        candidate_universe_id=binding.candidate_universe.candidate_universe_id,
        price_to_sales_policy_id=binding.price_to_sales_policy.price_to_sales_policy_id,
        issuer_id=request.issuer_id,
        as_of=request.cutoff.astimezone(UTC),
        reporting_currency=request.reporting_currency,
        availability=ResultAvailability.AVAILABLE,
        price_to_sales=_normalize_decimal(market_cap / revenue),
        market_cap=_normalize_decimal(market_cap),
        revenue=_normalize_decimal(revenue),
        confidence=confidence,
        revenue_basis=request.revenue_basis,
        component_count=component_count,
    )
