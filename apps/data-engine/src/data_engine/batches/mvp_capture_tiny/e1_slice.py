"""Terminal E1 evidence for the frozen D0 tiny capture corpus."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
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
    _snapshot_and_selection,
    run_e0_slice,
)
from psycopg import Connection
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, model_validator
from truealpha_contracts import RawCapture
from truealpha_contracts.capture_contracts import (
    CaptureCell,
    CaptureEvaluationReport,
    CaptureManifest,
    CaptureRecordEvidence,
    evaluate_capture_manifest,
)
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.data_quality import DataDomain
from truealpha_contracts.execution import (
    NormalizedRecordRef,
    SemanticDraft,
    SemanticProducerKind,
)
from truealpha_contracts.models import DataSource
from truealpha_contracts.registries import RegistrySnapshot, SemanticTypeRegistryEntry, SourceRegistryEntry
from truealpha_contracts.universe import SubjectKind, SubjectRef

E1_ASSET_NAME = "mvp_capture_tiny_e1_evidence"
_TABLE = "mvp_capture_tiny_normalized_records"


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
        if not all(case.passed for case in cases):
            raise ValueError("failed E1 cases cannot produce accepted evidence")
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
            select payload
            from {_TABLE}
            where subject_kind = %s and subject_id = %s and semantic_type_id = %s
              and knowable_at <= %s and valid_from <= %s and valid_to >= %s
            order by knowable_at desc, recorded_at desc, normalized_record_id desc
            """,
            (subject.kind.value, subject.id, semantic_type_id, as_of, valid_on, valid_on),
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


class FixtureFilingObservation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: SubjectRef
    accession: str
    form: str
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    filing_date: date


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
        fetched_at=knowable_at + timedelta(minutes=5),
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


def _restatement_pair(repository_root: Path) -> tuple[SubjectRef, NormalizedRecordRef, NormalizedRecordRef]:
    _, artifacts = _load_frozen_corpus(
        repository_root, Path("apps/data-engine/tests/fixtures/mvp_capture_tiny/corpus.v1.json")
    )
    subject = SubjectRef(kind=SubjectKind.ISSUER, id="issuer.plug")
    ledger = FixtureRawLedger()
    registry = _filing_registry()
    original_meta = artifacts["plug-original-filing"]
    amended_meta = artifacts["plug-amended-filing"]
    original_date = date(2021, 5, 14)
    amended_date = date(2022, 3, 16)
    original = _filing_record(
        observation=FixtureFilingObservation(
            subject=subject,
            accession="0001558370-21-007147",
            form="10-K",
            content_sha256=original_meta["sha256"],
            filing_date=original_date,
        ),
        body=_repository_path(repository_root, original_meta["path"]).read_bytes(),
        knowable_at=datetime.combine(original_date, time.min, UTC),
        ledger=ledger,
        registry=registry,
    )
    amended = _filing_record(
        observation=FixtureFilingObservation(
            subject=subject,
            accession="0001558370-22-003577",
            form="10-K/A",
            content_sha256=amended_meta["sha256"],
            filing_date=amended_date,
        ),
        body=_repository_path(repository_root, amended_meta["path"]).read_bytes(),
        knowable_at=datetime.combine(amended_date, time.min, UTC),
        ledger=ledger,
        registry=registry,
        supersedes=original,
    )
    return subject, original, amended


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


def run_e1_suite(repository_root: Path, connection: Connection[Any]) -> MvpCaptureTinyEvidence:
    base = run_e0_slice(repository_root)
    repository = EphemeralPostgresRecordRepository(connection)
    inserted = repository.put(base.record)
    duplicate = repository.put(base.record)
    selected = repository.select_pit(
        subject=base.payload.subject,
        semantic_type_id=base.record.draft.semantic_type_id,
        as_of=base.snapshot.request.as_of,
        valid_on=base.payload.valid_to,
    )
    if not inserted or duplicate or not selected:
        raise ValueError("Postgres idempotency or PIT selection failed")
    postgres_snapshot, postgres_selection = _snapshot_and_selection(
        registry=base.registry,
        capture_requirement=base.capture_requirement,
        data_requirement=base.data_requirement,
        record=selected[0],
        valid_on=base.payload.valid_to,
    )

    missing_raw = evaluate_evidence_variant(base, raw_id=None, raw_sha256=None)
    missing_normalized = evaluate_evidence_variant(base, normalized_id=None)
    future_known = evaluate_evidence_variant(
        base,
        knowable_at=base.capture_manifest.as_of + timedelta(seconds=1),
        recorded_at=base.capture_manifest.as_of + timedelta(seconds=2),
    )

    plug_subject, original, amended = _restatement_pair(repository_root)
    before_count = repository.count()
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

    wrong_listing_rejected = False
    try:
        validate_boundary_probe(_base_probe(base).model_copy(update={"observed_listing_id": "listing.nyse.nvda"}))
    except ValueError:
        wrong_listing_rejected = True

    publication_before = repository.select_pit(
        subject=base.payload.subject,
        semantic_type_id=base.record.draft.semantic_type_id,
        as_of=base.record.draft.knowable_at - timedelta(microseconds=1),
        valid_on=base.payload.valid_to,
    )
    publication_at = repository.select_pit(
        subject=base.payload.subject,
        semantic_type_id=base.record.draft.semantic_type_id,
        as_of=base.record.draft.knowable_at,
        valid_on=base.payload.valid_to,
    )

    cases = (
        E1CaseResult(
            case_id="applicable-success",
            passed=base.capture_evaluation.ready
            and base.snapshot.snapshot_id == postgres_snapshot.snapshot_id
            and base.runner_selection.selection_id == postgres_selection.selection_id,
            classification=FindingClass.NONE,
            observed_ids=(base.record.normalized_record_id, base.snapshot.snapshot_id),
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
            passed=repository.count() == before_count + 2
            and bool(pre_amendment)
            and pre_amendment[0].normalized_record_id == original.normalized_record_id
            and bool(post_amendment)
            and post_amendment[0].normalized_record_id == amended.normalized_record_id,
            classification=FindingClass.NONE,
            observed_ids=(original.normalized_record_id, amended.normalized_record_id),
        ),
        E1CaseResult(
            case_id="wrong-listing-identity",
            passed=wrong_listing_rejected,
            classification=FindingClass.SEMANTIC_DECISION,
            blocker_codes=("wrong-listing-at-cutoff",),
        ),
        E1CaseResult(
            case_id="publication-boundary",
            passed=not publication_before
            and bool(publication_at)
            and publication_at[0].normalized_record_id == base.record.normalized_record_id,
            classification=FindingClass.SEMANTIC_DECISION,
            observed_ids=(base.record.normalized_record_id,),
        ),
        E1CaseResult(
            case_id="look-ahead-sentinel",
            passed=not future_known.ready
            and any(code.startswith("evidence.future_knowledge") for code in future_known.blocking_reason_codes),
            classification=FindingClass.SEMANTIC_DECISION,
            blocker_codes=future_known.blocking_reason_codes,
        ),
        E1CaseResult(
            case_id="reordered-and-repeated-execution",
            passed=repository.count() == before_count + 2
            and repository.put(base.record) is False
            and base.record in repository.all_records(subject=base.payload.subject),
            classification=FindingClass.NONE,
            observed_ids=(base.record.normalized_record_id,),
        ),
    )
    return MvpCaptureTinyEvidence(
        corpus_sha256=base.corpus_sha256,
        fixture_snapshot_id=base.snapshot.snapshot_id,
        postgres_snapshot_id=postgres_snapshot.snapshot_id,
        fixture_runner_selection_id=base.runner_selection.selection_id,
        postgres_runner_selection_id=postgres_selection.selection_id,
        cases=cases,
        created_at=CUTOFF + timedelta(minutes=10),
    )


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
) -> dg.Definitions:
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
