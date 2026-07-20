"""Pinned Qlib selection adapter for the frozen S7 development corpus."""

from __future__ import annotations

import math
import platform
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from importlib.metadata import version
from types import SimpleNamespace
from typing import Literal, Self

from factors.batches.issuer_tier_valuation_tiny.kernel import (
    IssuerTierValuationTinyResult,
    TierValuationAvailability,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.research import LargeModelValueV0Binding

S2_TERMINAL_MANIFEST_SHA256 = "2908f2a33c358c3c349f4e3ec41b1c83e7d4f78345a4629d29ae53d1539fb127"
S4_TERMINAL_MANIFEST_SHA256 = "3a72c29f0c965e3aa8eaa4464726d183622aa930a7780f9a2d3b51b9e25afa16"
S6_TERMINAL_MANIFEST_SHA256 = "0b4962775b9c5b94f140b7173ab27bd411af6d6ed3245557ced6935436b25a19"
S7_PREPARED_MANIFEST_SHA256 = "9c7ca01ba6ee4b91c910188f5366efc72d81670a3a26e91a25ec8b59250a7863"
FROZEN_CORPUS_SHA256 = "0f203af37fa10d63d34bf42a4d9eadb20bcb0e0d007bbc51190050e2a2a46d81"
QLIB_LOCK_SHA256 = "bd15aa78073f2dd4565a65e32d2942b2def399928897fc48036db9aefd1ffd9c"
QLIB_DISTRIBUTION = "pyqlib"
QLIB_VERSION = "0.9.7"
QLIB_RELEASE_COMMIT = "da920b7f954f48ab1bb64117c976710de198373e"
QLIB_ADAPTER_ID = "truealpha.qlib-selection-adapter.v1"
QLIB_OPERATOR_REGISTRY_ID = "truealpha.qlib-operators.v1"
QLIB_STRATEGY_ID = "large_model_value_full_rebalance_top_n.v1"
QLIB_CONFIGURATION_SHA256 = "e1e2708a9badb4e5ffa1b489b39b7d4623332a56a6d52ea85a5b232876934d5c"

_QLIB_WHEEL_SHA256 = {
    ("Darwin", "arm64"): "9adffd819e0414cf288c84f6136fb397c63eb3d4bcadf063fd1ec8f4100857fc",
    ("Darwin", "x86_64"): "9adffd819e0414cf288c84f6136fb397c63eb3d4bcadf063fd1ec8f4100857fc",
    ("Linux", "x86_64"): "b50e70d127976d973c447af667b51aa2bb088d79bc0c344e295e9aadc753b86e",
    ("Windows", "AMD64"): "dfbee9f0f3005fe805798e2a21c73b198272f1341d5f0d7771e127166faac08e",
}


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def expected_qlib_runtime_artifact_sha256() -> str:
    """Return the wheel hash pinned for the current supported runtime."""

    coordinate = (platform.system(), platform.machine())
    try:
        return _QLIB_WHEEL_SHA256[coordinate]
    except KeyError as exc:
        raise RuntimeError(f"unsupported pyqlib 0.9.7 runtime coordinate: {coordinate!r}") from exc


class SelectionAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class CandidateSelectionReason(StrEnum):
    MISSING_REQUIRED_RESULT = "missing_required_result"
    UNAVAILABLE_VALUATION_RESULT = "unavailable_valuation_result"
    LOW_CONFIDENCE = "low_confidence"
    ELIGIBLE_NOT_SELECTED = "eligible_not_selected"


class SelectionFailureReason(StrEnum):
    BINDING_IDENTITY_MISMATCH = "binding_identity_mismatch"
    CANDIDATE_UNIVERSE_IDENTITY_MISMATCH = "candidate_universe_identity_mismatch"
    PRICE_TO_SALES_POLICY_IDENTITY_MISMATCH = "price_to_sales_policy_identity_mismatch"
    CUTOFF_IDENTITY_MISMATCH = "cutoff_identity_mismatch"
    REPORTING_CURRENCY_MISMATCH = "reporting_currency_mismatch"
    DUPLICATE_ISSUER_RESULT = "duplicate_issuer_result"
    EXTRA_ISSUER_RESULT = "extra_issuer_result"
    INSUFFICIENT_ELIGIBLE_CANDIDATES = "insufficient_eligible_candidates"
    RANKING_TIE_BREAK_UNAPPROVED = "ranking_tie_break_unapproved"
    QLIB_SCORE_ORDER_NOT_PRESERVED = "qlib_score_order_not_preserved"
    QLIB_EXECUTION_BINDING_MISMATCH = "qlib_execution_binding_mismatch"
    QLIB_RUNTIME_FAILURE = "qlib_runtime_failure"
    QLIB_STRATEGY_OUTPUT_INVALID = "qlib_strategy_output_invalid"


class IssuerStrategySelectionTinyActivation(_StrictFrozenModel):
    batch_id: Literal["S7-issuer-strategy-selection"] = "S7-issuer-strategy-selection"
    environment: Literal["local", "ci"]
    s2_terminal_manifest_sha256: str = Field(default=S2_TERMINAL_MANIFEST_SHA256, pattern=r"^[0-9a-f]{64}$")
    s4_terminal_manifest_sha256: str = Field(default=S4_TERMINAL_MANIFEST_SHA256, pattern=r"^[0-9a-f]{64}$")
    s6_terminal_manifest_sha256: str = Field(default=S6_TERMINAL_MANIFEST_SHA256, pattern=r"^[0-9a-f]{64}$")
    s7_prepared_manifest_sha256: str = Field(default=S7_PREPARED_MANIFEST_SHA256, pattern=r"^[0-9a-f]{64}$")
    frozen_corpus_sha256: str = Field(default=FROZEN_CORPUS_SHA256, pattern=r"^[0-9a-f]{64}$")
    qlib_lock_sha256: str = Field(default=QLIB_LOCK_SHA256, pattern=r"^[0-9a-f]{64}$")
    live_source_allowed: Literal[False] = False
    staging_allowed: Literal[False] = False
    schedule_allowed: Literal[False] = False
    release_allowed: Literal[False] = False

    @model_validator(mode="after")
    def bind_exact_artifacts(self) -> Self:
        actual = (
            self.s2_terminal_manifest_sha256,
            self.s4_terminal_manifest_sha256,
            self.s6_terminal_manifest_sha256,
            self.s7_prepared_manifest_sha256,
            self.frozen_corpus_sha256,
            self.qlib_lock_sha256,
        )
        expected = (
            S2_TERMINAL_MANIFEST_SHA256,
            S4_TERMINAL_MANIFEST_SHA256,
            S6_TERMINAL_MANIFEST_SHA256,
            S7_PREPARED_MANIFEST_SHA256,
            FROZEN_CORPUS_SHA256,
            QLIB_LOCK_SHA256,
        )
        if actual != expected:
            raise ValueError("S7 activation artifact identity drifted")
        return self


class QlibSelectionExecutionBinding(_StrictFrozenModel):
    qlib_execution_binding_id: str = Field(default="", pattern=r"^(?:|qlib-execution-binding:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    distribution: str = Field(min_length=1)
    version: str = Field(min_length=1)
    release_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    runtime_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    adapter_id: str = Field(min_length=1)
    operator_registry_id: str = Field(min_length=1)
    strategy_id: str = Field(min_length=1)
    configuration_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def identify(self) -> Self:
        payload = self.model_dump(mode="json", exclude={"qlib_execution_binding_id", "content_sha256"})
        expected_hash = canonical_sha256(payload)
        expected_id = f"qlib-execution-binding:{expected_hash}"
        if self.content_sha256 and self.content_sha256 != expected_hash:
            raise ValueError("Qlib execution binding content hash mismatch")
        if self.qlib_execution_binding_id and self.qlib_execution_binding_id != expected_id:
            raise ValueError("Qlib execution binding ID mismatch")
        object.__setattr__(self, "content_sha256", expected_hash)
        object.__setattr__(self, "qlib_execution_binding_id", expected_id)
        return self

    def is_current_runtime(self) -> bool:
        return (
            self.distribution == QLIB_DISTRIBUTION
            and self.version == QLIB_VERSION
            and self.release_commit == QLIB_RELEASE_COMMIT
            and self.runtime_artifact_sha256 == expected_qlib_runtime_artifact_sha256()
            and self.adapter_id == QLIB_ADAPTER_ID
            and self.operator_registry_id == QLIB_OPERATOR_REGISTRY_ID
            and self.strategy_id == QLIB_STRATEGY_ID
            and self.configuration_sha256 == QLIB_CONFIGURATION_SHA256
        )


def current_qlib_execution_binding() -> QlibSelectionExecutionBinding:
    return QlibSelectionExecutionBinding(
        distribution=QLIB_DISTRIBUTION,
        version=QLIB_VERSION,
        release_commit=QLIB_RELEASE_COMMIT,
        runtime_artifact_sha256=expected_qlib_runtime_artifact_sha256(),
        adapter_id=QLIB_ADAPTER_ID,
        operator_registry_id=QLIB_OPERATOR_REGISTRY_ID,
        strategy_id=QLIB_STRATEGY_ID,
        configuration_sha256=QLIB_CONFIGURATION_SHA256,
    )


class IssuerStrategySelectionTinyRequest(_StrictFrozenModel):
    activation: IssuerStrategySelectionTinyActivation
    execution: QlibSelectionExecutionBinding
    strategy_binding_id: str = Field(min_length=1)
    candidate_universe_id: str = Field(min_length=1)
    price_to_sales_policy_id: str = Field(min_length=1)
    cutoff: datetime
    reporting_currency: str = Field(pattern=r"^[A-Z]{3}$")
    valuation_results: tuple[IssuerTierValuationTinyResult, ...]

    @field_validator("cutoff")
    @classmethod
    def require_aware_cutoff(cls, value: datetime) -> datetime:
        return _aware_utc(value, "cutoff")


class IssuerSelectionDecision(_StrictFrozenModel):
    issuer_id: str
    execution_listing_id: str
    eligible: bool
    selected: bool
    rank: int | None = Field(default=None, ge=1)
    valuation_gap: Decimal | None = None
    confidence: Decimal = Field(ge=0, le=1)
    reason: CandidateSelectionReason | None = None

    @model_validator(mode="after")
    def validate_partition(self) -> Self:
        if self.eligible:
            if self.rank is None or self.valuation_gap is None:
                raise ValueError("eligible candidate requires rank and valuation gap")
            if self.reason not in {None, CandidateSelectionReason.ELIGIBLE_NOT_SELECTED}:
                raise ValueError("eligible candidate has an invalid reason")
            if self.selected == (self.reason is CandidateSelectionReason.ELIGIBLE_NOT_SELECTED):
                raise ValueError("eligible selected partition is inconsistent")
        elif self.selected or self.rank is not None or self.valuation_gap is not None or self.reason is None:
            raise ValueError("ineligible candidate must carry only an exclusion reason")
        return self


class IssuerStrategySelectionTinyResult(_StrictFrozenModel):
    selection_id: str = Field(default="", pattern=r"^(?:|issuer-strategy-selection:[0-9a-f]{64})$")
    semantic_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    evidence_id: str = Field(default="", pattern=r"^(?:|issuer-strategy-selection-evidence:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    qlib_execution_binding_id: str = Field(pattern=r"^qlib-execution-binding:[0-9a-f]{64}$")
    strategy_binding_id: str
    candidate_universe_id: str
    price_to_sales_policy_id: str
    as_of: datetime
    reporting_currency: str
    availability: SelectionAvailability
    decisions: tuple[IssuerSelectionDecision, ...]
    selected_issuer_ids: tuple[str, ...] = ()
    selected_execution_listing_ids: tuple[str, ...] = ()
    confidence: Decimal = Field(ge=0, le=1)
    reason_codes: tuple[SelectionFailureReason, ...] = ()

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        decisions = tuple(sorted(self.decisions, key=lambda item: item.issuer_id))
        if len({item.issuer_id for item in decisions}) != len(decisions):
            raise ValueError("selection decisions contain duplicate issuers")
        object.__setattr__(self, "decisions", decisions)
        ranked = tuple(sorted((item for item in decisions if item.eligible), key=lambda item: item.rank or 0))
        if tuple(item.rank for item in ranked) != tuple(range(1, len(ranked) + 1)):
            raise ValueError("eligible candidate ranks are not contiguous")
        selected = tuple(item for item in ranked if item.selected)
        if self.availability is SelectionAvailability.AVAILABLE:
            if self.reason_codes or not selected:
                raise ValueError("available selection cannot carry failure reasons or be empty")
            if self.selected_issuer_ids != tuple(item.issuer_id for item in selected):
                raise ValueError("selected issuer order does not match ranks")
            if self.selected_execution_listing_ids != tuple(item.execution_listing_id for item in selected):
                raise ValueError("selected listing order does not match ranks")
            if self.confidence != min(item.confidence for item in selected):
                raise ValueError("selection confidence must equal the minimum selected confidence")
        elif selected or self.selected_issuer_ids or self.selected_execution_listing_ids or not self.reason_codes:
            raise ValueError("unavailable selection must be empty and carry a failure reason")

        semantic_payload = self.model_dump(
            mode="json",
            exclude={"selection_id", "semantic_sha256", "evidence_id", "content_sha256", "qlib_execution_binding_id"},
        )
        semantic_hash = canonical_sha256(semantic_payload)
        evidence_hash = canonical_sha256(
            {"semantic_sha256": semantic_hash, "qlib_execution_binding_id": self.qlib_execution_binding_id}
        )
        expected_selection_id = f"issuer-strategy-selection:{semantic_hash}"
        expected_evidence_id = f"issuer-strategy-selection-evidence:{evidence_hash}"
        for supplied, expected, name in (
            (self.semantic_sha256, semantic_hash, "semantic_sha256"),
            (self.selection_id, expected_selection_id, "selection_id"),
            (self.content_sha256, evidence_hash, "content_sha256"),
            (self.evidence_id, expected_evidence_id, "evidence_id"),
        ):
            if supplied and supplied != expected:
                raise ValueError(f"{name} does not match canonical content")
        object.__setattr__(self, "semantic_sha256", semantic_hash)
        object.__setattr__(self, "selection_id", expected_selection_id)
        object.__setattr__(self, "content_sha256", evidence_hash)
        object.__setattr__(self, "evidence_id", expected_evidence_id)
        return self


def _unavailable(
    binding: LargeModelValueV0Binding,
    request: IssuerStrategySelectionTinyRequest,
    reason: SelectionFailureReason,
    decisions: tuple[IssuerSelectionDecision, ...] = (),
) -> IssuerStrategySelectionTinyResult:
    return IssuerStrategySelectionTinyResult(
        qlib_execution_binding_id=request.execution.qlib_execution_binding_id,
        strategy_binding_id=request.strategy_binding_id,
        candidate_universe_id=request.candidate_universe_id,
        price_to_sales_policy_id=request.price_to_sales_policy_id,
        as_of=request.cutoff,
        reporting_currency=request.reporting_currency,
        availability=SelectionAvailability.UNAVAILABLE,
        decisions=decisions,
        confidence=Decimal("0"),
        reason_codes=(reason,),
    )


class _SingleCutoffCalendar:
    def __init__(self, cutoff: datetime) -> None:
        self.cutoff = cutoff

    def get_trade_step(self) -> int:
        return 0

    def get_step_time(self, trade_step: int = 0, shift: int = 0) -> tuple[datetime, datetime]:
        del trade_step, shift
        return self.cutoff, self.cutoff


class _SelectionExchange:
    def is_stock_tradable(self, **_: object) -> bool:
        return True

    def get_deal_price(self, **_: object) -> float:
        return 1.0

    def get_factor(self, **_: object) -> float:
        return 1.0

    def round_amount_by_trade_unit(self, amount: float, factor: float) -> float:
        del factor
        return amount


def _run_qlib_top_n(scores: dict[str, float], cutoff: datetime, selection_count: int) -> tuple[str, ...]:
    if version(QLIB_DISTRIBUTION) != QLIB_VERSION:
        raise RuntimeError(f"expected {QLIB_DISTRIBUTION}=={QLIB_VERSION}")

    import pandas as pd  # type: ignore[import-untyped]
    from qlib.backtest.position import Position  # type: ignore[import-not-found,import-untyped]
    from qlib.contrib.strategy.signal_strategy import (  # type: ignore[import-not-found,import-untyped]
        TopkDropoutStrategy,
    )

    index = pd.MultiIndex.from_tuples(
        [(issuer_id, pd.Timestamp(cutoff)) for issuer_id in scores],
        names=("instrument", "datetime"),
    )
    signal = pd.Series(tuple(scores.values()), index=index, name="score", dtype="float64")
    strategy = TopkDropoutStrategy(
        topk=selection_count,
        n_drop=selection_count,
        method_buy="top",
        method_sell="bottom",
        signal=signal,
        risk_degree=1.0,
        trade_exchange=_SelectionExchange(),
    )
    strategy.level_infra = {"trade_calendar": _SingleCutoffCalendar(cutoff)}
    strategy.common_infra = {"trade_account": SimpleNamespace(current_position=Position(cash=float(selection_count)))}
    decision = strategy.generate_trade_decision()
    return tuple(order.stock_id for order in decision.get_decision())


def run_qlib_large_model_value_selection(
    binding: LargeModelValueV0Binding,
    request: IssuerStrategySelectionTinyRequest,
) -> IssuerStrategySelectionTinyResult:
    """Validate one complete S6 frame and select its top N through pinned Qlib."""

    if request.strategy_binding_id != binding.strategy_binding_id:
        return _unavailable(binding, request, SelectionFailureReason.BINDING_IDENTITY_MISMATCH)
    if request.candidate_universe_id != binding.candidate_universe.candidate_universe_id:
        return _unavailable(binding, request, SelectionFailureReason.CANDIDATE_UNIVERSE_IDENTITY_MISMATCH)
    if request.price_to_sales_policy_id != binding.price_to_sales_policy.price_to_sales_policy_id:
        return _unavailable(binding, request, SelectionFailureReason.PRICE_TO_SALES_POLICY_IDENTITY_MISMATCH)
    if not request.execution.is_current_runtime():
        return _unavailable(binding, request, SelectionFailureReason.QLIB_EXECUTION_BINDING_MISMATCH)

    candidates = {candidate.issuer.id: candidate for candidate in binding.candidate_universe.candidates}
    supplied: dict[str, IssuerTierValuationTinyResult] = {}
    for result in request.valuation_results:
        if result.issuer_id in supplied:
            return _unavailable(binding, request, SelectionFailureReason.DUPLICATE_ISSUER_RESULT)
        if result.issuer_id not in candidates:
            return _unavailable(binding, request, SelectionFailureReason.EXTRA_ISSUER_RESULT)
        if result.strategy_binding_id != request.strategy_binding_id:
            return _unavailable(binding, request, SelectionFailureReason.BINDING_IDENTITY_MISMATCH)
        if result.candidate_universe_id != request.candidate_universe_id:
            return _unavailable(binding, request, SelectionFailureReason.CANDIDATE_UNIVERSE_IDENTITY_MISMATCH)
        if result.price_to_sales_policy_id != request.price_to_sales_policy_id:
            return _unavailable(binding, request, SelectionFailureReason.PRICE_TO_SALES_POLICY_IDENTITY_MISMATCH)
        if result.as_of != request.cutoff:
            return _unavailable(binding, request, SelectionFailureReason.CUTOFF_IDENTITY_MISMATCH)
        if result.reporting_currency != request.reporting_currency:
            return _unavailable(binding, request, SelectionFailureReason.REPORTING_CURRENCY_MISMATCH)
        supplied[result.issuer_id] = result

    eligible: list[tuple[str, Decimal, Decimal]] = []
    ineligible: list[IssuerSelectionDecision] = []
    for issuer_id, candidate in sorted(candidates.items()):
        selection_result = supplied.get(issuer_id)
        if selection_result is None:
            ineligible.append(
                IssuerSelectionDecision(
                    issuer_id=issuer_id,
                    execution_listing_id=candidate.execution_listing.id,
                    eligible=False,
                    selected=False,
                    confidence=Decimal("0"),
                    reason=CandidateSelectionReason.MISSING_REQUIRED_RESULT,
                )
            )
        elif selection_result.availability is not TierValuationAvailability.AVAILABLE:
            ineligible.append(
                IssuerSelectionDecision(
                    issuer_id=issuer_id,
                    execution_listing_id=candidate.execution_listing.id,
                    eligible=False,
                    selected=False,
                    confidence=selection_result.confidence,
                    reason=CandidateSelectionReason.UNAVAILABLE_VALUATION_RESULT,
                )
            )
        elif selection_result.confidence < binding.strategy.eligibility.minimum_confidence:
            ineligible.append(
                IssuerSelectionDecision(
                    issuer_id=issuer_id,
                    execution_listing_id=candidate.execution_listing.id,
                    eligible=False,
                    selected=False,
                    confidence=selection_result.confidence,
                    reason=CandidateSelectionReason.LOW_CONFIDENCE,
                )
            )
        else:
            assert selection_result.valuation_gap is not None
            eligible.append((issuer_id, selection_result.valuation_gap, selection_result.confidence))

    selection_count = binding.strategy.selection_count
    if len(eligible) < selection_count:
        return _unavailable(
            binding,
            request,
            SelectionFailureReason.INSUFFICIENT_ELIGIBLE_CANDIDATES,
            tuple(ineligible),
        )
    ordered = sorted(eligible, key=lambda item: item[1], reverse=True)
    decimal_scores = tuple(item[1] for item in ordered)
    if len(decimal_scores) != len(set(decimal_scores)):
        return _unavailable(binding, request, SelectionFailureReason.RANKING_TIE_BREAK_UNAPPROVED, tuple(ineligible))
    float_scores = tuple(float(score) for score in decimal_scores)
    if (
        any(not math.isfinite(score) for score in float_scores)
        or len(float_scores) != len(set(float_scores))
        or any(left <= right for left, right in zip(float_scores, float_scores[1:], strict=False))
    ):
        return _unavailable(binding, request, SelectionFailureReason.QLIB_SCORE_ORDER_NOT_PRESERVED, tuple(ineligible))

    try:
        selected_ids = _run_qlib_top_n(
            {issuer_id: float(gap) for issuer_id, gap, _confidence in eligible},
            request.cutoff,
            selection_count,
        )
    except Exception:
        return _unavailable(binding, request, SelectionFailureReason.QLIB_RUNTIME_FAILURE, tuple(ineligible))
    expected_ids = tuple(item[0] for item in ordered[:selection_count])
    if selected_ids != expected_ids:
        return _unavailable(binding, request, SelectionFailureReason.QLIB_STRATEGY_OUTPUT_INVALID, tuple(ineligible))

    selected_set = set(selected_ids)
    ranked_decisions = tuple(
        IssuerSelectionDecision(
            issuer_id=issuer_id,
            execution_listing_id=candidates[issuer_id].execution_listing.id,
            eligible=True,
            selected=issuer_id in selected_set,
            rank=rank,
            valuation_gap=gap,
            confidence=confidence,
            reason=None if issuer_id in selected_set else CandidateSelectionReason.ELIGIBLE_NOT_SELECTED,
        )
        for rank, (issuer_id, gap, confidence) in enumerate(ordered, start=1)
    )
    selected_decisions = ranked_decisions[:selection_count]
    return IssuerStrategySelectionTinyResult(
        qlib_execution_binding_id=request.execution.qlib_execution_binding_id,
        strategy_binding_id=request.strategy_binding_id,
        candidate_universe_id=request.candidate_universe_id,
        price_to_sales_policy_id=request.price_to_sales_policy_id,
        as_of=request.cutoff,
        reporting_currency=request.reporting_currency,
        availability=SelectionAvailability.AVAILABLE,
        decisions=ranked_decisions + tuple(ineligible),
        selected_issuer_ids=tuple(item.issuer_id for item in selected_decisions),
        selected_execution_listing_ids=tuple(item.execution_listing_id for item in selected_decisions),
        confidence=min(item.confidence for item in selected_decisions),
    )
