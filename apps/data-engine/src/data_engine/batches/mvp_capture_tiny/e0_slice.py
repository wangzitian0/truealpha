"""Unregistered E0 capture slice over the frozen D0 fixture corpus."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict
from truealpha_contracts import RawCapture, RawIngestionEnvelope, RawObjectRef
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
from truealpha_contracts.universe import SubjectKind, SubjectRef, UniverseRef
from truealpha_contracts.usage import DataRequirement, RequirementLevel

CORPUS_PATH = Path("apps/data-engine/tests/fixtures/mvp_capture_tiny/corpus.v1.json")
CUTOFF = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
PARTITION = "nvda-fy2026"
SUBJECT = SubjectRef(kind=SubjectKind.ISSUER, id="issuer.nvda")


def _hash(label: str) -> str:
    return canonical_sha256({"mvp_capture_tiny_e0": label})


def _repository_path(root: Path, relative_path: str) -> Path:
    pure = PurePosixPath(relative_path)
    if pure.is_absolute() or "\\" in relative_path or any(part in {"", ".", ".."} for part in pure.parts):
        raise ValueError(f"fixture path escapes repository: {relative_path}")
    candidate = root.joinpath(*pure.parts)
    try:
        candidate.resolve().relative_to(root.resolve())
    except ValueError as error:
        raise ValueError(f"fixture path escapes repository: {relative_path}") from error
    return candidate


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_frozen_corpus(root: Path, corpus_path: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    path = _repository_path(root, corpus_path.as_posix())
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("unsupported tiny corpus manifest")
    source_manifest = payload.get("source_manifest")
    if not isinstance(source_manifest, dict):
        raise ValueError("tiny corpus source manifest reference is missing")
    source_path = _repository_path(root, str(source_manifest.get("path", "")))
    if not source_path.is_file() or _sha256(source_path) != source_manifest.get("sha256"):
        raise ValueError("tiny corpus source manifest bytes drifted")

    artifacts: dict[str, dict[str, Any]] = {}
    declared = payload.get("artifacts")
    if not isinstance(declared, list) or not declared:
        raise ValueError("tiny corpus artifacts are missing")
    for artifact in declared:
        if not isinstance(artifact, dict) or not isinstance(artifact.get("artifact_id"), str):
            raise ValueError("tiny corpus artifact is malformed")
        artifact_id = artifact["artifact_id"]
        if artifact_id in artifacts:
            raise ValueError(f"duplicate tiny corpus artifact: {artifact_id}")
        artifact_path = _repository_path(root, str(artifact.get("path", "")))
        if not artifact_path.is_file() or _sha256(artifact_path) != artifact.get("sha256"):
            raise ValueError(f"tiny corpus artifact bytes drifted: {artifact_id}")
        artifacts[artifact_id] = artifact
    cases = payload.get("cases")
    if not isinstance(cases, list) or len(cases) != 8:
        raise ValueError("tiny corpus must retain its eight predeclared cases")
    case_ids = [case.get("case_id") for case in cases if isinstance(case, dict)]
    if (
        len(case_ids) != 8
        or not all(isinstance(case_id, str) and bool(case_id) for case_id in case_ids)
        or len(set(case_ids)) != 8
    ):
        raise ValueError("tiny corpus case IDs are incomplete or duplicated")
    return payload, artifacts


class FixtureFinancialObservation(BaseModel):
    """Batch-private normalized payload parsed from one SEC company-facts fixture."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: SubjectRef
    metric: str
    value: Decimal
    unit: str
    fiscal_period: str
    accession: str
    form: str
    valid_from: date
    valid_to: date
    knowable_at: datetime


@dataclass(frozen=True)
class RawLedgerEntry:
    raw_id: str
    envelope: RawIngestionEnvelope


class FixtureRawLedger:
    """Content-addressed append-only raw ledger used only by this batch slice."""

    def __init__(self) -> None:
        self._entries: dict[tuple[DataSource, str, str], RawLedgerEntry] = {}

    @property
    def entries(self) -> tuple[RawLedgerEntry, ...]:
        return tuple(self._entries[key] for key in sorted(self._entries, key=lambda item: tuple(map(str, item))))

    def append(self, capture: RawCapture) -> RawLedgerEntry:
        body_sha256 = hashlib.sha256(capture.body).hexdigest()
        key = capture.source, capture.source_record_id, body_sha256
        existing = self._entries.get(key)
        if existing is not None:
            return existing
        raw_id = "raw.fixture:" + canonical_sha256(
            {
                "source": capture.source.value,
                "source_record_id": capture.source_record_id,
                "sha256": body_sha256,
            }
        )
        envelope = RawIngestionEnvelope(
            source=capture.source,
            source_record_id=capture.source_record_id,
            object=RawObjectRef(
                bucket="mvp-capture-tiny",
                key=f"{capture.source.value}/{capture.source_record_id}/{body_sha256}",
                sha256=body_sha256,
                byte_length=len(capture.body),
                content_type=capture.content_type,
            ),
            fetched_at=capture.fetched_at,
            source_published_at=capture.source_published_at,
            metadata=capture.metadata,
        )
        entry = RawLedgerEntry(raw_id=raw_id, envelope=envelope)
        self._entries[key] = entry
        return entry


@dataclass(frozen=True)
class E0SliceResult:
    corpus_sha256: str
    raw_capture: RawCapture
    raw_entry: RawLedgerEntry
    raw_ledger: FixtureRawLedger
    payload: FixtureFinancialObservation
    registry: RegistrySnapshot
    capture_requirement: CaptureRequirement
    data_requirement: DataRequirement
    scope: CaptureScope
    applicability: ApplicabilityMapping
    source_coverage: SourceCoverageMapping
    record: NormalizedRecordRef
    capture_manifest: CaptureManifest
    capture_evaluation: CaptureEvaluationReport
    snapshot: SnapshotManifest
    runner_selection: RunnerInputSelection


def _latest_annual_gross_profit(body: bytes, cutoff: datetime) -> FixtureFinancialObservation:
    try:
        payload = json.loads(body)
        rows = payload["facts"]["us-gaap"]["GrossProfit"]["units"]["USD"]
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise TypeError("GrossProfit USD units must be a list of objects")
        candidates = [
            row
            for row in rows
            if row.get("form") == "10-K" and row.get("fp") == "FY" and date.fromisoformat(row["filed"]) <= cutoff.date()
        ]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise ValueError("fixture company-facts schema drifted before normalization") from error
    if not candidates:
        raise ValueError("fixture has no annual gross-profit fact visible at the cutoff")
    selected = max(
        candidates,
        key=lambda row: (
            date.fromisoformat(row["filed"]),
            date.fromisoformat(row["end"]),
            str(row["accn"]),
            Decimal(str(row["val"])),
        ),
    )
    filed = date.fromisoformat(selected["filed"])
    return FixtureFinancialObservation(
        subject=SUBJECT,
        metric="gross_profit",
        value=Decimal(str(selected["val"])),
        unit="USD",
        fiscal_period=f"FY{selected['fy']}",
        accession=str(selected["accn"]),
        form=str(selected["form"]),
        valid_from=date.fromisoformat(selected["start"]),
        valid_to=date.fromisoformat(selected["end"]),
        knowable_at=datetime.combine(filed, time.min, UTC),
    )


def _registry() -> RegistrySnapshot:
    semantic_type = SemanticTypeRegistryEntry(
        semantic_type_id="semantic.financial-fact",
        version="1.0.0",
        domain=DataDomain.FINANCIAL_FACTS,
        schema_version="1.0.0",
        schema_fingerprint_sha256=_hash("fixture-financial-observation-schema"),
        normalized_model_key="batch:FixtureFinancialObservation",
        input_model_key="factors:FinancialFactInput",
        repository_key="batch:FixtureFinancialRepository",
        projector_key="batch:FixtureFinancialProjector",
        compatibility_sha256=_hash("fixture-financial-observation-compatibility"),
        model_implementation_sha256=_hash("fixture-financial-observation-model"),
        repository_implementation_sha256=_hash("fixture-financial-observation-repository"),
        projector_implementation_sha256=_hash("fixture-financial-observation-projector"),
    )
    source = SourceRegistryEntry(
        source_id="source.fixture-sec",
        version="1.0.0",
        adapter_id="batch:FixtureSecAdapter",
        adapter_version="1.0.0",
        normalizer_id="batch:FixtureSecNormalizer",
        normalizer_version="1.0.0",
        supported_domains=(DataDomain.FINANCIAL_FACTS,),
        supported_type_ids=(semantic_type.semantic_type_id,),
        configuration_schema_sha256=_hash("fixture-sec-configuration"),
        mapping_schema_sha256=_hash("fixture-sec-mapping"),
        adapter_implementation_sha256=_hash("fixture-sec-adapter"),
        normalizer_implementation_sha256=_hash("fixture-sec-normalizer"),
    )
    return RegistrySnapshot(
        sources=(source,),
        semantic_types=(semantic_type,),
        required_type_ids=(semantic_type.semantic_type_id,),
    )


def _capture_requirement() -> CaptureRequirement:
    return CaptureRequirement(
        semantic_type_id="semantic.financial-fact",
        semantic_type_version="1.0.0",
        domain=DataDomain.FINANCIAL_FACTS,
        required_fields=("accession", "confidence", "fiscal_period", "form", "metric", "unit", "value"),
        subject_kinds=(SubjectKind.ISSUER,),
        cadence=timedelta(days=365),
        partition_rule_id="partition.fiscal-year:v1",
        freshness_policy_id="freshness.annual:v1",
        maximum_age=timedelta(days=365),
        quality_policy_ids=("quality.non-null:v1", "quality.positive-value:v1"),
    )


def _data_requirement(requirement: CaptureRequirement) -> DataRequirement:
    return DataRequirement(
        capture_requirement_id=requirement.capture_requirement_id,
        semantic_type_id=requirement.semantic_type_id,
        domain=requirement.domain,
        metric="gross_profit",
        subject_kinds=frozenset(requirement.subject_kinds),
        level=RequirementLevel.REQUIRED,
        lookback=timedelta(days=365),
        valid_period_rule_id=requirement.partition_rule_id,
        maximum_age=requirement.maximum_age,
        cadence=requirement.cadence,
    )


def _projections(requirement: CaptureRequirement) -> tuple[ApplicabilityMapping, SourceCoverageMapping, str]:
    key = (SUBJECT.kind, SUBJECT.id, requirement.domain, PARTITION, requirement.capture_requirement_id)
    applicability: ApplicabilityMapping = {key: ("required", CUTOFF - timedelta(days=1))}
    coverage_entry = "source-coverage-entry:" + _hash("fixture-sec-coverage")
    source_coverage: SourceCoverageMapping = {(CaptureEnvironment.GITHUB_CI, *key): (coverage_entry,)}
    return applicability, source_coverage, coverage_entry


def _scope(
    registry: RegistrySnapshot,
    requirement: CaptureRequirement,
    applicability: ApplicabilityMapping,
    source_coverage: SourceCoverageMapping,
) -> CaptureScope:
    return CaptureScope(
        research_catalog_id="research-catalog:" + _hash("catalog"),
        research_catalog_sha256=_hash("catalog"),
        universe=UniverseRef(
            universe_id="universe.topt-tiny",
            universe_version="corpus-v1",
            content_sha256=_hash("universe"),
        ),
        applicability_catalog_id="applicability:" + _hash("applicability"),
        applicability_catalog_sha256=_hash("applicability"),
        applicability_projection_sha256=canonical_applicability_projection_sha256(applicability),
        source_coverage_catalog_id="source-coverage:" + _hash("source-coverage"),
        source_coverage_catalog_sha256=_hash("source-coverage"),
        source_coverage_projection_sha256=canonical_source_coverage_projection_sha256(source_coverage),
        slo_catalog_id="module-slo:" + _hash("slo"),
        slo_catalog_sha256=_hash("slo"),
        source_registry_id=registry.source_registry_snapshot_id,
        source_registry_sha256=registry.source_registry_sha256,
        semantic_type_registry_id=registry.semantic_type_registry_snapshot_id,
        semantic_type_registry_sha256=registry.semantic_type_registry_sha256,
        requirements=(requirement,),
        effective_at=CUTOFF - timedelta(days=1),
        owner="batch-d0-mvp-capture-tiny",
    )


def _normalize(
    payload: FixtureFinancialObservation,
    raw_entry: RawLedgerEntry,
    registry: RegistrySnapshot,
) -> NormalizedRecordRef:
    semantic_type = registry.semantic_types[0]
    source = registry.sources[0]
    draft = SemanticDraft(
        semantic_type_id=semantic_type.semantic_type_id,
        semantic_type_version=semantic_type.version,
        payload_model_key=semantic_type.normalized_model_key,
        payload_schema_sha256=semantic_type.schema_fingerprint_sha256,
        payload_sha256=canonical_sha256(payload.model_dump(mode="json")),
        subject=payload.subject,
        valid_from=payload.valid_from,
        valid_to=payload.valid_to,
        knowable_at=payload.knowable_at,
        produced_at=payload.knowable_at + timedelta(minutes=1),
        producer_kind=SemanticProducerKind.DETERMINISTIC_NORMALIZER,
        producer_id=source.normalizer_id,
        producer_version=source.normalizer_version,
        producer_implementation_sha256=source.normalizer_implementation_sha256,
    )
    return NormalizedRecordRef(
        draft=draft,
        document_id=f"document:{payload.accession}",
        raw_object_id=raw_entry.raw_id,
        raw_object_sha256=raw_entry.envelope.object.sha256,
        source_registry_entry_id=source.source_registry_entry_id,
        source_registry_entry_sha256=source.content_sha256,
        mapping_version="fixture-sec:1.0.0",
        mapping_implementation_sha256=source.normalizer_implementation_sha256,
        recorded_at=payload.knowable_at + timedelta(minutes=2),
        confidence=Decimal("0.99"),
    )


def _capture_evidence(
    *,
    scope: CaptureScope,
    requirement: CaptureRequirement,
    record: NormalizedRecordRef,
    raw_entry: RawLedgerEntry,
    coverage_entry: str,
    applicability: ApplicabilityMapping,
    source_coverage: SourceCoverageMapping,
) -> tuple[CaptureManifest, CaptureEvaluationReport]:
    evidence = CaptureRecordEvidence(
        source_coverage_entry_id=coverage_entry,
        raw_id=raw_entry.raw_id,
        raw_sha256=raw_entry.envelope.object.sha256,
        normalized_id=record.normalized_record_id,
        semantic_type_id=requirement.semantic_type_id,
        semantic_type_version=requirement.semantic_type_version,
        populated_fields=requirement.required_fields,
        knowable_at=record.draft.knowable_at,
        recorded_at=record.recorded_at,
        valid_from=datetime.combine(record.draft.valid_from, time.min, UTC),
        valid_to=datetime.combine(record.draft.valid_to, time.max, UTC),
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
    cell = CaptureCell(
        subject=SUBJECT,
        domain=requirement.domain,
        partition_key=PARTITION,
        capture_requirement_id=requirement.capture_requirement_id,
        applicability="required",
        status="complete",
        evidence=(evidence,),
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
        partition_key=PARTITION,
        as_of=CUTOFF,
        started_at=CUTOFF,
        cells=(cell,),
        created_at=CUTOFF + timedelta(minutes=1),
    )
    return manifest, evaluate_capture_manifest(
        scope,
        manifest,
        applicability_catalog_id=scope.applicability_catalog_id,
        applicability_catalog_sha256=scope.applicability_catalog_sha256,
        applicability=applicability,
        source_coverage=source_coverage,
        evaluated_at=manifest.created_at + timedelta(minutes=1),
    )


def _snapshot_and_selection(
    *,
    registry: RegistrySnapshot,
    capture_requirement: CaptureRequirement,
    data_requirement: DataRequirement,
    record: NormalizedRecordRef,
    valid_on: date,
) -> tuple[SnapshotManifest, RunnerInputSelection]:
    demand = SnapshotDemandCell(
        requirement_id=data_requirement.requirement_id,
        capture_requirement_id=capture_requirement.capture_requirement_id,
        semantic_type_id=data_requirement.semantic_type_id,
        semantic_type_version=capture_requirement.semantic_type_version,
        domain=data_requirement.domain,
        subject=SUBJECT,
        partition_key=PARTITION,
        level=data_requirement.level,
    )
    request = SnapshotRequest(
        subjects=(SUBJECT,),
        as_of=CUTOFF + timedelta(minutes=3),
        valid_on=valid_on,
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
        demand_cells=(demand,),
    )
    snapshot = SnapshotManifest(
        request=request,
        registry_snapshot=registry,
        resolved_subjects=(SUBJECT,),
        normalized_records=(record,),
        selections=(
            SnapshotCellSelection(
                demand=demand,
                normalized_record_ids=(record.normalized_record_id,),
            ),
        ),
        resolved_at=request.as_of + timedelta(seconds=1),
        resolver_id="batch:FixtureSnapshotResolver",
        resolver_version="1.0.0",
        resolver_implementation_sha256=_hash("snapshot-resolver"),
    )
    template = FactorInvocationTemplate(
        factor_id="mvp_capture_tiny_e0_probe",
        factor_version="1.0.0",
        factor_implementation_sha256=_hash("unregistered-factor-probe"),
        factor_kind=FactorKind.BASE,
        parameter_model_key="batch:NoParameters",
        parameter_schema_sha256=_hash("no-parameters-schema"),
        canonical_parameters_sha256=_hash("no-parameters"),
        data_requirement_ids=(data_requirement.requirement_id,),
    )
    execution = FactorExecution(
        template=template,
        snapshot_id=snapshot.snapshot_id,
        snapshot_sha256=snapshot.content_sha256,
        ordered_subjects=(SUBJECT,),
        started_at=snapshot.resolved_at + timedelta(seconds=1),
    )
    selection = build_runner_input_selection(
        execution=execution,
        snapshot=snapshot,
        selected_at=execution.started_at + timedelta(seconds=1),
        runner_id="batch:FixtureRunner",
        runner_version="1.0.0",
        runner_implementation_sha256=_hash("fixture-runner"),
    )
    return snapshot, selection


def run_e0_slice(
    repository_root: Path,
    *,
    corpus_path: Path = CORPUS_PATH,
    raw_ledger: FixtureRawLedger | None = None,
) -> E0SliceResult:
    """Execute the exact E0 vertical slice without registering it for release."""

    corpus, artifacts = _load_frozen_corpus(repository_root, corpus_path)
    artifact = artifacts["nvda-company-facts"]
    artifact_path = _repository_path(repository_root, artifact["path"])
    body = artifact_path.read_bytes()
    observation = _latest_annual_gross_profit(body, CUTOFF)
    capture = RawCapture(
        source=DataSource.SEC,
        source_record_id="fixture:nvda-company-facts",
        body=body,
        content_type="application/json",
        fetched_at=CUTOFF,
        source_published_at=observation.knowable_at,
        metadata={"artifact_id": artifact["artifact_id"], "corpus_id": corpus["corpus_id"]},
    )
    ledger = raw_ledger or FixtureRawLedger()
    raw_entry = ledger.append(capture)
    registry = _registry()
    capture_requirement = _capture_requirement()
    data_requirement = _data_requirement(capture_requirement)
    applicability, source_coverage, coverage_entry = _projections(capture_requirement)
    scope = _scope(registry, capture_requirement, applicability, source_coverage)
    record = _normalize(observation, raw_entry, registry)
    manifest, evaluation = _capture_evidence(
        scope=scope,
        requirement=capture_requirement,
        record=record,
        raw_entry=raw_entry,
        coverage_entry=coverage_entry,
        applicability=applicability,
        source_coverage=source_coverage,
    )
    snapshot, selection = _snapshot_and_selection(
        registry=registry,
        capture_requirement=capture_requirement,
        data_requirement=data_requirement,
        record=record,
        valid_on=observation.valid_to,
    )
    return E0SliceResult(
        corpus_sha256=_sha256(_repository_path(repository_root, corpus_path.as_posix())),
        raw_capture=capture,
        raw_entry=raw_entry,
        raw_ledger=ledger,
        payload=observation,
        registry=registry,
        capture_requirement=capture_requirement,
        data_requirement=data_requirement,
        scope=scope,
        applicability=applicability,
        source_coverage=source_coverage,
        record=record,
        capture_manifest=manifest,
        capture_evaluation=evaluation,
        snapshot=snapshot,
        runner_selection=selection,
    )


__all__ = [
    "CORPUS_PATH",
    "E0SliceResult",
    "FixtureFinancialObservation",
    "FixtureRawLedger",
    "RawLedgerEntry",
    "run_e0_slice",
]
