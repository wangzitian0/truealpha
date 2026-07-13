"""Explicit Dagster composition for the D1 E0 evidence and E2 handoff."""

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

import dagster as dg
from dagster import AssetExecutionContext
from psycopg import Connection
from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator, model_validator
from truealpha_contracts import RawObjectStore
from truealpha_contracts.capture_contracts import CaptureEvaluationReport, CaptureManifest
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.execution import RunnerInputSelection, SnapshotManifest
from truealpha_contracts.release import ReleaseManifest

from data_engine.mvp_pipeline import run_filing_pipeline
from data_engine.mvp_repository import PostgresFilingDocumentRepository
from data_engine.mvp_snapshot import build_filing_snapshot
from data_engine.mvp_sources import FROZEN_CORPUS_SHA256

D1_E0_ASSET_NAME = "mvp_filing_document_e0_evidence"
D1_E2_ASSET_NAME = "mvp_normalization_e2_handoff"
D1_E2_CONSUMERS = (
    "D2-mvp-medium-validation",
    "H0-core-headcount-extraction",
)


class D1E0Evidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str = Field(default="", pattern=r"^(?:|d1-e0-evidence:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    record_ids: tuple[str, ...] = Field(min_length=2, max_length=2)
    fixture_evaluation_id: str
    postgres_evaluation_id: str
    fixture_snapshot_id: str = Field(pattern=r"^snapshot:[0-9a-f]{64}$")
    postgres_snapshot_id: str = Field(pattern=r"^snapshot:[0-9a-f]{64}$")
    fixture_runner_selection_id: str = Field(pattern=r"^runner-selection:[0-9a-f]{64}$")
    postgres_runner_selection_id: str = Field(pattern=r"^runner-selection:[0-9a-f]{64}$")
    pre_amendment_record_id: str = Field(pattern=r"^normalized-record:[0-9a-f]{64}$")
    at_amendment_record_id: str = Field(pattern=r"^normalized-record:[0-9a-f]{64}$")
    post_amendment_record_id: str = Field(pattern=r"^normalized-record:[0-9a-f]{64}$")
    row_counts: dict[str, int]
    stable_handoff: Literal[False] = False
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def freeze_and_identify(self) -> "D1E0Evidence":
        records = tuple(sorted(set(self.record_ids)))
        if len(records) != 2:
            raise ValueError("D1 E0 requires exactly two normalized filing records")
        if self.fixture_snapshot_id != self.postgres_snapshot_id:
            raise ValueError("fixture and Postgres snapshot identities differ")
        if self.fixture_runner_selection_id != self.postgres_runner_selection_id:
            raise ValueError("fixture and Postgres runner selections differ")
        if self.pre_amendment_record_id == self.at_amendment_record_id:
            raise ValueError("pre-amendment PIT selection did not retain the original")
        if self.at_amendment_record_id != self.post_amendment_record_id:
            raise ValueError("amendment publication boundary is unstable")
        expected_counts = {"filing_documents": 2, "normalized_records": 2, "raw_fetches": 2}
        if self.row_counts != expected_counts:
            raise ValueError("D1 E0 persistent row counts are incomplete or duplicated")
        object.__setattr__(self, "record_ids", records)
        object.__setattr__(self, "row_counts", dict(sorted(self.row_counts.items())))
        payload = self.model_dump(mode="json", exclude={"evidence_id", "content_sha256"})
        expected_hash = canonical_sha256(payload)
        expected_id = f"d1-e0-evidence:{expected_hash}"
        if self.content_sha256 and self.content_sha256 != expected_hash:
            raise ValueError("content_sha256 does not match D1 E0 evidence")
        if self.evidence_id and self.evidence_id != expected_id:
            raise ValueError("evidence_id does not match D1 E0 evidence")
        object.__setattr__(self, "content_sha256", expected_hash)
        object.__setattr__(self, "evidence_id", expected_id)
        return self


class MvpNormalizationHandoff(BaseModel):
    """Content-addressed Local/CI input contract for the next capture consumers."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    handoff_id: str = Field(default="", pattern=r"^(?:|mvp-normalization-handoff:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    schema_version: Literal[1] = 1
    schema_epoch: Literal["staging.filing-document.v1+0019"] = "staging.filing-document.v1+0019"
    corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    migration_ids: tuple[str, ...] = ("0019_mvp_filing_document.sql",)
    migration_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    migration_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    registry_snapshot_id: str = Field(pattern=r"^registry-snapshot:[0-9a-f]{64}$")
    registry_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_registry_id: str = Field(pattern=r"^source-registry:[0-9a-f]{64}$")
    source_registry_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    semantic_type_registry_id: str = Field(pattern=r"^semantic-type-registry:[0-9a-f]{64}$")
    semantic_type_registry_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    semantic_type_entry_id: str = Field(pattern=r"^semantic-type-registry-entry:[0-9a-f]{64}$")
    payload_schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    capture_scope_id: str = Field(pattern=r"^capture-scope:[0-9a-f]{64}$")
    capture_scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    capture_manifest: CaptureManifest
    evaluation: CaptureEvaluationReport
    snapshot: SnapshotManifest
    runner_selection: RunnerInputSelection
    normalized_record_ids: tuple[str, ...] = Field(min_length=2, max_length=2)
    selected_record_id: str = Field(pattern=r"^normalized-record:[0-9a-f]{64}$")
    allowed_consumers: tuple[str, ...] = D1_E2_CONSUMERS
    allowed_environments: tuple[Literal["local", "ci"], ...] = ("ci", "local")
    stable_handoff: Literal[True] = True
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def validate_handoff_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value

    @field_serializer("evaluation")
    def serialize_evaluation(self, value: CaptureEvaluationReport) -> dict[str, Any]:
        return value.model_dump(mode="json", exclude_computed_fields=True)

    @model_validator(mode="after")
    def freeze_handoff(self) -> "MvpNormalizationHandoff":
        migration_ids = tuple(sorted(set(self.migration_ids)))
        records = tuple(sorted(set(self.normalized_record_ids)))
        consumers = tuple(sorted(set(self.allowed_consumers)))
        environments = tuple(sorted(set(self.allowed_environments)))
        if migration_ids != ("0019_mvp_filing_document.sql",):
            raise ValueError("E2 handoff must bind the filing normalization migration")
        migration_set = {migration_ids[0]: self.migration_sha256}
        if self.migration_set_sha256 != canonical_sha256(migration_set):
            raise ValueError("migration_set_sha256 does not match migration IDs")
        if len(records) != 2:
            raise ValueError("E2 handoff requires the exact original/amendment record pair")
        if consumers != D1_E2_CONSUMERS:
            raise ValueError("E2 handoff consumer allow-list drifted")
        if environments != ("ci", "local"):
            raise ValueError("E2 handoff is restricted to Local/CI")
        for reference_id, content_hash, prefix in (
            (self.registry_snapshot_id, self.registry_snapshot_sha256, "registry-snapshot"),
            (self.source_registry_id, self.source_registry_sha256, "source-registry"),
            (
                self.semantic_type_registry_id,
                self.semantic_type_registry_sha256,
                "semantic-type-registry",
            ),
            (self.capture_scope_id, self.capture_scope_sha256, "capture-scope"),
        ):
            if reference_id != f"{prefix}:{content_hash}":
                raise ValueError(f"{prefix} ID and hash do not match")
        if not self.evaluation.ready:
            raise ValueError("E2 handoff requires a ready capture evaluation")
        if (
            self.evaluation.capture_manifest_id != self.capture_manifest.capture_manifest_id
            or self.evaluation.capture_manifest_sha256 != self.capture_manifest.content_sha256
        ):
            raise ValueError("capture evaluation does not bind the handoff manifest")
        if (
            self.evaluation.environment != self.capture_manifest.environment
            or self.evaluation.applicability_catalog_id != self.capture_manifest.applicability_catalog_id
            or self.evaluation.applicability_catalog_sha256 != self.capture_manifest.applicability_catalog_sha256
            or self.evaluation.evaluated_at < self.capture_manifest.created_at
        ):
            raise ValueError("capture evaluation does not match the handoff manifest context")
        if (
            self.evaluation.capture_scope_id != self.capture_scope_id
            or self.evaluation.capture_scope_sha256 != self.capture_scope_sha256
            or self.capture_manifest.capture_scope_id != self.capture_scope_id
            or self.capture_manifest.capture_scope_sha256 != self.capture_scope_sha256
        ):
            raise ValueError("capture evaluation does not bind the handoff scope")
        registry = self.snapshot.registry_snapshot
        if (
            registry.registry_snapshot_id != self.registry_snapshot_id
            or registry.content_sha256 != self.registry_snapshot_sha256
            or registry.source_registry_snapshot_id != self.source_registry_id
            or registry.source_registry_sha256 != self.source_registry_sha256
            or registry.semantic_type_registry_snapshot_id != self.semantic_type_registry_id
            or registry.semantic_type_registry_sha256 != self.semantic_type_registry_sha256
            or self.capture_manifest.source_registry_id != self.source_registry_id
            or self.capture_manifest.source_registry_sha256 != self.source_registry_sha256
            or self.capture_manifest.semantic_type_registry_id != self.semantic_type_registry_id
            or self.capture_manifest.semantic_type_registry_sha256 != self.semantic_type_registry_sha256
        ):
            raise ValueError("snapshot registry does not match the handoff registry")
        if self.capture_manifest.as_of != self.snapshot.request.as_of:
            raise ValueError("capture manifest and snapshot use different PIT cutoffs")
        semantic_entries = {entry.semantic_type_registry_entry_id: entry for entry in registry.semantic_types}
        semantic_entry = semantic_entries.get(self.semantic_type_entry_id)
        if semantic_entry is None or semantic_entry.schema_fingerprint_sha256 != self.payload_schema_sha256:
            raise ValueError("handoff semantic entry or payload schema drifted")
        if self.runner_selection.snapshot_id != self.snapshot.snapshot_id:
            raise ValueError("runner selection does not bind the handoff snapshot")
        snapshot_records = {record.normalized_record_id: record for record in self.snapshot.normalized_records}
        selected = snapshot_records.get(self.selected_record_id)
        if selected is None or self.selected_record_id not in records:
            raise ValueError("selected handoff record is absent from the frozen records")
        predecessor_ids = tuple(record_id for record_id in records if record_id != self.selected_record_id)
        if (
            not selected.is_restatement
            or len(predecessor_ids) != 1
            or selected.supersedes_record_id != predecessor_ids[0]
        ):
            raise ValueError("E2 handoff records must be the exact original/amendment chain")
        if (
            selected.draft.semantic_type_id != semantic_entry.semantic_type_id
            or selected.draft.semantic_type_version != semantic_entry.version
            or selected.draft.payload_schema_sha256 != self.payload_schema_sha256
        ):
            raise ValueError("selected record does not match the frozen semantic schema")
        record_evidence = tuple(evidence for cell in self.capture_manifest.cells for evidence in cell.evidence)
        evidence_record_ids = tuple(
            sorted(evidence.normalized_id for evidence in record_evidence if evidence.normalized_id is not None)
        )
        if len(evidence_record_ids) != len(record_evidence) or evidence_record_ids != records:
            raise ValueError("capture manifest does not reconcile the original/amendment records")
        if any(
            evidence.semantic_type_id != semantic_entry.semantic_type_id
            or evidence.semantic_type_version != semantic_entry.version
            for evidence in record_evidence
        ):
            raise ValueError("capture evidence does not match the frozen semantic type")
        selected_evidence = tuple(
            evidence for evidence in record_evidence if evidence.normalized_id == self.selected_record_id
        )
        if len(selected_evidence) != 1 or selected_evidence[0].raw_sha256 != selected.raw_object_sha256:
            raise ValueError("capture manifest lacks the selected record/raw evidence")
        if len(self.runner_selection.bindings) != 1:
            raise ValueError("runner selection does not expose the selected typed input")
        binding = self.runner_selection.bindings[0]
        selected_cells = tuple(
            selection
            for selection in self.snapshot.selections
            if self.selected_record_id in selection.normalized_record_ids
        )
        expected_observation = type(binding.observation)(
            subject=selected.draft.subject,
            payload_model_key=selected.draft.payload_model_key,
            payload_sha256=selected.draft.payload_sha256,
            valid_from=selected.draft.valid_from,
            valid_to=selected.draft.valid_to,
            confidence=selected.confidence,
            as_of=self.snapshot.request.as_of,
        )
        if (
            binding.input_id != self.selected_record_id
            or len(selected_cells) != 1
            or binding.demand != selected_cells[0].demand
            or binding.observation != expected_observation
            or binding.evidence_status.value != "verified"
            or binding.upstream_batch_id is not None
        ):
            raise ValueError("runner selection does not match the selected snapshot record")
        if self.created_at < max(
            self.capture_manifest.created_at,
            self.evaluation.evaluated_at,
            self.snapshot.resolved_at,
            self.runner_selection.selected_at,
        ):
            raise ValueError("handoff cannot predate its frozen evidence")
        object.__setattr__(self, "migration_ids", migration_ids)
        object.__setattr__(self, "normalized_record_ids", records)
        object.__setattr__(self, "allowed_consumers", consumers)
        object.__setattr__(self, "allowed_environments", environments)
        payload = self.model_dump(
            mode="json",
            exclude={"handoff_id", "content_sha256"},
            exclude_computed_fields=True,
        )
        expected_hash = canonical_sha256(payload)
        expected_id = f"mvp-normalization-handoff:{expected_hash}"
        if self.content_sha256 and self.content_sha256 != expected_hash:
            raise ValueError("content_sha256 does not match E2 handoff")
        if self.handoff_id and self.handoff_id != expected_id:
            raise ValueError("handoff_id does not match E2 handoff")
        object.__setattr__(self, "content_sha256", expected_hash)
        object.__setattr__(self, "handoff_id", expected_id)
        return self


def run_d1_e0(
    repository_root: Path,
    connection: Connection[Any],
    raw_store: RawObjectStore,
) -> D1E0Evidence:
    pipeline = run_filing_pipeline(
        repository_root=repository_root,
        connection=connection,
        raw_store=raw_store,
    )
    original, amended = pipeline.records
    repository = PostgresFilingDocumentRepository(connection)
    pre = repository.select_pit(
        subject=original.draft.subject,
        semantic_type_id=original.draft.semantic_type_id,
        semantic_type_version=original.draft.semantic_type_version,
        source_registry_entry_id=original.source_registry_entry_id,
        valid_on=original.draft.valid_to,
        as_of=amended.draft.knowable_at - timedelta(microseconds=1),
    )
    at = repository.select_pit(
        subject=original.draft.subject,
        semantic_type_id=original.draft.semantic_type_id,
        semantic_type_version=original.draft.semantic_type_version,
        source_registry_entry_id=original.source_registry_entry_id,
        valid_on=original.draft.valid_to,
        as_of=amended.draft.knowable_at,
    )
    post = repository.select_pit(
        subject=original.draft.subject,
        semantic_type_id=original.draft.semantic_type_id,
        semantic_type_version=original.draft.semantic_type_version,
        source_registry_entry_id=original.source_registry_entry_id,
        valid_on=original.draft.valid_to,
        as_of=amended.draft.knowable_at + timedelta(microseconds=1),
    )
    if len(pre) != 1 or len(at) != 1 or len(post) != 1:
        raise ValueError("filing PIT selection is not singular at the publication boundary")
    fixture_bundle = build_filing_snapshot(
        records=pipeline.records,
        selected_record=amended,
        registry=pipeline.registry,
        as_of=amended.draft.knowable_at,
    )
    postgres_bundle = build_filing_snapshot(
        records=at,
        selected_record=at[0],
        registry=pipeline.registry,
        as_of=amended.draft.knowable_at,
    )
    raw_count = connection.execute(
        """
        select count(*)
        from raw.fetches
        where (source_record_id, payload_sha256) in ((%s, %s), (%s, %s))
        """,
        (
            pipeline.artifacts[0].source_record_id,
            pipeline.artifacts[0].sha256,
            pipeline.artifacts[1].source_record_id,
            pipeline.artifacts[1].sha256,
        ),
    ).fetchone()
    normalized_count = connection.execute(
        "select count(*) from staging.normalized_records where normalized_record_id = any(%s)",
        ([record.normalized_record_id for record in pipeline.records],),
    ).fetchone()
    filing_count = connection.execute(
        "select count(*) from staging.filing_documents where normalized_record_id = any(%s)",
        ([record.normalized_record_id for record in pipeline.records],),
    ).fetchone()
    if raw_count is None or normalized_count is None or filing_count is None:
        raise ValueError("persistent filing row counts could not be read")
    return D1E0Evidence(
        corpus_sha256=FROZEN_CORPUS_SHA256,
        record_ids=tuple(record.normalized_record_id for record in pipeline.records),
        fixture_evaluation_id=fixture_bundle.evaluation.capture_evaluation_report_id,
        postgres_evaluation_id=postgres_bundle.evaluation.capture_evaluation_report_id,
        fixture_snapshot_id=fixture_bundle.snapshot.snapshot_id,
        postgres_snapshot_id=postgres_bundle.snapshot.snapshot_id,
        fixture_runner_selection_id=fixture_bundle.runner_selection.selection_id,
        postgres_runner_selection_id=postgres_bundle.runner_selection.selection_id,
        pre_amendment_record_id=pre[0].normalized_record_id,
        at_amendment_record_id=at[0].normalized_record_id,
        post_amendment_record_id=post[0].normalized_record_id,
        row_counts={
            "raw_fetches": raw_count[0],
            "normalized_records": normalized_count[0],
            "filing_documents": filing_count[0],
        },
        created_at=amended.recorded_at + timedelta(minutes=10),
    )


def run_d1_e2(
    repository_root: Path,
    connection: Connection[Any],
    raw_store: RawObjectStore,
) -> MvpNormalizationHandoff:
    pipeline = run_filing_pipeline(
        repository_root=repository_root,
        connection=connection,
        raw_store=raw_store,
    )
    original, amended = pipeline.records
    semantic_entry = next(
        (
            entry
            for entry in pipeline.registry.semantic_types
            if (entry.semantic_type_id, entry.version)
            == (amended.draft.semantic_type_id, amended.draft.semantic_type_version)
        ),
        None,
    )
    if semantic_entry is None:
        raise ValueError("E2 handoff semantic type/version is absent from the registry snapshot")
    selected = PostgresFilingDocumentRepository(connection).select_pit(
        subject=original.draft.subject,
        semantic_type_id=original.draft.semantic_type_id,
        semantic_type_version=original.draft.semantic_type_version,
        source_registry_entry_id=original.source_registry_entry_id,
        valid_on=original.draft.valid_to,
        as_of=amended.draft.knowable_at,
    )
    if selected != (amended,):
        raise ValueError("E2 handoff requires the exact post-amendment PIT selection")
    bundle = build_filing_snapshot(
        records=pipeline.records,
        selected_record=selected[0],
        registry=pipeline.registry,
        as_of=amended.draft.knowable_at,
    )
    migration_id = "0019_mvp_filing_document.sql"
    migration_sha256 = hashlib.sha256((repository_root / "db" / "migrations" / migration_id).read_bytes()).hexdigest()
    return MvpNormalizationHandoff(
        corpus_sha256=FROZEN_CORPUS_SHA256,
        migration_sha256=migration_sha256,
        migration_set_sha256=canonical_sha256({migration_id: migration_sha256}),
        registry_snapshot_id=pipeline.registry.registry_snapshot_id,
        registry_snapshot_sha256=pipeline.registry.content_sha256,
        source_registry_id=pipeline.registry.source_registry_snapshot_id,
        source_registry_sha256=pipeline.registry.source_registry_sha256,
        semantic_type_registry_id=pipeline.registry.semantic_type_registry_snapshot_id,
        semantic_type_registry_sha256=pipeline.registry.semantic_type_registry_sha256,
        semantic_type_entry_id=semantic_entry.semantic_type_registry_entry_id,
        payload_schema_sha256=semantic_entry.schema_fingerprint_sha256,
        capture_scope_id=bundle.scope.capture_scope_id,
        capture_scope_sha256=bundle.scope.content_sha256,
        capture_manifest=bundle.manifest,
        evaluation=bundle.evaluation,
        snapshot=bundle.snapshot,
        runner_selection=bundle.runner_selection,
        normalized_record_ids=tuple(record.normalized_record_id for record in pipeline.records),
        selected_record_id=selected[0].normalized_record_id,
        created_at=amended.recorded_at + timedelta(minutes=20),
    )


@dataclass(frozen=True)
class D1E0RunnerResource:
    repository_root: Path
    connection: Connection[Any]
    raw_store: RawObjectStore

    def run(self) -> D1E0Evidence:
        return run_d1_e0(self.repository_root, self.connection, self.raw_store)


@dataclass(frozen=True)
class D1E2RunnerResource:
    repository_root: Path
    connection: Connection[Any]
    raw_store: RawObjectStore
    activation: "D1HandoffActivation"

    def run(self) -> MvpNormalizationHandoff:
        handoff = run_d1_e2(self.repository_root, self.connection, self.raw_store)
        if (
            handoff.handoff_id != self.activation.expected_handoff_id
            or handoff.content_sha256 != self.activation.expected_handoff_sha256
        ):
            raise ValueError("materialized E2 handoff does not match the activated identity")
        if self.activation.consumer not in handoff.allowed_consumers:
            raise ValueError("E2 handoff does not allow the activated consumer")
        if self.activation.environment not in handoff.allowed_environments:
            raise ValueError("E2 handoff does not allow the activated environment")
        return handoff


class D1ProvisionalActivation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    batch_id: Literal["D1-mvp-normalization-handoff"] = "D1-mvp-normalization-handoff"
    release_allowed: Literal[False] = False


class D1HandoffActivation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    batch_id: Literal["D1-mvp-normalization-handoff"] = "D1-mvp-normalization-handoff"
    consumer: Literal[
        "D2-mvp-medium-validation",
        "H0-core-headcount-extraction",
    ]
    environment: Literal["local", "ci"]
    expected_handoff_id: str = Field(pattern=r"^mvp-normalization-handoff:[0-9a-f]{64}$")
    expected_handoff_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    release_allowed: Literal[False] = False

    @model_validator(mode="after")
    def bind_handoff_identity(self) -> "D1HandoffActivation":
        if self.expected_handoff_id != f"mvp-normalization-handoff:{self.expected_handoff_sha256}":
            raise ValueError("activation handoff ID and hash do not match")
        return self


@dg.asset(
    name=D1_E0_ASSET_NAME,
    group_name="mvp_filing_document_e0",
    required_resource_keys={"d1_e0_runner"},
    description="Execute the frozen D1 E0 filing slice without release activation.",
)
def materialize_mvp_filing_document_e0(context: AssetExecutionContext) -> dg.Output[D1E0Evidence]:
    runner = cast(D1E0RunnerResource, context.resources.d1_e0_runner)
    evidence = runner.run()
    return dg.Output(
        evidence,
        metadata={
            "evidence_id": evidence.evidence_id,
            "record_count": len(evidence.record_ids),
            "stable_handoff": evidence.stable_handoff,
        },
        data_version=dg.DataVersion(evidence.content_sha256),
    )


@dg.asset(
    name=D1_E2_ASSET_NAME,
    group_name="mvp_normalization_e2",
    required_resource_keys={"d1_e2_runner"},
    description="Publish the pinned Local/CI normalization handoff for named consumers.",
)
def materialize_mvp_normalization_e2(context: AssetExecutionContext) -> dg.Output[MvpNormalizationHandoff]:
    runner = cast(D1E2RunnerResource, context.resources.d1_e2_runner)
    handoff = runner.run()
    return dg.Output(
        handoff,
        metadata={
            "handoff_id": handoff.handoff_id,
            "schema_epoch": handoff.schema_epoch,
            "consumer": runner.activation.consumer,
            "environment": runner.activation.environment,
            "stable_handoff": handoff.stable_handoff,
        },
        data_version=dg.DataVersion(handoff.content_sha256),
    )


def build_d1_e0_definitions(
    *,
    repository_root: Path,
    connection: Connection[Any],
    raw_store: RawObjectStore,
    activation: D1ProvisionalActivation | ReleaseManifest,
) -> dg.Definitions:
    if not isinstance(activation, D1ProvisionalActivation):
        raise ValueError("the provisional D1 E0 batch is not release-activated")
    return dg.Definitions(
        assets=[materialize_mvp_filing_document_e0],
        resources={
            "d1_e0_runner": cast(
                Any,
                D1E0RunnerResource(
                    repository_root=repository_root,
                    connection=connection,
                    raw_store=raw_store,
                ),
            )
        },
    )


def build_d1_e2_definitions(
    *,
    repository_root: Path,
    connection: Connection[Any],
    raw_store: RawObjectStore,
    activation: D1HandoffActivation | ReleaseManifest,
) -> dg.Definitions:
    if not isinstance(activation, D1HandoffActivation):
        raise ValueError("the D1 E2 handoff cannot be release-activated")
    return dg.Definitions(
        assets=[materialize_mvp_normalization_e2],
        resources={
            "d1_e2_runner": cast(
                Any,
                D1E2RunnerResource(
                    repository_root=repository_root,
                    connection=connection,
                    raw_store=raw_store,
                    activation=activation,
                ),
            )
        },
    )


__all__ = [
    "D1_E0_ASSET_NAME",
    "D1_E2_ASSET_NAME",
    "D1_E2_CONSUMERS",
    "D1E0Evidence",
    "D1E0RunnerResource",
    "D1E2RunnerResource",
    "D1HandoffActivation",
    "D1ProvisionalActivation",
    "MvpNormalizationHandoff",
    "build_d1_e0_definitions",
    "build_d1_e2_definitions",
    "materialize_mvp_filing_document_e0",
    "materialize_mvp_normalization_e2",
    "run_d1_e0",
    "run_d1_e2",
]
