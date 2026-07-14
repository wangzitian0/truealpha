from __future__ import annotations

import hashlib
import inspect
import json
import os
import subprocess
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast

import psycopg
import pytest
from data_engine.batches.mvp_medium_validation.e0_slice import build_price_registry
from data_engine.config import settings
from data_engine.contract_repository import (
    PostgresRegistrySnapshotRepository,
    PostgresSnapshotRepository,
    PostgresStrategyUsageAuditRepository,
)
from data_engine.mvp_medium_models import MvpNormalizationDraft
from data_engine.mvp_medium_pipeline import (
    LandedMediumCapture,
    MediumAdapterRegistration,
    MediumCaptureWorkItem,
    MediumComponentCatalog,
    MediumNormalizerRegistration,
    land_medium_capture_plan,
    normalize_medium_capture_batch,
)
from data_engine.mvp_medium_registry import MEDIUM_VERSION, build_medium_registry
from data_engine.mvp_medium_repository import (
    MediumRepositoryRegistration,
    PostgresMediumSemanticRepository,
    build_medium_repository_registrations,
    project_provenance_neutral,
)
from data_engine.mvp_medium_snapshot import PostgresMediumSnapshotResolver
from data_engine.mvp_probe import (
    FixtureProbeRepository,
    ProbeExecutionResult,
    ProbeExecutionSpec,
    execute_contract_probe,
)
from factors.base.registered_semantic_probe import (
    PROBE_FACTOR_VERSION,
    PROBE_IMPLEMENTATION_SHA256,
)
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from truealpha_contracts import DataSource, RawCapture, RawIngestionEnvelope, RawObjectRef
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.data_quality import DataDomain
from truealpha_contracts.execution import (
    FactorInvocationTemplate,
    FactorKind,
    PolicyBinding,
    PolicyRole,
    SnapshotDemandCell,
    SnapshotRequest,
)
from truealpha_contracts.registries import (
    RegistryHistory,
    RegistrySnapshot,
    SemanticTypeRegistryEntry,
    SourceRegistryEntry,
)
from truealpha_contracts.universe import SubjectKind, SubjectRef, UniverseRef
from truealpha_contracts.usage import (
    DataRequirement,
    DataUsageEvent,
    PlannedDemandCell,
    RequirementLevel,
    ReverseLineageEdge,
    StrategyUsageAudit,
    UsageEmitterKind,
    UsageStage,
    build_strategy_usage_audit,
)

REPOSITORY_ROOT = next(
    parent
    for parent in Path(__file__).resolve().parents
    if (parent / "governance" / "vision-issue-graph.json").is_file()
)
EXTENSION_TYPE_ID = "semantic.extension-signal"
EXTENSION_SOURCE_ID = "source.test-extension-signal"
EXTENSION_ADAPTER_ID = "d2_typed_extension:ExtensionSignalAdapter"
EXTENSION_NORMALIZER_ID = "d2_typed_extension:ExtensionSignalNormalizer"
EXTENSION_REPOSITORY_KEY = "d2_typed_extension:ExtensionSignalRepository"
EXTENSION_PROJECTOR_KEY = "data_engine.mvp_medium_repository:project_provenance_neutral"
SUBJECT = SubjectRef(kind=SubjectKind.ISSUER, id="issuer.extension-probe")
OBSERVATION_DATE = date(2026, 3, 31)
FIRST_KNOWABLE_AT = datetime(2026, 4, 1, 12, tzinfo=UTC)
SECOND_KNOWABLE_AT = datetime(2026, 4, 2, 12, tzinfo=UTC)


class MemoryRawObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def store(self, capture: RawCapture) -> RawIngestionEnvelope:
        sha256 = hashlib.sha256(capture.body).hexdigest()
        ref = RawObjectRef(
            bucket="d2-typed-extension",
            key=sha256,
            sha256=sha256,
            byte_length=len(capture.body),
            content_type=capture.content_type,
        )
        existing = self.objects.setdefault(ref.uri, capture.body)
        if existing != capture.body:
            raise ValueError("content-addressed raw object collision")
        return RawIngestionEnvelope(
            source=capture.source,
            source_record_id=capture.source_record_id,
            object=ref,
            fetched_at=capture.fetched_at,
            source_published_at=capture.source_published_at,
            metadata=capture.metadata,
        )

    def get(self, ref: RawObjectRef) -> bytes:
        body = self.objects[ref.uri]
        if hashlib.sha256(body).hexdigest() != ref.sha256:
            raise ValueError("raw object checksum mismatch")
        return body


class _ExtensionSignalPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    signal_id: str = Field(min_length=1)
    issuer_id: str = Field(min_length=1)
    observation_date: date
    value: Decimal
    knowable_at: datetime
    recorded_at: datetime
    confidence: Decimal = Field(ge=0, le=1)
    raw_ref: str = Field(pattern=r"^raw-object:[0-9a-f]{64}$")

    @field_validator("value", "confidence", mode="before")
    @classmethod
    def reject_binary_float(cls, value: Any) -> Any:
        if isinstance(value, (float, bool)):
            raise ValueError("extension values must use exact Decimals")
        return value

    @field_validator("knowable_at", "recorded_at")
    @classmethod
    def validate_datetimes(cls, value: datetime, info) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{info.field_name} must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_clock(self) -> _ExtensionSignalPayload:
        if self.recorded_at < self.knowable_at:
            raise ValueError("extension signal cannot be recorded before it is knowable")
        return self


class _ExtensionSourceRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    source_record_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    signal_id: str = Field(min_length=1)
    issuer_id: str = Field(min_length=1)
    observation_date: date
    value: Decimal
    knowable_at: datetime
    recorded_at: datetime
    confidence: Decimal = Field(ge=0, le=1)
    supersedes_document_id: str | None = None

    @field_validator("value", "confidence", mode="before")
    @classmethod
    def reject_binary_float(cls, value: Any) -> Any:
        if isinstance(value, (float, bool)):
            raise ValueError("source values must use exact Decimals")
        return value

    @field_validator("knowable_at", "recorded_at")
    @classmethod
    def validate_datetimes(cls, value: datetime, info) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{info.field_name} must be timezone-aware")
        return value


class _ExtensionCaptureConfiguration(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_record_id: str = Field(min_length=1)
    body: bytes
    body_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_published_at: datetime
    fetched_at: datetime

    @field_validator("source_published_at", "fetched_at")
    @classmethod
    def validate_datetimes(cls, value: datetime, info) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{info.field_name} must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_body(self) -> _ExtensionCaptureConfiguration:
        if hashlib.sha256(self.body).hexdigest() != self.body_sha256:
            raise ValueError("extension source bytes do not match their checksum")
        if self.fetched_at < self.source_published_at:
            raise ValueError("extension bytes cannot be fetched before publication")
        return self


def _capture_extension_signal(configuration: BaseModel) -> RawCapture:
    configured = cast(_ExtensionCaptureConfiguration, configuration)
    return RawCapture(
        source=DataSource.SEC,
        source_record_id=configured.source_record_id,
        body=configured.body,
        content_type="application/json",
        source_published_at=configured.source_published_at,
        fetched_at=configured.fetched_at,
        metadata={"semantic_type_id": EXTENSION_TYPE_ID},
    )


def _normalize_extension_signal(capture: LandedMediumCapture) -> tuple[MvpNormalizationDraft, ...]:
    source = _ExtensionSourceRow.model_validate_json(capture.body)
    if source.source_record_id != capture.source_record_id:
        raise ValueError("extension source row does not match the captured record ID")
    payload = _ExtensionSignalPayload(
        signal_id=source.signal_id,
        issuer_id=source.issuer_id,
        observation_date=source.observation_date,
        value=source.value,
        knowable_at=source.knowable_at,
        recorded_at=source.recorded_at,
        confidence=source.confidence,
        raw_ref=f"raw-object:{capture.raw_object_sha256}",
    )
    return (
        MvpNormalizationDraft(
            semantic_type_id=EXTENSION_TYPE_ID,
            payload=payload,
            subject=SubjectRef(kind=SubjectKind.ISSUER, id=source.issuer_id),
            valid_from=source.observation_date,
            valid_to=source.observation_date,
            knowable_at=source.knowable_at,
            produced_at=source.knowable_at + timedelta(minutes=1),
            recorded_at=source.recorded_at,
            document_id=source.document_id,
            confidence=source.confidence,
            raw_ref=capture.raw_ref,
            is_restatement=source.supersedes_document_id is not None,
            supersedes_document_id=source.supersedes_document_id,
        ),
    )


def _write_extension_signal(
    connection: psycopg.Connection[Any],
    record,
    payload: BaseModel,
    _source_id: str,
) -> bool:
    signal = cast(_ExtensionSignalPayload, payload)
    raw_row = connection.execute(
        "select raw_ref from staging.normalized_records where normalized_record_id = %s",
        (record.normalized_record_id,),
    ).fetchone()
    if raw_row is None:
        raise ValueError("extension normalized record disappeared before projection")
    inserted = connection.execute(
        """
        insert into staging.d2_extension_signals (
            normalized_record_id, signal_id, issuer_id, observation_date, value,
            valid_time, transaction_time, recorded_at, confidence, raw_ref
        ) values (
            %s, %s, %s, %s, %s, daterange(%s, %s, '[]'), %s, %s, %s, %s
        ) on conflict (normalized_record_id) do nothing
        returning normalized_record_id
        """,
        (
            record.normalized_record_id,
            signal.signal_id,
            signal.issuer_id,
            signal.observation_date,
            signal.value,
            record.draft.valid_from,
            record.draft.valid_to,
            record.draft.knowable_at,
            record.recorded_at,
            record.confidence,
            raw_row[0],
        ),
    ).fetchone()
    return inserted is not None


def _implementation_sha256(component: object) -> str:
    return hashlib.sha256(inspect.getsource(component).encode()).hexdigest()


def _prepare_projection_table(connection: psycopg.Connection[Any]) -> None:
    connection.execute(
        """
        create table staging.d2_extension_signals (
            normalized_record_id text primary key
                references staging.normalized_records(normalized_record_id),
            signal_id text not null,
            issuer_id text not null,
            observation_date date not null,
            value numeric not null,
            valid_time daterange not null,
            transaction_time timestamptz not null,
            recorded_at timestamptz not null,
            confidence numeric not null check (confidence between 0 and 1),
            raw_ref text not null,
            check (recorded_at >= transaction_time)
        )
        """
    )
    connection.execute(
        """
        create trigger trg_d2_extension_signals_append_only
        before update or delete on staging.d2_extension_signals
        for each row execute function staging.reject_point_in_time_mutation()
        """
    )


def _extension_runtime(
    connection: psycopg.Connection[Any],
    *,
    disabled: bool = False,
) -> tuple[
    RegistrySnapshot,
    RegistrySnapshot,
    RegistryHistory,
    SourceRegistryEntry,
    SemanticTypeRegistryEntry,
    MediumComponentCatalog,
    PostgresMediumSemanticRepository,
]:
    e2_implementation_sha256 = hashlib.sha256(
        (REPOSITORY_ROOT / "apps/data-engine/src/data_engine/batches/mvp_medium_validation/e2_slice.py").read_bytes()
    ).hexdigest()
    base_registry, base_history = build_medium_registry(
        build_price_registry(),
        source_implementation_sha256=e2_implementation_sha256,
    )
    payload_schema_sha256 = canonical_sha256(_ExtensionSignalPayload.model_json_schema(mode="validation"))
    semantic_type = SemanticTypeRegistryEntry(
        semantic_type_id=EXTENSION_TYPE_ID,
        version=MEDIUM_VERSION,
        domain=DataDomain.FINANCIAL_FACTS,
        schema_version=MEDIUM_VERSION,
        schema_fingerprint_sha256=payload_schema_sha256,
        normalized_model_key="d2_typed_extension:ExtensionSignalPayload",
        input_model_key="truealpha_contracts:ProvenanceNeutralInput",
        repository_key=EXTENSION_REPOSITORY_KEY,
        projector_key=EXTENSION_PROJECTOR_KEY,
        compatibility_sha256=canonical_sha256({"compatible_schema_versions": []}),
        model_implementation_sha256=_implementation_sha256(_ExtensionSignalPayload),
        repository_implementation_sha256=_implementation_sha256(_write_extension_signal),
        projector_implementation_sha256=_implementation_sha256(project_provenance_neutral),
    )
    adapter_sha256 = _implementation_sha256(_capture_extension_signal)
    normalizer_sha256 = _implementation_sha256(_normalize_extension_signal)
    source = SourceRegistryEntry(
        source_id=EXTENSION_SOURCE_ID,
        version=MEDIUM_VERSION,
        adapter_id=EXTENSION_ADAPTER_ID,
        adapter_version=MEDIUM_VERSION,
        normalizer_id=EXTENSION_NORMALIZER_ID,
        normalizer_version=MEDIUM_VERSION,
        supported_domains=(DataDomain.FINANCIAL_FACTS,),
        supported_type_ids=(EXTENSION_TYPE_ID,),
        configuration_schema_sha256=canonical_sha256(
            _ExtensionCaptureConfiguration.model_json_schema(mode="validation")
        ),
        mapping_schema_sha256=canonical_sha256(_ExtensionSourceRow.model_json_schema(mode="validation")),
        adapter_implementation_sha256=adapter_sha256,
        normalizer_implementation_sha256=normalizer_sha256,
    )
    registry = base_registry.extend(
        sources=(source,),
        semantic_types=(semantic_type,),
        required_type_ids=(*base_registry.required_type_ids, EXTENSION_TYPE_ID),
    )
    history = RegistryHistory(snapshots=(*base_history.snapshots, registry))
    adapter = MediumAdapterRegistration(
        source_id=source.source_id,
        source_version=source.version,
        adapter_id=source.adapter_id,
        adapter_version=source.adapter_version,
        adapter_implementation_sha256=source.adapter_implementation_sha256,
        configuration_type=_ExtensionCaptureConfiguration,
        raw_source=DataSource.SEC,
        capture=_capture_extension_signal,
    )
    normalizer = MediumNormalizerRegistration(
        source_id=source.source_id,
        source_version=source.version,
        semantic_type_id=semantic_type.semantic_type_id,
        semantic_type_version=semantic_type.version,
        normalizer_id=source.normalizer_id,
        normalizer_version=source.normalizer_version,
        normalizer_implementation_sha256=source.normalizer_implementation_sha256,
        normalize=_normalize_extension_signal,
    )
    catalog = MediumComponentCatalog(
        registry=registry,
        adapters=(adapter,),
        normalizers=(normalizer,),
        disabled_type_ids=(frozenset({EXTENSION_TYPE_ID}) if disabled else frozenset()),
    )
    registration = MediumRepositoryRegistration(
        semantic_type_id=semantic_type.semantic_type_id,
        semantic_type_version=semantic_type.version,
        model_type=_ExtensionSignalPayload,
        repository_key=semantic_type.repository_key,
        projector_key=semantic_type.projector_key,
        mapping_versions={source.source_id: f"{source.normalizer_id}:{source.normalizer_version}"},
        writer=_write_extension_signal,
        logical_key=lambda payload: (cast(_ExtensionSignalPayload, payload).signal_id,),
        partition_filter=lambda _payload, partition: partition == "all",
        source_rank=lambda _payload, source_id: 0 if source_id == EXTENSION_SOURCE_ID else None,
    )
    repository = PostgresMediumSemanticRepository(
        connection,
        registry=registry,
        registrations=(*build_medium_repository_registrations(base_registry), registration),
    )
    return base_registry, registry, history, source, semantic_type, catalog, repository


def _source_rows() -> tuple[_ExtensionSourceRow, _ExtensionSourceRow]:
    first = _ExtensionSourceRow(
        source_record_id="extension-signal:v1",
        document_id="document:extension-signal-v1",
        signal_id="signal:extension-probe",
        issuer_id=SUBJECT.id,
        observation_date=OBSERVATION_DATE,
        value=Decimal("0.73"),
        knowable_at=FIRST_KNOWABLE_AT,
        recorded_at=FIRST_KNOWABLE_AT + timedelta(minutes=2),
        confidence=Decimal("0.97"),
    )
    second = _ExtensionSourceRow(
        source_record_id="extension-signal:v2",
        document_id="document:extension-signal-v2",
        signal_id=first.signal_id,
        issuer_id=first.issuer_id,
        observation_date=first.observation_date,
        value=Decimal("0.81"),
        knowable_at=SECOND_KNOWABLE_AT,
        recorded_at=SECOND_KNOWABLE_AT + timedelta(minutes=2),
        confidence=Decimal("0.99"),
        supersedes_document_id=first.document_id,
    )
    return first, second


def _work_item_from_body(
    *,
    source_record_id: str,
    body: bytes,
    knowable_at: datetime,
    recorded_at: datetime,
) -> MediumCaptureWorkItem:
    configuration = _ExtensionCaptureConfiguration(
        source_record_id=source_record_id,
        body=body,
        body_sha256=hashlib.sha256(body).hexdigest(),
        source_published_at=knowable_at,
        fetched_at=knowable_at + timedelta(seconds=10),
    )
    return MediumCaptureWorkItem(
        source_id=EXTENSION_SOURCE_ID,
        source_version=MEDIUM_VERSION,
        semantic_type_ids=(EXTENSION_TYPE_ID,),
        semantic_type_version=MEDIUM_VERSION,
        configuration=configuration,
        recorded_at=recorded_at,
    )


def _work_item(source: _ExtensionSourceRow) -> MediumCaptureWorkItem:
    return _work_item_from_body(
        source_record_id=source.source_record_id,
        body=source.model_dump_json().encode(),
        knowable_at=source.knowable_at,
        recorded_at=source.recorded_at,
    )


def _demand() -> tuple[DataRequirement, SnapshotDemandCell]:
    requirement = DataRequirement(
        capture_requirement_id="capture-requirement:" + canonical_sha256({"semantic_type_id": EXTENSION_TYPE_ID}),
        semantic_type_id=EXTENSION_TYPE_ID,
        domain=DataDomain.FINANCIAL_FACTS,
        metric="extension_signal",
        subject_kinds=frozenset({SubjectKind.ISSUER}),
        level=RequirementLevel.REQUIRED,
        lookback=timedelta(days=365),
        valid_period_rule_id="partition.extension-signal:v1",
        maximum_age=timedelta(days=365),
        cadence=timedelta(days=1),
    )
    demand = SnapshotDemandCell(
        requirement_id=requirement.requirement_id,
        capture_requirement_id=requirement.capture_requirement_id,
        semantic_type_id=requirement.semantic_type_id,
        semantic_type_version=MEDIUM_VERSION,
        domain=requirement.domain,
        subject=SUBJECT,
        partition_key="all",
        level=requirement.level,
    )
    return requirement, demand


def _request(
    registry: RegistrySnapshot,
    demand: SnapshotDemandCell,
    *,
    as_of: datetime,
) -> SnapshotRequest:
    return SnapshotRequest(
        subjects=(SUBJECT,),
        as_of=as_of,
        valid_on=OBSERVATION_DATE,
        registry_snapshot_id=registry.registry_snapshot_id,
        registry_snapshot_sha256=registry.content_sha256,
        source_registry_id=registry.source_registry_snapshot_id,
        source_registry_sha256=registry.source_registry_sha256,
        semantic_type_registry_id=registry.semantic_type_registry_snapshot_id,
        semantic_type_registry_sha256=registry.semantic_type_registry_sha256,
        policy_bindings=tuple(
            PolicyBinding(
                role=role,
                policy_id=f"policy.extension-signal.{role.value}",
                policy_version=MEDIUM_VERSION,
                implementation_sha256=canonical_sha256({"policy": role.value}),
            )
            for role in PolicyRole
            if role is not PolicyRole.MEMBERSHIP
        ),
        demand_cells=(demand,),
    )


def _probe_spec(requirement: DataRequirement, *, started_at: datetime) -> ProbeExecutionSpec:
    template = FactorInvocationTemplate(
        factor_id="registered_semantic_probe",
        factor_version=PROBE_FACTOR_VERSION,
        factor_implementation_sha256=PROBE_IMPLEMENTATION_SHA256,
        factor_kind=FactorKind.BASE,
        parameter_model_key="d2_typed_extension:NoParameters",
        parameter_schema_sha256=canonical_sha256({"type": "object", "additionalProperties": False}),
        canonical_parameters_sha256=canonical_sha256({}),
        data_requirement_ids=(requirement.requirement_id,),
    )
    return ProbeExecutionSpec(
        template=template,
        subject=SUBJECT,
        started_at=started_at,
        runner_id="runner.d2-typed-extension",
        runner_version=MEDIUM_VERSION,
        runner_implementation_sha256=canonical_sha256({"runner": "d2-typed-extension"}),
        repository_commit_id="commit:d2-typed-extension-output-replay",
    )


def _usage_audit(
    *,
    registry: RegistrySnapshot,
    requirement: DataRequirement,
    record_id: str,
    result: ProbeExecutionResult,
) -> StrategyUsageAudit:
    run_id = "strategy-run:d2-typed-extension-output-replay"
    trace_id = "trace:d2-typed-extension-output-replay"
    decision_id = "decision:d2-typed-extension-output-replay"
    planned = PlannedDemandCell(
        requirement_id=requirement.requirement_id,
        capture_requirement_id=requirement.capture_requirement_id,
        semantic_type_id=requirement.semantic_type_id,
        domain=requirement.domain,
        subject=SUBJECT,
        partition_key="all",
        level=requirement.level,
        expected_stages=frozenset({UsageStage.FACTOR_CONSUMPTION}),
    )
    occurred_at = result.output.materialized_at + timedelta(seconds=1)
    event = DataUsageEvent(
        operation_id="operation:d2-typed-extension-factor-consumption",
        emitter_kind=UsageEmitterKind.INSTRUMENTED_RUNNER,
        emitter_id="runner.d2-typed-extension",
        stage=UsageStage.FACTOR_CONSUMPTION,
        requirement_id=requirement.requirement_id,
        capture_requirement_id=requirement.capture_requirement_id,
        semantic_type_id=requirement.semantic_type_id,
        domain=requirement.domain,
        subject=SUBJECT,
        partition_key="all",
        run_id=run_id,
        trace_id=trace_id,
        normalized_record_ids=(record_id,),
        evidence_ids=(result.selection.selection_id, result.output.materialized_output_id),
        occurred_at=occurred_at,
        recorded_at=occurred_at + timedelta(seconds=1),
        retained_until=occurred_at + timedelta(days=365),
    )
    consumed_ids = (record_id, result.selection.selection_id, result.output.materialized_output_id)
    reverse_lineage = (
        ReverseLineageEdge(downstream_id=run_id, upstream_id=decision_id, relation="produced"),
        ReverseLineageEdge(downstream_id=decision_id, upstream_id=trace_id, relation="traced"),
        *(ReverseLineageEdge(downstream_id=trace_id, upstream_id=item, relation="consumed") for item in consumed_ids),
    )
    content_hash = canonical_sha256({"audit": "d2-typed-extension"})
    return build_strategy_usage_audit(
        strategy_run_id=run_id,
        planned_cells=(planned,),
        events=(event,),
        trace_bundle_ids=("trace-bundle:" + canonical_sha256({"trace": trace_id}),),
        reverse_lineage=reverse_lineage,
        affected_decision_ids=(decision_id,),
        research_catalog_id="research-catalog:" + content_hash,
        research_catalog_sha256=content_hash,
        universe=UniverseRef(
            universe_id="universe.d2-typed-extension",
            universe_version=MEDIUM_VERSION,
            content_sha256=canonical_sha256({"universe": SUBJECT.model_dump(mode="json")}),
        ),
        applicability_catalog_id="applicability:" + content_hash,
        applicability_catalog_sha256=content_hash,
        slo_catalog_id="slo-catalog:d2-typed-extension",
        slo_catalog_sha256=content_hash,
        release_manifest_id="release-manifest:" + content_hash,
        registry_snapshot=registry,
        run_started_at=result.execution.started_at,
        run_completed_at=event.recorded_at + timedelta(seconds=1),
        audited_at=event.recorded_at + timedelta(seconds=2),
        auditor_id="auditor.d2-typed-extension",
        auditor_version=MEDIUM_VERSION,
        auditor_implementation_sha256=canonical_sha256({"auditor": "d2-typed-extension"}),
    )


@pytest.fixture(scope="module")
def typed_extension_database_url() -> Iterator[str]:
    parameters = conninfo_to_dict(settings.database_url)
    database_name = f"truealpha_d2_typed_extension_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    admin_url = make_conninfo(**(parameters | {"dbname": "postgres"}))
    target_url = make_conninfo(**(parameters | {"dbname": database_name}))
    try:
        with psycopg.connect(admin_url, connect_timeout=3, autocommit=True) as admin:
            admin.execute(sql.SQL("create database {}").format(sql.Identifier(database_name)))
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    try:
        for migration in (
            *sorted((REPOSITORY_ROOT / "db/migrations").glob("*.sql")),
            REPOSITORY_ROOT / "db/roles.sql",
        ):
            completed = subprocess.run(
                ["psql", target_url, "-v", "ON_ERROR_STOP=1", "-f", str(migration)],
                check=False,
                capture_output=True,
                text=True,
            )
            if completed.returncode != 0:
                pytest.fail(completed.stdout + completed.stderr, pytrace=False)
        yield target_url
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(
                "select pg_terminate_backend(pid) from pg_stat_activity where datname = %s",
                (database_name,),
            )
            admin.execute(sql.SQL("drop database if exists {}").format(sql.Identifier(database_name)))


@pytest.fixture
def connection(typed_extension_database_url: str) -> Iterator[psycopg.Connection[Any]]:
    active = psycopg.connect(typed_extension_database_url, connect_timeout=3, autocommit=False)
    active.execute("begin")
    try:
        yield active
    finally:
        active.rollback()
        active.close()


def test_typed_record_extension_survives_disable_and_snapshot_replay(
    connection: psycopg.Connection[Any],
) -> None:
    _prepare_projection_table(connection)
    base_registry, registry, history, source, semantic_type, catalog, repository = _extension_runtime(connection)
    assert history.snapshots[-2:] == (base_registry, registry)
    registry_store = PostgresRegistrySnapshotRepository(connection)
    assert registry_store.put(base_registry)
    assert registry_store.put(registry)
    registry_bytes = registry.model_dump_json()
    first_source, second_source = _source_rows()
    first_item = _work_item(first_source)
    second_item = _work_item(second_source)
    object_store = MemoryRawObjectStore()

    first_capture = land_medium_capture_plan(
        connection,
        object_store=object_store,
        catalog=catalog,
        work_items=(first_item,),
    )
    first_normalized = normalize_medium_capture_batch(
        batch=first_capture,
        catalog=catalog,
        repository=repository,
    )
    changed_capture = land_medium_capture_plan(
        connection,
        object_store=object_store,
        catalog=catalog,
        work_items=(first_item, second_item),
    )
    changed_normalized = normalize_medium_capture_batch(
        batch=changed_capture,
        catalog=catalog,
        repository=repository,
    )
    repeated_capture = land_medium_capture_plan(
        connection,
        object_store=object_store,
        catalog=catalog,
        work_items=(first_item, second_item),
    )
    repeated_normalized = normalize_medium_capture_batch(
        batch=repeated_capture,
        catalog=catalog,
        repository=repository,
    )

    assert len(first_normalized.inserted_record_ids) == 1
    assert len(changed_normalized.inserted_record_ids) == 1
    assert repeated_normalized.inserted_record_ids == ()
    assert changed_normalized.normalized_records == repeated_normalized.normalized_records
    assert tuple(item.fetch_id for item in changed_capture.captures) == tuple(
        item.fetch_id for item in repeated_capture.captures
    )
    records = changed_normalized.normalized_records
    first_record = next(record for record in records if not record.is_restatement)
    second_record = next(record for record in records if record.is_restatement)
    assert second_record.supersedes_record_id == first_record.normalized_record_id
    assert all(record.source_registry_entry_id == source.source_registry_entry_id for record in records)
    assert all(record.draft.semantic_type_id == semantic_type.semantic_type_id for record in records)

    for capture, record in zip(changed_capture.captures, (first_record, second_record), strict=True):
        raw_row = connection.execute(
            "select source_record_id, payload_sha256, object_uri from raw.fetches where id = %s",
            (capture.fetch_id,),
        ).fetchone()
        assert raw_row == (
            capture.source_record_id,
            capture.raw_object_sha256,
            f"s3://d2-typed-extension/{capture.raw_object_sha256}",
        )
        stored = repository.get(record.normalized_record_id)
        assert stored is not None
        assert stored.record == record
        assert stored.payload.raw_ref == f"raw-object:{capture.raw_object_sha256}"
        projection = connection.execute(
            """
            select raw_ref, confidence from staging.d2_extension_signals
            where normalized_record_id = %s
            """,
            (record.normalized_record_id,),
        ).fetchone()
        assert projection == (capture.raw_ref, record.confidence)

    _requirement, demand = _demand()
    before = repository.visible_records(
        demand,
        as_of=FIRST_KNOWABLE_AT - timedelta(microseconds=1),
        valid_on=OBSERVATION_DATE,
    )
    middle = repository.visible_records(
        demand,
        as_of=SECOND_KNOWABLE_AT - timedelta(microseconds=1),
        valid_on=OBSERVATION_DATE,
    )
    after = repository.visible_records(
        demand,
        as_of=SECOND_KNOWABLE_AT,
        valid_on=OBSERVATION_DATE,
    )
    assert before == ()
    assert tuple(item.record for item in middle) == (first_record,)
    assert tuple(item.record for item in after) == (second_record,)

    snapshots = PostgresSnapshotRepository(connection)
    resolver = PostgresMediumSnapshotResolver(semantic_records=repository, snapshots=snapshots)
    middle_request = _request(
        registry,
        demand,
        as_of=SECOND_KNOWABLE_AT - timedelta(microseconds=1),
    )
    after_request = _request(registry, demand, as_of=SECOND_KNOWABLE_AT)
    middle_snapshot = resolver.resolve(
        middle_request,
        registry=registry,
        resolved_at=SECOND_KNOWABLE_AT + timedelta(minutes=1),
    )
    after_snapshot = resolver.resolve(
        after_request,
        registry=registry,
        resolved_at=SECOND_KNOWABLE_AT + timedelta(minutes=2),
    )
    assert middle_snapshot.normalized_records == (first_record,)
    assert after_snapshot.normalized_records == (second_record,)

    counts_before_disable = connection.execute(
        """
        select
          (select count(*) from raw.fetches),
          (select count(*) from staging.normalized_records),
          (select count(*) from staging.d2_extension_signals)
        """
    ).fetchone()
    assert counts_before_disable == (2, 2, 2)
    _, disabled_registry, _, _, _, disabled_catalog, disabled_repository = _extension_runtime(
        connection,
        disabled=True,
    )
    assert disabled_registry == registry
    with pytest.raises(ValueError, match="disabled semantic type"):
        land_medium_capture_plan(
            connection,
            object_store=object_store,
            catalog=disabled_catalog,
            work_items=(second_item,),
        )

    disabled_resolver = PostgresMediumSnapshotResolver(
        semantic_records=disabled_repository,
        snapshots=snapshots,
    )
    replayed_middle = disabled_resolver.resolve(
        middle_request,
        registry=disabled_registry,
        resolved_at=SECOND_KNOWABLE_AT + timedelta(minutes=1),
    )
    replayed_after = disabled_resolver.resolve(
        after_request,
        registry=disabled_registry,
        resolved_at=SECOND_KNOWABLE_AT + timedelta(minutes=2),
    )
    assert registry.model_dump_json() == registry_bytes
    assert registry_store.get(base_registry.registry_snapshot_id) == base_registry
    assert registry_store.get(registry.registry_snapshot_id) == registry
    assert replayed_middle == middle_snapshot
    assert replayed_after == after_snapshot
    assert snapshots.get(middle_snapshot.snapshot_id) == middle_snapshot
    assert snapshots.get(after_snapshot.snapshot_id) == after_snapshot
    assert (
        connection.execute(
            """
        select
          (select count(*) from raw.fetches),
          (select count(*) from staging.normalized_records),
          (select count(*) from staging.d2_extension_signals)
        """
        ).fetchone()
        == counts_before_disable
    )

    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        connection.execute(
            "update staging.d2_extension_signals set confidence = 0.5 where normalized_record_id = %s",
            (first_record.normalized_record_id,),
        )


def test_typed_record_extension_rejects_schema_drift_and_missing_confidence(
    connection: psycopg.Connection[Any],
) -> None:
    _prepare_projection_table(connection)
    _base, _registry, _history, _source, _type, catalog, repository = _extension_runtime(connection)
    valid, _changed = _source_rows()
    drifted = valid.model_dump(mode="json") | {"unexpected_source_field": "schema drift"}
    missing_confidence = valid.model_dump(mode="json")
    missing_confidence.pop("confidence")
    invalid_items = (
        _work_item_from_body(
            source_record_id="extension-signal:drifted",
            body=canonical_json.encode(),
            knowable_at=valid.knowable_at,
            recorded_at=valid.recorded_at,
        )
        for canonical_json in (
            json.dumps(drifted, sort_keys=True, separators=(",", ":")),
            json.dumps(missing_confidence, sort_keys=True, separators=(",", ":")),
        )
    )

    for item, message in zip(
        invalid_items,
        ("Extra inputs are not permitted", "Field required"),
        strict=True,
    ):
        landed = land_medium_capture_plan(
            connection,
            object_store=MemoryRawObjectStore(),
            catalog=catalog,
            work_items=(item,),
        )
        with pytest.raises(ValidationError, match=message):
            normalize_medium_capture_batch(
                batch=landed,
                catalog=catalog,
                repository=repository,
            )

    assert connection.execute("select count(*) from raw.fetches").fetchone() == (2,)
    assert connection.execute("select count(*) from staging.normalized_records").fetchone() == (0,)
    assert connection.execute("select count(*) from staging.d2_extension_signals").fetchone() == (0,)


def test_disabled_typed_extension_replays_usage_and_factor_output(
    connection: psycopg.Connection[Any],
) -> None:
    _prepare_projection_table(connection)
    _base, registry, _history, _source, _type, catalog, repository = _extension_runtime(connection)
    first_source, second_source = _source_rows()
    object_store = MemoryRawObjectStore()
    captured = land_medium_capture_plan(
        connection,
        object_store=object_store,
        catalog=catalog,
        work_items=(_work_item(first_source), _work_item(second_source)),
    )
    normalized = normalize_medium_capture_batch(
        batch=captured,
        catalog=catalog,
        repository=repository,
    )
    selected_record = next(record for record in normalized.normalized_records if record.is_restatement)
    requirement, demand = _demand()
    request = _request(registry, demand, as_of=SECOND_KNOWABLE_AT)
    snapshots = PostgresSnapshotRepository(connection)
    snapshot = PostgresMediumSnapshotResolver(
        semantic_records=repository,
        snapshots=snapshots,
    ).resolve(
        request,
        registry=registry,
        resolved_at=SECOND_KNOWABLE_AT + timedelta(minutes=3),
    )
    output_repository = FixtureProbeRepository(snapshot)
    spec = _probe_spec(requirement, started_at=snapshot.resolved_at + timedelta(seconds=1))
    first_result = execute_contract_probe(
        repository=output_repository,
        snapshot=snapshot,
        spec=spec,
    )
    first_audit = _usage_audit(
        registry=registry,
        requirement=requirement,
        record_id=selected_record.normalized_record_id,
        result=first_result,
    )
    audit_repository = PostgresStrategyUsageAuditRepository(connection)
    assert audit_repository.put(first_audit)
    counts_before_disable = connection.execute(
        """
        select
          (select count(*) from raw.fetches),
          (select count(*) from staging.normalized_records),
          (select count(*) from staging.d2_extension_signals)
        """
    ).fetchone()

    _, disabled_registry, _, _, _, disabled_catalog, disabled_repository = _extension_runtime(
        connection,
        disabled=True,
    )
    with pytest.raises(ValueError, match="disabled semantic type"):
        land_medium_capture_plan(
            connection,
            object_store=object_store,
            catalog=disabled_catalog,
            work_items=(_work_item(second_source),),
        )
    replayed_snapshot = PostgresMediumSnapshotResolver(
        semantic_records=disabled_repository,
        snapshots=snapshots,
    ).resolve(
        request,
        registry=disabled_registry,
        resolved_at=SECOND_KNOWABLE_AT + timedelta(minutes=3),
    )
    replayed_result = execute_contract_probe(
        repository=output_repository,
        snapshot=replayed_snapshot,
        spec=spec,
    )
    replayed_audit = _usage_audit(
        registry=disabled_registry,
        requirement=requirement,
        record_id=selected_record.normalized_record_id,
        result=replayed_result,
    )

    assert replayed_snapshot == snapshot
    assert replayed_result == first_result
    assert replayed_audit == first_audit
    assert audit_repository.put(replayed_audit) is False
    assert audit_repository.get(first_audit.strategy_usage_audit_id) == first_audit
    assert output_repository.get_output(first_result.output.materialized_output_id) == first_result.output
    assert output_repository.get_batch(first_result.batch.materialized_batch_id) == first_result.batch
    assert first_audit.telemetry_complete
    assert (
        connection.execute(
            """
        select
          (select count(*) from raw.fetches),
          (select count(*) from staging.normalized_records),
          (select count(*) from staging.d2_extension_signals)
        """
        ).fetchone()
        == counts_before_disable
    )
