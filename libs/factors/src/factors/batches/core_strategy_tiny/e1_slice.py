"""Terminal E1 evidence for the frozen core-strategy tiny corpus.

The runner owns point-in-time selection and evidence identities.  The E0 factor
continues to receive only provenance-neutral ``CoreTinyRequest`` values.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol

from factors.batches.core_strategy_tiny.e0_slice import (
    FROZEN_CORPUS_SHA256,
    PUBLIC_GOLDEN_MANIFEST_SHA256,
    CoreMetric,
    CoreObservation,
    CoreTinyActivation,
    CoreTinyRequest,
    CoreTinyResult,
    H0HeadcountFactorInput,
    IssuerBranch,
    ProvisionalRanking,
    RankingCandidate,
    SubjectKind,
    evaluate_core_tiny,
    rank_provisional_candidates,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.execution import AvailabilityStatus, FactorValidationStatus

_SHA256 = r"^[0-9a-f]{64}$"
_CODE = r"^[a-z][a-z0-9_-]*$"
_E1_CREATED_AT = datetime(2026, 4, 1, tzinfo=UTC)
_PROVISIONAL_LOW_CONFIDENCE_CUTOFF = Decimal("0.50")
_PROVISIONAL_MAXIMUM_INPUT_AGE = timedelta(days=366)
_EXPECTED_CASE_IDS = frozenset(
    {
        "plug-restatement-publication-boundary",
        "ddog-provenance-free-success",
        "jpm-financial-proxy-branch",
        "nvda-missing-headcount",
        "alphabet-dual-listing-identity",
        "nice-lookahead-sentinel",
        "ddog-stale-low-confidence",
        "nice-cross-currency-without-fx",
        "public-golden-ordering-and-boundaries",
    }
)
_EXPECTED_PUBLIC_GOLDEN_CASE_KEYS = frozenset(
    {
        "gppe.boundary.1000000",
        "gppe.boundary.3000000",
        "tier.exact_boundaries",
        "strategy.twelve_candidate_top_10",
    }
)


def _identify(model: BaseModel, *, id_field: str, hash_field: str, prefix: str) -> None:
    payload = model.model_dump(mode="json", exclude={id_field, hash_field})
    content_sha256 = canonical_sha256(payload)
    content_id = f"{prefix}:{content_sha256}"
    supplied_sha256 = getattr(model, hash_field)
    supplied_id = getattr(model, id_field)
    if supplied_sha256 and supplied_sha256 != content_sha256:
        raise ValueError(f"{hash_field} does not match canonical content")
    if supplied_id and supplied_id != content_id:
        raise ValueError(f"{id_field} does not match canonical content")
    object.__setattr__(model, hash_field, content_sha256)
    object.__setattr__(model, id_field, content_id)


def _fixture_identity(prefix: str, payload: object) -> str:
    return f"{prefix}:{canonical_sha256(payload)}"


class FindingClass(StrEnum):
    LOCAL_STRATEGY_BUG = "local-strategy-bug"
    CONTRACT_TOOLKIT_GAP = "contract-toolkit-gap"
    SEMANTIC_DECISION = "semantic-decision"
    SOURCE_DATA_ISSUE = "source-data-issue"


class CoreTinyFinding(BaseModel):
    """A discovered gap, never an implicit semantic approval."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    classification: FindingClass
    code: str = Field(pattern=_CODE)
    detail: str = Field(min_length=1)


class FrozenSubject(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    issuer_id: str = Field(pattern=r"^issuer\.[a-z0-9]+$")
    instrument_id: str | None = Field(default=None, pattern=r"^instrument\.[a-z0-9.]+$")
    issuer_branch: IssuerBranch
    reporting_currency: str = Field(pattern=r"^[A-Z]{3}$")


class FrozenHeadcountVintage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    normalized_record_id: str = Field(pattern=r"^normalized-record:[0-9a-f]{64}$")
    value: Decimal = Field(gt=0)
    confidence: Decimal = Field(ge=0, le=1)
    knowable_at: datetime
    valid_period: date
    supersedes_record_id: str | None = Field(default=None, pattern=r"^normalized-record:[0-9a-f]{64}$")

    @field_validator("knowable_at")
    @classmethod
    def require_aware_knowable_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("headcount vintage knowable_at must be timezone-aware")
        return value


class FrozenHeadcountBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    normalized_record_id: str | None = Field(default=None, pattern=r"^normalized-record:[0-9a-f]{64}$")
    value: Decimal | None = Field(default=None, gt=0)
    confidence: Decimal | None = Field(default=None, ge=0, le=1)
    as_of: datetime | None = None
    valid_period: date | None = None
    availability: Literal["unavailable"] | None = None
    reason: str | None = Field(default=None, pattern=_CODE)

    @field_validator("as_of")
    @classmethod
    def require_aware_as_of(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("headcount binding as_of must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_shape(self) -> FrozenHeadcountBinding:
        available = (self.normalized_record_id, self.value, self.confidence, self.as_of, self.valid_period)
        if self.availability == "unavailable":
            if self.reason is None or any(value is not None for value in available[1:]):
                raise ValueError("unavailable headcount binding requires only an explicit reason")
        elif any(value is None for value in available):
            raise ValueError(
                "available headcount binding requires exact identity, value, confidence, as_of, and period"
            )
        return self


class FrozenObservation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    metric: CoreMetric
    value: Decimal
    unit: str = Field(min_length=1)
    confidence: Decimal = Field(ge=0, le=1)
    knowable_at: datetime
    valid_period: date

    @field_validator("knowable_at")
    @classmethod
    def require_aware_knowable_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observation knowable_at must be timezone-aware")
        return value


class FrozenInstrumentObservation(FrozenObservation):
    instrument_id: str = Field(pattern=r"^instrument\.[a-z0-9.]+$")
    cusip: str = Field(pattern=r"^[A-Z0-9]{9}$")


class FrozenCoreTinyCase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)+$")
    strata: tuple[str, ...] = ()
    subject: FrozenSubject | None = None
    cutoffs: tuple[datetime, ...] = ()
    headcount_binding: FrozenHeadcountBinding | None = None
    headcount_vintages: tuple[FrozenHeadcountVintage, ...] = ()
    observations: tuple[FrozenObservation, ...] = ()
    instrument_observations: tuple[FrozenInstrumentObservation, ...] = ()
    public_golden_case_keys: tuple[str, ...] = ()
    controls: tuple[str, ...] = Field(min_length=1)

    @field_validator("cutoffs")
    @classmethod
    def require_aware_cutoffs(cls, values: tuple[datetime, ...]) -> tuple[datetime, ...]:
        for value in values:
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("corpus cutoff must be timezone-aware")
        return values

    @model_validator(mode="after")
    def validate_case_shape(self) -> FrozenCoreTinyCase:
        if self.case_id == "public-golden-ordering-and-boundaries":
            if self.subject is not None or self.cutoffs or self.headcount_binding or self.headcount_vintages:
                raise ValueError("public-golden case cannot contain issuer inputs")
            if set(self.public_golden_case_keys) != _EXPECTED_PUBLIC_GOLDEN_CASE_KEYS:
                raise ValueError("public-golden case must bind the exact declared artifact groups")
            return self
        if self.subject is None or not self.cutoffs:
            raise ValueError("issuer cases require a subject and at least one cutoff")
        if self.headcount_binding is not None and self.headcount_vintages:
            raise ValueError("a case cannot mix a binding and a vintage headcount fixture")
        return self


class FrozenCoreTinyCorpus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1]
    corpus_id: Literal["s0-core-strategy-tiny-v1"]
    claim_ceiling: str
    allowed_environments: tuple[Literal["ci", "local"], ...]
    producer_handoff: dict[str, Any]
    candidate_policy: dict[str, Any]
    release_isolation: dict[str, Any]
    required_strata: tuple[str, ...]
    cases: tuple[FrozenCoreTinyCase, ...]

    @model_validator(mode="after")
    def validate_exact_cases(self) -> FrozenCoreTinyCorpus:
        case_ids = {case.case_id for case in self.cases}
        if case_ids != _EXPECTED_CASE_IDS or len(self.cases) != len(_EXPECTED_CASE_IDS):
            raise ValueError("frozen corpus does not contain the exact E1 case set")
        expected_isolation = {
            "default_factor_registration": False,
            "default_dagster_definition": False,
            "schedule_or_sensor": False,
            "release_manifest_activation": False,
            "staging_route": False,
            "live_source_call": False,
        }
        if self.release_isolation != expected_isolation:
            raise ValueError("frozen corpus release isolation controls drifted")
        return self


class CoreTinyInputSelection(BaseModel):
    """Runner-owned selection identity; never included in a factor request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_id: str = Field(default="", pattern=r"^(?:|core-tiny-input:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=rf"^(?:|{_SHA256[1:-1]})$")
    input_kind: Literal["headcount", "observation", "instrument_observation", "public_golden"]
    source_identity: str = Field(min_length=1)
    metric: str = Field(min_length=1)
    selected: bool
    knowable_at: datetime | None = None
    confidence: Decimal | None = Field(default=None, ge=0, le=1)
    rejection_codes: tuple[str, ...] = ()

    @field_validator("knowable_at")
    @classmethod
    def require_aware_knowable_at(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("input selection knowable_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def freeze_and_identify(self) -> CoreTinyInputSelection:
        rejection_codes = tuple(sorted(set(self.rejection_codes)))
        if len(rejection_codes) != len(self.rejection_codes):
            raise ValueError("rejection_codes must be unique")
        if self.selected and rejection_codes:
            raise ValueError("selected input cannot have rejection codes")
        if not self.selected and not rejection_codes:
            raise ValueError("rejected input requires an explicit rejection code")
        object.__setattr__(self, "rejection_codes", rejection_codes)
        _identify(self, id_field="input_id", hash_field="content_sha256", prefix="core-tiny-input")
        return self


class CoreTinyConfidenceEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    confidence_id: str = Field(default="", pattern=r"^(?:|core-tiny-confidence:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=rf"^(?:|{_SHA256[1:-1]})$")
    input_id: str = Field(pattern=r"^core-tiny-input:[0-9a-f]{64}$")
    value: Decimal = Field(ge=0, le=1)

    @model_validator(mode="after")
    def freeze_and_identify(self) -> CoreTinyConfidenceEvidence:
        _identify(self, id_field="confidence_id", hash_field="content_sha256", prefix="core-tiny-confidence")
        return self


class CoreTinyTrace(BaseModel):
    """Batch-local reverse trace that does not claim a complete capture trace."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    trace_id: str = Field(default="", pattern=r"^(?:|core-tiny-trace:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=rf"^(?:|{_SHA256[1:-1]})$")
    case_id: str
    cutoff: datetime
    request_sha256: str | None = Field(default=None, pattern=_SHA256)
    selected_input_ids: tuple[str, ...]
    rejected_input_ids: tuple[str, ...] = ()
    confidence_ids: tuple[str, ...] = ()
    output_id: str = Field(min_length=1)
    trace_scope: Literal["fixture_selection_to_provisional_output"] = "fixture_selection_to_provisional_output"

    @field_validator("cutoff")
    @classmethod
    def require_aware_cutoff(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("trace cutoff must be timezone-aware")
        return value

    @model_validator(mode="after")
    def freeze_and_identify(self) -> CoreTinyTrace:
        for field_name in ("selected_input_ids", "rejected_input_ids", "confidence_ids"):
            values = tuple(sorted(set(getattr(self, field_name))))
            if len(values) != len(getattr(self, field_name)):
                raise ValueError(f"{field_name} must be unique")
            object.__setattr__(self, field_name, values)
        if set(self.selected_input_ids) & set(self.rejected_input_ids):
            raise ValueError("a trace input cannot be both selected and rejected")
        _identify(self, id_field="trace_id", hash_field="content_sha256", prefix="core-tiny-trace")
        return self


class CoreTinyUsageAudit(BaseModel):
    """One runner-owned use of selected fixture inputs by a provisional output."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    usage_audit_id: str = Field(default="", pattern=r"^(?:|core-tiny-usage:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=rf"^(?:|{_SHA256[1:-1]})$")
    trace_id: str = Field(pattern=r"^core-tiny-trace:[0-9a-f]{64}$")
    input_ids: tuple[str, ...]
    confidence_ids: tuple[str, ...]
    output_id: str = Field(min_length=1)
    usage_scope: Literal["provisional_factor_or_strategy"] = "provisional_factor_or_strategy"

    @model_validator(mode="after")
    def freeze_and_identify(self) -> CoreTinyUsageAudit:
        for field_name in ("input_ids", "confidence_ids"):
            values = tuple(sorted(set(getattr(self, field_name))))
            if len(values) != len(getattr(self, field_name)):
                raise ValueError(f"{field_name} must be unique")
            object.__setattr__(self, field_name, values)
        _identify(self, id_field="usage_audit_id", hash_field="content_sha256", prefix="core-tiny-usage")
        return self


class CoreTinyReverseReview(BaseModel):
    """The evidence-side path from a provisional output back to its exact inputs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reverse_review_id: str = Field(default="", pattern=r"^(?:|core-tiny-review:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=rf"^(?:|{_SHA256[1:-1]})$")
    trace_id: str = Field(pattern=r"^core-tiny-trace:[0-9a-f]{64}$")
    usage_audit_id: str = Field(pattern=r"^core-tiny-usage:[0-9a-f]{64}$")
    output_id: str = Field(min_length=1)
    reviewed_input_ids: tuple[str, ...]
    blocker_codes: tuple[str, ...] = ()
    findings: tuple[CoreTinyFinding, ...] = ()
    factor_validation_status: FactorValidationStatus = FactorValidationStatus.NOT_EVALUATED
    review_scope: Literal["fixture_reverse_review_only"] = "fixture_reverse_review_only"

    @model_validator(mode="after")
    def freeze_and_identify(self) -> CoreTinyReverseReview:
        input_ids = tuple(sorted(set(self.reviewed_input_ids)))
        blockers = tuple(sorted(set(self.blocker_codes)))
        findings = tuple(sorted(set(self.findings), key=lambda item: (item.classification.value, item.code)))
        if len(input_ids) != len(self.reviewed_input_ids):
            raise ValueError("reviewed_input_ids must be unique")
        if len(blockers) != len(self.blocker_codes):
            raise ValueError("blocker_codes must be unique")
        if len(findings) != len(self.findings):
            raise ValueError("findings must be unique")
        if self.factor_validation_status is not FactorValidationStatus.NOT_EVALUATED:
            raise ValueError("E1 cannot claim a completed factor validation")
        object.__setattr__(self, "reviewed_input_ids", input_ids)
        object.__setattr__(self, "blocker_codes", blockers)
        object.__setattr__(self, "findings", findings)
        _identify(self, id_field="reverse_review_id", hash_field="content_sha256", prefix="core-tiny-review")
        return self


class CoreTinyRunEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(default="", pattern=r"^(?:|core-tiny-run:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=rf"^(?:|{_SHA256[1:-1]})$")
    cutoff: datetime
    passed: bool
    input_selections: tuple[CoreTinyInputSelection, ...]
    confidence_evidence: tuple[CoreTinyConfidenceEvidence, ...]
    trace: CoreTinyTrace
    usage_audit: CoreTinyUsageAudit
    reverse_review: CoreTinyReverseReview
    factor_result: CoreTinyResult | None = None
    ranking: ProvisionalRanking | None = None

    @field_validator("cutoff")
    @classmethod
    def require_aware_cutoff(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("run cutoff must be timezone-aware")
        return value

    @model_validator(mode="after")
    def freeze_and_identify(self) -> CoreTinyRunEvidence:
        if (self.factor_result is None) == (self.ranking is None):
            raise ValueError("a tiny run requires exactly one provisional output")
        selections = tuple(sorted(self.input_selections, key=lambda item: item.input_id))
        confidences = tuple(sorted(self.confidence_evidence, key=lambda item: item.confidence_id))
        if len({item.input_id for item in selections}) != len(selections):
            raise ValueError("run input identities must be unique")
        if len({item.confidence_id for item in confidences}) != len(confidences):
            raise ValueError("run confidence identities must be unique")
        selected_ids = tuple(item.input_id for item in selections if item.selected)
        rejected_ids = tuple(item.input_id for item in selections if not item.selected)
        confidence_ids = tuple(item.confidence_id for item in confidences)
        if self.factor_result is not None:
            output_id = self.factor_result.result_id
            request_sha256 = self.factor_result.request_sha256
        else:
            assert self.ranking is not None
            output_id = self.ranking.ranking_id
            request_sha256 = None
        if self.trace.cutoff != self.cutoff or self.trace.request_sha256 != request_sha256:
            raise ValueError("run trace does not bind the exact cutoff and request")
        if self.trace.selected_input_ids != selected_ids or self.trace.rejected_input_ids != rejected_ids:
            raise ValueError("run trace does not bind selected and rejected inputs")
        if self.trace.confidence_ids != confidence_ids or self.trace.output_id != output_id:
            raise ValueError("run trace does not bind confidence and output identities")
        if self.usage_audit.trace_id != self.trace.trace_id or self.usage_audit.output_id != output_id:
            raise ValueError("usage audit does not bind the run trace and output")
        if self.usage_audit.input_ids != selected_ids or self.usage_audit.confidence_ids != confidence_ids:
            raise ValueError("usage audit does not bind exact selected inputs")
        if self.reverse_review.trace_id != self.trace.trace_id:
            raise ValueError("reverse review does not bind the run trace")
        if self.reverse_review.usage_audit_id != self.usage_audit.usage_audit_id:
            raise ValueError("reverse review does not bind the usage audit")
        if self.reverse_review.output_id != output_id:
            raise ValueError("reverse review does not bind the provisional output")
        object.__setattr__(self, "input_selections", selections)
        object.__setattr__(self, "confidence_evidence", confidences)
        _identify(self, id_field="run_id", hash_field="content_sha256", prefix="core-tiny-run")
        return self


class CoreTinyCaseEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    strata: tuple[str, ...]
    passed: bool
    runs: tuple[CoreTinyRunEvidence, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_runs(self) -> CoreTinyCaseEvidence:
        runs = tuple(sorted(self.runs, key=lambda item: item.cutoff))
        if len({item.cutoff for item in runs}) != len(runs):
            raise ValueError("case cutoffs must be unique")
        if self.passed != all(item.passed for item in runs):
            raise ValueError("case pass state must be derived from its runs")
        object.__setattr__(self, "runs", runs)
        return self


class CoreTinyEvidence(BaseModel):
    """Content-addressed terminal E1 evidence, explicitly not a handoff."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str = Field(default="", pattern=r"^(?:|core-tiny-evidence:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=rf"^(?:|{_SHA256[1:-1]})$")
    corpus_sha256: str = Field(pattern=_SHA256)
    activation: CoreTinyActivation
    cases: tuple[CoreTinyCaseEvidence, ...] = Field(min_length=9, max_length=9)
    created_at: datetime
    factor_validation_status: FactorValidationStatus = FactorValidationStatus.NOT_EVALUATED
    semantic_policy_state: Literal["candidate_unapproved"] = "candidate_unapproved"
    requires_e2_contract_repair: bool = False
    stable_handoff: Literal[False] = False
    claim_ceiling: Literal["E1_tiny_development_evidence_only"] = "E1_tiny_development_evidence_only"

    @field_validator("created_at")
    @classmethod
    def require_aware_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("evidence created_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def freeze_and_identify(self) -> CoreTinyEvidence:
        if self.corpus_sha256 != FROZEN_CORPUS_SHA256:
            raise ValueError("E1 evidence must bind the exact frozen corpus")
        if self.factor_validation_status is not FactorValidationStatus.NOT_EVALUATED:
            raise ValueError("E1 evidence must remain factor-validation not evaluated")
        cases = tuple(sorted(self.cases, key=lambda item: item.case_id))
        case_ids = {case.case_id for case in cases}
        if case_ids != _EXPECTED_CASE_IDS or len(cases) != len(_EXPECTED_CASE_IDS):
            raise ValueError("E1 evidence requires every frozen corpus case exactly once")
        expected_repair = any(
            finding.classification is FindingClass.CONTRACT_TOOLKIT_GAP
            for case in cases
            for run in case.runs
            for finding in run.reverse_review.findings
        )
        if self.requires_e2_contract_repair != expected_repair:
            raise ValueError("E2 repair requirement must be derived from classified findings")
        object.__setattr__(self, "cases", cases)
        _identify(self, id_field="evidence_id", hash_field="content_sha256", prefix="core-tiny-evidence")
        return self


class CoreTinyEvidenceRepository(Protocol):
    def put(self, evidence: CoreTinyEvidence) -> bool: ...

    def get(self, evidence_id: str) -> CoreTinyEvidence | None: ...


class InMemoryCoreTinyEvidenceRepository:
    """Append-only, idempotent storage for reproducible Local/CI evidence."""

    def __init__(self) -> None:
        self._records: dict[str, CoreTinyEvidence] = {}

    def put(self, evidence: CoreTinyEvidence) -> bool:
        existing = self._records.get(evidence.evidence_id)
        if existing is None:
            self._records[evidence.evidence_id] = evidence
            return True
        if existing != evidence:
            raise ValueError("evidence ID is already bound to different content")
        return False

    def get(self, evidence_id: str) -> CoreTinyEvidence | None:
        return self._records.get(evidence_id)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_frozen_corpus(repository_root: Path) -> FrozenCoreTinyCorpus:
    corpus_path = repository_root / "libs/factors/tests/batches/core_strategy_tiny/fixtures/corpus.v1.json"
    raw = corpus_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != FROZEN_CORPUS_SHA256:
        raise ValueError("core strategy tiny corpus hash drifted")
    return FrozenCoreTinyCorpus.model_validate(json.loads(raw))


def _selection(
    *,
    input_kind: Literal["headcount", "observation", "instrument_observation", "public_golden"],
    source_identity: str,
    metric: str,
    selected: bool,
    knowable_at: datetime | None = None,
    confidence: Decimal | None = None,
    rejection_codes: tuple[str, ...] = (),
) -> CoreTinyInputSelection:
    return CoreTinyInputSelection(
        input_kind=input_kind,
        source_identity=source_identity,
        metric=metric,
        selected=selected,
        knowable_at=knowable_at,
        confidence=confidence,
        rejection_codes=rejection_codes,
    )


def _confidence_evidence(selections: tuple[CoreTinyInputSelection, ...]) -> tuple[CoreTinyConfidenceEvidence, ...]:
    return tuple(
        CoreTinyConfidenceEvidence(input_id=selection.input_id, value=selection.confidence)
        for selection in selections
        if selection.confidence is not None
    )


def _select_headcount_vintage(
    vintages: tuple[FrozenHeadcountVintage, ...], cutoff: datetime
) -> FrozenHeadcountVintage | None:
    eligible = tuple(item for item in vintages if item.knowable_at <= cutoff)
    superseded = {item.supersedes_record_id for item in eligible if item.supersedes_record_id is not None}
    active = tuple(item for item in eligible if item.normalized_record_id not in superseded)
    if not active:
        return None
    return max(active, key=lambda item: (item.knowable_at, item.normalized_record_id))


def _is_fixture_stale(case: FrozenCoreTinyCase, observation: FrozenObservation, cutoff: datetime) -> bool:
    """Exercise the one declared stale fixture without publishing a freshness policy."""

    return (
        case.case_id == "ddog-stale-low-confidence"
        and cutoff.date() - observation.valid_period > _PROVISIONAL_MAXIMUM_INPUT_AGE
    )


def _observation_identity(case_id: str, observation: FrozenObservation) -> str:
    return _fixture_identity(
        "fixture-observation",
        {"case_id": case_id, "observation": observation.model_dump(mode="json")},
    )


def _factor_observation(
    observation: FrozenObservation,
    *,
    subject_kind: SubjectKind,
    entity_id: str,
    cutoff: datetime,
) -> CoreObservation:
    currency = None if observation.metric is CoreMetric.CURRENT_PS else observation.unit
    return CoreObservation(
        subject_kind=subject_kind,
        entity_id=entity_id,
        metric=observation.metric,
        value=observation.value,
        unit=observation.unit,
        currency=currency,
        valid_period=observation.valid_period,
        confidence=observation.confidence,
        as_of=cutoff,
        availability_status=AvailabilityStatus.AVAILABLE,
    )


def _headcount_factor_observation(
    *,
    subject: FrozenSubject,
    record_id: str,
    value: Decimal,
    confidence: Decimal,
    valid_period: date,
    cutoff: datetime,
) -> CoreObservation:
    del record_id  # The factor intentionally cannot receive the selected record identity.
    return CoreObservation.from_h0_headcount(
        H0HeadcountFactorInput(
            entity_id=subject.issuer_id,
            value=value,
            confidence=confidence,
            as_of=cutoff,
            fiscal_period=valid_period.isoformat(),
        )
    )


def _requested_instrument_id(case: FrozenCoreTinyCase, subject: FrozenSubject) -> str:
    if subject.instrument_id is not None:
        return subject.instrument_id
    if case.case_id != "alphabet-dual-listing-identity":
        raise ValueError("issuer case is missing an explicit instrument demand")
    expected_listing_ids = ("instrument.us.goog", "instrument.us.googl")
    listing_ids = tuple(sorted(observation.instrument_id for observation in case.instrument_observations))
    if listing_ids != expected_listing_ids:
        raise ValueError("dual-listing fixture must declare the exact GOOG and GOOGL listings")
    # The fixture deliberately probes a GOOG demand against a separate GOOGL listing.
    return "instrument.us.goog"


def _record_finding(
    findings: list[CoreTinyFinding],
    classification: FindingClass,
    code: str,
    detail: str,
) -> None:
    candidate = CoreTinyFinding(classification=classification, code=code, detail=detail)
    if candidate not in findings:
        findings.append(candidate)


def _build_run(
    *,
    case: FrozenCoreTinyCase,
    cutoff: datetime,
    activation: CoreTinyActivation,
    selections: tuple[CoreTinyInputSelection, ...],
    factor_result: CoreTinyResult | None,
    ranking: ProvisionalRanking | None,
    findings: tuple[CoreTinyFinding, ...],
    blocker_codes: tuple[str, ...],
    passed: bool,
) -> CoreTinyRunEvidence:
    if (factor_result is None) == (ranking is None):
        raise ValueError("E1 run construction requires exactly one output")
    confidences = _confidence_evidence(selections)
    selected_ids = tuple(sorted(item.input_id for item in selections if item.selected))
    rejected_ids = tuple(sorted(item.input_id for item in selections if not item.selected))
    confidence_ids = tuple(sorted(item.confidence_id for item in confidences))
    if factor_result is not None:
        output_id = factor_result.result_id
        request_sha256 = factor_result.request_sha256
    else:
        assert ranking is not None
        output_id = ranking.ranking_id
        request_sha256 = None
    trace = CoreTinyTrace(
        case_id=case.case_id,
        cutoff=cutoff,
        request_sha256=request_sha256,
        selected_input_ids=selected_ids,
        rejected_input_ids=rejected_ids,
        confidence_ids=confidence_ids,
        output_id=output_id,
    )
    usage_audit = CoreTinyUsageAudit(
        trace_id=trace.trace_id,
        input_ids=selected_ids,
        confidence_ids=confidence_ids,
        output_id=output_id,
    )
    reverse_review = CoreTinyReverseReview(
        trace_id=trace.trace_id,
        usage_audit_id=usage_audit.usage_audit_id,
        output_id=output_id,
        reviewed_input_ids=tuple(item.input_id for item in selections),
        blocker_codes=blocker_codes,
        findings=findings,
    )
    return CoreTinyRunEvidence(
        cutoff=cutoff,
        passed=passed,
        input_selections=selections,
        confidence_evidence=confidences,
        trace=trace,
        usage_audit=usage_audit,
        reverse_review=reverse_review,
        factor_result=factor_result,
        ranking=ranking,
    )


def _run_issuer_case(
    case: FrozenCoreTinyCase,
    *,
    cutoff: datetime,
    activation: CoreTinyActivation,
) -> CoreTinyRunEvidence:
    if case.subject is None:
        raise ValueError("issuer runner requires a corpus subject")
    subject = case.subject
    requested_instrument_id = _requested_instrument_id(case, subject)
    selections: list[CoreTinyInputSelection] = []
    observations: list[CoreObservation] = []
    findings: list[CoreTinyFinding] = []
    blocker_codes: list[str] = []

    headcount = None
    if case.headcount_vintages:
        headcount = _select_headcount_vintage(case.headcount_vintages, cutoff)
        for vintage in case.headcount_vintages:
            selected = vintage == headcount
            rejection = (
                None
                if selected
                else "future_known_headcount"
                if vintage.knowable_at > cutoff
                else "superseded_headcount"
            )
            selections.append(
                _selection(
                    input_kind="headcount",
                    source_identity=vintage.normalized_record_id,
                    metric=CoreMetric.EMPLOYEE_HEADCOUNT.value,
                    selected=selected,
                    knowable_at=vintage.knowable_at,
                    confidence=vintage.confidence,
                    rejection_codes=() if rejection is None else (rejection,),
                )
            )
        if headcount is not None:
            observations.append(
                _headcount_factor_observation(
                    subject=subject,
                    record_id=headcount.normalized_record_id,
                    value=headcount.value,
                    confidence=headcount.confidence,
                    valid_period=headcount.valid_period,
                    cutoff=cutoff,
                )
            )
        else:
            blocker_codes.append("future_known_headcount")
    elif case.headcount_binding is not None:
        binding = case.headcount_binding
        if binding.availability == "unavailable":
            reason = binding.reason or "unavailable_headcount"
            source_identity = binding.normalized_record_id or _fixture_identity(
                "fixture-unavailable-headcount", {"case_id": case.case_id, "reason": reason}
            )
            selections.append(
                _selection(
                    input_kind="headcount",
                    source_identity=source_identity,
                    metric=CoreMetric.EMPLOYEE_HEADCOUNT.value,
                    selected=False,
                    rejection_codes=(reason,),
                )
            )
            blocker_codes.append(reason)
            _record_finding(
                findings,
                FindingClass.SOURCE_DATA_ISSUE,
                reason,
                "The frozen H0 fixture has no usable total-headcount observation for this issuer.",
            )
        else:
            assert binding.normalized_record_id is not None
            assert binding.value is not None and binding.confidence is not None
            assert binding.as_of is not None and binding.valid_period is not None
            selected = binding.as_of <= cutoff
            rejection = None if selected else "future_known_headcount"
            selections.append(
                _selection(
                    input_kind="headcount",
                    source_identity=binding.normalized_record_id,
                    metric=CoreMetric.EMPLOYEE_HEADCOUNT.value,
                    selected=selected,
                    knowable_at=binding.as_of,
                    confidence=binding.confidence,
                    rejection_codes=() if rejection is None else (rejection,),
                )
            )
            if selected:
                observations.append(
                    _headcount_factor_observation(
                        subject=subject,
                        record_id=binding.normalized_record_id,
                        value=binding.value,
                        confidence=binding.confidence,
                        valid_period=binding.valid_period,
                        cutoff=cutoff,
                    )
                )
            else:
                assert rejection is not None
                blocker_codes.append(rejection)

    for observation in case.observations:
        source_identity = _observation_identity(case.case_id, observation)
        rejection_codes: list[str] = []
        if observation.knowable_at > cutoff:
            rejection_codes.append(f"future_known_{observation.metric.value}")
        if observation.confidence < _PROVISIONAL_LOW_CONFIDENCE_CUTOFF:
            rejection_codes.append(f"low_confidence_{observation.metric.value}")
        if _is_fixture_stale(case, observation, cutoff):
            rejection_codes.append(f"stale_{observation.metric.value}")
        selected = not rejection_codes
        selections.append(
            _selection(
                input_kind="observation",
                source_identity=source_identity,
                metric=observation.metric.value,
                selected=selected,
                knowable_at=observation.knowable_at,
                confidence=observation.confidence,
                rejection_codes=tuple(rejection_codes),
            )
        )
        blocker_codes.extend(rejection_codes)
        if selected:
            subject_kind = (
                SubjectKind.INSTRUMENT
                if observation.metric
                in {CoreMetric.MARKET_CAP, CoreMetric.CURRENT_PS, CoreMetric.FX_REPORTING_TO_MARKET}
                else SubjectKind.ISSUER
            )
            entity_id = requested_instrument_id if subject_kind is SubjectKind.INSTRUMENT else subject.issuer_id
            observations.append(
                _factor_observation(
                    observation,
                    subject_kind=subject_kind,
                    entity_id=entity_id,
                    cutoff=cutoff,
                )
            )

    for observation in case.instrument_observations:
        source_identity = _fixture_identity(
            "fixture-instrument-observation",
            {"case_id": case.case_id, "observation": observation.model_dump(mode="json")},
        )
        selected = observation.instrument_id == requested_instrument_id and observation.knowable_at <= cutoff
        rejection = (
            None
            if selected
            else "future_known_market_cap"
            if observation.knowable_at > cutoff
            else "wrong_instrument_market_cap"
        )
        selections.append(
            _selection(
                input_kind="instrument_observation",
                source_identity=source_identity,
                metric=observation.metric.value,
                selected=selected,
                knowable_at=observation.knowable_at,
                confidence=observation.confidence,
                rejection_codes=() if rejection is None else (rejection,),
            )
        )
        if selected:
            observations.append(
                _factor_observation(
                    observation,
                    subject_kind=SubjectKind.INSTRUMENT,
                    entity_id=requested_instrument_id,
                    cutoff=cutoff,
                )
            )
        else:
            assert rejection is not None
            blocker_codes.append(rejection)

    market_currency = next(
        (
            observation.currency
            for observation in observations
            if observation.metric is CoreMetric.MARKET_CAP and observation.currency is not None
        ),
        subject.reporting_currency,
    )
    request = CoreTinyRequest(
        issuer_id=subject.issuer_id,
        instrument_id=requested_instrument_id,
        issuer_branch=subject.issuer_branch,
        reporting_currency=subject.reporting_currency,
        market_currency=market_currency,
        as_of=cutoff,
        observations=tuple(observations),
    )
    result = evaluate_core_tiny(activation=activation, request=request)

    has_point_headcount = any(item.metric is CoreMetric.EMPLOYEE_HEADCOUNT for item in observations)
    if has_point_headcount:
        _record_finding(
            findings,
            FindingClass.SEMANTIC_DECISION,
            "point_headcount_not_period_average",
            "The accepted H0 point-in-time headcount cannot satisfy the period-average denominator without a semantic decision.",
        )
        blocker_codes.append("missing_period_average_employee_count")
    if case.case_id == "ddog-stale-low-confidence":
        _record_finding(
            findings,
            FindingClass.SEMANTIC_DECISION,
            "provisional_freshness_threshold_unapproved",
            "The stale and low-confidence fixture controls are local E1 predicates, not an accepted quality threshold.",
        )
    if case.subject.issuer_branch is IssuerBranch.FINANCIAL:
        _record_finding(
            findings,
            FindingClass.SEMANTIC_DECISION,
            "financial_tier_mapping_unapproved",
            "The financial issuer branch has no accepted tier mapping in the candidate policy.",
        )
    metric_set = {item.metric for item in observations}
    if {CoreMetric.ANNUAL_REVENUE, CoreMetric.MARKET_CAP} <= metric_set:
        if subject.reporting_currency != market_currency and CoreMetric.FX_REPORTING_TO_MARKET not in metric_set:
            _record_finding(
                findings,
                FindingClass.SEMANTIC_DECISION,
                "cross_currency_without_accepted_fx",
                "The fixture has reporting and market values in different currencies without an accepted PIT FX input.",
            )
            blocker_codes.append("cross_currency_without_accepted_fx")
        elif CoreMetric.CURRENT_PS not in metric_set:
            _record_finding(
                findings,
                FindingClass.SEMANTIC_DECISION,
                "current_ps_construction_unapproved",
                "Revenue and market capitalization cannot be silently converted into an approved current P/S input.",
            )

    control_passed = True
    if case.case_id == "plug-restatement-publication-boundary":
        expected = (
            "normalized-record:dd0e02b953d4ee566b675186ded6130f7ba78acd766a6b4b7be1ce62d4716c6e"
            if cutoff < datetime(2022, 3, 14, 21, 30, 4, tzinfo=UTC)
            else "normalized-record:0120c267f8e692bb421815ea592dee787aa0bf3e684557c2972c41a1aa1e6cab"
        )
        control_passed = any(item.selected and item.source_identity == expected for item in selections)
    elif case.case_id == "nice-lookahead-sentinel":
        if cutoff < datetime(2026, 2, 26, 21, 17, 11, tzinfo=UTC):
            control_passed = all(not item.selected for item in selections)
        else:
            control_passed = all(item.selected for item in selections)
    elif case.case_id == "ddog-stale-low-confidence":
        control_passed = {
            "low_confidence_annual_gross_profit",
            "stale_annual_gross_profit",
        }.issubset(set(blocker_codes))
    elif case.case_id == "alphabet-dual-listing-identity":
        control_passed = any("wrong_instrument_market_cap" in item.rejection_codes for item in selections)
    elif case.case_id == "nice-cross-currency-without-fx":
        control_passed = "cross_currency_without_accepted_fx" in blocker_codes
    elif case.case_id == "nvda-missing-headcount":
        control_passed = "no-total-headcount-disclosure" in blocker_codes

    if not control_passed:
        _record_finding(
            findings,
            FindingClass.LOCAL_STRATEGY_BUG,
            "frozen_control_not_observed",
            "The runner did not observe the control declared by the frozen corpus.",
        )
    return _build_run(
        case=case,
        cutoff=cutoff,
        activation=activation,
        selections=tuple(selections),
        factor_result=result,
        ranking=None,
        findings=tuple(findings),
        blocker_codes=tuple(blocker_codes),
        passed=control_passed,
    )


def _load_public_strategy_ranking(
    repository_root: Path,
    *,
    case_keys: tuple[str, ...],
) -> tuple[ProvisionalRanking, tuple[CoreTinyInputSelection, ...], bool]:
    manifest_path = repository_root / "governance/gate0/public-goldens/manifest.v1.json"
    if _sha256(manifest_path) != PUBLIC_GOLDEN_MANIFEST_SHA256:
        raise ValueError("public golden manifest hash drifted")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    cases_by_key = {item["case_key"]: item for item in manifest["cases"]}
    if set(case_keys) != _EXPECTED_PUBLIC_GOLDEN_CASE_KEYS:
        raise ValueError("public golden runner received an unexpected artifact group")
    selections: list[CoreTinyInputSelection] = []
    loaded: dict[str, dict[str, Any]] = {}
    for case_key in sorted(case_keys):
        golden_case = cases_by_key.get(case_key)
        if golden_case is None:
            raise ValueError(f"public golden case is missing: {case_key}")
        artifacts = golden_case["artifacts"]
        loaded[case_key] = {}
        for name, artifact in sorted(artifacts.items()):
            path = repository_root / artifact["path"]
            if _sha256(path) != artifact["sha256"]:
                raise ValueError(f"public golden {case_key}/{name} hash drifted")
            loaded[case_key][name] = json.loads(path.read_text(encoding="utf-8"))
            selections.append(
                _selection(
                    input_kind="public_golden",
                    source_identity=f"public-golden:{case_key}:{artifact['sha256']}",
                    metric=f"{case_key}:{name}",
                    selected=True,
                )
            )
    strategy_artifacts = loaded["strategy.twelve_candidate_top_10"]
    candidates = tuple(
        RankingCandidate.model_validate(value) for value in strategy_artifacts["input"]["input"]["candidates"]
    )
    ranking = rank_provisional_candidates(tuple(reversed(candidates)))
    expected = strategy_artifacts["expected"]["expected"]
    expected_selected = tuple(expected["selected_top_10"])
    passed = (
        ranking.availability_status is AvailabilityStatus.AVAILABLE
        and ranking.selected_candidate_ids == expected_selected
        and tuple(item.candidate_id for item in ranking.ineligible_candidates) == ("candidate-12",)
    )
    return ranking, tuple(selections), passed


def _run_public_golden_case(
    case: FrozenCoreTinyCase,
    *,
    repository_root: Path,
    activation: CoreTinyActivation,
) -> CoreTinyRunEvidence:
    ranking, selections, passed = _load_public_strategy_ranking(
        repository_root,
        case_keys=case.public_golden_case_keys,
    )
    findings: tuple[CoreTinyFinding, ...] = ()
    if not passed:
        findings = (
            CoreTinyFinding(
                classification=FindingClass.LOCAL_STRATEGY_BUG,
                code="public_golden_ordering_mismatch",
                detail="The candidate ranking no longer matches the pinned public development golden.",
            ),
        )
    return _build_run(
        case=case,
        cutoff=_E1_CREATED_AT,
        activation=activation,
        selections=selections,
        factor_result=None,
        ranking=ranking,
        findings=findings,
        blocker_codes=(),
        passed=passed,
    )


def run_e1_suite(
    repository_root: Path,
    *,
    environment: Literal["ci", "local"] = "ci",
    reverse_case_order: bool = False,
    reverse_observation_order: bool = False,
) -> CoreTinyEvidence:
    """Execute the exact E1 corpus without live sources, schedules, or release activation."""

    corpus = load_frozen_corpus(repository_root)
    if environment not in corpus.allowed_environments:
        raise ValueError("E1 environment is not authorized by the frozen corpus")
    activation = CoreTinyActivation(environment=environment)
    cases = tuple(reversed(corpus.cases)) if reverse_case_order else corpus.cases
    evidence_cases: list[CoreTinyCaseEvidence] = []
    for case in cases:
        runs: tuple[CoreTinyRunEvidence, ...]
        if case.case_id == "public-golden-ordering-and-boundaries":
            runs = (_run_public_golden_case(case, repository_root=repository_root, activation=activation),)
        else:
            if reverse_observation_order:
                case = case.model_copy(
                    update={
                        "observations": tuple(reversed(case.observations)),
                        "instrument_observations": tuple(reversed(case.instrument_observations)),
                    }
                )
            runs = tuple(_run_issuer_case(case, cutoff=cutoff, activation=activation) for cutoff in case.cutoffs)
        evidence_cases.append(
            CoreTinyCaseEvidence(
                case_id=case.case_id,
                strata=case.strata,
                passed=all(run.passed for run in runs),
                runs=runs,
            )
        )
    return CoreTinyEvidence(
        corpus_sha256=FROZEN_CORPUS_SHA256,
        activation=activation,
        cases=tuple(evidence_cases),
        created_at=_E1_CREATED_AT,
    )


__all__ = [
    "CoreTinyCaseEvidence",
    "CoreTinyConfidenceEvidence",
    "CoreTinyEvidence",
    "CoreTinyEvidenceRepository",
    "CoreTinyFinding",
    "CoreTinyInputSelection",
    "CoreTinyReverseReview",
    "CoreTinyRunEvidence",
    "CoreTinyTrace",
    "CoreTinyUsageAudit",
    "FindingClass",
    "FrozenCoreTinyCorpus",
    "InMemoryCoreTinyEvidenceRepository",
    "load_frozen_corpus",
    "run_e1_suite",
]
