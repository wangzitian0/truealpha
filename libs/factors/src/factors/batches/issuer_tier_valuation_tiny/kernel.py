"""Provisional issuer tier and valuation-gap composition for the S6 corpus."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from enum import StrEnum
from typing import Literal, Self

from factors.batches.issuer_price_to_sales_tiny.kernel import (
    IssuerPriceToSalesTinyResult,
)
from factors.batches.issuer_price_to_sales_tiny.kernel import (
    ResultAvailability as PriceToSalesAvailability,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.research import LargeModelValueV0Binding, ValuationTier

S4_MANIFEST_SHA256 = "3a72c29f0c965e3aa8eaa4464726d183622aa930a7780f9a2d3b51b9e25afa16"
S5_TERMINAL_MANIFEST_SHA256 = "0b15868afa439684c8c91bdaa14afbabb09eb8580e6bc158d4428a5e0e08b7de"
S6_PREPARED_MANIFEST_SHA256 = "a7e3de12886dbac45aa259158afe71672a1832f3ca9279e8050a892ed8086bde"
FROZEN_CORPUS_SHA256 = "8e0c10c1e0a1ceac78e17daa8e577bb85442f61b9f031f56de872313c8677476"
SEMANTIC_CANDIDATE_SHA256 = "d0b2865cbde85181bb17801ac3be467c5049906f793876c8b6ac319b7525cc5a"
PUBLIC_GOLDEN_MANIFEST_SHA256 = "8a9e1d23ea633f772c16cfaff6706518acce6cbcba5d343eaa33a0acdb01a8bc"

_DECIMAL_CONTEXT = Context(prec=28, rounding=ROUND_HALF_EVEN)
_TARGET_PS = {
    ValuationTier.TRADITIONAL: (Decimal("3"), Decimal("4")),
    ValuationTier.TECH: (Decimal("8"), Decimal("10")),
    ValuationTier.LARGE_MODEL_NATIVE: (Decimal("20"), Decimal("30")),
}


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _normalize_decimal(value: Decimal) -> Decimal:
    if value == 0:
        return Decimal("0")
    canonical_context = Context(
        prec=max(_DECIMAL_CONTEXT.prec, len(value.as_tuple().digits)),
        rounding=ROUND_HALF_EVEN,
    )
    with localcontext(canonical_context):
        return value.normalize()


class GppeAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class GppeMetric(StrEnum):
    GPPE_LEVEL = "gppe_level"
    FINANCIAL_EFFICIENCY = "financial_efficiency"


class TierValuationAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class TierValuationReasonCode(StrEnum):
    BINDING_IDENTITY_MISMATCH = "binding_identity_mismatch"
    CANDIDATE_UNIVERSE_IDENTITY_MISMATCH = "candidate_universe_identity_mismatch"
    PRICE_TO_SALES_POLICY_IDENTITY_MISMATCH = "price_to_sales_policy_identity_mismatch"
    ISSUER_NOT_IN_CANDIDATE_UNIVERSE = "issuer_not_in_candidate_universe"
    ISSUER_IDENTITY_MISMATCH = "issuer_identity_mismatch"
    CUTOFF_IDENTITY_MISMATCH = "cutoff_identity_mismatch"
    FUTURE_GPPE = "future_gppe"
    REPORTING_CURRENCY_MISMATCH = "reporting_currency_mismatch"
    MISSING_GPPE = "missing_gppe"
    UNAVAILABLE_GPPE = "unavailable_gppe"
    FINANCIAL_TIER_MAPPING_UNAPPROVED = "financial_tier_mapping_unapproved"
    UNAVAILABLE_PRICE_TO_SALES = "unavailable_price_to_sales"
    NONPOSITIVE_PRICE_TO_SALES = "nonpositive_price_to_sales"


class IssuerTierValuationTinyActivation(_StrictFrozenModel):
    """Exact candidate-only pins; this activation cannot reach a live environment."""

    batch_id: Literal["S6-issuer-tier-valuation"] = "S6-issuer-tier-valuation"
    environment: Literal["local", "ci"]
    s4_manifest_sha256: str = Field(default=S4_MANIFEST_SHA256, pattern=r"^[0-9a-f]{64}$")
    s5_terminal_manifest_sha256: str = Field(
        default=S5_TERMINAL_MANIFEST_SHA256,
        pattern=r"^[0-9a-f]{64}$",
    )
    s6_prepared_manifest_sha256: str = Field(
        default=S6_PREPARED_MANIFEST_SHA256,
        pattern=r"^[0-9a-f]{64}$",
    )
    frozen_corpus_sha256: str = Field(default=FROZEN_CORPUS_SHA256, pattern=r"^[0-9a-f]{64}$")
    semantic_candidate_sha256: str = Field(
        default=SEMANTIC_CANDIDATE_SHA256,
        pattern=r"^[0-9a-f]{64}$",
    )
    public_golden_manifest_sha256: str = Field(
        default=PUBLIC_GOLDEN_MANIFEST_SHA256,
        pattern=r"^[0-9a-f]{64}$",
    )
    semantic_policy_state: Literal["candidate_unapproved"] = "candidate_unapproved"
    live_source_allowed: Literal[False] = False
    staging_allowed: Literal[False] = False
    schedule_allowed: Literal[False] = False
    release_allowed: Literal[False] = False

    @model_validator(mode="after")
    def bind_exact_artifacts(self) -> Self:
        actual = (
            self.s4_manifest_sha256,
            self.s5_terminal_manifest_sha256,
            self.s6_prepared_manifest_sha256,
            self.frozen_corpus_sha256,
            self.semantic_candidate_sha256,
            self.public_golden_manifest_sha256,
        )
        expected = (
            S4_MANIFEST_SHA256,
            S5_TERMINAL_MANIFEST_SHA256,
            S6_PREPARED_MANIFEST_SHA256,
            FROZEN_CORPUS_SHA256,
            SEMANTIC_CANDIDATE_SHA256,
            PUBLIC_GOLDEN_MANIFEST_SHA256,
        )
        if actual != expected:
            raise ValueError("S6 activation artifact identity drifted")
        return self


class GppeLevelObservation(_StrictFrozenModel):
    """One provenance-neutral runner-selected GPPE or financial-efficiency value."""

    input_id: str = Field(min_length=1)
    entity_id: str = Field(min_length=1)
    metric: GppeMetric
    value: Decimal | None = None
    unit: str = Field(pattern=r"^[A-Z]{3}_per_employee$")
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    as_of: datetime
    confidence: Decimal = Field(ge=0, le=1)
    availability: GppeAvailability

    @field_validator("as_of")
    @classmethod
    def require_aware_as_of(cls, value: datetime) -> datetime:
        return _aware_utc(value, "as_of")

    @model_validator(mode="after")
    def validate_semantic_value(self) -> Self:
        if self.unit != f"{self.currency}_per_employee":
            raise ValueError("GPPE unit must match its currency")
        if (self.availability is GppeAvailability.AVAILABLE) != (self.value is not None):
            raise ValueError("GPPE value must exist exactly when the observation is available")
        if self.value is not None:
            if not self.value.is_finite():
                raise ValueError("GPPE value must be finite")
            object.__setattr__(self, "value", _normalize_decimal(self.value))
        return self


class IssuerTierValuationRequest(_StrictFrozenModel):
    activation: IssuerTierValuationTinyActivation
    strategy_binding_id: str = Field(min_length=1)
    candidate_universe_id: str = Field(min_length=1)
    price_to_sales_policy_id: str = Field(min_length=1)
    issuer_id: str = Field(min_length=1)
    cutoff: datetime
    reporting_currency: str = Field(pattern=r"^[A-Z]{3}$")
    gppe: GppeLevelObservation | None
    price_to_sales: IssuerPriceToSalesTinyResult

    @field_validator("cutoff")
    @classmethod
    def require_aware_cutoff(cls, value: datetime) -> datetime:
        return _aware_utc(value, "cutoff")


class IssuerTierValuationTinyResult(_StrictFrozenModel):
    issuer_tier_valuation_id: str = Field(
        default="",
        pattern=r"^(?:|issuer-tier-valuation:[0-9a-f]{64})$",
    )
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    strategy_binding_id: str
    candidate_universe_id: str
    price_to_sales_policy_id: str
    issuer_id: str
    as_of: datetime
    reporting_currency: str = Field(pattern=r"^[A-Z]{3}$")
    availability: TierValuationAvailability
    tier: ValuationTier | None = None
    target_ps_lower: Decimal | None = None
    target_ps_upper: Decimal | None = None
    target_ps_midpoint: Decimal | None = None
    current_price_to_sales: Decimal | None = None
    valuation_gap: Decimal | None = None
    confidence: Decimal = Field(ge=0, le=1)
    reason_codes: tuple[TierValuationReasonCode, ...] = ()
    semantic_policy_state: Literal["candidate_unapproved"] = "candidate_unapproved"
    semantic_candidate_sha256: str = Field(default=SEMANTIC_CANDIDATE_SHA256, pattern=r"^[0-9a-f]{64}$")
    public_golden_manifest_sha256: str = Field(
        default=PUBLIC_GOLDEN_MANIFEST_SHA256,
        pattern=r"^[0-9a-f]{64}$",
    )
    stable_handoff: Literal[False] = False
    release_eligible: Literal[False] = False

    @field_validator("as_of")
    @classmethod
    def require_aware_as_of(cls, value: datetime) -> datetime:
        return _aware_utc(value, "as_of")

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        if (
            self.semantic_candidate_sha256 != SEMANTIC_CANDIDATE_SHA256
            or self.public_golden_manifest_sha256 != PUBLIC_GOLDEN_MANIFEST_SHA256
        ):
            raise ValueError("S6 result semantic artifact identity drifted")
        values = (
            self.tier,
            self.target_ps_lower,
            self.target_ps_upper,
            self.target_ps_midpoint,
            self.current_price_to_sales,
            self.valuation_gap,
        )
        if self.availability is TierValuationAvailability.AVAILABLE:
            if any(value is None for value in values) or self.reason_codes:
                raise ValueError("available tier valuation requires complete values and no reason codes")
            assert self.tier is not None
            assert self.target_ps_lower is not None
            assert self.target_ps_upper is not None
            assert self.target_ps_midpoint is not None
            assert self.current_price_to_sales is not None
            assert self.valuation_gap is not None
            decimal_values = (
                self.target_ps_lower,
                self.target_ps_upper,
                self.target_ps_midpoint,
                self.current_price_to_sales,
                self.valuation_gap,
            )
            if any(not value.is_finite() for value in decimal_values):
                raise ValueError("available tier valuation values must be finite")
            if self.target_ps_lower >= self.target_ps_upper or self.current_price_to_sales <= 0:
                raise ValueError("available tier valuation has invalid bounds or P/S")
            if (self.target_ps_lower, self.target_ps_upper) != _TARGET_PS[self.tier]:
                raise ValueError("target P/S band does not match the assigned tier")
            with localcontext(_DECIMAL_CONTEXT):
                expected_midpoint = (self.target_ps_lower + self.target_ps_upper) / Decimal("2")
                expected_gap = expected_midpoint / self.current_price_to_sales - Decimal("1")
            if self.target_ps_midpoint != expected_midpoint:
                raise ValueError("target P/S midpoint does not match its band")
            if self.valuation_gap != expected_gap:
                raise ValueError("valuation gap does not match target_ps/current_ps-1")
        elif any(value is not None for value in values) or not self.reason_codes:
            raise ValueError("unavailable tier valuation requires no values and at least one reason code")
        for field_name in (
            "target_ps_lower",
            "target_ps_upper",
            "target_ps_midpoint",
            "current_price_to_sales",
            "valuation_gap",
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, _normalize_decimal(value))
        reasons = tuple(sorted(set(self.reason_codes), key=str))
        object.__setattr__(self, "reason_codes", reasons)
        payload = self.model_dump(mode="json", exclude={"issuer_tier_valuation_id", "content_sha256"})
        digest = canonical_sha256(payload)
        expected_id = f"issuer-tier-valuation:{digest}"
        if self.content_sha256 not in {"", digest} or self.issuer_tier_valuation_id not in {"", expected_id}:
            raise ValueError("issuer tier valuation result identity mismatch")
        object.__setattr__(self, "content_sha256", digest)
        object.__setattr__(self, "issuer_tier_valuation_id", expected_id)
        return self


def _unavailable(
    binding: LargeModelValueV0Binding,
    request: IssuerTierValuationRequest,
    reason: TierValuationReasonCode,
) -> IssuerTierValuationTinyResult:
    return IssuerTierValuationTinyResult(
        strategy_binding_id=binding.strategy_binding_id,
        candidate_universe_id=binding.candidate_universe.candidate_universe_id,
        price_to_sales_policy_id=binding.price_to_sales_policy.price_to_sales_policy_id,
        issuer_id=request.issuer_id,
        as_of=request.cutoff,
        reporting_currency=request.reporting_currency,
        availability=TierValuationAvailability.UNAVAILABLE,
        confidence=Decimal("0"),
        reason_codes=(reason,),
    )


def _issuer_is_candidate(binding: LargeModelValueV0Binding, issuer_id: str) -> bool:
    return any(candidate.issuer.id == issuer_id for candidate in binding.candidate_universe.candidates)


def tier_for_gppe(value: Decimal) -> ValuationTier:
    if value < Decimal("1000000"):
        return ValuationTier.TRADITIONAL
    if value < Decimal("3000000"):
        return ValuationTier.TECH
    return ValuationTier.LARGE_MODEL_NATIVE


def compute_issuer_tier_valuation(
    binding: LargeModelValueV0Binding,
    request: IssuerTierValuationRequest,
) -> IssuerTierValuationTinyResult:
    """Compose one candidate-only tier and valuation gap from exact same-cutoff inputs."""

    identity_checks = (
        (
            request.strategy_binding_id == binding.strategy_binding_id
            and request.price_to_sales.strategy_binding_id == binding.strategy_binding_id,
            TierValuationReasonCode.BINDING_IDENTITY_MISMATCH,
        ),
        (
            request.candidate_universe_id == binding.candidate_universe.candidate_universe_id
            and request.price_to_sales.candidate_universe_id == binding.candidate_universe.candidate_universe_id,
            TierValuationReasonCode.CANDIDATE_UNIVERSE_IDENTITY_MISMATCH,
        ),
        (
            request.price_to_sales_policy_id == binding.price_to_sales_policy.price_to_sales_policy_id
            and request.price_to_sales.price_to_sales_policy_id
            == binding.price_to_sales_policy.price_to_sales_policy_id,
            TierValuationReasonCode.PRICE_TO_SALES_POLICY_IDENTITY_MISMATCH,
        ),
    )
    for matches, reason in identity_checks:
        if not matches:
            return _unavailable(binding, request, reason)
    if not _issuer_is_candidate(binding, request.issuer_id):
        return _unavailable(binding, request, TierValuationReasonCode.ISSUER_NOT_IN_CANDIDATE_UNIVERSE)
    if request.price_to_sales.issuer_id != request.issuer_id:
        return _unavailable(binding, request, TierValuationReasonCode.ISSUER_IDENTITY_MISMATCH)
    if request.price_to_sales.as_of != request.cutoff:
        return _unavailable(binding, request, TierValuationReasonCode.CUTOFF_IDENTITY_MISMATCH)
    if request.price_to_sales.reporting_currency != request.reporting_currency:
        return _unavailable(binding, request, TierValuationReasonCode.REPORTING_CURRENCY_MISMATCH)
    if request.price_to_sales.availability is not PriceToSalesAvailability.AVAILABLE:
        return _unavailable(binding, request, TierValuationReasonCode.UNAVAILABLE_PRICE_TO_SALES)
    current_ps = request.price_to_sales.price_to_sales
    if current_ps is None:
        return _unavailable(binding, request, TierValuationReasonCode.UNAVAILABLE_PRICE_TO_SALES)
    if current_ps <= 0:
        return _unavailable(binding, request, TierValuationReasonCode.NONPOSITIVE_PRICE_TO_SALES)
    if request.gppe is None:
        return _unavailable(binding, request, TierValuationReasonCode.MISSING_GPPE)
    if request.gppe.entity_id != request.issuer_id:
        return _unavailable(binding, request, TierValuationReasonCode.ISSUER_IDENTITY_MISMATCH)
    if request.gppe.as_of > request.cutoff:
        return _unavailable(binding, request, TierValuationReasonCode.FUTURE_GPPE)
    if request.gppe.as_of != request.cutoff:
        return _unavailable(binding, request, TierValuationReasonCode.CUTOFF_IDENTITY_MISMATCH)
    if request.gppe.currency != request.reporting_currency:
        return _unavailable(binding, request, TierValuationReasonCode.REPORTING_CURRENCY_MISMATCH)
    if request.gppe.availability is not GppeAvailability.AVAILABLE or request.gppe.value is None:
        return _unavailable(binding, request, TierValuationReasonCode.UNAVAILABLE_GPPE)
    if request.gppe.metric is GppeMetric.FINANCIAL_EFFICIENCY:
        return _unavailable(binding, request, TierValuationReasonCode.FINANCIAL_TIER_MAPPING_UNAPPROVED)

    tier = tier_for_gppe(request.gppe.value)
    target_low, target_high = _TARGET_PS[tier]
    with localcontext(_DECIMAL_CONTEXT):
        midpoint = (target_low + target_high) / Decimal("2")
        valuation_gap = midpoint / current_ps - Decimal("1")
    return IssuerTierValuationTinyResult(
        strategy_binding_id=binding.strategy_binding_id,
        candidate_universe_id=binding.candidate_universe.candidate_universe_id,
        price_to_sales_policy_id=binding.price_to_sales_policy.price_to_sales_policy_id,
        issuer_id=request.issuer_id,
        as_of=request.cutoff,
        reporting_currency=request.reporting_currency,
        availability=TierValuationAvailability.AVAILABLE,
        tier=tier,
        target_ps_lower=target_low,
        target_ps_upper=target_high,
        target_ps_midpoint=midpoint,
        current_price_to_sales=current_ps,
        valuation_gap=valuation_gap,
        confidence=min(request.gppe.confidence, request.price_to_sales.confidence),
    )
