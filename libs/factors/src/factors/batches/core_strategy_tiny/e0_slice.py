"""Provisional E0 formulas for the isolated core-strategy tiny batch."""

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.execution import AvailabilityStatus, FactorValidationStatus

H0_GOVERNANCE_HANDOFF_ID = (
    "handoff:h0-core-headcount-extraction:447ab8a382ab258eb935ab8e643456d624708773782f8aed8b0d21c4bfdd43ac"
)
H0_GOVERNANCE_HANDOFF_SHA256 = "e4968b32e896d3c8dae9188e1fd47b01b62e14a5215671fed1b24c3ede510199"
H0_RUNTIME_HANDOFF_SHA256 = "48d9265464c1da98cf480d951e3f4cc859ab63010b6489d5ec63b2bc76687ce5"
H0_RUNTIME_HANDOFF_ID = f"core-headcount-extraction-handoff:{H0_RUNTIME_HANDOFF_SHA256}"
SEMANTIC_CANDIDATE_SHA256 = "d0b2865cbde85181bb17801ac3be467c5049906f793876c8b6ac319b7525cc5a"
PUBLIC_GOLDEN_MANIFEST_SHA256 = "8a9e1d23ea633f772c16cfaff6706518acce6cbcba5d343eaa33a0acdb01a8bc"
FROZEN_CORPUS_SHA256 = "ba52119cd1f93dc68768b541db2d88e9a28301441c2d02ca35007d21b218f4ad"


class IssuerBranch(StrEnum):
    NON_FINANCIAL = "non_financial"
    FINANCIAL = "financial"


class SubjectKind(StrEnum):
    ISSUER = "issuer"
    INSTRUMENT = "instrument"


class CoreMetric(StrEnum):
    ANNUAL_GROSS_PROFIT = "annual_gross_profit"
    PRE_PROVISION_PROFIT = "pre_provision_profit"
    EMPLOYEE_HEADCOUNT = "employee_headcount"
    PERIOD_AVERAGE_EMPLOYEE_COUNT = "period_average_employee_count"
    CURRENT_PS = "current_ps"
    ANNUAL_REVENUE = "annual_revenue"
    MARKET_CAP = "market_cap"
    FX_REPORTING_TO_MARKET = "fx_reporting_to_market"


class ValuationTier(StrEnum):
    TRADITIONAL = "traditional"
    TECH = "tech"
    LARGE_MODEL_NATIVE = "large_model_native"


class CoreTinyActivation(BaseModel):
    """Exact Local/CI pins; this type cannot activate a release or Staging run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    batch_id: Literal["S0-core-strategy-tiny"] = "S0-core-strategy-tiny"
    environment: Literal["local", "ci"]
    governance_handoff_id: str = H0_GOVERNANCE_HANDOFF_ID
    governance_handoff_sha256: str = Field(default=H0_GOVERNANCE_HANDOFF_SHA256, pattern=r"^[0-9a-f]{64}$")
    runtime_handoff_id: str = H0_RUNTIME_HANDOFF_ID
    runtime_handoff_sha256: str = Field(default=H0_RUNTIME_HANDOFF_SHA256, pattern=r"^[0-9a-f]{64}$")
    semantic_candidate_sha256: str = Field(default=SEMANTIC_CANDIDATE_SHA256, pattern=r"^[0-9a-f]{64}$")
    public_golden_manifest_sha256: str = Field(default=PUBLIC_GOLDEN_MANIFEST_SHA256, pattern=r"^[0-9a-f]{64}$")
    frozen_corpus_sha256: str = Field(default=FROZEN_CORPUS_SHA256, pattern=r"^[0-9a-f]{64}$")
    semantic_policy_state: Literal["candidate_unapproved"] = "candidate_unapproved"
    live_source_allowed: Literal[False] = False
    staging_allowed: Literal[False] = False
    schedule_allowed: Literal[False] = False
    release_allowed: Literal[False] = False

    @model_validator(mode="after")
    def bind_exact_artifacts(self) -> "CoreTinyActivation":
        actual = (
            self.governance_handoff_id,
            self.governance_handoff_sha256,
            self.runtime_handoff_id,
            self.runtime_handoff_sha256,
            self.semantic_candidate_sha256,
            self.public_golden_manifest_sha256,
            self.frozen_corpus_sha256,
        )
        expected = (
            H0_GOVERNANCE_HANDOFF_ID,
            H0_GOVERNANCE_HANDOFF_SHA256,
            H0_RUNTIME_HANDOFF_ID,
            H0_RUNTIME_HANDOFF_SHA256,
            SEMANTIC_CANDIDATE_SHA256,
            PUBLIC_GOLDEN_MANIFEST_SHA256,
            FROZEN_CORPUS_SHA256,
        )
        if actual != expected:
            raise ValueError("E0 activation artifact identity drifted")
        return self


class H0HeadcountFactorInput(BaseModel):
    """Batch-private mirror of the accepted H0 provenance-free wire shape."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entity_id: str = Field(pattern=r"^issuer\.[a-z0-9]+$")
    metric: Literal["employee_headcount"] = "employee_headcount"
    value: Decimal = Field(gt=0)
    confidence: Decimal = Field(ge=0, le=1)
    as_of: datetime
    fiscal_period: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

    @field_validator("as_of")
    @classmethod
    def require_aware_as_of(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("H0 factor input as_of must be timezone-aware")
        return value


class CoreObservation(BaseModel):
    """Strict factor-visible value; source, raw, model, and review fields are forbidden."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject_kind: SubjectKind
    entity_id: str = Field(min_length=1)
    metric: CoreMetric
    value: Decimal | None
    unit: str = Field(min_length=1)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    valid_period: date
    confidence: Decimal = Field(ge=0, le=1)
    as_of: datetime
    availability_status: AvailabilityStatus = AvailabilityStatus.AVAILABLE

    @field_validator("as_of")
    @classmethod
    def require_aware_as_of(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observation as_of must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_metric_unit(self) -> "CoreObservation":
        if self.metric in {CoreMetric.EMPLOYEE_HEADCOUNT, CoreMetric.PERIOD_AVERAGE_EMPLOYEE_COUNT}:
            if self.unit != "employees" or self.currency is not None:
                raise ValueError("headcount observations require employees and no currency")
        elif self.metric is CoreMetric.CURRENT_PS:
            if self.unit != "ratio" or self.currency is not None:
                raise ValueError("current P/S observations require a ratio and no currency")
        elif self.metric is CoreMetric.FX_REPORTING_TO_MARKET:
            quote_and_base = self.unit.split("_per_")
            if (
                self.currency is not None
                or len(quote_and_base) != 2
                or any(len(code) != 3 or not code.isalpha() or not code.isupper() for code in quote_and_base)
            ):
                raise ValueError("FX observations require a quote_per_base unit and no currency")
        else:
            if self.currency is None or self.unit != self.currency:
                raise ValueError("monetary observations require a matching currency unit")
        return self

    @classmethod
    def from_h0_headcount(cls, value: H0HeadcountFactorInput) -> "CoreObservation":
        return cls(
            subject_kind=SubjectKind.ISSUER,
            entity_id=value.entity_id,
            metric=CoreMetric.EMPLOYEE_HEADCOUNT,
            value=value.value,
            unit="employees",
            currency=None,
            valid_period=date.fromisoformat(value.fiscal_period),
            confidence=value.confidence,
            as_of=value.as_of,
            availability_status=AvailabilityStatus.AVAILABLE,
        )


class CoreTinyRequest(BaseModel):
    """One already PIT-resolved issuer/instrument calibration request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    issuer_id: str = Field(min_length=1)
    instrument_id: str = Field(min_length=1)
    issuer_branch: IssuerBranch
    reporting_currency: str = Field(pattern=r"^[A-Z]{3}$")
    market_currency: str = Field(pattern=r"^[A-Z]{3}$")
    as_of: datetime
    observations: tuple[CoreObservation, ...]
    scope_state: Literal["development_calibration_only"] = "development_calibration_only"

    @field_validator("as_of")
    @classmethod
    def require_aware_as_of(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("request as_of must be timezone-aware")
        return value

    @model_validator(mode="after")
    def canonicalize_inputs(self) -> "CoreTinyRequest":
        observations = tuple(
            sorted(
                self.observations,
                key=lambda item: (item.metric.value, item.subject_kind.value, item.entity_id),
            )
        )
        metrics = [item.metric for item in observations]
        if len(metrics) != len(set(metrics)):
            raise ValueError("factor inputs must contain at most one PIT-resolved observation per metric")
        if any(item.as_of != self.as_of for item in observations):
            raise ValueError("factor inputs must already be PIT-resolved at the request cutoff")
        for item in observations:
            if (
                item.metric
                in {
                    CoreMetric.ANNUAL_GROSS_PROFIT,
                    CoreMetric.PRE_PROVISION_PROFIT,
                    CoreMetric.ANNUAL_REVENUE,
                }
                and item.currency != self.reporting_currency
            ):
                raise ValueError(f"{item.metric.value} currency must match reporting_currency")
            if item.metric is CoreMetric.MARKET_CAP and item.currency != self.market_currency:
                raise ValueError("market_cap currency must match market_currency")
            if item.metric is CoreMetric.FX_REPORTING_TO_MARKET:
                expected_unit = f"{self.market_currency}_per_{self.reporting_currency}"
                if item.unit != expected_unit:
                    raise ValueError("FX unit must match market_currency_per_reporting_currency")
        object.__setattr__(self, "observations", observations)
        return self


class LevelResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    availability_status: AvailabilityStatus
    comparison_branch: IssuerBranch
    metric: Literal["gppe_level", "financial_efficiency"]
    value: Decimal | None = None
    comparison_band: str | None = None
    confidence: Decimal | None = Field(default=None, ge=0, le=1)
    reason_codes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_status(self) -> "LevelResult":
        reasons = tuple(sorted(set(self.reason_codes)))
        if self.availability_status is AvailabilityStatus.AVAILABLE:
            if self.value is None or self.comparison_band is None or self.confidence is None or reasons:
                raise ValueError("available level result requires value, band, confidence, and no reasons")
        elif not reasons:
            raise ValueError("unavailable level result requires an explicit reason")
        object.__setattr__(self, "reason_codes", reasons)
        return self


class ValuationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    availability_status: AvailabilityStatus
    tier: ValuationTier | None = None
    target_ps_low: Decimal | None = None
    target_ps_high: Decimal | None = None
    target_ps_midpoint: Decimal | None = None
    current_ps: Decimal | None = None
    current_ps_basis: Literal["supplied_precomputed"] | None = None
    current_ps_construction_state: Literal["candidate_unapproved"] | None = None
    valuation_gap: Decimal | None = None
    confidence: Decimal | None = Field(default=None, ge=0, le=1)
    reason_codes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_status(self) -> "ValuationResult":
        reasons = tuple(sorted(set(self.reason_codes)))
        values = (
            self.tier,
            self.target_ps_low,
            self.target_ps_high,
            self.target_ps_midpoint,
            self.current_ps,
            self.current_ps_basis,
            self.current_ps_construction_state,
            self.valuation_gap,
            self.confidence,
        )
        if self.availability_status is AvailabilityStatus.AVAILABLE:
            if any(value is None for value in values) or reasons:
                raise ValueError("available valuation result requires complete values and no reasons")
        elif not reasons:
            raise ValueError("unavailable valuation result requires an explicit reason")
        object.__setattr__(self, "reason_codes", reasons)
        return self


class CoreTinyResult(BaseModel):
    """Content-addressed E0 calibration result, never an accepted factor handoff."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    result_id: str = Field(default="", pattern=r"^(?:|core-tiny-result:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    issuer_id: str
    instrument_id: str
    as_of: datetime
    activation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    level: LevelResult
    elasticity_status: AvailabilityStatus = AvailabilityStatus.UNAVAILABLE
    elasticity_reason_codes: tuple[str, ...] = ("insufficient_distinct_periods",)
    valuation: ValuationResult
    semantic_policy_state: Literal["candidate_unapproved"] = "candidate_unapproved"
    semantic_candidate_sha256: str = Field(default=SEMANTIC_CANDIDATE_SHA256, pattern=r"^[0-9a-f]{64}$")
    factor_validation_status: FactorValidationStatus = FactorValidationStatus.NOT_EVALUATED
    claim_ceiling: Literal["development_calibration_only"] = "development_calibration_only"
    stable_handoff: Literal[False] = False
    release_activation: Literal[False] = False

    @model_validator(mode="after")
    def freeze_and_identify(self) -> "CoreTinyResult":
        reasons = tuple(sorted(set(self.elasticity_reason_codes)))
        if self.elasticity_status is AvailabilityStatus.AVAILABLE or not reasons:
            raise ValueError("E0 cannot claim the five-period elasticity component")
        if self.factor_validation_status is not FactorValidationStatus.NOT_EVALUATED:
            raise ValueError("E0 factor validation must remain not evaluated")
        if self.semantic_candidate_sha256 != SEMANTIC_CANDIDATE_SHA256:
            raise ValueError("E0 result semantic candidate identity drifted")
        object.__setattr__(self, "elasticity_reason_codes", reasons)
        payload = self.model_dump(mode="json", exclude={"result_id", "content_sha256"})
        expected_hash = canonical_sha256(payload)
        expected_id = f"core-tiny-result:{expected_hash}"
        if self.content_sha256 and self.content_sha256 != expected_hash:
            raise ValueError("content_sha256 does not match the E0 result")
        if self.result_id and self.result_id != expected_id:
            raise ValueError("result_id does not match the E0 result")
        object.__setattr__(self, "content_sha256", expected_hash)
        object.__setattr__(self, "result_id", expected_id)
        return self


class RankingCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: str = Field(pattern=r"^[a-z0-9]+(?:[-._][a-z0-9]+)+$")
    target_ps: Decimal = Field(gt=0)
    current_ps: Decimal | None = Field(default=None, gt=0)


class RankedCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    rank: int = Field(gt=0)
    candidate_id: str
    gap: Decimal


class IneligibleCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: str
    reason: Literal["missing_current_ps", "unavailable_valuation"]


class ProvisionalRanking(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ranking_id: str = Field(default="", pattern=r"^(?:|core-tiny-ranking:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    availability_status: AvailabilityStatus
    ranking: tuple[RankedCandidate, ...] = ()
    selected_candidate_ids: tuple[str, ...] = ()
    ineligible_candidates: tuple[IneligibleCandidate, ...] = ()
    input_result_ids: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()
    selection_count: Literal[10] = 10
    strategy_alias: Literal["large-model-value-v0"] = "large-model-value-v0"
    semantic_policy_state: Literal["candidate_unapproved"] = "candidate_unapproved"
    factor_validation_status: FactorValidationStatus = FactorValidationStatus.NOT_EVALUATED
    claim_ceiling: Literal["development_calibration_only"] = "development_calibration_only"
    stable_handoff: Literal[False] = False
    release_activation: Literal[False] = False

    @model_validator(mode="after")
    def freeze_and_identify(self) -> "ProvisionalRanking":
        reasons = tuple(sorted(set(self.reason_codes)))
        if self.availability_status is AvailabilityStatus.AVAILABLE:
            if reasons:
                raise ValueError("available ranking cannot carry reasons")
            if len(self.ranking) < self.selection_count or len(self.selected_candidate_ids) != self.selection_count:
                raise ValueError("available ranking requires ten selected candidates")
        if self.availability_status is not AvailabilityStatus.AVAILABLE and not reasons:
            raise ValueError("unavailable ranking requires a reason")
        if self.availability_status is not AvailabilityStatus.AVAILABLE and (
            self.ranking or self.selected_candidate_ids
        ):
            raise ValueError("unavailable ranking cannot expose a partial decision")
        if self.factor_validation_status is not FactorValidationStatus.NOT_EVALUATED:
            raise ValueError("E0 ranking validation must remain not evaluated")
        result_ids = tuple(sorted(set(self.input_result_ids)))
        if len(result_ids) != len(self.input_result_ids):
            raise ValueError("ranking input result IDs must be unique")
        object.__setattr__(self, "input_result_ids", result_ids)
        object.__setattr__(self, "reason_codes", reasons)
        payload = self.model_dump(mode="json", exclude={"ranking_id", "content_sha256"})
        expected_hash = canonical_sha256(payload)
        expected_id = f"core-tiny-ranking:{expected_hash}"
        if self.content_sha256 and self.content_sha256 != expected_hash:
            raise ValueError("content_sha256 does not match the ranking")
        if self.ranking_id and self.ranking_id != expected_id:
            raise ValueError("ranking_id does not match the ranking")
        object.__setattr__(self, "content_sha256", expected_hash)
        object.__setattr__(self, "ranking_id", expected_id)
        return self


_NON_FINANCIAL_BANDS = (
    (Decimal("1000000"), ValuationTier.TRADITIONAL),
    (Decimal("3000000"), ValuationTier.TECH),
)
_TARGET_PS = {
    ValuationTier.TRADITIONAL: (Decimal("3"), Decimal("4")),
    ValuationTier.TECH: (Decimal("8"), Decimal("10")),
    ValuationTier.LARGE_MODEL_NATIVE: (Decimal("20"), Decimal("30")),
}


def _observation_map(request: CoreTinyRequest) -> dict[CoreMetric, CoreObservation]:
    return {item.metric: item for item in request.observations}


def _resolve(
    observations: dict[CoreMetric, CoreObservation],
    metric: CoreMetric,
    *,
    subject_kind: SubjectKind,
    entity_id: str,
    require_positive: bool = False,
) -> tuple[CoreObservation | None, str | None]:
    observation = observations.get(metric)
    if observation is None or observation.value is None:
        return None, f"missing_{metric.value}"
    if observation.subject_kind is not subject_kind or observation.entity_id != entity_id:
        return None, f"wrong_subject_{metric.value}"
    if observation.availability_status is not AvailabilityStatus.AVAILABLE:
        return None, f"{observation.availability_status.value}_{metric.value}"
    if require_positive and observation.value <= 0:
        return None, f"non_positive_{metric.value}"
    return observation, None


def _unavailable_level(
    branch: IssuerBranch, metric: Literal["gppe_level", "financial_efficiency"], reason: str
) -> LevelResult:
    status = (
        AvailabilityStatus.STALE
        if reason.startswith("stale_")
        else AvailabilityStatus.LOW_CONFIDENCE
        if reason.startswith("low_confidence_")
        else AvailabilityStatus.UNAVAILABLE
    )
    return LevelResult(
        availability_status=status,
        comparison_branch=branch,
        metric=metric,
        reason_codes=(reason,),
    )


def _compute_level(request: CoreTinyRequest, observations: dict[CoreMetric, CoreObservation]) -> LevelResult:
    is_financial = request.issuer_branch is IssuerBranch.FINANCIAL
    numerator_metric = CoreMetric.PRE_PROVISION_PROFIT if is_financial else CoreMetric.ANNUAL_GROSS_PROFIT
    output_metric: Literal["gppe_level", "financial_efficiency"] = (
        "financial_efficiency" if is_financial else "gppe_level"
    )
    numerator, numerator_reason = _resolve(
        observations,
        numerator_metric,
        subject_kind=SubjectKind.ISSUER,
        entity_id=request.issuer_id,
    )
    denominator, denominator_reason = _resolve(
        observations,
        CoreMetric.PERIOD_AVERAGE_EMPLOYEE_COUNT,
        subject_kind=SubjectKind.ISSUER,
        entity_id=request.issuer_id,
        require_positive=True,
    )
    reason = numerator_reason or denominator_reason
    if reason is not None or numerator is None or denominator is None:
        return _unavailable_level(request.issuer_branch, output_metric, reason or "missing_required_level_input")
    if numerator.valid_period != denominator.valid_period:
        return _unavailable_level(request.issuer_branch, output_metric, "level_period_mismatch")
    assert numerator.value is not None and denominator.value is not None
    value = numerator.value / denominator.value  # both values are non-null after resolution
    confidence = min(numerator.confidence, denominator.confidence)
    if is_financial:
        band = (
            "[-inf,500000)"
            if value < Decimal("500000")
            else "[500000,1000000)"
            if value < Decimal("1000000")
            else "[1000000,+inf)"
        )
    else:
        tier = (
            ValuationTier.TRADITIONAL
            if value < _NON_FINANCIAL_BANDS[0][0]
            else ValuationTier.TECH
            if value < _NON_FINANCIAL_BANDS[1][0]
            else ValuationTier.LARGE_MODEL_NATIVE
        )
        band = tier.value
    return LevelResult(
        availability_status=AvailabilityStatus.AVAILABLE,
        comparison_branch=request.issuer_branch,
        metric=output_metric,
        value=value,
        comparison_band=band,
        confidence=confidence,
    )


def _current_ps_reason(request: CoreTinyRequest, observations: dict[CoreMetric, CoreObservation]) -> str:
    revenue = observations.get(CoreMetric.ANNUAL_REVENUE)
    market_cap = observations.get(CoreMetric.MARKET_CAP)
    if revenue is not None and market_cap is not None:
        if revenue.currency != market_cap.currency or request.reporting_currency != request.market_currency:
            fx = observations.get(CoreMetric.FX_REPORTING_TO_MARKET)
            if fx is None or fx.availability_status is not AvailabilityStatus.AVAILABLE:
                return "cross_currency_without_accepted_fx"
        return "current_ps_construction_unapproved"
    return "missing_current_ps"


def _compute_valuation(
    request: CoreTinyRequest,
    observations: dict[CoreMetric, CoreObservation],
    level: LevelResult,
) -> ValuationResult:
    if level.availability_status is not AvailabilityStatus.AVAILABLE:
        return ValuationResult(
            availability_status=level.availability_status,
            reason_codes=level.reason_codes,
        )
    if request.issuer_branch is IssuerBranch.FINANCIAL:
        return ValuationResult(
            availability_status=AvailabilityStatus.UNAVAILABLE,
            reason_codes=("financial_tier_mapping_unapproved",),
        )
    current_ps, current_ps_reason = _resolve(
        observations,
        CoreMetric.CURRENT_PS,
        subject_kind=SubjectKind.INSTRUMENT,
        entity_id=request.instrument_id,
        require_positive=True,
    )
    if current_ps is None:
        reason = (
            _current_ps_reason(request, observations)
            if current_ps_reason == "missing_current_ps"
            else current_ps_reason
        )
        if reason is None:
            reason = "missing_current_ps"
        status = (
            AvailabilityStatus.STALE
            if reason.startswith("stale_")
            else AvailabilityStatus.LOW_CONFIDENCE
            if reason.startswith("low_confidence_")
            else AvailabilityStatus.UNAVAILABLE
        )
        return ValuationResult(availability_status=status, reason_codes=(reason,))
    assert level.comparison_band is not None and level.confidence is not None and current_ps.value is not None
    tier = ValuationTier(level.comparison_band)
    target_low, target_high = _TARGET_PS[tier]
    midpoint = (target_low + target_high) / Decimal("2")
    gap = midpoint / current_ps.value - Decimal("1")
    return ValuationResult(
        availability_status=AvailabilityStatus.AVAILABLE,
        tier=tier,
        target_ps_low=target_low,
        target_ps_high=target_high,
        target_ps_midpoint=midpoint,
        current_ps=current_ps.value,
        current_ps_basis="supplied_precomputed",
        current_ps_construction_state="candidate_unapproved",
        valuation_gap=gap,
        confidence=min(level.confidence or Decimal("0"), current_ps.confidence),
    )


def evaluate_core_tiny(
    *,
    activation: CoreTinyActivation,
    request: CoreTinyRequest,
) -> CoreTinyResult:
    """Run the candidate-only E0 level/tier/gap calibration without registering a factor."""

    activation = CoreTinyActivation.model_validate(activation)
    request = CoreTinyRequest.model_validate(request)
    observations = _observation_map(request)
    level = _compute_level(request, observations)
    valuation = _compute_valuation(request, observations, level)
    return CoreTinyResult(
        issuer_id=request.issuer_id,
        instrument_id=request.instrument_id,
        as_of=request.as_of,
        activation_sha256=canonical_sha256(activation.model_dump(mode="json")),
        request_sha256=canonical_sha256(request.model_dump(mode="json")),
        level=level,
        valuation=valuation,
    )


def _rank_candidates(
    candidates: tuple[RankingCandidate, ...],
    *,
    input_result_ids: tuple[str, ...] = (),
    extra_ineligible: tuple[IneligibleCandidate, ...] = (),
) -> ProvisionalRanking:
    ordered = tuple(sorted(candidates, key=lambda item: item.candidate_id))
    if len({item.candidate_id for item in ordered}) != len(ordered):
        raise ValueError("ranking candidates must be unique")
    ineligible = tuple(
        sorted(
            (
                *extra_ineligible,
                *(
                    IneligibleCandidate(candidate_id=item.candidate_id, reason="missing_current_ps")
                    for item in ordered
                    if item.current_ps is None
                ),
            ),
            key=lambda item: item.candidate_id,
        )
    )
    scored = [(item, item.target_ps / item.current_ps - Decimal("1")) for item in ordered if item.current_ps]
    if len(scored) < 10:
        return ProvisionalRanking(
            availability_status=AvailabilityStatus.UNAVAILABLE,
            ineligible_candidates=ineligible,
            input_result_ids=input_result_ids,
            reason_codes=("insufficient_eligible_candidates",),
        )
    gaps = [gap for _, gap in scored]
    if len(gaps) != len(set(gaps)):
        return ProvisionalRanking(
            availability_status=AvailabilityStatus.UNAVAILABLE,
            ineligible_candidates=ineligible,
            input_result_ids=input_result_ids,
            reason_codes=("ranking_tie_break_unapproved",),
        )
    ranked = tuple(
        RankedCandidate(rank=index, candidate_id=item.candidate_id, gap=gap)
        for index, (item, gap) in enumerate(sorted(scored, key=lambda pair: pair[1], reverse=True), start=1)
    )
    return ProvisionalRanking(
        availability_status=AvailabilityStatus.AVAILABLE,
        ranking=ranked,
        selected_candidate_ids=tuple(item.candidate_id for item in ranked[:10]),
        ineligible_candidates=ineligible,
        input_result_ids=input_result_ids,
    )


def rank_provisional_candidates(candidates: tuple[RankingCandidate, ...]) -> ProvisionalRanking:
    """Exercise candidate ranking semantics without claiming an approved strategy."""

    return _rank_candidates(candidates)


def rank_core_tiny_results(results: tuple[CoreTinyResult, ...]) -> ProvisionalRanking:
    """Rank only valuation outputs produced by this exact provisional E0 slice."""

    ordered = tuple(sorted(results, key=lambda item: item.instrument_id))
    if len({item.instrument_id for item in ordered}) != len(ordered):
        raise ValueError("core tiny ranking instruments must be unique")
    eligible: list[RankingCandidate] = []
    ineligible: list[IneligibleCandidate] = []
    for result in ordered:
        valuation = result.valuation
        if valuation.availability_status is AvailabilityStatus.AVAILABLE:
            assert valuation.target_ps_midpoint is not None and valuation.current_ps is not None
            eligible.append(
                RankingCandidate(
                    candidate_id=result.instrument_id,
                    target_ps=valuation.target_ps_midpoint,
                    current_ps=valuation.current_ps,
                )
            )
        else:
            reason: Literal["missing_current_ps", "unavailable_valuation"] = (
                "missing_current_ps" if valuation.reason_codes == ("missing_current_ps",) else "unavailable_valuation"
            )
            ineligible.append(IneligibleCandidate(candidate_id=result.instrument_id, reason=reason))
    return _rank_candidates(
        tuple(eligible),
        input_result_ids=tuple(item.result_id for item in ordered),
        extra_ineligible=tuple(ineligible),
    )


__all__ = [
    "CoreMetric",
    "CoreObservation",
    "CoreTinyActivation",
    "CoreTinyRequest",
    "CoreTinyResult",
    "FROZEN_CORPUS_SHA256",
    "H0_GOVERNANCE_HANDOFF_ID",
    "H0_GOVERNANCE_HANDOFF_SHA256",
    "H0_RUNTIME_HANDOFF_ID",
    "H0_RUNTIME_HANDOFF_SHA256",
    "H0HeadcountFactorInput",
    "IssuerBranch",
    "PUBLIC_GOLDEN_MANIFEST_SHA256",
    "ProvisionalRanking",
    "RankingCandidate",
    "SEMANTIC_CANDIDATE_SHA256",
    "SubjectKind",
    "ValuationTier",
    "evaluate_core_tiny",
    "rank_provisional_candidates",
    "rank_core_tiny_results",
]
