"""D3 E2 ephemeral raw, normalized, manifest, and evaluation persistence."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal

from data_engine.batches.mvp_medium_validation.e0_slice import (
    MarketPricePayload,
    PostgresMarketPriceRepository,
    build_price_registry,
)
from data_engine.batches.staging_topt_capture.e0_slice import (
    EXPECTED_DENOMINATOR,
    YahooChartRequest,
    YahooChartSourceConfig,
)
from data_engine.batches.staging_topt_capture.e1_slice import D3E1TinyExecution
from data_engine.contract_repository import (
    PostgresCaptureEvaluationRepository,
    PostgresCaptureManifestRepository,
    PostgresCaptureScopeRepository,
    PostgresRegistrySnapshotRepository,
)
from data_engine.raw_store import get_payload, insert_fetch, raw_ref
from psycopg import Connection
from truealpha_contracts import RawObjectStore
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
from truealpha_contracts.execution import NormalizedRecordRef, SemanticDraft, SemanticProducerKind
from truealpha_contracts.market import PriceBasis
from truealpha_contracts.models import DataSource
from truealpha_contracts.registries import RegistrySnapshot, SourceRegistryEntry
from truealpha_contracts.universe import SubjectKind, SubjectRef, UniverseRef

VERSION = "1.0.0"
PARTITION_KEY = "nvda:2026-03-31"
SOURCE_COVERAGE_ENTRY_ID = "source-coverage-entry:" + canonical_sha256(
    {"batch": "D3-staging-topt-capture", "source": "source.yahoo-chart-public"}
)
E1_MODULE_PATH = Path("apps/data-engine/src/data_engine/batches/staging_topt_capture/e1_slice.py")


def _sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _binding(label: str) -> tuple[str, str]:
    digest = canonical_sha256({"batch": "D3-staging-topt-capture", "binding": label})
    return f"{label}:{digest}", digest


def _file_sha256(path: Path) -> str:
    return _sha256(path.read_bytes())


def _capture_environment(environment: str) -> CaptureEnvironment:
    try:
        return {
            "local": CaptureEnvironment.LOCAL_TEST,
            "ci": CaptureEnvironment.GITHUB_CI,
        }[environment]
    except KeyError as error:
        raise ValueError("D3 E2 only permits local or ci execution") from error


@dataclass(frozen=True)
class D3E2CaptureContext:
    environment: CaptureEnvironment
    registry: RegistrySnapshot
    source_entry: SourceRegistryEntry
    requirement: CaptureRequirement
    scope: CaptureScope
    applicability: ApplicabilityMapping
    source_coverage: SourceCoverageMapping


def build_e2_capture_context(
    request: YahooChartRequest,
    *,
    environment: Literal["local", "ci"],
    repository_root: Path,
) -> D3E2CaptureContext:
    """Freeze the tiny E2 denominator before its E1 HTTP interaction."""

    capture_environment = _capture_environment(environment)
    if request.listing_id != "listing:xnas:nvda" or request.expected_trading_date != date(2026, 3, 31):
        raise ValueError("D3 E2 is frozen to the NVDA tiny coordinate")
    if EXPECTED_DENOMINATOR != {
        "universe_id": "universe:topt-us-2026-03-31",
        "accession": "000207169126012475",
        "issuer_count": 20,
        "instrument_count": 21,
        "required_cell_count": 84,
    }:
        raise ValueError("D3 E2 parent denominator drifted")

    base_registry = build_price_registry()
    type_entry = next(
        entry for entry in base_registry.semantic_types if entry.key == ("semantic.market-price", VERSION)
    )
    adapter_implementation_sha256 = _file_sha256(repository_root / E1_MODULE_PATH)
    normalizer_implementation_sha256 = _file_sha256(Path(__file__))
    source_entry = SourceRegistryEntry(
        source_id="source.yahoo-chart-public",
        version=VERSION,
        adapter_id="d3:YahooRawHttpAdapter",
        adapter_version=VERSION,
        normalizer_id="d3:YahooPersistenceNormalizer",
        normalizer_version=VERSION,
        supported_domains=(DataDomain.MARKET_PRICES,),
        supported_type_ids=(type_entry.semantic_type_id,),
        configuration_schema_sha256=canonical_sha256(YahooChartSourceConfig.model_json_schema()),
        mapping_schema_sha256=type_entry.schema_fingerprint_sha256,
        adapter_implementation_sha256=adapter_implementation_sha256,
        normalizer_implementation_sha256=normalizer_implementation_sha256,
    )
    registry = RegistrySnapshot(
        parent_snapshot_id=base_registry.registry_snapshot_id,
        sources=(*base_registry.sources, source_entry),
        semantic_types=base_registry.semantic_types,
        identifier_types=base_registry.identifier_types,
        required_type_ids=base_registry.required_type_ids,
        required_identifier_type_ids=base_registry.required_identifier_type_ids,
    )
    requirement = CaptureRequirement(
        semantic_type_id="semantic.market-price",
        semantic_type_version=VERSION,
        domain=DataDomain.MARKET_PRICES,
        required_fields=(
            "calendar_id",
            "close",
            "confidence",
            "confidence_policy_id",
            "currency",
            "exchange_mic",
            "high",
            "issuer_id",
            "listing_id",
            "low",
            "open",
            "price_basis",
            "price_policy_id",
            "security_id",
            "session_close_at",
            "share_class",
            "ticker",
            "trading_date",
            "volume",
        ),
        subject_kinds=(SubjectKind.LISTING,),
        cadence=timedelta(days=1),
        partition_rule_id="partition.market-session:v1",
        freshness_policy_id="freshness.market-price:v1",
        maximum_age=timedelta(days=1),
        quality_policy_ids=("quality.decimal-ohlcv:v1", "quality.raw-lineage:v1"),
    )
    subject = SubjectRef(kind=SubjectKind.LISTING, id=request.listing_id)
    availability = datetime.combine(request.expected_trading_date, time(20, 0), UTC)
    key = (subject.kind, subject.id, requirement.domain, PARTITION_KEY, requirement.capture_requirement_id)
    applicability: ApplicabilityMapping = {key: ("required", availability)}
    source_coverage: SourceCoverageMapping = {
        (environment_tier, *key): (SOURCE_COVERAGE_ENTRY_ID,)
        for environment_tier in (CaptureEnvironment.LOCAL_TEST, CaptureEnvironment.GITHUB_CI)
    }
    research_catalog_id, research_catalog_sha256 = _binding("research-catalog")
    applicability_catalog_id, applicability_catalog_sha256 = _binding("applicability")
    source_coverage_catalog_id, source_coverage_catalog_sha256 = _binding("source-coverage")
    slo_catalog_id, slo_catalog_sha256 = _binding("module-slo")
    universe_hash = canonical_sha256(EXPECTED_DENOMINATOR)
    scope = CaptureScope(
        research_catalog_id=research_catalog_id,
        research_catalog_sha256=research_catalog_sha256,
        universe=UniverseRef(
            universe_id=str(EXPECTED_DENOMINATOR["universe_id"]),
            universe_version="2026-03-31",
            content_sha256=universe_hash,
        ),
        applicability_catalog_id=applicability_catalog_id,
        applicability_catalog_sha256=applicability_catalog_sha256,
        applicability_projection_sha256=canonical_applicability_projection_sha256(applicability),
        source_coverage_catalog_id=source_coverage_catalog_id,
        source_coverage_catalog_sha256=source_coverage_catalog_sha256,
        source_coverage_projection_sha256=canonical_source_coverage_projection_sha256(source_coverage),
        slo_catalog_id=slo_catalog_id,
        slo_catalog_sha256=slo_catalog_sha256,
        source_registry_id=registry.source_registry_snapshot_id,
        source_registry_sha256=registry.source_registry_sha256,
        semantic_type_registry_id=registry.semantic_type_registry_snapshot_id,
        semantic_type_registry_sha256=registry.semantic_type_registry_sha256,
        requirements=(requirement,),
        effective_at=availability,
        owner="batch-d3-staging-topt-capture",
    )
    return D3E2CaptureContext(
        environment=capture_environment,
        registry=registry,
        source_entry=source_entry,
        requirement=requirement,
        scope=scope,
        applicability=applicability,
        source_coverage=source_coverage,
    )


def _verify_execution_binding(execution: D3E1TinyExecution, context: D3E2CaptureContext) -> None:
    result = execution.result
    response = execution.landed_raw_responses[-1].response
    if (
        result.capture_scope_id != context.scope.capture_scope_id
        or result.capture_scope_sha256 != context.scope.content_sha256
    ):
        raise ValueError("E1 call plan does not bind the frozen E2 scope")
    if (
        result.source_id != context.source_entry.source_id
        or result.source_version != context.source_entry.version
        or response.sha256 != result.normalized_bar.raw_response_sha256
    ):
        raise ValueError("E1 result does not bind the E2 source or raw bytes")


def _normalization_clock(
    execution: D3E1TinyExecution,
    *,
    fetched_at: datetime,
    recorded_at: datetime,
) -> tuple[datetime, datetime, datetime]:
    knowable_at = max(execution.result.normalized_bar.session_close_at, fetched_at)
    produced_at = knowable_at + timedelta(seconds=1)
    if recorded_at < produced_at:
        raise ValueError("raw landing time cannot precede deterministic normalization")
    return knowable_at, produced_at, recorded_at


def _normalize_execution(
    execution: D3E1TinyExecution,
    context: D3E2CaptureContext,
    *,
    predecessor: NormalizedRecordRef | None,
    fetched_at: datetime,
    recorded_at: datetime,
    ticker: str | None = None,
    calendar_id: str = "calendar.xnas",
) -> tuple[MarketPricePayload, NormalizedRecordRef]:
    result = execution.result
    bar = result.normalized_bar
    response = execution.landed_raw_responses[-1].response
    _verify_execution_binding(execution, context)

    knowable_at, produced_at, recorded_at = _normalization_clock(
        execution,
        fetched_at=fetched_at,
        recorded_at=recorded_at,
    )
    payload = MarketPricePayload(
        input_id=result.call_plan_id,
        issuer_id=bar.issuer_id,
        security_id=bar.security_id,
        listing_id=bar.listing_id,
        share_class="common",
        exchange_mic=bar.exchange_mic,
        ticker=ticker or bar.symbol,
        calendar_id=calendar_id,
        calendar_version=VERSION,
        trading_date=bar.trading_date,
        session_close_at=bar.session_close_at,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        volume=bar.volume,
        currency=bar.currency,
        price_basis=PriceBasis.UNADJUSTED,
        knowable_at=knowable_at,
        produced_at=produced_at,
        recorded_at=recorded_at,
        confidence=bar.confidence,
        confidence_policy_id=bar.confidence_policy_id,
        price_policy_id="price.unadjusted:v1",
    )
    type_entry = next(
        entry
        for entry in context.registry.semantic_types
        if entry.key == (context.requirement.semantic_type_id, VERSION)
    )
    draft = SemanticDraft(
        semantic_type_id=type_entry.semantic_type_id,
        semantic_type_version=type_entry.version,
        payload_model_key=type_entry.normalized_model_key,
        payload_schema_sha256=type_entry.schema_fingerprint_sha256,
        payload_sha256=canonical_sha256(payload.model_dump(mode="json")),
        subject=SubjectRef(kind=SubjectKind.LISTING, id=bar.listing_id),
        valid_from=bar.trading_date,
        valid_to=bar.trading_date,
        knowable_at=knowable_at,
        produced_at=produced_at,
        producer_kind=SemanticProducerKind.DETERMINISTIC_NORMALIZER,
        producer_id=context.source_entry.normalizer_id,
        producer_version=context.source_entry.normalizer_version,
        producer_implementation_sha256=context.source_entry.normalizer_implementation_sha256,
    )
    if predecessor is not None:
        if response.sha256 == predecessor.raw_object_sha256:
            raise ValueError("identical raw bytes cannot create a restatement")
        if predecessor.draft.subject != draft.subject or predecessor.draft.valid_from != draft.valid_from:
            raise ValueError("E2 predecessor belongs to another semantic coordinate")
        if knowable_at <= predecessor.draft.knowable_at:
            raise ValueError("E2 restatement must become knowable after its predecessor")
    record = NormalizedRecordRef(
        draft=draft,
        document_id=f"document:yahoo-chart:{bar.symbol.lower()}:{bar.trading_date.isoformat()}",
        raw_object_id=response.raw_object_id,
        raw_object_sha256=response.sha256,
        source_registry_entry_id=context.source_entry.source_registry_entry_id,
        source_registry_entry_sha256=context.source_entry.content_sha256,
        mapping_version="yahoo-chart-public:1.0.0",
        mapping_implementation_sha256=context.source_entry.normalizer_implementation_sha256,
        recorded_at=recorded_at,
        confidence=bar.confidence,
        is_restatement=predecessor is not None,
        supersedes_record_id=None if predecessor is None else predecessor.normalized_record_id,
    )
    return payload, record


def _build_manifest(
    context: D3E2CaptureContext,
    record: NormalizedRecordRef,
) -> tuple[CaptureManifest, CaptureEvaluationReport]:
    requirement = context.requirement
    evidence = CaptureRecordEvidence(
        source_coverage_entry_id=SOURCE_COVERAGE_ENTRY_ID,
        raw_id=f"raw.object:{record.raw_object_sha256}",
        raw_sha256=record.raw_object_sha256,
        normalized_id=record.normalized_record_id,
        semantic_type_id=record.draft.semantic_type_id,
        semantic_type_version=record.draft.semantic_type_version,
        populated_fields=requirement.required_fields,
        knowable_at=record.draft.knowable_at,
        recorded_at=record.recorded_at,
        valid_from=datetime.combine(record.draft.valid_from, time.min, UTC),
        valid_to=datetime.combine(record.draft.valid_to, time.max, UTC),
        confidence=record.confidence,
        mapping_version=record.mapping_version,
        policy_versions={
            requirement.freshness_policy_id: VERSION,
            requirement.partition_rule_id: VERSION,
        },
        quality_check_ids=requirement.quality_policy_ids,
        quality_status=QualityStatus.PASS,
        lineage_sha256=record.content_sha256,
    )
    cell = CaptureCell(
        subject=record.draft.subject,
        domain=requirement.domain,
        partition_key=PARTITION_KEY,
        capture_requirement_id=requirement.capture_requirement_id,
        applicability="required",
        status="complete",
        evidence=(evidence,),
    )
    manifest = CaptureManifest(
        capture_scope_id=context.scope.capture_scope_id,
        capture_scope_sha256=context.scope.content_sha256,
        environment=context.environment,
        research_catalog_id=context.scope.research_catalog_id,
        research_catalog_sha256=context.scope.research_catalog_sha256,
        applicability_catalog_id=context.scope.applicability_catalog_id,
        applicability_catalog_sha256=context.scope.applicability_catalog_sha256,
        source_coverage_catalog_id=context.scope.source_coverage_catalog_id,
        source_coverage_catalog_sha256=context.scope.source_coverage_catalog_sha256,
        slo_catalog_id=context.scope.slo_catalog_id,
        slo_catalog_sha256=context.scope.slo_catalog_sha256,
        source_registry_id=context.scope.source_registry_id,
        source_registry_sha256=context.scope.source_registry_sha256,
        semantic_type_registry_id=context.scope.semantic_type_registry_id,
        semantic_type_registry_sha256=context.scope.semantic_type_registry_sha256,
        partition_key=PARTITION_KEY,
        as_of=record.recorded_at,
        started_at=record.draft.knowable_at,
        cells=(cell,),
        created_at=record.recorded_at + timedelta(seconds=1),
    )
    evaluation = evaluate_capture_manifest(
        context.scope,
        manifest,
        applicability_catalog_id=context.scope.applicability_catalog_id,
        applicability_catalog_sha256=context.scope.applicability_catalog_sha256,
        applicability=context.applicability,
        source_coverage=context.source_coverage,
        evaluated_at=manifest.created_at + timedelta(seconds=1),
    )
    if not evaluation.ready:
        raise ValueError(f"D3 E2 capture is not ready: {evaluation.blocking_reason_codes}")
    return manifest, evaluation


@dataclass(frozen=True)
class D3E2PersistenceResult:
    raw_fetch_ids: tuple[int, ...]
    normalized_inserted: bool
    registry_inserted: bool
    scope_inserted: bool
    manifest_inserted: bool
    evaluation_inserted: bool
    payload: MarketPricePayload
    record: NormalizedRecordRef
    registry: RegistrySnapshot
    scope: CaptureScope
    manifest: CaptureManifest
    evaluation: CaptureEvaluationReport


def persist_e1_execution(
    connection: Connection[Any],
    raw_store: RawObjectStore,
    execution: D3E1TinyExecution,
    context: D3E2CaptureContext,
    *,
    predecessor: NormalizedRecordRef | None = None,
) -> D3E2PersistenceResult:
    """Persist one accepted E1 execution through immutable E2 stores."""

    if not execution.landed_raw_responses:
        raise ValueError("D3 E2 requires retained E1 raw bytes")
    _verify_execution_binding(execution, context)
    raw_fetch_ids: list[int] = []
    final_landing = execution.landed_raw_responses[-1]
    for landed in execution.landed_raw_responses:
        response = landed.response
        source_record_suffix = "result" if landed is final_landing else f"attempt:{response.attempt_number}"
        fetch_id = insert_fetch(
            connection,
            source=DataSource.YAHOO,
            source_record_id=f"{response.call_plan_id}:{source_record_suffix}",
            body=response.body,
            content_type=response.content_type or "application/octet-stream",
            fetched_at=response.fetched_at,
            metadata={
                "source_id": response.source_id,
                "source_version": response.source_version,
                "adapter_id": response.adapter_id,
                "adapter_version": response.adapter_version,
                "call_plan_id": response.call_plan_id,
                "configuration_sha256": response.configuration_sha256,
                "attempt_number": response.attempt_number,
                "landing_id": landed.landing_id,
                "e1_interaction_id": execution.result.interaction_id,
            },
            store=raw_store,
            recorded_at=response.fetched_at + timedelta(seconds=2),
        )
        if get_payload(connection, fetch_id, store=raw_store) != response.body:
            raise ValueError("persisted raw bytes failed checksum-verified readback")
        raw_fetch_ids.append(fetch_id)

    persisted_clock = connection.execute(
        "select fetched_at, recorded_at from raw.fetches where id = %s",
        (raw_fetch_ids[-1],),
    ).fetchone()
    if persisted_clock is None:
        raise ValueError("final raw fetch disappeared before normalization")
    payload, record = _normalize_execution(
        execution,
        context,
        predecessor=predecessor,
        fetched_at=persisted_clock[0],
        recorded_at=persisted_clock[1],
    )
    final_reference = raw_ref(raw_fetch_ids[-1])
    normalized_repository = PostgresMarketPriceRepository(connection)
    normalized_inserted = normalized_repository.put(record, payload, raw_reference=final_reference)
    if normalized_repository.payload_for(record.normalized_record_id) != payload:
        raise ValueError("persisted normalized payload failed replay")
    manifest, evaluation = _build_manifest(context, record)
    registry_inserted = PostgresRegistrySnapshotRepository(connection).put(context.registry)
    scope_inserted = PostgresCaptureScopeRepository(connection).put(context.scope)
    manifest_inserted = PostgresCaptureManifestRepository(connection).put(manifest)
    evaluation_inserted = PostgresCaptureEvaluationRepository(connection).put(evaluation)
    return D3E2PersistenceResult(
        raw_fetch_ids=tuple(raw_fetch_ids),
        normalized_inserted=normalized_inserted,
        registry_inserted=registry_inserted,
        scope_inserted=scope_inserted,
        manifest_inserted=manifest_inserted,
        evaluation_inserted=evaluation_inserted,
        payload=payload,
        record=record,
        registry=context.registry,
        scope=context.scope,
        manifest=manifest,
        evaluation=evaluation,
    )


__all__ = [
    "D3E2CaptureContext",
    "D3E2PersistenceResult",
    "build_e2_capture_context",
    "persist_e1_execution",
]
