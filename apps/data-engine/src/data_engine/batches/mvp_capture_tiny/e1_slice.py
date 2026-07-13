"""Terminal E1 evidence for the frozen D0 tiny capture corpus."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, cast

import dagster as dg
from dagster import AssetExecutionContext
from data_engine.batches.mvp_capture_tiny.e0_slice import (
    CUTOFF,
    E0SliceResult,
    FixtureRawLedger,
    _hash,
    _load_frozen_corpus,
    _repository_path,
    run_e0_slice,
)
from psycopg import Connection
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, model_validator
from truealpha_contracts import RawCapture
from truealpha_contracts.capture_contracts import (
    ApplicabilityMapping,
    CaptureCell,
    CaptureEvaluationReport,
    CaptureManifest,
    CaptureRecordEvidence,
    CaptureRequirement,
    CaptureScope,
    SourceCoverageMapping,
    canonical_applicability_projection_sha256,
    canonical_source_coverage_projection_sha256,
    evaluate_capture_manifest,
)
from truealpha_contracts.common import CaptureEnvironment, canonical_sha256
from truealpha_contracts.data_quality import DataDomain, QualityStatus
from truealpha_contracts.execution import (
    FactorExecution,
    FactorInvocationTemplate,
    FactorKind,
    NormalizedRecordRef,
    PolicyBinding,
    PolicyRole,
    RunnerInputSelection,
    SemanticDraft,
    SemanticProducerKind,
    SnapshotCellSelection,
    SnapshotDemandCell,
    SnapshotManifest,
    SnapshotRequest,
    build_runner_input_selection,
)
from truealpha_contracts.models import DataSource
from truealpha_contracts.registries import RegistrySnapshot, SemanticTypeRegistryEntry, SourceRegistryEntry
from truealpha_contracts.release import ReleaseManifest
from truealpha_contracts.universe import SubjectKind, SubjectRef
from truealpha_contracts.usage import DataRequirement, RequirementLevel

E1_ASSET_NAME = "mvp_capture_tiny_e1_evidence"
_TABLE = "mvp_capture_tiny_normalized_records"
_EVIDENCE_TABLE = "mvp_capture_tiny_evidence"


class FindingClass(StrEnum):
    NONE = "none"
    LOCAL_CAPTURE_BUG = "local-capture-bug"
    CONTRACT_TOOLKIT_GAP = "contract-toolkit-gap"
    SEMANTIC_DECISION = "semantic-decision"
    SOURCE_DATA_ISSUE = "source-data-issue"


class E1CaseResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    passed: bool
    classification: FindingClass
    observed_ids: tuple[str, ...] = ()
    blocker_codes: tuple[str, ...] = ()


class MvpCaptureTinyEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str = Field(default="", pattern=r"^(?:|mvp-capture-tiny-evidence:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    fixture_snapshot_id: str = Field(pattern=r"^snapshot:[0-9a-f]{64}$")
    postgres_snapshot_id: str = Field(pattern=r"^snapshot:[0-9a-f]{64}$")
    fixture_runner_selection_id: str = Field(pattern=r"^runner-selection:[0-9a-f]{64}$")
    postgres_runner_selection_id: str = Field(pattern=r"^runner-selection:[0-9a-f]{64}$")
    cases: tuple[E1CaseResult, ...] = Field(min_length=8, max_length=8)
    created_at: datetime
    stable_handoff: Literal[False] = False

    @model_validator(mode="after")
    def freeze_and_identify(self) -> "MvpCaptureTinyEvidence":
        cases = tuple(sorted(self.cases, key=lambda case: case.case_id))
        if len({case.case_id for case in cases}) != 8:
            raise ValueError("E1 evidence requires eight unique cases")
        object.__setattr__(self, "cases", cases)
        payload = self.model_dump(mode="json", exclude={"evidence_id", "content_sha256"})
        expected_hash = canonical_sha256(payload)
        expected_id = f"mvp-capture-tiny-evidence:{expected_hash}"
        if self.content_sha256 and self.content_sha256 != expected_hash:
            raise ValueError("content_sha256 does not match E1 evidence")
        if self.evidence_id and self.evidence_id != expected_id:
            raise ValueError("evidence_id does not match E1 evidence")
        object.__setattr__(self, "content_sha256", expected_hash)
        object.__setattr__(self, "evidence_id", expected_id)
        return self


class EphemeralPostgresRecordRepository:
    """Append-only generic record repository scoped to one Postgres session."""

    def __init__(self, connection: Connection[Any]) -> None:
        self.connection = connection
        connection.execute(
            """
            create or replace function pg_temp.reject_mvp_capture_tiny_mutation()
            returns trigger language plpgsql as $$
            begin
                raise exception 'mvp capture tiny records are append-only';
            end;
            $$
            """
        )
        connection.execute(
            f"""
            create temporary table if not exists {_TABLE} (
                normalized_record_id text primary key,
                semantic_type_id text not null,
                subject_kind text not null,
                subject_id text not null,
                knowable_at timestamptz not null,
                recorded_at timestamptz not null,
                valid_from date not null,
                valid_to date not null,
                payload jsonb not null
            ) on commit preserve rows
            """
        )
        connection.execute(f"drop trigger if exists reject_mvp_capture_tiny_mutation on {_TABLE}")
        connection.execute(
            f"""
            create trigger reject_mvp_capture_tiny_mutation
            before update or delete on {_TABLE}
            for each row execute function pg_temp.reject_mvp_capture_tiny_mutation()
            """
        )

    def put(self, record: NormalizedRecordRef) -> bool:
        payload = record.model_dump(mode="json")
        inserted = self.connection.execute(
            f"""
            insert into {_TABLE} (
                normalized_record_id, semantic_type_id, subject_kind, subject_id,
                knowable_at, recorded_at, valid_from, valid_to, payload
            ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (normalized_record_id) do nothing
            returning normalized_record_id
            """,
            (
                record.normalized_record_id,
                record.draft.semantic_type_id,
                record.draft.subject.kind.value,
                record.draft.subject.id,
                record.draft.knowable_at,
                record.recorded_at,
                record.draft.valid_from,
                record.draft.valid_to,
                Jsonb(payload),
            ),
        ).fetchone()
        if inserted is not None:
            return True
        existing = self.connection.execute(
            f"select payload from {_TABLE} where normalized_record_id = %s",
            (record.normalized_record_id,),
        ).fetchone()
        if existing is None or existing[0] != payload:
            raise ValueError("normalized record ID is already bound to different content")
        NormalizedRecordRef.model_validate(existing[0])
        return False

    def select_pit(
        self,
        *,
        subject: SubjectRef,
        semantic_type_id: str,
        as_of: datetime,
        valid_on: date,
    ) -> tuple[NormalizedRecordRef, ...]:
        rows = self.connection.execute(
            f"""
            select candidate.payload
            from {_TABLE} candidate
            where candidate.subject_kind = %s
              and candidate.subject_id = %s
              and candidate.semantic_type_id = %s
              and candidate.knowable_at <= %s
              and candidate.valid_from <= %s
              and candidate.valid_to >= %s
              and not exists (
                  select 1
                  from {_TABLE} replacement
                  where replacement.payload ->> 'supersedes_record_id' = candidate.normalized_record_id
                    and replacement.knowable_at <= %s
              )
            order by candidate.knowable_at desc, candidate.recorded_at desc,
                     candidate.normalized_record_id desc
            """,
            (subject.kind.value, subject.id, semantic_type_id, as_of, valid_on, valid_on, as_of),
        ).fetchall()
        return tuple(NormalizedRecordRef.model_validate(row[0]) for row in rows)

    def all_records(self, *, subject: SubjectRef) -> tuple[NormalizedRecordRef, ...]:
        rows = self.connection.execute(
            f"select payload from {_TABLE} where subject_kind = %s and subject_id = %s order by normalized_record_id",
            (subject.kind.value, subject.id),
        ).fetchall()
        return tuple(NormalizedRecordRef.model_validate(row[0]) for row in rows)

    def count(self) -> int:
        row = self.connection.execute(f"select count(*) from {_TABLE}").fetchone()
        if row is None:
            raise RuntimeError("ephemeral record table disappeared")
        return int(row[0])


class EphemeralPostgresEvidenceRepository:
    """Content-addressed append-only evidence retained for the Postgres session."""

    def __init__(self, connection: Connection[Any]) -> None:
        self.connection = connection
        connection.execute(
            """
            create or replace function pg_temp.reject_mvp_capture_tiny_mutation()
            returns trigger language plpgsql as $$
            begin
                raise exception 'mvp capture tiny records are append-only';
            end;
            $$
            """
        )
        connection.execute(
            f"""
            create temporary table if not exists {_EVIDENCE_TABLE} (
                evidence_id text primary key,
                content_sha256 text not null,
                payload jsonb not null
            ) on commit preserve rows
            """
        )
        connection.execute(f"drop trigger if exists reject_mvp_capture_tiny_mutation on {_EVIDENCE_TABLE}")
        connection.execute(
            f"""
            create trigger reject_mvp_capture_tiny_mutation
            before update or delete on {_EVIDENCE_TABLE}
            for each row execute function pg_temp.reject_mvp_capture_tiny_mutation()
            """
        )

    def put(self, evidence: MvpCaptureTinyEvidence) -> bool:
        payload = evidence.model_dump(mode="json")
        inserted = self.connection.execute(
            f"""
            insert into {_EVIDENCE_TABLE} (evidence_id, content_sha256, payload)
            values (%s, %s, %s)
            on conflict (evidence_id) do nothing
            returning evidence_id
            """,
            (evidence.evidence_id, evidence.content_sha256, Jsonb(payload)),
        ).fetchone()
        if inserted is not None:
            return True
        existing = self.connection.execute(
            f"select payload from {_EVIDENCE_TABLE} where evidence_id = %s",
            (evidence.evidence_id,),
        ).fetchone()
        if existing is None or existing[0] != payload:
            raise ValueError("evidence ID is already bound to different content")
        MvpCaptureTinyEvidence.model_validate(existing[0])
        return False

    def get(self, evidence_id: str) -> MvpCaptureTinyEvidence | None:
        row = self.connection.execute(
            f"select payload from {_EVIDENCE_TABLE} where evidence_id = %s",
            (evidence_id,),
        ).fetchone()
        return None if row is None else MvpCaptureTinyEvidence.model_validate(row[0])


class FixtureFilingObservation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: SubjectRef
    accession: str
    form: str
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    filing_date: date


class FrozenArtifactObservation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: SubjectRef
    artifact_id: str
    semantic_type: str
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    valid_from: date
    valid_to: date
    knowable_at: datetime


def _filing_registry() -> RegistrySnapshot:
    semantic_type = SemanticTypeRegistryEntry(
        semantic_type_id="semantic.filing-document",
        version="1.0.0",
        domain=DataDomain.FILINGS,
        schema_version="1.0.0",
        schema_fingerprint_sha256=_hash("fixture-filing-schema"),
        normalized_model_key="batch:FixtureFilingObservation",
        input_model_key="factors:FilingInput",
        repository_key="batch:FixtureFilingRepository",
        projector_key="batch:FixtureFilingProjector",
        compatibility_sha256=_hash("fixture-filing-compatibility"),
        model_implementation_sha256=_hash("fixture-filing-model"),
        repository_implementation_sha256=_hash("fixture-filing-repository"),
        projector_implementation_sha256=_hash("fixture-filing-projector"),
    )
    source = SourceRegistryEntry(
        source_id="source.fixture-sec",
        version="1.0.0",
        adapter_id="batch:FixtureSecAdapter",
        adapter_version="1.0.0",
        normalizer_id="batch:FixtureSecFilingNormalizer",
        normalizer_version="1.0.0",
        supported_domains=(DataDomain.FILINGS,),
        supported_type_ids=(semantic_type.semantic_type_id,),
        configuration_schema_sha256=_hash("fixture-filing-configuration"),
        mapping_schema_sha256=_hash("fixture-filing-mapping"),
        adapter_implementation_sha256=_hash("fixture-filing-adapter"),
        normalizer_implementation_sha256=_hash("fixture-filing-normalizer"),
    )
    return RegistrySnapshot(
        sources=(source,),
        semantic_types=(semantic_type,),
        required_type_ids=(semantic_type.semantic_type_id,),
    )


def _filing_record(
    *,
    observation: FixtureFilingObservation,
    body: bytes,
    knowable_at: datetime,
    ledger: FixtureRawLedger,
    registry: RegistrySnapshot,
    supersedes: NormalizedRecordRef | None = None,
) -> NormalizedRecordRef:
    capture = RawCapture(
        source=DataSource.SEC,
        source_record_id=f"fixture:{observation.accession}",
        body=body,
        content_type="text/html",
        fetched_at=knowable_at + timedelta(seconds=30),
        source_published_at=knowable_at,
        metadata={"form": observation.form},
    )
    raw_entry = ledger.append(capture)
    semantic_type = registry.semantic_types[0]
    source = registry.sources[0]
    draft = SemanticDraft(
        semantic_type_id=semantic_type.semantic_type_id,
        semantic_type_version=semantic_type.version,
        payload_model_key=semantic_type.normalized_model_key,
        payload_schema_sha256=semantic_type.schema_fingerprint_sha256,
        payload_sha256=canonical_sha256(observation.model_dump(mode="json")),
        subject=observation.subject,
        valid_from=date(2020, 1, 1),
        valid_to=date(2020, 12, 31),
        knowable_at=knowable_at,
        produced_at=knowable_at + timedelta(minutes=1),
        producer_kind=SemanticProducerKind.DETERMINISTIC_NORMALIZER,
        producer_id=source.normalizer_id,
        producer_version=source.normalizer_version,
        producer_implementation_sha256=source.normalizer_implementation_sha256,
    )
    return NormalizedRecordRef(
        draft=draft,
        document_id=f"document:{observation.accession}",
        raw_object_id=raw_entry.raw_id,
        raw_object_sha256=raw_entry.envelope.object.sha256,
        source_registry_entry_id=source.source_registry_entry_id,
        source_registry_entry_sha256=source.content_sha256,
        mapping_version="fixture-sec-filing:1.0.0",
        mapping_implementation_sha256=source.normalizer_implementation_sha256,
        recorded_at=knowable_at + timedelta(minutes=2),
        confidence=Decimal("0.98"),
        is_restatement=supersedes is not None,
        supersedes_record_id=None if supersedes is None else supersedes.normalized_record_id,
    )


def _artifact_registry(*, source: DataSource, semantic_type: str, domain: DataDomain) -> RegistrySnapshot:
    type_entry = SemanticTypeRegistryEntry(
        semantic_type_id=f"semantic.{semantic_type}",
        version="1.0.0",
        domain=domain,
        schema_version="1.0.0",
        schema_fingerprint_sha256=_hash(f"{semantic_type}-artifact-schema"),
        normalized_model_key="batch:FrozenArtifactObservation",
        input_model_key=f"factors:{semantic_type}-input",
        repository_key="batch:FrozenArtifactRepository",
        projector_key="batch:FrozenArtifactProjector",
        compatibility_sha256=_hash(f"{semantic_type}-artifact-compatibility"),
        model_implementation_sha256=_hash("frozen-artifact-model"),
        repository_implementation_sha256=_hash("frozen-artifact-repository"),
        projector_implementation_sha256=_hash("frozen-artifact-projector"),
    )
    source_entry = SourceRegistryEntry(
        source_id=f"source.fixture-{source.value}-{semantic_type}",
        version="1.0.0",
        adapter_id=f"batch:Fixture{source.value.title()}-{semantic_type}Adapter",
        adapter_version="1.0.0",
        normalizer_id=f"batch:FrozenArtifact-{semantic_type}Normalizer",
        normalizer_version="1.0.0",
        supported_domains=(domain,),
        supported_type_ids=(type_entry.semantic_type_id,),
        configuration_schema_sha256=_hash(f"{source.value}-artifact-configuration"),
        mapping_schema_sha256=_hash(f"{semantic_type}-artifact-mapping"),
        adapter_implementation_sha256=_hash(f"{source.value}-artifact-adapter"),
        normalizer_implementation_sha256=_hash("frozen-artifact-normalizer"),
    )
    return RegistrySnapshot(
        sources=(source_entry,),
        semantic_types=(type_entry,),
        required_type_ids=(type_entry.semantic_type_id,),
    )


def _validate_artifact_body(artifact_id: str, body: bytes) -> None:
    if artifact_id == "nvda-daily-price":
        lines = body.decode("utf-8").splitlines()
        if lines[0] != "Date,Open,High,Low,Close,Adj Close,Volume" or not lines[-1].startswith("2026-07-10,"):
            raise ValueError("NVDA daily-price fixture schema or cutoff drifted")
    elif artifact_id == "nvda-split-filing":
        if b"ten-for-one forward stock split" not in body or b"June 7, 2024" not in body:
            raise ValueError("NVDA split filing fixture semantics drifted")
    elif artifact_id == "meta-symbol-change":
        if b"ticker symbol 'META'" not in body or b"current ticker symbol 'FB'" not in body:
            raise ValueError("META listing-identity fixture semantics drifted")


def _artifact_accepted_at(artifact: dict[str, Any]) -> datetime:
    value = artifact.get("accepted_at")
    source = artifact.get("acceptance_source")
    if not isinstance(value, str) or not isinstance(source, str) or not source.startswith("https://data.sec.gov/"):
        raise ValueError("SEC artifact acceptance evidence is missing")
    accepted_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if accepted_at.tzinfo is None or accepted_at.utcoffset() is None:
        raise ValueError("SEC artifact acceptance timestamp must be timezone-aware")
    return accepted_at


def _artifact_record(
    *,
    repository_root: Path,
    artifact: dict[str, Any],
    subject: SubjectRef,
    valid_from: date,
    valid_to: date,
    knowable_at: datetime,
    domain: DataDomain,
    ledger: FixtureRawLedger,
) -> NormalizedRecordRef:
    artifact_id = cast(str, artifact["artifact_id"])
    source = DataSource(cast(str, artifact["source"]))
    semantic_type = cast(str, artifact["semantic_type"])
    if source is DataSource.SEC and knowable_at != _artifact_accepted_at(artifact):
        raise ValueError("SEC artifact knowable_at differs from frozen acceptance evidence")
    body = _repository_path(repository_root, cast(str, artifact["path"])).read_bytes()
    _validate_artifact_body(artifact_id, body)
    registry = _artifact_registry(source=source, semantic_type=semantic_type, domain=domain)
    source_entry = registry.sources[0]
    type_entry = registry.semantic_types[0]
    raw_entry = ledger.append(
        RawCapture(
            source=source,
            source_record_id=f"fixture:{artifact_id}",
            body=body,
            content_type="text/csv" if artifact_id == "nvda-daily-price" else "text/html",
            fetched_at=knowable_at + timedelta(seconds=30),
            source_published_at=knowable_at,
            metadata={"artifact_id": artifact_id},
        )
    )
    observation = FrozenArtifactObservation(
        subject=subject,
        artifact_id=artifact_id,
        semantic_type=semantic_type,
        content_sha256=cast(str, artifact["sha256"]),
        valid_from=valid_from,
        valid_to=valid_to,
        knowable_at=knowable_at,
    )
    draft = SemanticDraft(
        semantic_type_id=type_entry.semantic_type_id,
        semantic_type_version=type_entry.version,
        payload_model_key=type_entry.normalized_model_key,
        payload_schema_sha256=type_entry.schema_fingerprint_sha256,
        payload_sha256=canonical_sha256(observation.model_dump(mode="json")),
        subject=subject,
        valid_from=valid_from,
        valid_to=valid_to,
        knowable_at=knowable_at,
        produced_at=knowable_at + timedelta(minutes=1),
        producer_kind=SemanticProducerKind.DETERMINISTIC_NORMALIZER,
        producer_id=source_entry.normalizer_id,
        producer_version=source_entry.normalizer_version,
        producer_implementation_sha256=source_entry.normalizer_implementation_sha256,
    )
    return NormalizedRecordRef(
        draft=draft,
        document_id=f"document:{artifact_id}",
        raw_object_id=raw_entry.raw_id,
        raw_object_sha256=raw_entry.envelope.object.sha256,
        source_registry_entry_id=source_entry.source_registry_entry_id,
        source_registry_entry_sha256=source_entry.content_sha256,
        mapping_version="frozen-artifact:1.0.0",
        mapping_implementation_sha256=source_entry.normalizer_implementation_sha256,
        recorded_at=knowable_at + timedelta(minutes=2),
        confidence=Decimal("0.97"),
    )


def _listing_symbol(body: bytes, valid_on: date) -> str:
    _validate_artifact_body("meta-symbol-change", body)
    return "FB" if valid_on < date(2022, 6, 9) else "META"


def _restatement_pair(
    repository_root: Path,
) -> tuple[SubjectRef, NormalizedRecordRef, NormalizedRecordRef, FixtureRawLedger]:
    _, artifacts = _load_frozen_corpus(
        repository_root, Path("apps/data-engine/tests/fixtures/mvp_capture_tiny/corpus.v1.json")
    )
    subject = SubjectRef(kind=SubjectKind.ISSUER, id="issuer.plug")
    ledger = FixtureRawLedger()
    registry = _filing_registry()
    original_meta = artifacts["plug-original-filing"]
    amended_meta = artifacts["plug-amended-filing"]
    original_accepted_at = _artifact_accepted_at(original_meta)
    amended_accepted_at = _artifact_accepted_at(amended_meta)
    original = _filing_record(
        observation=FixtureFilingObservation(
            subject=subject,
            accession="0001558370-21-007147",
            form="10-K",
            content_sha256=original_meta["sha256"],
            filing_date=original_accepted_at.date(),
        ),
        body=_repository_path(repository_root, original_meta["path"]).read_bytes(),
        knowable_at=original_accepted_at,
        ledger=ledger,
        registry=registry,
    )
    amended = _filing_record(
        observation=FixtureFilingObservation(
            subject=subject,
            accession="0001558370-22-003577",
            form="10-K/A",
            content_sha256=amended_meta["sha256"],
            filing_date=amended_accepted_at.date(),
        ),
        body=_repository_path(repository_root, amended_meta["path"]).read_bytes(),
        knowable_at=amended_accepted_at,
        ledger=ledger,
        registry=registry,
        supersedes=original,
    )
    return subject, original, amended, ledger


def evaluate_evidence_variant(result: E0SliceResult, **updates: Any) -> CaptureEvaluationReport:
    source = result.capture_manifest.cells[0].evidence[0]
    values = source.model_dump(mode="python", exclude={"evidence_id", "content_sha256"})
    values.update(updates)
    evidence = CaptureRecordEvidence(**values)
    source_cell = result.capture_manifest.cells[0]
    cell = CaptureCell(
        **source_cell.model_dump(mode="python", exclude={"capture_cell_id", "content_sha256", "evidence"}),
        evidence=(evidence,),
    )
    manifest = CaptureManifest(
        **result.capture_manifest.model_dump(
            mode="python",
            exclude={"capture_manifest_id", "content_sha256", "cells"},
        ),
        cells=(cell,),
    )
    return evaluate_capture_manifest(
        result.scope,
        manifest,
        applicability_catalog_id=result.scope.applicability_catalog_id,
        applicability_catalog_sha256=result.scope.applicability_catalog_sha256,
        applicability=result.applicability,
        source_coverage=result.source_coverage,
        evaluated_at=manifest.created_at,
    )


class BoundaryProbe(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    expected_subject: SubjectRef
    observed_subject: SubjectRef
    expected_listing_id: str
    observed_listing_id: str
    expected_share_class: str
    observed_share_class: str
    price_basis: Literal["raw", "adjusted"]
    apply_explicit_actions: bool
    fx_knowable_at: datetime
    as_of: datetime
    maximum_fx_age: timedelta
    expected_source_entry_id: str
    observed_source_entry_id: str
    expected_semantic_type_id: str
    observed_semantic_type_id: str


def validate_boundary_probe(probe: BoundaryProbe) -> None:
    if probe.observed_subject != probe.expected_subject:
        raise ValueError("mandatory identity mismatch")
    if probe.observed_listing_id != probe.expected_listing_id:
        raise ValueError("wrong listing at cutoff")
    if probe.observed_share_class != probe.expected_share_class:
        raise ValueError("wrong share class at cutoff")
    if probe.price_basis == "adjusted" and probe.apply_explicit_actions:
        raise ValueError("adjusted prices and explicit actions would double count returns")
    if probe.fx_knowable_at > probe.as_of:
        raise ValueError("FX observation is future-known")
    if probe.as_of - probe.fx_knowable_at > probe.maximum_fx_age:
        raise ValueError("FX observation is stale")
    if probe.observed_source_entry_id != probe.expected_source_entry_id:
        raise ValueError("source registry binding drift")
    if probe.observed_semantic_type_id != probe.expected_semantic_type_id:
        raise ValueError("semantic type binding drift")


def _base_probe(result: E0SliceResult) -> BoundaryProbe:
    source = result.registry.sources[0]
    semantic_type = result.registry.semantic_types[0]
    return BoundaryProbe(
        expected_subject=result.payload.subject,
        observed_subject=result.payload.subject,
        expected_listing_id="listing.nasdaq.nvda",
        observed_listing_id="listing.nasdaq.nvda",
        expected_share_class="common",
        observed_share_class="common",
        price_basis="raw",
        apply_explicit_actions=True,
        fx_knowable_at=CUTOFF - timedelta(hours=1),
        as_of=CUTOFF,
        maximum_fx_age=timedelta(days=1),
        expected_source_entry_id=source.source_registry_entry_id,
        observed_source_entry_id=source.source_registry_entry_id,
        expected_semantic_type_id=semantic_type.semantic_type_id,
        observed_semantic_type_id=semantic_type.semantic_type_id,
    )


def _multi_capture_vertical(
    *,
    base: E0SliceResult,
    records: tuple[NormalizedRecordRef, ...],
) -> tuple[CaptureEvaluationReport, SnapshotManifest, RunnerInputSelection]:
    record_by_type = {record.draft.semantic_type_id: record for record in records}
    price = record_by_type["semantic.market-price"]
    split = record_by_type["semantic.corporate-action"]
    price_registry = _artifact_registry(
        source=DataSource.YAHOO,
        semantic_type="market-price",
        domain=DataDomain.MARKET_PRICES,
    )
    split_registry = _artifact_registry(
        source=DataSource.SEC,
        semantic_type="corporate-action",
        domain=DataDomain.CORPORATE_ACTIONS,
    )
    registry = RegistrySnapshot(
        sources=(*base.registry.sources, *price_registry.sources, *split_registry.sources),
        semantic_types=(
            *base.registry.semantic_types,
            *price_registry.semantic_types,
            *split_registry.semantic_types,
        ),
        required_type_ids=tuple(record_by_type),
    )
    price_requirement = CaptureRequirement(
        semantic_type_id=price.draft.semantic_type_id,
        semantic_type_version=price.draft.semantic_type_version,
        domain=DataDomain.MARKET_PRICES,
        required_fields=("artifact_id", "content_sha256", "valid_from", "valid_to"),
        subject_kinds=(SubjectKind.ISSUER,),
        cadence=timedelta(days=1),
        partition_rule_id="partition.price-history:v1",
        freshness_policy_id="freshness.daily-price:v1",
        maximum_age=timedelta(days=7),
        quality_policy_ids=("quality.non-null:v1",),
    )
    split_requirement = CaptureRequirement(
        semantic_type_id=split.draft.semantic_type_id,
        semantic_type_version=split.draft.semantic_type_version,
        domain=DataDomain.CORPORATE_ACTIONS,
        required_fields=("artifact_id", "content_sha256", "valid_from", "valid_to"),
        subject_kinds=(SubjectKind.ISSUER,),
        cadence=timedelta(days=365),
        partition_rule_id="partition.corporate-action:v1",
        freshness_policy_id="freshness.corporate-action:v1",
        maximum_age=timedelta(days=3650),
        quality_policy_ids=("quality.non-null:v1",),
    )
    requirements = (base.capture_requirement, price_requirement, split_requirement)
    data_requirements = (
        base.data_requirement,
        DataRequirement(
            capture_requirement_id=price_requirement.capture_requirement_id,
            semantic_type_id=price_requirement.semantic_type_id,
            domain=price_requirement.domain,
            metric="artifact_id",
            subject_kinds=frozenset(price_requirement.subject_kinds),
            level=RequirementLevel.REQUIRED,
            lookback=timedelta(days=1096),
            valid_period_rule_id=price_requirement.partition_rule_id,
            maximum_age=price_requirement.maximum_age,
            cadence=price_requirement.cadence,
        ),
        DataRequirement(
            capture_requirement_id=split_requirement.capture_requirement_id,
            semantic_type_id=split_requirement.semantic_type_id,
            domain=split_requirement.domain,
            metric="artifact_id",
            subject_kinds=frozenset(split_requirement.subject_kinds),
            level=RequirementLevel.REQUIRED,
            lookback=timedelta(days=3650),
            valid_period_rule_id=split_requirement.partition_rule_id,
            maximum_age=split_requirement.maximum_age,
            cadence=split_requirement.cadence,
        ),
    )
    partitions = {requirement.capture_requirement_id: "nvda-e1-multi" for requirement in requirements}
    applicability_rows: dict[Any, Any] = {}
    source_coverage_rows: dict[Any, Any] = {}
    coverage_entries: dict[str, str] = {}
    for requirement in requirements:
        partition = partitions[requirement.capture_requirement_id]
        key = (
            base.payload.subject.kind,
            base.payload.subject.id,
            requirement.domain,
            partition,
            requirement.capture_requirement_id,
        )
        applicability_rows[key] = ("required", CUTOFF - timedelta(days=1))
        coverage_entry = "source-coverage-entry:" + _hash(f"e1-{requirement.semantic_type_id}-coverage")
        coverage_entries[requirement.capture_requirement_id] = coverage_entry
        source_coverage_rows[(CaptureEnvironment.GITHUB_CI, *key)] = (coverage_entry,)
    applicability = cast(ApplicabilityMapping, applicability_rows)
    source_coverage = cast(SourceCoverageMapping, source_coverage_rows)
    scope = CaptureScope(
        research_catalog_id="research-catalog:" + _hash("e1-multi-catalog"),
        research_catalog_sha256=_hash("e1-multi-catalog"),
        universe=base.scope.universe,
        applicability_catalog_id="applicability:" + _hash("e1-multi-applicability"),
        applicability_catalog_sha256=_hash("e1-multi-applicability"),
        applicability_projection_sha256=canonical_applicability_projection_sha256(applicability),
        source_coverage_catalog_id="source-coverage:" + _hash("e1-multi-source-coverage"),
        source_coverage_catalog_sha256=_hash("e1-multi-source-coverage"),
        source_coverage_projection_sha256=canonical_source_coverage_projection_sha256(source_coverage),
        slo_catalog_id="module-slo:" + _hash("e1-multi-slo"),
        slo_catalog_sha256=_hash("e1-multi-slo"),
        source_registry_id=registry.source_registry_snapshot_id,
        source_registry_sha256=registry.source_registry_sha256,
        semantic_type_registry_id=registry.semantic_type_registry_snapshot_id,
        semantic_type_registry_sha256=registry.semantic_type_registry_sha256,
        requirements=requirements,
        effective_at=CUTOFF - timedelta(days=1),
        owner="batch-d0-mvp-capture-tiny-e1",
    )
    raw_by_sha256 = {entry.envelope.object.sha256: entry for entry in base.raw_ledger.entries}
    cells = []
    requirement_by_type = {requirement.semantic_type_id: requirement for requirement in requirements}
    for record in records:
        requirement = requirement_by_type[record.draft.semantic_type_id]
        raw = raw_by_sha256[record.raw_object_sha256]
        evidence = CaptureRecordEvidence(
            source_coverage_entry_id=coverage_entries[requirement.capture_requirement_id],
            raw_id=raw.raw_id,
            raw_sha256=raw.envelope.object.sha256,
            normalized_id=record.normalized_record_id,
            semantic_type_id=requirement.semantic_type_id,
            semantic_type_version=requirement.semantic_type_version,
            populated_fields=requirement.required_fields,
            knowable_at=record.draft.knowable_at,
            recorded_at=record.recorded_at,
            valid_from=datetime.combine(record.draft.valid_from, datetime.min.time(), UTC),
            valid_to=datetime.combine(record.draft.valid_to, datetime.max.time(), UTC),
            confidence=record.confidence,
            mapping_version=record.mapping_version,
            policy_versions={
                requirement.freshness_policy_id: "v1",
                requirement.partition_rule_id: "v1",
            },
            quality_check_ids=requirement.quality_policy_ids,
            quality_status=QualityStatus.PASS,
            lineage_sha256=record.content_sha256,
        )
        cells.append(
            CaptureCell(
                subject=base.payload.subject,
                domain=requirement.domain,
                partition_key=partitions[requirement.capture_requirement_id],
                capture_requirement_id=requirement.capture_requirement_id,
                applicability="required",
                status="complete",
                evidence=(evidence,),
            )
        )
    manifest = CaptureManifest(
        capture_scope_id=scope.capture_scope_id,
        capture_scope_sha256=scope.content_sha256,
        environment=CaptureEnvironment.GITHUB_CI,
        research_catalog_id=scope.research_catalog_id,
        research_catalog_sha256=scope.research_catalog_sha256,
        applicability_catalog_id=scope.applicability_catalog_id,
        applicability_catalog_sha256=scope.applicability_catalog_sha256,
        source_coverage_catalog_id=scope.source_coverage_catalog_id,
        source_coverage_catalog_sha256=scope.source_coverage_catalog_sha256,
        slo_catalog_id=scope.slo_catalog_id,
        slo_catalog_sha256=scope.slo_catalog_sha256,
        source_registry_id=scope.source_registry_id,
        source_registry_sha256=scope.source_registry_sha256,
        semantic_type_registry_id=scope.semantic_type_registry_id,
        semantic_type_registry_sha256=scope.semantic_type_registry_sha256,
        partition_key="nvda-e1-multi",
        as_of=CUTOFF,
        started_at=CUTOFF,
        cells=tuple(cells),
        created_at=CUTOFF + timedelta(minutes=3),
    )
    evaluation = evaluate_capture_manifest(
        scope,
        manifest,
        applicability_catalog_id=scope.applicability_catalog_id,
        applicability_catalog_sha256=scope.applicability_catalog_sha256,
        applicability=applicability,
        source_coverage=source_coverage,
        evaluated_at=manifest.created_at + timedelta(minutes=1),
    )
    demand_cells = tuple(
        SnapshotDemandCell(
            requirement_id=data_requirement.requirement_id,
            capture_requirement_id=data_requirement.capture_requirement_id,
            semantic_type_id=data_requirement.semantic_type_id,
            semantic_type_version=requirement_by_type[data_requirement.semantic_type_id].semantic_type_version,
            domain=data_requirement.domain,
            subject=base.payload.subject,
            partition_key=partitions[data_requirement.capture_requirement_id],
            level=data_requirement.level,
        )
        for data_requirement in data_requirements
    )
    request = SnapshotRequest(
        subjects=(base.payload.subject,),
        as_of=CUTOFF + timedelta(minutes=3),
        valid_on=base.payload.valid_to,
        registry_snapshot_id=registry.registry_snapshot_id,
        registry_snapshot_sha256=registry.content_sha256,
        source_registry_id=registry.source_registry_snapshot_id,
        source_registry_sha256=registry.source_registry_sha256,
        semantic_type_registry_id=registry.semantic_type_registry_snapshot_id,
        semantic_type_registry_sha256=registry.semantic_type_registry_sha256,
        policy_bindings=tuple(
            PolicyBinding(
                role=role,
                policy_id=f"policy.{role.value}",
                policy_version="1.0.0",
                implementation_sha256=_hash(f"policy-{role.value}"),
            )
            for role in PolicyRole
            if role is not PolicyRole.MEMBERSHIP
        ),
        demand_cells=demand_cells,
    )
    record_by_type = {record.draft.semantic_type_id: record for record in records}
    snapshot = SnapshotManifest(
        request=request,
        registry_snapshot=registry,
        resolved_subjects=(base.payload.subject,),
        normalized_records=records,
        selections=tuple(
            SnapshotCellSelection(
                demand=demand,
                normalized_record_ids=(record_by_type[demand.semantic_type_id].normalized_record_id,),
            )
            for demand in demand_cells
        ),
        resolved_at=request.as_of + timedelta(seconds=1),
        resolver_id="batch:FixtureE1SnapshotResolver",
        resolver_version="1.0.0",
        resolver_implementation_sha256=_hash("e1-snapshot-resolver"),
    )
    template = FactorInvocationTemplate(
        factor_id="mvp_capture_tiny_e1_probe",
        factor_version="1.0.0",
        factor_implementation_sha256=_hash("unregistered-e1-factor-probe"),
        factor_kind=FactorKind.BASE,
        parameter_model_key="batch:NoParameters",
        parameter_schema_sha256=_hash("no-parameters-schema"),
        canonical_parameters_sha256=_hash("no-parameters"),
        data_requirement_ids=tuple(item.requirement_id for item in data_requirements),
    )
    execution = FactorExecution(
        template=template,
        snapshot_id=snapshot.snapshot_id,
        snapshot_sha256=snapshot.content_sha256,
        ordered_subjects=(base.payload.subject,),
        started_at=snapshot.resolved_at + timedelta(seconds=1),
    )
    selection = build_runner_input_selection(
        execution=execution,
        snapshot=snapshot,
        selected_at=execution.started_at + timedelta(seconds=1),
        runner_id="batch:FixtureE1Runner",
        runner_version="1.0.0",
        runner_implementation_sha256=_hash("fixture-e1-runner"),
    )
    return evaluation, snapshot, selection


def run_e1_suite(repository_root: Path, connection: Connection[Any]) -> MvpCaptureTinyEvidence:
    corpus, artifacts = _load_frozen_corpus(
        repository_root, Path("apps/data-engine/tests/fixtures/mvp_capture_tiny/corpus.v1.json")
    )
    case_artifacts = {
        case["case_id"]: tuple(case["artifact_ids"]) for case in corpus["cases"] if isinstance(case, dict)
    }
    base = run_e0_slice(repository_root)
    repository = EphemeralPostgresRecordRepository(connection)
    repository.put(base.record)
    duplicate = repository.put(base.record)
    selected = repository.select_pit(
        subject=base.payload.subject,
        semantic_type_id=base.record.draft.semantic_type_id,
        as_of=base.snapshot.request.as_of,
        valid_on=base.payload.valid_to,
    )
    if duplicate or not selected:
        raise ValueError("Postgres idempotency or PIT selection failed")

    price = _artifact_record(
        repository_root=repository_root,
        artifact=artifacts["nvda-daily-price"],
        subject=base.payload.subject,
        valid_from=date(2023, 7, 10),
        valid_to=date(2026, 7, 10),
        knowable_at=datetime(2026, 7, 10, 20, 0, tzinfo=UTC),
        domain=DataDomain.MARKET_PRICES,
        ledger=base.raw_ledger,
    )
    split = _artifact_record(
        repository_root=repository_root,
        artifact=artifacts["nvda-split-filing"],
        subject=base.payload.subject,
        valid_from=date(2024, 6, 7),
        valid_to=CUTOFF.date(),
        knowable_at=_artifact_accepted_at(artifacts["nvda-split-filing"]),
        domain=DataDomain.CORPORATE_ACTIONS,
        ledger=base.raw_ledger,
    )
    nvda_records = {
        "nvda-company-facts": base.record,
        "nvda-daily-price": price,
        "nvda-split-filing": split,
    }
    reverse_results = tuple(repository.put(nvda_records[artifact_id]) for artifact_id in reversed(nvda_records))
    repeat_results = tuple(repository.put(nvda_records[artifact_id]) for artifact_id in nvda_records)
    persisted_nvda = repository.all_records(subject=base.payload.subject)
    persisted_nvda_ids = {record.normalized_record_id for record in persisted_nvda}
    raw_artifact_ids = {entry.envelope.source_record_id.removeprefix("fixture:") for entry in base.raw_ledger.entries}
    fixture_evaluation, fixture_snapshot, fixture_selection = _multi_capture_vertical(
        base=base,
        records=tuple(nvda_records.values()),
    )
    postgres_records = tuple(
        repository.select_pit(
            subject=base.payload.subject,
            semantic_type_id=record.draft.semantic_type_id,
            as_of=fixture_snapshot.request.as_of,
            valid_on=fixture_snapshot.request.valid_on,
        )[0]
        for record in nvda_records.values()
    )
    postgres_evaluation, postgres_snapshot, postgres_selection = _multi_capture_vertical(
        base=base,
        records=postgres_records,
    )

    missing_raw = evaluate_evidence_variant(base, raw_id=None, raw_sha256=None)
    missing_normalized = evaluate_evidence_variant(base, normalized_id=None)

    plug_subject, original, amended, _filing_ledger = _restatement_pair(repository_root)
    repository.put(amended)
    repository.put(original)
    repository.put(amended)
    pre_amendment = repository.select_pit(
        subject=plug_subject,
        semantic_type_id=original.draft.semantic_type_id,
        as_of=amended.draft.knowable_at - timedelta(seconds=1),
        valid_on=date(2020, 12, 31),
    )
    post_amendment = repository.select_pit(
        subject=plug_subject,
        semantic_type_id=amended.draft.semantic_type_id,
        as_of=amended.draft.knowable_at,
        valid_on=date(2020, 12, 31),
    )
    plug_records = repository.all_records(subject=plug_subject)

    wrong_listing_rejected = False
    meta_subject = SubjectRef(kind=SubjectKind.ISSUER, id="issuer.meta")
    meta_ledger = FixtureRawLedger()
    meta = _artifact_record(
        repository_root=repository_root,
        artifact=artifacts["meta-symbol-change"],
        subject=meta_subject,
        valid_from=date(2022, 6, 9),
        valid_to=CUTOFF.date(),
        knowable_at=_artifact_accepted_at(artifacts["meta-symbol-change"]),
        domain=DataDomain.ENTITY_IDENTITY,
        ledger=meta_ledger,
    )
    repository.put(meta)
    meta_body = _repository_path(repository_root, artifacts["meta-symbol-change"]["path"]).read_bytes()
    try:
        validate_boundary_probe(
            _base_probe(base).model_copy(
                update={
                    "expected_subject": meta_subject,
                    "observed_subject": meta_subject,
                    "expected_listing_id": f"listing.nasdaq.{_listing_symbol(meta_body, date(2022, 6, 8)).lower()}",
                    "observed_listing_id": "listing.nasdaq.meta",
                }
            )
        )
    except ValueError:
        wrong_listing_rejected = True

    publication_before = repository.select_pit(
        subject=base.payload.subject,
        semantic_type_id=split.draft.semantic_type_id,
        as_of=split.draft.knowable_at - timedelta(microseconds=1),
        valid_on=split.draft.valid_from,
    )
    publication_at = repository.select_pit(
        subject=base.payload.subject,
        semantic_type_id=split.draft.semantic_type_id,
        as_of=split.draft.knowable_at,
        valid_on=split.draft.valid_from,
    )
    publication_after = repository.select_pit(
        subject=base.payload.subject,
        semantic_type_id=split.draft.semantic_type_id,
        as_of=split.draft.knowable_at + timedelta(microseconds=1),
        valid_on=split.draft.valid_from,
    )

    expected_nvda_artifacts = case_artifacts["applicable-success"]
    expected_nvda_ids = {nvda_records[artifact_id].normalized_record_id for artifact_id in expected_nvda_artifacts}
    cases = (
        E1CaseResult(
            case_id="applicable-success",
            passed=fixture_evaluation.ready
            and postgres_evaluation.ready
            and fixture_snapshot.snapshot_id == postgres_snapshot.snapshot_id
            and fixture_selection.selection_id == postgres_selection.selection_id
            and len(fixture_snapshot.selections) == len(expected_nvda_artifacts)
            and len(fixture_selection.bindings) == len(expected_nvda_artifacts)
            and set(expected_nvda_artifacts) == set(nvda_records)
            and set(expected_nvda_artifacts) <= raw_artifact_ids
            and expected_nvda_ids <= persisted_nvda_ids,
            classification=FindingClass.NONE,
            observed_ids=tuple(sorted(expected_nvda_ids | {fixture_snapshot.snapshot_id})),
        ),
        E1CaseResult(
            case_id="missing-raw-evidence",
            passed=not missing_raw.ready,
            classification=FindingClass.SOURCE_DATA_ISSUE,
            blocker_codes=missing_raw.blocking_reason_codes,
        ),
        E1CaseResult(
            case_id="missing-normalized-evidence",
            passed=not missing_normalized.ready,
            classification=FindingClass.SOURCE_DATA_ISSUE,
            blocker_codes=missing_normalized.blocking_reason_codes,
        ),
        E1CaseResult(
            case_id="append-only-restatement",
            passed=len(plug_records) == 2
            and len(pre_amendment) == 1
            and pre_amendment[0].normalized_record_id == original.normalized_record_id
            and len(post_amendment) == 1
            and post_amendment[0].normalized_record_id == amended.normalized_record_id,
            classification=FindingClass.NONE,
            observed_ids=(original.normalized_record_id, amended.normalized_record_id),
        ),
        E1CaseResult(
            case_id="wrong-listing-identity",
            passed=wrong_listing_rejected
            and case_artifacts["wrong-listing-identity"] == ("meta-symbol-change",)
            and repository.all_records(subject=meta_subject) == (meta,)
            and {entry.envelope.source_record_id for entry in meta_ledger.entries} == {"fixture:meta-symbol-change"},
            classification=FindingClass.SEMANTIC_DECISION,
            observed_ids=(meta.normalized_record_id,),
            blocker_codes=("wrong-listing-at-cutoff",),
        ),
        E1CaseResult(
            case_id="publication-boundary",
            passed=not publication_before
            and len(publication_at) == 1
            and publication_at[0].normalized_record_id == split.normalized_record_id
            and len(publication_after) == 1
            and publication_after[0].normalized_record_id == split.normalized_record_id
            and case_artifacts["publication-boundary"] == ("nvda-split-filing",),
            classification=FindingClass.SEMANTIC_DECISION,
            observed_ids=(split.normalized_record_id,),
        ),
        E1CaseResult(
            case_id="look-ahead-sentinel",
            passed=len(pre_amendment) == 1
            and all(record.normalized_record_id != amended.normalized_record_id for record in pre_amendment)
            and case_artifacts["look-ahead-sentinel"] == ("plug-amended-filing",),
            classification=FindingClass.SEMANTIC_DECISION,
            blocker_codes=("future-known-vintage",),
        ),
        E1CaseResult(
            case_id="reordered-and-repeated-execution",
            passed=not any(repeat_results)
            and len(persisted_nvda_ids & expected_nvda_ids) == len(expected_nvda_ids)
            and len(persisted_nvda_ids) == len(expected_nvda_ids)
            and len(reverse_results) == len(expected_nvda_ids)
            and case_artifacts["reordered-and-repeated-execution"] == tuple(nvda_records),
            classification=FindingClass.NONE,
            observed_ids=tuple(sorted(expected_nvda_ids)),
        ),
    )
    evidence = MvpCaptureTinyEvidence(
        corpus_sha256=base.corpus_sha256,
        fixture_snapshot_id=fixture_snapshot.snapshot_id,
        postgres_snapshot_id=postgres_snapshot.snapshot_id,
        fixture_runner_selection_id=fixture_selection.selection_id,
        postgres_runner_selection_id=postgres_selection.selection_id,
        cases=cases,
        created_at=CUTOFF + timedelta(minutes=10),
    )
    EphemeralPostgresEvidenceRepository(connection).put(evidence)
    return evidence


@dataclass(frozen=True)
class E1RunnerResource:
    repository_root: Path
    connection: Connection[Any]

    def run(self) -> MvpCaptureTinyEvidence:
        return run_e1_suite(self.repository_root, self.connection)


@dg.asset(
    name=E1_ASSET_NAME,
    group_name="mvp_capture_tiny_e1",
    required_resource_keys={"mvp_capture_tiny_e1_runner"},
    description="Execute the frozen E1 corpus without registering a release asset.",
)
def materialize_mvp_capture_tiny_e1(context: AssetExecutionContext) -> dg.Output[MvpCaptureTinyEvidence]:
    runner = cast(E1RunnerResource, context.resources.mvp_capture_tiny_e1_runner)
    evidence = runner.run()
    return dg.Output(
        evidence,
        metadata={
            "evidence_id": evidence.evidence_id,
            "case_count": len(evidence.cases),
            "stable_handoff": evidence.stable_handoff,
        },
        data_version=dg.DataVersion(evidence.content_sha256),
    )


def build_e1_definitions(
    *,
    repository_root: Path,
    connection: Connection[Any],
    release_manifest: ReleaseManifest | None = None,
) -> dg.Definitions:
    if release_manifest is not None:
        raise ValueError("the provisional E1 batch is not release-activated")
    return dg.Definitions(
        assets=[materialize_mvp_capture_tiny_e1],
        resources={
            "mvp_capture_tiny_e1_runner": cast(
                Any,
                E1RunnerResource(repository_root=repository_root, connection=connection),
            )
        },
    )


__all__ = [
    "BoundaryProbe",
    "E1_ASSET_NAME",
    "E1CaseResult",
    "E1RunnerResource",
    "EphemeralPostgresRecordRepository",
    "FindingClass",
    "MvpCaptureTinyEvidence",
    "build_e1_definitions",
    "evaluate_evidence_variant",
    "materialize_mvp_capture_tiny_e1",
    "run_e1_suite",
    "validate_boundary_probe",
]
