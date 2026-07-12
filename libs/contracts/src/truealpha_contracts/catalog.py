"""Immutable Research Catalog contracts and anti-shrink transition checks."""

from __future__ import annotations

import json
import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.execution import FactorInvocationTemplate, FactorKind
from truealpha_contracts.models import _require_aware
from truealpha_contracts.universe import SubjectRef, UniverseRef

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_CONTENT_ID_PATTERN = r"^[a-z][a-z0-9-]*:[0-9a-f]{64}$"
_OPEN_ID_PATTERN = r"^[a-z][a-z0-9]*(?:[._:/-][a-z0-9]+)*$"
_VERSION_PATTERN = r"^[a-z0-9][a-z0-9]*(?:[._-][a-z0-9]+)*$"
_ALIAS_PATTERN = r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$"
_MUTABLE_TOKENS = frozenset({"current", "default", "head", "latest", "main", "stable"})


def _canonical_model_key(value: BaseModel) -> str:
    return json.dumps(
        value.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def _validate_unique(values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must not contain duplicates")
    return tuple(sorted(values))


def _reject_mutable_coordinate(value: str, field_name: str) -> str:
    tokens = tuple(token for token in re.split(r"[._:/-]", value.lower()) if token)
    if value.lower() in _MUTABLE_TOKENS or (tokens and tokens[-1] in _MUTABLE_TOKENS):
        raise ValueError(f"{field_name} cannot use a mutable alias")
    return value


def _bind_content_address(model: BaseModel, *, id_field: str, prefix: str) -> None:
    payload = model.model_dump(mode="json", exclude={id_field, "content_sha256"})
    expected_hash = canonical_sha256(payload)
    expected_id = f"{prefix}:{expected_hash}"
    supplied_hash = getattr(model, "content_sha256")
    supplied_id = getattr(model, id_field)
    if supplied_hash and supplied_hash != expected_hash:
        raise ValueError("content_sha256 does not match canonical content")
    if supplied_id and supplied_id != expected_id:
        raise ValueError(f"{id_field} does not match canonical content")
    object.__setattr__(model, "content_sha256", expected_hash)
    object.__setattr__(model, id_field, expected_id)


def _validate_ref_hash(reference_id: str, content_sha256: str, field_name: str) -> None:
    if not reference_id.endswith(f":{content_sha256}"):
        raise ValueError(f"{field_name} ID and hash do not match")


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class CatalogTargetKind(StrEnum):
    FACTOR = "factor"
    RANKING = "ranking"
    SCREEN = "screen"
    THEME = "theme"
    SCENARIO = "scenario"
    STRATEGY = "strategy"


class CatalogRequirementLevel(StrEnum):
    REQUIRED = "required"
    OPTIONAL = "optional"
    NOT_APPLICABLE = "not_applicable"


class ExpectedOutputStatus(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    STALE = "stale"
    EXCLUDED = "excluded"
    LOW_CONFIDENCE = "low_confidence"
    ERROR = "error"


class ProductOwnerApproval(_StrictFrozenModel):
    """Verifiable approval evidence; a free-text approver name is insufficient."""

    approver_role: Literal["product_owner"] = "product_owner"
    approved_by: str = Field(min_length=1)
    approval_record_id: str = Field(pattern=_CONTENT_ID_PATTERN)
    approval_record_sha256: str = Field(pattern=_SHA256_PATTERN)
    approved_at: datetime

    @field_validator("approved_at")
    @classmethod
    def validate_approved_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "approved_at")

    @model_validator(mode="after")
    def validate_record_binding(self) -> ProductOwnerApproval:
        _validate_ref_hash(self.approval_record_id, self.approval_record_sha256, "approval record")
        return self


class InvocationParameter(_StrictFrozenModel):
    name: str = Field(pattern=_ALIAS_PATTERN)
    canonical_json: str = Field(min_length=1)

    @field_validator("canonical_json")
    @classmethod
    def validate_canonical_json(cls, value: str) -> str:
        def reject_constant(constant: str) -> None:
            raise ValueError(f"non-finite JSON value {constant} is not permitted")

        try:
            decoded = json.loads(value, parse_constant=reject_constant)
        except (TypeError, ValueError) as error:
            raise ValueError("canonical_json must contain valid finite JSON") from error
        canonical = json.dumps(decoded, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        if value != canonical:
            raise ValueError("canonical_json must use canonical JSON encoding")
        return value


class InvocationTemplateSelector(_StrictFrozenModel):
    """Catalog metadata around the one executable FactorInvocationTemplate."""

    target_kind: CatalogTargetKind
    factor_template: FactorInvocationTemplate
    parameters: tuple[InvocationParameter, ...] = ()
    frozen_at: datetime

    @field_validator("frozen_at")
    @classmethod
    def validate_frozen_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "frozen_at")

    @model_validator(mode="after")
    def sort_and_validate(self) -> InvocationTemplateSelector:
        parameters = tuple(sorted(self.parameters, key=lambda item: item.name))
        parameter_names = [item.name for item in parameters]
        if len(parameter_names) != len(set(parameter_names)):
            raise ValueError("invocation parameters must not contain duplicates")
        decoded_parameters = {item.name: json.loads(item.canonical_json) for item in parameters}
        if canonical_sha256(decoded_parameters) != self.factor_template.canonical_parameters_sha256:
            raise ValueError("catalog parameters do not match the executable factor template")
        object.__setattr__(self, "parameters", parameters)
        return self

    @property
    def invocation_template_id(self) -> str:
        return self.factor_template.factor_template_id

    @property
    def content_sha256(self) -> str:
        return self.factor_template.content_sha256


class FactorCatalogTarget(_StrictFrozenModel):
    target_kind: Literal[CatalogTargetKind.FACTOR] = CatalogTargetKind.FACTOR
    factor_id: str = Field(pattern=_OPEN_ID_PATTERN)
    factor_version: str = Field(pattern=_VERSION_PATTERN)
    definition_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("factor_id", "factor_version")
    @classmethod
    def reject_mutable_coordinates(cls, value: str, info: Any) -> str:
        return _reject_mutable_coordinate(value, info.field_name)


class RankingCatalogTarget(_StrictFrozenModel):
    target_kind: Literal[CatalogTargetKind.RANKING] = CatalogTargetKind.RANKING
    ranking_id: str = Field(pattern=_OPEN_ID_PATTERN)
    ranking_version: str = Field(pattern=_VERSION_PATTERN)
    definition_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("ranking_id", "ranking_version")
    @classmethod
    def reject_mutable_coordinates(cls, value: str, info: Any) -> str:
        return _reject_mutable_coordinate(value, info.field_name)


class ScreenCatalogTarget(_StrictFrozenModel):
    target_kind: Literal[CatalogTargetKind.SCREEN] = CatalogTargetKind.SCREEN
    screen_id: str = Field(pattern=_OPEN_ID_PATTERN)
    screen_version: str = Field(pattern=_VERSION_PATTERN)
    definition_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("screen_id", "screen_version")
    @classmethod
    def reject_mutable_coordinates(cls, value: str, info: Any) -> str:
        return _reject_mutable_coordinate(value, info.field_name)


class ThemeCatalogTarget(_StrictFrozenModel):
    target_kind: Literal[CatalogTargetKind.THEME] = CatalogTargetKind.THEME
    theme_id: str = Field(pattern=_OPEN_ID_PATTERN)
    ontology_version: str = Field(pattern=_VERSION_PATTERN)
    ontology_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("theme_id", "ontology_version")
    @classmethod
    def reject_mutable_coordinates(cls, value: str, info: Any) -> str:
        return _reject_mutable_coordinate(value, info.field_name)


class ScenarioCatalogTarget(_StrictFrozenModel):
    target_kind: Literal[CatalogTargetKind.SCENARIO] = CatalogTargetKind.SCENARIO
    scenario_id: str = Field(pattern=_OPEN_ID_PATTERN)
    scenario_version: str = Field(pattern=_VERSION_PATTERN)
    definition_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("scenario_id", "scenario_version")
    @classmethod
    def reject_mutable_coordinates(cls, value: str, info: Any) -> str:
        return _reject_mutable_coordinate(value, info.field_name)


class StrategyCatalogTarget(_StrictFrozenModel):
    target_kind: Literal[CatalogTargetKind.STRATEGY] = CatalogTargetKind.STRATEGY
    strategy_id: str = Field(pattern=_OPEN_ID_PATTERN)
    strategy_version: str = Field(pattern=_VERSION_PATTERN)
    definition_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("strategy_id", "strategy_version")
    @classmethod
    def reject_mutable_coordinates(cls, value: str, info: Any) -> str:
        return _reject_mutable_coordinate(value, info.field_name)


CatalogTarget = Annotated[
    FactorCatalogTarget
    | RankingCatalogTarget
    | ScreenCatalogTarget
    | ThemeCatalogTarget
    | ScenarioCatalogTarget
    | StrategyCatalogTarget,
    Field(discriminator="target_kind"),
]


def _target_execution_binding(target: CatalogTarget) -> tuple[str, str, str]:
    if isinstance(target, FactorCatalogTarget):
        return target.factor_id, target.factor_version, target.definition_sha256
    if isinstance(target, RankingCatalogTarget):
        return target.ranking_id, target.ranking_version, target.definition_sha256
    if isinstance(target, ScreenCatalogTarget):
        return target.screen_id, target.screen_version, target.definition_sha256
    if isinstance(target, ThemeCatalogTarget):
        return target.theme_id, target.ontology_version, target.ontology_sha256
    if isinstance(target, ScenarioCatalogTarget):
        return target.scenario_id, target.scenario_version, target.definition_sha256
    return target.strategy_id, target.strategy_version, target.definition_sha256


class CanonicalQuestion(_StrictFrozenModel):
    canonical_question_id: str = Field(
        default="",
        pattern=r"^(?:|canonical-question:[0-9a-f]{64})$",
    )
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    question_key: str = Field(pattern=_OPEN_ID_PATTERN)
    tool_kind: CatalogTargetKind
    catalog_aliases: tuple[str, ...] = Field(min_length=1)
    subject_scope: tuple[SubjectRef, ...] = Field(min_length=1)
    requirement_level: CatalogRequirementLevel
    expected_output_type_ids: tuple[str, ...] = Field(min_length=1)
    expected_statuses: tuple[ExpectedOutputStatus, ...] = Field(min_length=1)
    prompt_examples: tuple[str, ...] = Field(min_length=1)
    approved_at: datetime

    @field_validator("question_key")
    @classmethod
    def reject_mutable_question_key(cls, value: str) -> str:
        return _reject_mutable_coordinate(value, "question_key")

    @field_validator("approved_at")
    @classmethod
    def validate_approved_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "approved_at")

    @field_validator("catalog_aliases")
    @classmethod
    def validate_aliases(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            if not re.fullmatch(_ALIAS_PATTERN, value):
                raise ValueError("catalog aliases must use stable lowercase coordinates")
            _reject_mutable_coordinate(value, "catalog_alias")
        return _validate_unique(values, "catalog_aliases")

    @field_validator("expected_output_type_ids")
    @classmethod
    def validate_output_types(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not re.fullmatch(_OPEN_ID_PATTERN, value) for value in values):
            raise ValueError("expected output type IDs must use stable lowercase coordinates")
        return _validate_unique(values, "expected_output_type_ids")

    @field_validator("expected_statuses")
    @classmethod
    def validate_statuses(cls, values: tuple[ExpectedOutputStatus, ...]) -> tuple[ExpectedOutputStatus, ...]:
        if len(values) != len(set(values)):
            raise ValueError("expected_statuses must not contain duplicates")
        return tuple(sorted(values, key=lambda item: item.value))

    @field_validator("prompt_examples")
    @classmethod
    def validate_prompt_examples(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("prompt examples cannot be blank")
        return _validate_unique(values, "prompt_examples")

    @model_validator(mode="after")
    def sort_validate_and_identify(self) -> CanonicalQuestion:
        subjects = tuple(sorted(self.subject_scope, key=_canonical_model_key))
        subject_keys = [_canonical_model_key(subject) for subject in subjects]
        if len(subject_keys) != len(set(subject_keys)):
            raise ValueError("subject_scope must not contain duplicates")
        object.__setattr__(self, "subject_scope", subjects)
        _bind_content_address(self, id_field="canonical_question_id", prefix="canonical-question")
        return self


class ResearchCatalogEntry(_StrictFrozenModel):
    catalog_entry_id: str = Field(
        default="",
        pattern=r"^(?:|catalog-entry:[0-9a-f]{64})$",
    )
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    catalog_alias: str = Field(pattern=_ALIAS_PATTERN)
    requirement_level: CatalogRequirementLevel
    target: CatalogTarget
    universe: UniverseRef
    subject_scope: tuple[SubjectRef, ...] = Field(min_length=1)
    invocation_template: InvocationTemplateSelector
    applicability_policy_id: str = Field(pattern=r"^applicability-policy:[0-9a-f]{64}$")
    applicability_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    slo_policy_id: str = Field(pattern=r"^slo-policy:[0-9a-f]{64}$")
    slo_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    canonical_question_ids: tuple[str, ...] = Field(min_length=1)
    expected_output_type_ids: tuple[str, ...] = Field(min_length=1)
    approved_at: datetime

    @field_validator("catalog_alias")
    @classmethod
    def reject_mutable_alias(cls, value: str) -> str:
        return _reject_mutable_coordinate(value, "catalog_alias")

    @field_validator("approved_at")
    @classmethod
    def validate_approved_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "approved_at")

    @field_validator("canonical_question_ids")
    @classmethod
    def validate_question_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not re.fullmatch(r"^canonical-question:[0-9a-f]{64}$", value) for value in values):
            raise ValueError("canonical_question_ids must be content-addressed")
        return _validate_unique(values, "canonical_question_ids")

    @field_validator("expected_output_type_ids")
    @classmethod
    def validate_output_types(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not re.fullmatch(_OPEN_ID_PATTERN, value) for value in values):
            raise ValueError("expected output type IDs must use stable lowercase coordinates")
        return _validate_unique(values, "expected_output_type_ids")

    @model_validator(mode="after")
    def sort_validate_and_identify(self) -> ResearchCatalogEntry:
        if self.invocation_template.target_kind is not self.target.target_kind:
            raise ValueError("invocation template target kind does not match catalog target")
        target_id, target_version, target_sha256 = _target_execution_binding(self.target)
        factor_template = self.invocation_template.factor_template
        if (
            factor_template.factor_id != target_id
            or factor_template.factor_version != target_version
            or factor_template.factor_implementation_sha256 != target_sha256
        ):
            raise ValueError("catalog target does not match the executable factor template")
        if self.target.target_kind is CatalogTargetKind.STRATEGY:
            if factor_template.factor_kind is not FactorKind.STRATEGY:
                raise ValueError("strategy catalog targets require a strategy factor template")
        elif factor_template.factor_kind is FactorKind.STRATEGY:
            raise ValueError("non-strategy catalog targets cannot use a strategy factor template")
        _validate_ref_hash(
            self.applicability_policy_id,
            self.applicability_policy_sha256,
            "applicability policy",
        )
        _validate_ref_hash(self.slo_policy_id, self.slo_policy_sha256, "SLO policy")
        subjects = tuple(sorted(self.subject_scope, key=_canonical_model_key))
        subject_keys = [_canonical_model_key(subject) for subject in subjects]
        if len(subject_keys) != len(set(subject_keys)):
            raise ValueError("subject_scope must not contain duplicates")
        object.__setattr__(self, "subject_scope", subjects)
        _bind_content_address(self, id_field="catalog_entry_id", prefix="catalog-entry")
        return self


class ResearchScopeMinimums(_StrictFrozenModel):
    issuers: int = Field(ge=1)
    funds: int = Field(ge=1)
    themes: int = Field(ge=1)
    analysts: int = Field(ge=1)
    scenarios: int = Field(ge=1)
    screens: int = Field(ge=1)
    rankings: int = Field(ge=1)
    strategies: int = Field(ge=1)
    canonical_questions: int = Field(ge=1)


class ResearchScopeFloor(_StrictFrozenModel):
    research_scope_floor_id: str = Field(
        default="",
        pattern=r"^(?:|research-scope-floor:[0-9a-f]{64})$",
    )
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    universe: UniverseRef
    minimums: ResearchScopeMinimums
    required_entry_ids: tuple[str, ...] = Field(min_length=1)
    required_question_ids: tuple[str, ...] = Field(min_length=1)
    approval: ProductOwnerApproval

    @field_validator("required_entry_ids")
    @classmethod
    def validate_entry_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not re.fullmatch(r"^catalog-entry:[0-9a-f]{64}$", value) for value in values):
            raise ValueError("required_entry_ids must be content-addressed")
        return _validate_unique(values, "required_entry_ids")

    @field_validator("required_question_ids")
    @classmethod
    def validate_question_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not re.fullmatch(r"^canonical-question:[0-9a-f]{64}$", value) for value in values):
            raise ValueError("required_question_ids must be content-addressed")
        return _validate_unique(values, "required_question_ids")

    @model_validator(mode="after")
    def identify(self) -> ResearchScopeFloor:
        if self.minimums.canonical_questions > len(self.required_question_ids):
            raise ValueError("required questions do not satisfy the canonical-question scope floor")
        _bind_content_address(self, id_field="research_scope_floor_id", prefix="research-scope-floor")
        return self


class NarrowedResearchClaim(_StrictFrozenModel):
    """Product-owner approval for the exact anti-shrink violations in one transition."""

    narrowed_claim_id: str = Field(
        default="",
        pattern=r"^(?:|narrowed-research-claim:[0-9a-f]{64})$",
    )
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    predecessor_catalog_id: str = Field(pattern=r"^research-catalog:[0-9a-f]{64}$")
    previous_vision_sha256: str = Field(pattern=_SHA256_PATTERN)
    narrowed_vision_sha256: str = Field(pattern=_SHA256_PATTERN)
    removed_entry_ids: tuple[str, ...] = ()
    removed_question_ids: tuple[str, ...] = ()
    removed_floor_entry_ids: tuple[str, ...] = ()
    removed_floor_question_ids: tuple[str, ...] = ()
    weakened_entry_aliases: tuple[str, ...] = ()
    weakened_question_keys: tuple[str, ...] = ()
    decreased_floor_dimensions: tuple[str, ...] = ()
    universe_substitution: bool = False
    rationale: str = Field(min_length=1)
    approval: ProductOwnerApproval

    @field_validator(
        "removed_entry_ids",
        "removed_question_ids",
        "removed_floor_entry_ids",
        "removed_floor_question_ids",
        "weakened_entry_aliases",
        "weakened_question_keys",
        "decreased_floor_dimensions",
    )
    @classmethod
    def validate_unique_changes(cls, values: tuple[str, ...], info: Any) -> tuple[str, ...]:
        return _validate_unique(values, info.field_name)

    @model_validator(mode="after")
    def validate_and_identify(self) -> NarrowedResearchClaim:
        if self.previous_vision_sha256 == self.narrowed_vision_sha256:
            raise ValueError("a narrowed claim requires a new Vision revision")
        if not any(
            (
                self.removed_entry_ids,
                self.removed_question_ids,
                self.removed_floor_entry_ids,
                self.removed_floor_question_ids,
                self.weakened_entry_aliases,
                self.weakened_question_keys,
                self.decreased_floor_dimensions,
                self.universe_substitution,
            )
        ):
            raise ValueError("a narrowed claim must enumerate at least one scope change")
        _bind_content_address(self, id_field="narrowed_claim_id", prefix="narrowed-research-claim")
        return self


class ResearchCatalogManifest(_StrictFrozenModel):
    research_catalog_id: str = Field(
        default="",
        pattern=r"^(?:|research-catalog:[0-9a-f]{64})$",
    )
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    catalog_version: str = Field(pattern=_VERSION_PATTERN)
    vision_sha256: str = Field(pattern=_SHA256_PATTERN)
    predecessor_catalog_id: str | None = Field(
        default=None,
        pattern=r"^research-catalog:[0-9a-f]{64}$",
    )
    scope_floor: ResearchScopeFloor
    entries: tuple[ResearchCatalogEntry, ...] = Field(min_length=1)
    canonical_questions: tuple[CanonicalQuestion, ...] = Field(min_length=1)
    narrowed_claim: NarrowedResearchClaim | None = None
    catalog_approval: ProductOwnerApproval
    created_at: datetime
    effective_at: datetime

    @field_validator("catalog_version")
    @classmethod
    def reject_mutable_version(cls, value: str) -> str:
        return _reject_mutable_coordinate(value, "catalog_version")

    @field_validator("created_at", "effective_at")
    @classmethod
    def validate_datetimes(cls, value: datetime, info: Any) -> datetime:
        return _require_aware(value, info.field_name)

    @model_validator(mode="after")
    def validate_and_identify(self) -> ResearchCatalogManifest:
        if self.created_at > self.effective_at:
            raise ValueError("catalog creation must not postdate its effective time")
        if self.catalog_approval.approved_at > self.created_at:
            raise ValueError("catalog approval must not postdate catalog creation")
        if self.scope_floor.approval.approved_at > self.created_at:
            raise ValueError("scope-floor approval must not postdate catalog creation")

        entries = tuple(sorted(self.entries, key=lambda item: item.catalog_entry_id))
        entry_ids = [entry.catalog_entry_id for entry in entries]
        aliases = [entry.catalog_alias for entry in entries]
        selector_ids = [entry.invocation_template.invocation_template_id for entry in entries]
        if len(entry_ids) != len(set(entry_ids)):
            raise ValueError("catalog entries must not contain duplicates")
        if len(aliases) != len(set(aliases)):
            raise ValueError("catalog aliases must not contain duplicates")
        if len(selector_ids) != len(set(selector_ids)):
            raise ValueError("invocation template selectors must not contain duplicates")
        if any(entry.universe != self.scope_floor.universe for entry in entries):
            raise ValueError("every catalog entry must bind the exact scope-floor UniverseRef")
        if any(
            entry.approved_at > self.created_at or entry.invocation_template.frozen_at > self.created_at
            for entry in entries
        ):
            raise ValueError("postdated catalog entries or invocation templates are not permitted")

        questions = tuple(sorted(self.canonical_questions, key=lambda item: item.canonical_question_id))
        question_ids = [question.canonical_question_id for question in questions]
        question_keys = [question.question_key for question in questions]
        if len(question_ids) != len(set(question_ids)):
            raise ValueError("canonical questions must not contain duplicates")
        if len(question_keys) != len(set(question_keys)):
            raise ValueError("canonical question keys must not contain duplicates")
        if any(question.approved_at > self.created_at for question in questions):
            raise ValueError("postdated canonical questions are not permitted")

        entry_by_alias = {entry.catalog_alias: entry for entry in entries}
        question_by_id = {question.canonical_question_id: question for question in questions}
        for question in questions:
            unknown_aliases = set(question.catalog_aliases) - set(entry_by_alias)
            if unknown_aliases:
                raise ValueError(f"canonical question references unknown catalog aliases: {sorted(unknown_aliases)}")
            if any(
                entry_by_alias[alias].target.target_kind is not question.tool_kind for alias in question.catalog_aliases
            ):
                raise ValueError("canonical question tool kind does not match its catalog aliases")
        for entry in entries:
            unknown_questions = set(entry.canonical_question_ids) - set(question_by_id)
            if unknown_questions:
                raise ValueError(f"catalog entry references unknown canonical questions: {sorted(unknown_questions)}")
            for question_id in entry.canonical_question_ids:
                if entry.catalog_alias not in question_by_id[question_id].catalog_aliases:
                    raise ValueError("catalog entry and canonical question aliases do not bind each other")

        unknown_required_entries = set(self.scope_floor.required_entry_ids) - set(entry_ids)
        unknown_required_questions = set(self.scope_floor.required_question_ids) - set(question_ids)
        if unknown_required_entries:
            raise ValueError(f"scope floor references unknown catalog entries: {sorted(unknown_required_entries)}")
        if unknown_required_questions:
            raise ValueError(
                f"scope floor references unknown canonical questions: {sorted(unknown_required_questions)}"
            )
        if any(
            next(entry for entry in entries if entry.catalog_entry_id == entry_id).requirement_level
            is not CatalogRequirementLevel.REQUIRED
            for entry_id in self.scope_floor.required_entry_ids
        ):
            raise ValueError("scope-floor entries must remain required")
        if any(
            question_by_id[question_id].requirement_level is not CatalogRequirementLevel.REQUIRED
            for question_id in self.scope_floor.required_question_ids
        ):
            raise ValueError("scope-floor questions must remain required")

        if self.narrowed_claim is not None:
            if self.predecessor_catalog_id is None:
                raise ValueError("a narrowed claim requires a predecessor catalog")
            if self.narrowed_claim.predecessor_catalog_id != self.predecessor_catalog_id:
                raise ValueError("narrowed claim binds a different predecessor catalog")
            if self.narrowed_claim.narrowed_vision_sha256 != self.vision_sha256:
                raise ValueError("narrowed claim binds a different Vision revision")
            if self.narrowed_claim.approval.approved_at > self.created_at:
                raise ValueError("narrowing approval must not postdate catalog creation")

        object.__setattr__(self, "entries", entries)
        object.__setattr__(self, "canonical_questions", questions)
        _bind_content_address(self, id_field="research_catalog_id", prefix="research-catalog")
        return self


class ScopeFloorDecrease(_StrictFrozenModel):
    dimension: str
    previous_minimum: int = Field(ge=1)
    candidate_minimum: int = Field(ge=1)


class RequirementWeakening(_StrictFrozenModel):
    coordinate: str
    previous_level: CatalogRequirementLevel
    candidate_level: CatalogRequirementLevel


class ResearchCatalogDiff(_StrictFrozenModel):
    predecessor_catalog_id: str
    candidate_catalog_id: str
    added_entry_ids: tuple[str, ...]
    added_question_ids: tuple[str, ...]
    removed_entry_ids: tuple[str, ...]
    removed_question_ids: tuple[str, ...]
    removed_floor_entry_ids: tuple[str, ...]
    removed_floor_question_ids: tuple[str, ...]
    entry_weakenings: tuple[RequirementWeakening, ...]
    question_weakenings: tuple[RequirementWeakening, ...]
    scope_floor_decreases: tuple[ScopeFloorDecrease, ...]
    universe_substituted: bool
    blockers: tuple[str, ...]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def accepted(self) -> bool:
        return not self.blockers


_LEVEL_STRENGTH = {
    CatalogRequirementLevel.NOT_APPLICABLE: 0,
    CatalogRequirementLevel.OPTIONAL: 1,
    CatalogRequirementLevel.REQUIRED: 2,
}


def _requirement_weakenings(
    previous: dict[str, CatalogRequirementLevel],
    candidate: dict[str, CatalogRequirementLevel],
) -> tuple[RequirementWeakening, ...]:
    weakenings = [
        RequirementWeakening(
            coordinate=coordinate,
            previous_level=previous_level,
            candidate_level=candidate[coordinate],
        )
        for coordinate, previous_level in previous.items()
        if coordinate in candidate and _LEVEL_STRENGTH[candidate[coordinate]] < _LEVEL_STRENGTH[previous_level]
    ]
    return tuple(sorted(weakenings, key=lambda item: item.coordinate))


def diff_research_catalogs(
    predecessor: ResearchCatalogManifest,
    candidate: ResearchCatalogManifest,
) -> ResearchCatalogDiff:
    """Compare immutable content, without accepting a caller-supplied pass flag."""

    previous_entry_ids = {entry.catalog_entry_id for entry in predecessor.entries}
    candidate_entry_ids = {entry.catalog_entry_id for entry in candidate.entries}
    previous_question_ids = {question.canonical_question_id for question in predecessor.canonical_questions}
    candidate_question_ids = {question.canonical_question_id for question in candidate.canonical_questions}
    removed_entry_ids = tuple(sorted(previous_entry_ids - candidate_entry_ids))
    removed_question_ids = tuple(sorted(previous_question_ids - candidate_question_ids))
    removed_floor_entry_ids = tuple(
        sorted(set(predecessor.scope_floor.required_entry_ids) - set(candidate.scope_floor.required_entry_ids))
    )
    removed_floor_question_ids = tuple(
        sorted(set(predecessor.scope_floor.required_question_ids) - set(candidate.scope_floor.required_question_ids))
    )

    entry_weakenings = _requirement_weakenings(
        {entry.catalog_alias: entry.requirement_level for entry in predecessor.entries},
        {entry.catalog_alias: entry.requirement_level for entry in candidate.entries},
    )
    question_weakenings = _requirement_weakenings(
        {question.question_key: question.requirement_level for question in predecessor.canonical_questions},
        {question.question_key: question.requirement_level for question in candidate.canonical_questions},
    )
    previous_minimums = predecessor.scope_floor.minimums.model_dump()
    candidate_minimums = candidate.scope_floor.minimums.model_dump()
    decreases = tuple(
        ScopeFloorDecrease(
            dimension=dimension,
            previous_minimum=previous_value,
            candidate_minimum=candidate_minimums[dimension],
        )
        for dimension, previous_value in sorted(previous_minimums.items())
        if candidate_minimums[dimension] < previous_value
    )
    universe_substituted = predecessor.scope_floor.universe != candidate.scope_floor.universe

    changes_present = any(
        (
            removed_entry_ids,
            removed_question_ids,
            removed_floor_entry_ids,
            removed_floor_question_ids,
            entry_weakenings,
            question_weakenings,
            decreases,
            universe_substituted,
        )
    )
    blockers: list[str] = []
    if candidate.predecessor_catalog_id != predecessor.research_catalog_id:
        blockers.append("candidate does not bind the exact predecessor catalog")
    if candidate.created_at <= predecessor.created_at or candidate.effective_at < predecessor.effective_at:
        blockers.append("candidate catalog is not a later catalog revision")

    claim = candidate.narrowed_claim
    if not changes_present:
        if claim is not None:
            blockers.append("an additive catalog must not carry a narrowed Vision claim")
        if candidate.vision_sha256 != predecessor.vision_sha256:
            blockers.append("an additive catalog must preserve the prior Vision revision")
    elif claim is None:
        blockers.append("scope reduction requires an explicit product-owner-approved narrowed Vision claim")
    else:
        expected_decreased_dimensions = tuple(item.dimension for item in decreases)
        expected_weakened_entries = tuple(item.coordinate for item in entry_weakenings)
        expected_weakened_questions = tuple(item.coordinate for item in question_weakenings)
        if claim.predecessor_catalog_id != predecessor.research_catalog_id:
            blockers.append("narrowed claim does not bind the exact predecessor catalog")
        if claim.previous_vision_sha256 != predecessor.vision_sha256:
            blockers.append("narrowed claim does not bind the predecessor Vision revision")
        if claim.narrowed_vision_sha256 != candidate.vision_sha256:
            blockers.append("narrowed claim does not bind the candidate Vision revision")
        claimed_changes = (
            claim.removed_entry_ids,
            claim.removed_question_ids,
            claim.removed_floor_entry_ids,
            claim.removed_floor_question_ids,
            claim.weakened_entry_aliases,
            claim.weakened_question_keys,
            claim.decreased_floor_dimensions,
            claim.universe_substitution,
        )
        expected_changes = (
            removed_entry_ids,
            removed_question_ids,
            removed_floor_entry_ids,
            removed_floor_question_ids,
            expected_weakened_entries,
            expected_weakened_questions,
            expected_decreased_dimensions,
            universe_substituted,
        )
        if claimed_changes != expected_changes:
            blockers.append("narrowed claim does not exactly enumerate the detected scope changes")

    return ResearchCatalogDiff(
        predecessor_catalog_id=predecessor.research_catalog_id,
        candidate_catalog_id=candidate.research_catalog_id,
        added_entry_ids=tuple(sorted(candidate_entry_ids - previous_entry_ids)),
        added_question_ids=tuple(sorted(candidate_question_ids - previous_question_ids)),
        removed_entry_ids=removed_entry_ids,
        removed_question_ids=removed_question_ids,
        removed_floor_entry_ids=removed_floor_entry_ids,
        removed_floor_question_ids=removed_floor_question_ids,
        entry_weakenings=entry_weakenings,
        question_weakenings=question_weakenings,
        scope_floor_decreases=decreases,
        universe_substituted=universe_substituted,
        blockers=tuple(blockers),
    )


def evaluate_catalog_transition(
    predecessor: ResearchCatalogManifest,
    candidate: ResearchCatalogManifest,
) -> ResearchCatalogDiff:
    """Return the machine-readable diff or fail closed on an unapproved shrink."""

    result = diff_research_catalogs(predecessor, candidate)
    if not result.accepted:
        raise ValueError("; ".join(result.blockers))
    return result


def resolve_catalog_alias(
    catalog: ResearchCatalogManifest,
    *,
    bound_catalog_id: str,
    bound_catalog_sha256: str,
    bound_universe: UniverseRef,
    catalog_alias: str,
) -> ResearchCatalogEntry:
    """Resolve an alias only inside the exact catalog and universe bound by a release."""

    _reject_mutable_coordinate(catalog_alias, "catalog_alias")
    if bound_catalog_id != catalog.research_catalog_id or bound_catalog_sha256 != catalog.content_sha256:
        raise ValueError("runtime catalog binding does not match the immutable Research Catalog")
    if bound_universe != catalog.scope_floor.universe:
        raise ValueError("runtime universe binding does not match the immutable Research Catalog")
    matches = [entry for entry in catalog.entries if entry.catalog_alias == catalog_alias]
    if not matches:
        raise LookupError(f"catalog alias {catalog_alias!r} does not exist in the bound catalog")
    if len(matches) != 1:
        raise ValueError(f"catalog alias {catalog_alias!r} is ambiguous")
    return matches[0]
