"""Explicit Local/CI Dagster composition for the H0 E1 and E2 rungs."""

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast

import dagster as dg
from dagster import AssetExecutionContext
from factors import Fact
from psycopg import Connection
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from truealpha_contracts import RawObjectStore
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.execution import ExtractionTemplate, ModelRevisionRef
from truealpha_contracts.registries import RegistrySnapshot
from truealpha_contracts.release import ReleaseManifest

from data_engine.headcount_models import (
    D1_RUNTIME_HANDOFF_ID,
    D1_RUNTIME_HANDOFF_SHA256,
    HEADCOUNT_CORPUS_SHA256,
    HEADCOUNT_SEMANTIC_TYPE_ID,
    HEADCOUNT_SEMANTIC_TYPE_VERSION,
    HeadcountAvailability,
)
from data_engine.headcount_pipeline import (
    D1_GOVERNANCE_HANDOFF_ID,
    D1_GOVERNANCE_HANDOFF_SHA256,
    H0E1Evidence,
    build_e1_fixture_extraction_identity,
    replay_headcount_e1,
    run_headcount_e1,
)
from data_engine.headcount_registry import build_headcount_registry
from data_engine.mvp_registry import build_filing_registry

H0_E1_ASSET_NAME = "core_headcount_extraction_e1_evidence"
H0_E2_ASSET_NAME = "core_headcount_extraction_e2_handoff"
H0_E2_CONSUMERS = (
    "H0-core-headcount-extraction",
    "S0-core-strategy-tiny",
)

_H0_CASE_SUBJECTS = {
    "d1-selected-plug-total": "issuer.plug",
    "ddog-total-versus-departments": "issuer.ddog",
    "nice-worldwide-total-with-contractors": "issuer.nice",
    "missing-total-headcount": "issuer.nvda",
    "jpm-financial-issuer-branch-input": "issuer.jpm",
}
_H0_AVAILABLE_SUBJECTS = tuple(sorted(set(_H0_CASE_SUBJECTS.values()) - {"issuer.nvda"}))


class H0E1Activation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    batch_id: Literal["H0-core-headcount-extraction"] = "H0-core-headcount-extraction"
    environment: Literal["local", "ci"]
    expected_corpus_sha256: str = Field(default=HEADCOUNT_CORPUS_SHA256, pattern=r"^[0-9a-f]{64}$")
    expected_d1_handoff_id: str = Field(
        default=D1_RUNTIME_HANDOFF_ID,
        pattern=r"^mvp-normalization-handoff:[0-9a-f]{64}$",
    )
    expected_d1_handoff_sha256: str = Field(
        default=D1_RUNTIME_HANDOFF_SHA256,
        pattern=r"^[0-9a-f]{64}$",
    )
    live_source_allowed: Literal[False] = False
    live_model_allowed: Literal[False] = False
    staging_allowed: Literal[False] = False
    release_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_frozen_inputs(self) -> "H0E1Activation":
        if self.expected_corpus_sha256 != HEADCOUNT_CORPUS_SHA256:
            raise ValueError("H0 E1 activation corpus checksum drifted")
        if (
            self.expected_d1_handoff_id != D1_RUNTIME_HANDOFF_ID
            or self.expected_d1_handoff_sha256 != D1_RUNTIME_HANDOFF_SHA256
        ):
            raise ValueError("H0 E1 activation D1 handoff identity drifted")
        return self


class CoreHeadcountFactorInput(BaseModel):
    """The exact provenance-free value shape exposed to factor consumers."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entity_id: str = Field(pattern=r"^issuer\.[a-z0-9]+$")
    metric: Literal["employee_headcount"] = "employee_headcount"
    value: Decimal = Field(gt=0)
    confidence: Decimal = Field(ge=0, le=1)
    as_of: datetime
    fiscal_period: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

    @field_validator("as_of")
    @classmethod
    def require_aware_cutoff(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("factor input cutoff must be timezone-aware")
        return value

    def as_fact(self) -> Fact:
        return Fact.model_validate(self.model_dump(mode="python"))


class CoreHeadcountExtractionHandoff(BaseModel):
    """Content-addressed fixture contract for named Local/CI consumers."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    handoff_id: str = Field(
        default="",
        pattern=r"^(?:|core-headcount-extraction-handoff:[0-9a-f]{64})$",
    )
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    schema_version: Literal[1] = 1
    schema_epoch: Literal["staging.employee-headcount.v0.1.0+0020"] = "staging.employee-headcount.v0.1.0+0020"
    e1_evidence: H0E1Evidence
    migration_ids: tuple[str, ...] = ("0020_headcount_extraction.sql",)
    migration_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    migration_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    registry_snapshot: RegistrySnapshot
    semantic_type_entry_id: str = Field(pattern=r"^semantic-type-registry-entry:[0-9a-f]{64}$")
    payload_schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_revision: ModelRevisionRef
    extraction_template: ExtractionTemplate
    normalized_record_ids: tuple[str, ...] = Field(min_length=6, max_length=6)
    selected_record_ids: tuple[str, ...] = Field(min_length=5, max_length=5)
    factor_inputs: tuple[CoreHeadcountFactorInput, ...] = Field(min_length=4, max_length=4)
    unavailable_subject_ids: tuple[str, ...] = ("issuer.nvda",)
    allowed_consumers: tuple[str, ...] = H0_E2_CONSUMERS
    allowed_environments: tuple[Literal["local", "ci"], ...] = ("ci", "local")
    readiness_ceiling: Literal["local-ci-contract-only"] = "local-ci-contract-only"
    semantic_policy_state: Literal["provisional-pending-issue-59"] = "provisional-pending-issue-59"
    model_source_policy_state: Literal["fixture-only-pending-issue-60"] = "fixture-only-pending-issue-60"
    live_source_calls: Literal[False] = False
    live_model_calls: Literal[False] = False
    staging_activation: Literal[False] = False
    release_activation: Literal[False] = False
    stable_handoff: Literal[True] = True
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def require_aware_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("handoff creation time must be timezone-aware")
        return value

    @model_validator(mode="after")
    def freeze_and_identify(self) -> "CoreHeadcountExtractionHandoff":
        evidence = self.e1_evidence
        migration_ids = tuple(sorted(set(self.migration_ids)))
        records = tuple(sorted(set(self.normalized_record_ids)))
        selected_records = tuple(sorted(set(self.selected_record_ids)))
        factor_inputs = tuple(sorted(self.factor_inputs, key=lambda value: value.entity_id))
        unavailable_subjects = tuple(sorted(set(self.unavailable_subject_ids)))
        consumers = tuple(sorted(set(self.allowed_consumers)))
        environments = tuple(sorted(set(self.allowed_environments)))

        if (
            evidence.environment != "ci"
            or evidence.corpus_sha256 != HEADCOUNT_CORPUS_SHA256
            or evidence.governance_handoff_id != D1_GOVERNANCE_HANDOFF_ID
            or evidence.governance_handoff_sha256 != D1_GOVERNANCE_HANDOFF_SHA256
            or evidence.runtime_handoff_id != D1_RUNTIME_HANDOFF_ID
            or evidence.runtime_handoff_sha256 != D1_RUNTIME_HANDOFF_SHA256
            or evidence.live_source_calls
            or evidence.live_model_calls
            or evidence.release_activation
            or evidence.stable_handoff
        ):
            raise ValueError("E2 handoff requires the exact fixture-only H0 E1 evidence")
        if migration_ids != ("0020_headcount_extraction.sql",):
            raise ValueError("E2 handoff must bind only the headcount migration")
        if self.migration_set_sha256 != canonical_sha256({migration_ids[0]: self.migration_sha256}):
            raise ValueError("headcount migration set hash does not match")
        if (
            self.registry_snapshot.registry_snapshot_id != evidence.registry_snapshot_id
            or self.registry_snapshot.content_sha256 != evidence.registry_snapshot_sha256
        ):
            raise ValueError("E2 registry snapshot does not match E1 evidence")
        semantic_entry = next(
            (
                entry
                for entry in self.registry_snapshot.semantic_types
                if entry.semantic_type_registry_entry_id == self.semantic_type_entry_id
            ),
            None,
        )
        if (
            semantic_entry is None
            or semantic_entry.key != (HEADCOUNT_SEMANTIC_TYPE_ID, HEADCOUNT_SEMANTIC_TYPE_VERSION)
            or semantic_entry.schema_fingerprint_sha256 != self.payload_schema_sha256
            or self.extraction_template.semantic_type_id != semantic_entry.semantic_type_id
            or self.extraction_template.semantic_type_version != semantic_entry.version
            or self.extraction_template.output_schema_sha256 != self.payload_schema_sha256
            or self.extraction_template.model_revision_id != self.model_revision.model_revision_id
            or self.extraction_template.model_revision_sha256 != self.model_revision.content_sha256
        ):
            raise ValueError("E2 semantic, model, template, or registry identity drifted")

        case_results = {result.case_id: result for result in evidence.case_results}
        expected_selected_records = tuple(sorted(result.normalized_record_id for result in case_results.values()))
        if (
            len(records) != 6
            or len(selected_records) != 5
            or selected_records != expected_selected_records
            or not set(selected_records) < set(records)
        ):
            raise ValueError("E2 record set does not match the E1 selected vintages")
        inputs_by_subject = {value.entity_id: value for value in factor_inputs}
        if (
            len(inputs_by_subject) != 4
            or tuple(sorted(inputs_by_subject)) != _H0_AVAILABLE_SUBJECTS
            or unavailable_subjects != ("issuer.nvda",)
        ):
            raise ValueError("E2 factor projection does not cover the frozen subject matrix")
        for case_id, subject_id in _H0_CASE_SUBJECTS.items():
            result = case_results[case_id]
            factor_input = inputs_by_subject.get(subject_id)
            if result.availability is HeadcountAvailability.AVAILABLE:
                if (
                    factor_input is None
                    or result.selected_value is None
                    or factor_input.value != Decimal(result.selected_value)
                    or factor_input.as_of != evidence.created_at
                ):
                    raise ValueError("E2 factor projection does not match an available E1 result")
            elif factor_input is not None or subject_id not in unavailable_subjects:
                raise ValueError("E2 unavailable result entered the usable factor projection")
        if consumers != H0_E2_CONSUMERS or environments != ("ci", "local"):
            raise ValueError("E2 handoff consumer or environment allow-list drifted")
        if self.created_at != evidence.created_at + timedelta(minutes=1):
            raise ValueError("E2 handoff creation time does not follow its E1 evidence")

        object.__setattr__(self, "migration_ids", migration_ids)
        object.__setattr__(self, "normalized_record_ids", records)
        object.__setattr__(self, "selected_record_ids", selected_records)
        object.__setattr__(self, "factor_inputs", factor_inputs)
        object.__setattr__(self, "unavailable_subject_ids", unavailable_subjects)
        object.__setattr__(self, "allowed_consumers", consumers)
        object.__setattr__(self, "allowed_environments", environments)
        payload = self.model_dump(mode="json", exclude={"handoff_id", "content_sha256"})
        expected_hash = canonical_sha256(payload)
        expected_id = f"core-headcount-extraction-handoff:{expected_hash}"
        if self.content_sha256 and self.content_sha256 != expected_hash:
            raise ValueError("content_sha256 does not match the E2 handoff")
        if self.handoff_id and self.handoff_id != expected_id:
            raise ValueError("handoff_id does not match the E2 handoff")
        object.__setattr__(self, "content_sha256", expected_hash)
        object.__setattr__(self, "handoff_id", expected_id)
        return self


class H0HandoffActivation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    batch_id: Literal["H0-core-headcount-extraction"] = "H0-core-headcount-extraction"
    consumer: Literal["H0-core-headcount-extraction", "S0-core-strategy-tiny"]
    environment: Literal["local", "ci"]
    expected_handoff_id: str = Field(pattern=r"^core-headcount-extraction-handoff:[0-9a-f]{64}$")
    expected_handoff_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    live_source_allowed: Literal[False] = False
    live_model_allowed: Literal[False] = False
    staging_allowed: Literal[False] = False
    release_allowed: Literal[False] = False

    @model_validator(mode="after")
    def bind_handoff_identity(self) -> "H0HandoffActivation":
        expected_id = f"core-headcount-extraction-handoff:{self.expected_handoff_sha256}"
        if self.expected_handoff_id != expected_id:
            raise ValueError("activation handoff ID and hash do not match")
        return self


def run_h0_e2(
    *,
    repository_root: Path,
    connection: Connection[Any],
    raw_store: RawObjectStore,
) -> CoreHeadcountExtractionHandoff:
    """Build one Local/CI-consumable handoff from CI-pinned fixture evidence."""

    evidence = run_headcount_e1(
        repository_root=repository_root,
        connection=connection,
        raw_store=raw_store,
        environment="ci",
    )
    bundles = replay_headcount_e1(connection, evidence)
    selected_record_ids = {result.normalized_record_id for result in evidence.case_results}
    selected_bundles = tuple(bundle for bundle in bundles if bundle.record.normalized_record_id in selected_record_ids)
    if len(selected_bundles) != 5:
        raise ValueError("H0 E2 could not resolve the five selected E1 records")

    factor_inputs: list[CoreHeadcountFactorInput] = []
    unavailable_subjects: list[str] = []
    for bundle in selected_bundles:
        subject_id = bundle.record.draft.subject.id
        if bundle.payload.availability is HeadcountAvailability.AVAILABLE:
            factor_inputs.append(
                CoreHeadcountFactorInput.model_validate(
                    bundle.factor_input(as_of=evidence.created_at).model_dump(mode="python")
                )
            )
        else:
            unavailable_subjects.append(subject_id)

    registry = build_headcount_registry(build_filing_registry())
    semantic_entry = next(
        entry
        for entry in registry.semantic_types
        if entry.key == (HEADCOUNT_SEMANTIC_TYPE_ID, HEADCOUNT_SEMANTIC_TYPE_VERSION)
    )
    model_revision, template = build_e1_fixture_extraction_identity()
    migration_id = "0020_headcount_extraction.sql"
    migration_sha256 = hashlib.sha256((repository_root / "db" / "migrations" / migration_id).read_bytes()).hexdigest()
    return CoreHeadcountExtractionHandoff(
        e1_evidence=evidence,
        migration_sha256=migration_sha256,
        migration_set_sha256=canonical_sha256({migration_id: migration_sha256}),
        registry_snapshot=registry,
        semantic_type_entry_id=semantic_entry.semantic_type_registry_entry_id,
        payload_schema_sha256=semantic_entry.schema_fingerprint_sha256,
        model_revision=model_revision,
        extraction_template=template,
        normalized_record_ids=tuple(bundle.record.normalized_record_id for bundle in bundles),
        selected_record_ids=tuple(selected_record_ids),
        factor_inputs=tuple(factor_inputs),
        unavailable_subject_ids=tuple(unavailable_subjects),
        created_at=evidence.created_at + timedelta(minutes=1),
    )


@dataclass(frozen=True)
class H0E1RunnerResource:
    repository_root: Path
    connection: Connection[Any]
    raw_store: RawObjectStore
    activation: H0E1Activation

    def run(self) -> H0E1Evidence:
        evidence = run_headcount_e1(
            repository_root=self.repository_root,
            connection=self.connection,
            raw_store=self.raw_store,
            environment=self.activation.environment,
        )
        if (
            evidence.corpus_sha256 != self.activation.expected_corpus_sha256
            or evidence.runtime_handoff_id != self.activation.expected_d1_handoff_id
            or evidence.runtime_handoff_sha256 != self.activation.expected_d1_handoff_sha256
        ):
            raise ValueError("materialized H0 E1 evidence does not match its activation")
        return evidence


@dataclass(frozen=True)
class H0E2RunnerResource:
    repository_root: Path
    connection: Connection[Any]
    raw_store: RawObjectStore
    activation: H0HandoffActivation

    def run(self) -> CoreHeadcountExtractionHandoff:
        handoff = run_h0_e2(
            repository_root=self.repository_root,
            connection=self.connection,
            raw_store=self.raw_store,
        )
        if (
            handoff.handoff_id != self.activation.expected_handoff_id
            or handoff.content_sha256 != self.activation.expected_handoff_sha256
        ):
            raise ValueError("materialized H0 E2 handoff does not match its activation")
        if self.activation.consumer not in handoff.allowed_consumers:
            raise ValueError("H0 E2 handoff does not allow the activated consumer")
        if self.activation.environment not in handoff.allowed_environments:
            raise ValueError("H0 E2 handoff does not allow the activated environment")
        return handoff


@dg.asset(
    name=H0_E1_ASSET_NAME,
    group_name="core_headcount_extraction_e1",
    required_resource_keys={"h0_e1_runner"},
    description="Run the frozen H0 corpus in Local/CI without live or release activation.",
)
def materialize_core_headcount_extraction_e1(
    context: AssetExecutionContext,
) -> dg.Output[H0E1Evidence]:
    runner = cast(H0E1RunnerResource, context.resources.h0_e1_runner)
    evidence = runner.run()
    return dg.Output(
        evidence,
        metadata={
            "evidence_id": evidence.evidence_id,
            "environment": evidence.environment,
            "case_count": len(evidence.case_results),
            "persisted_result_count": evidence.persisted_result_count,
            "stable_handoff": evidence.stable_handoff,
        },
        data_version=dg.DataVersion(evidence.content_sha256),
    )


@dg.asset(
    name=H0_E2_ASSET_NAME,
    group_name="core_headcount_extraction_e2",
    required_resource_keys={"h0_e2_runner"},
    description="Publish the pinned fixture-only headcount handoff for named Local/CI consumers.",
)
def materialize_core_headcount_extraction_e2(
    context: AssetExecutionContext,
) -> dg.Output[CoreHeadcountExtractionHandoff]:
    runner = cast(H0E2RunnerResource, context.resources.h0_e2_runner)
    handoff = runner.run()
    return dg.Output(
        handoff,
        metadata={
            "handoff_id": handoff.handoff_id,
            "schema_epoch": handoff.schema_epoch,
            "consumer": runner.activation.consumer,
            "environment": runner.activation.environment,
            "factor_input_count": len(handoff.factor_inputs),
            "stable_handoff": handoff.stable_handoff,
        },
        data_version=dg.DataVersion(handoff.content_sha256),
    )


def build_h0_e1_definitions(
    *,
    repository_root: Path,
    connection: Connection[Any],
    raw_store: RawObjectStore,
    activation: H0E1Activation | ReleaseManifest,
) -> dg.Definitions:
    if not isinstance(activation, H0E1Activation):
        raise ValueError("H0 E1 cannot be activated by a release manifest")
    return dg.Definitions(
        assets=[materialize_core_headcount_extraction_e1],
        resources={
            "h0_e1_runner": cast(
                Any,
                H0E1RunnerResource(
                    repository_root=repository_root,
                    connection=connection,
                    raw_store=raw_store,
                    activation=activation,
                ),
            )
        },
    )


def build_h0_e2_definitions(
    *,
    repository_root: Path,
    connection: Connection[Any],
    raw_store: RawObjectStore,
    activation: H0HandoffActivation | ReleaseManifest,
) -> dg.Definitions:
    if not isinstance(activation, H0HandoffActivation):
        raise ValueError("H0 E2 cannot be activated by a release manifest")
    return dg.Definitions(
        assets=[materialize_core_headcount_extraction_e2],
        resources={
            "h0_e2_runner": cast(
                Any,
                H0E2RunnerResource(
                    repository_root=repository_root,
                    connection=connection,
                    raw_store=raw_store,
                    activation=activation,
                ),
            )
        },
    )


__all__ = [
    "CoreHeadcountExtractionHandoff",
    "CoreHeadcountFactorInput",
    "H0_E1_ASSET_NAME",
    "H0_E2_ASSET_NAME",
    "H0_E2_CONSUMERS",
    "H0E1Activation",
    "H0E1RunnerResource",
    "H0E2RunnerResource",
    "H0HandoffActivation",
    "build_h0_e1_definitions",
    "build_h0_e2_definitions",
    "materialize_core_headcount_extraction_e1",
    "materialize_core_headcount_extraction_e2",
    "run_h0_e2",
]
