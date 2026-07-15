"""D3 E3 full-denominator Yahoo interaction and persistence evidence."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal

import httpx
from data_engine.batches.mvp_medium_validation.e0_slice import PostgresMarketPriceRepository
from data_engine.batches.mvp_medium_validation.e3_slice import (
    D2E3Evidence,
    FrozenToptMarketFixture,
    ToptMarketRow,
    load_topt_market_fixture,
    run_d2_e3,
)
from data_engine.batches.staging_topt_capture.e0_slice import (
    ADAPTER_ID,
    ADAPTER_VERSION,
    EXPECTED_PARENT,
    SOURCE_ID,
    SOURCE_VERSION,
    FrozenYahooRequestPlan,
    YahooChartRequest,
    YahooChartSourceConfig,
    freeze_yahoo_request_plan,
)
from data_engine.batches.staging_topt_capture.e1_slice import (
    D3E1TinyExecution,
    D3E1TinyResult,
    InMemoryRawResponseLedger,
    YahooDailyBarNormalizer,
    YahooRawHttpAdapter,
    execute_e1_tiny_interaction,
)
from data_engine.batches.staging_topt_capture.e2_slice import D3E2CaptureContext, _normalize_execution
from data_engine.contract_repository import (
    PostgresCaptureEvaluationRepository,
    PostgresCaptureManifestRepository,
    PostgresCaptureScopeRepository,
    PostgresRegistrySnapshotRepository,
)
from data_engine.raw_store import get_payload, insert_fetch, raw_ref
from psycopg import Connection
from pydantic import BaseModel, ConfigDict, Field, model_validator
from truealpha_contracts import DataSource, RawObjectStore
from truealpha_contracts.capture_contracts import (
    CaptureCell,
    CaptureEvaluationReport,
    CaptureManifest,
    CaptureRecordEvidence,
    CaptureScope,
    SourceCoverageMapping,
    canonical_source_coverage_projection_sha256,
    evaluate_capture_manifest,
)
from truealpha_contracts.common import CaptureEnvironment, canonical_sha256
from truealpha_contracts.data_quality import DataDomain, QualityStatus
from truealpha_contracts.execution import NormalizedRecordRef
from truealpha_contracts.registries import RegistrySnapshot, SourceRegistryEntry

D3_E3_SOURCE_CALL_COUNT = 42
D3_E3_SOURCE_COVERAGE_ENTRY_ID = "source-coverage-entry:" + canonical_sha256(
    {"batch": "D3-staging-topt-capture", "rung": "E3", "source": SOURCE_ID}
)
E1_MODULE_PATH = Path("apps/data-engine/src/data_engine/batches/staging_topt_capture/e1_slice.py")
E3_MODULE_PATH = Path("apps/data-engine/src/data_engine/batches/staging_topt_capture/e3_slice.py")


class D3E3PlannedInteraction(BaseModel):
    """One predeclared source interaction at one evidence vintage."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    vintage: Literal["original", "changed"]
    plan: FrozenYahooRequestPlan


class D3E3YahooInteraction(BaseModel):
    """One completed E3 interaction bound to its predeclared plan."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    vintage: Literal["original", "changed"]
    plan_id: str = Field(pattern=r"^d3-yahoo-call-plan:[0-9a-f]{64}$")
    raw_fetch_id: int = Field(gt=0, strict=True)
    normalized_record_id: str = Field(pattern=r"^normalized-record:[0-9a-f]{64}$")
    result: D3E1TinyResult

    @model_validator(mode="after")
    def validate_plan_binding(self) -> D3E3YahooInteraction:
        if self.plan_id != self.result.call_plan_id:
            raise ValueError("D3 E3 result does not bind its predeclared plan")
        return self


class D3E3Evidence(BaseModel):
    """Content-addressed E3 evidence for all 84 cells and two vintages."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str = Field(default="", pattern=r"^(?:|d3-e3-evidence:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    batch_id: Literal["D3-staging-topt-capture"] = "D3-staging-topt-capture"
    environment: Literal["local", "ci"]
    accepted_d2_evidence: D2E3Evidence
    registry: RegistrySnapshot
    capture_scope: CaptureScope
    capture_manifests: tuple[CaptureManifest, CaptureManifest]
    capture_evaluations: tuple[CaptureEvaluationReport, CaptureEvaluationReport]
    interactions: tuple[D3E3YahooInteraction, ...] = Field(
        min_length=D3_E3_SOURCE_CALL_COUNT,
        max_length=D3_E3_SOURCE_CALL_COUNT,
    )
    xom_predecessor_cik: Literal["0000034088"]
    stable_handoff: Literal[False] = False
    staging_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_full_denominator(self) -> D3E3Evidence:
        parent = self.accepted_d2_evidence
        if parent.evidence_id != EXPECTED_PARENT["d2_e3_evidence_id"]:
            raise ValueError("D3 E3 does not bind the accepted D2 E3 evidence")
        if self.environment != parent.environment:
            raise ValueError("D3 E3 environment differs from its persistence evidence")
        if (
            len(parent.denominator.instruments) != 21
            or len({item.issuer_lei for item in parent.denominator.instruments}) != 20
            or len(parent.capture_plan.cells) != 84
            or len(parent.capture_manifests) != 2
            or any(len(manifest.cells) != 84 for manifest in parent.capture_manifests)
            or any(not report.ready for report in parent.capture_evaluations)
        ):
            raise ValueError("D3 E3 persistence evidence is not 84-cell complete")
        if (
            self.capture_scope.capture_scope_id == parent.capture_plan.scope.capture_scope_id
            or self.capture_scope.source_registry_id != self.registry.source_registry_snapshot_id
            or self.capture_scope.source_registry_sha256 != self.registry.source_registry_sha256
        ):
            raise ValueError("D3 E3 scope did not replace the fixture-only source registry")

        by_vintage: dict[str, list[D3E3YahooInteraction]] = defaultdict(list)
        for interaction in self.interactions:
            by_vintage[interaction.vintage].append(interaction)
            result = interaction.result
            if (
                result.capture_scope_id != self.capture_scope.capture_scope_id
                or result.capture_scope_sha256 != self.capture_scope.content_sha256
                or result.source_call_count != 1
                or result.landed_response_count != 1
                or not result.raw_bytes_retained
            ):
                raise ValueError("D3 E3 interaction lost its scope or raw-response binding")
        if {name: len(rows) for name, rows in by_vintage.items()} != {"original": 21, "changed": 21}:
            raise ValueError("D3 E3 requires 21 interactions at each vintage")
        if (
            len({item.raw_fetch_id for item in self.interactions}) != D3_E3_SOURCE_CALL_COUNT
            or len({item.normalized_record_id for item in self.interactions}) != D3_E3_SOURCE_CALL_COUNT
        ):
            raise ValueError("D3 E3 raw or normalized persistence collapsed a vintage")

        expected_security_ids = {item.security_id for item in parent.denominator.instruments}
        for rows in by_vintage.values():
            if {item.result.normalized_bar.security_id for item in rows} != expected_security_ids:
                raise ValueError("D3 E3 source denominator shrank or changed identity")
        originals = {item.result.normalized_bar.listing_id: item for item in by_vintage["original"]}
        changed = {item.result.normalized_bar.listing_id: item for item in by_vintage["changed"]}
        if len(originals) != 21 or set(originals) != set(changed):
            raise ValueError("D3 E3 listing denominator changed between vintages")
        for listing_id, original in originals.items():
            later = changed[listing_id]
            original_bar = original.result.normalized_bar
            later_bar = later.result.normalized_bar
            if original.result.raw_object_ids == later.result.raw_object_ids:
                raise ValueError("D3 E3 changed vintage reused identical raw bytes")
            if original_bar.model_dump(exclude={"raw_response_sha256"}) != later_bar.model_dump(
                exclude={"raw_response_sha256"}
            ):
                raise ValueError("D3 E3 changed bytes altered the frozen semantic projection")

        alphabet = {
            item.result.normalized_bar.symbol: item.result.normalized_bar
            for item in by_vintage["original"]
            if item.result.normalized_bar.symbol in {"GOOG", "GOOGL"}
        }
        if (
            set(alphabet) != {"GOOG", "GOOGL"}
            or alphabet["GOOG"].issuer_id != alphabet["GOOGL"].issuer_id
            or alphabet["GOOG"].security_id == alphabet["GOOGL"].security_id
        ):
            raise ValueError("D3 E3 collapsed the Alphabet sibling instruments")

        manifests = tuple(sorted(self.capture_manifests, key=lambda item: item.as_of))
        evaluations = {item.capture_manifest_id: item for item in self.capture_evaluations}
        if len(manifests) != 2 or len(evaluations) != 2:
            raise ValueError("D3 E3 requires two distinct manifests and evaluations")
        for manifest in manifests:
            evaluation = evaluations.get(manifest.capture_manifest_id)
            if (
                manifest.capture_scope_id != self.capture_scope.capture_scope_id
                or manifest.capture_scope_sha256 != self.capture_scope.content_sha256
                or len(manifest.cells) != 84
                or evaluation is None
                or not evaluation.ready
            ):
                raise ValueError("D3 E3 manifest is not blocker-free and 84-cell complete")
        manifest_market_ids = {
            evidence.normalized_id
            for manifest in manifests
            for cell in manifest.cells
            if cell.domain is DataDomain.MARKET_PRICES
            for evidence in cell.evidence
        }
        if manifest_market_ids != {item.normalized_record_id for item in self.interactions}:
            raise ValueError("D3 E3 manifests do not bind every Yahoo normalized vintage")

        interactions = tuple(
            sorted(self.interactions, key=lambda item: (item.vintage, item.result.normalized_bar.security_id))
        )
        object.__setattr__(self, "interactions", interactions)
        object.__setattr__(self, "capture_manifests", manifests)
        object.__setattr__(
            self,
            "capture_evaluations",
            tuple(sorted(self.capture_evaluations, key=lambda item: item.capture_manifest_id)),
        )
        payload = self.model_dump(mode="json", exclude={"evidence_id", "content_sha256"})
        digest = canonical_sha256(payload)
        if self.content_sha256 and self.content_sha256 != digest:
            raise ValueError("D3 E3 evidence content hash mismatch")
        if self.evidence_id and self.evidence_id != f"d3-e3-evidence:{digest}":
            raise ValueError("D3 E3 evidence ID mismatch")
        object.__setattr__(self, "content_sha256", digest)
        object.__setattr__(self, "evidence_id", f"d3-e3-evidence:{digest}")
        return self


def _exchange_metadata(exchange_mic: str) -> tuple[str, str]:
    try:
        return {
            "XNAS": ("NMS", "NasdaqGS"),
            "XNYS": ("NYQ", "NYSE"),
        }[exchange_mic]
    except KeyError as error:
        raise ValueError(f"unsupported D3 E3 exchange MIC: {exchange_mic}") from error


def build_e3_yahoo_response(row: ToptMarketRow, *, changed: bool) -> bytes:
    """Build exact mocked Yahoo bytes without converting Decimal values to float."""

    exchange_name, full_exchange_name = _exchange_metadata(row.exchange_mic)
    meta = json.dumps(
        {
            "currency": "USD",
            "symbol": row.vendor_symbol,
            "exchangeName": exchange_name,
            "fullExchangeName": full_exchange_name,
            "instrumentType": "EQUITY",
            "exchangeTimezoneName": "America/New_York",
            "gmtoffset": -14400,
        },
        separators=(",", ":"),
    )
    body = (
        '{"chart":{"result":[{"meta":'
        + meta
        + f',"timestamp":[{int(row.session_close_at.timestamp())}],"indicators":{{"quote":[{{'
        + f'"open":[{row.open}],"high":[{row.high}],"low":[{row.low}],'
        + f'"close":[{row.close}],"volume":[{row.volume}]'
        + f'}}],"adjclose":[{{"adjclose":[{row.close}]}}]}}}}],"error":null}}}}'
    ).encode()
    return body + (b"\n" if changed else b"")


def build_e3_call_plans(
    scope: CaptureScope,
    fixture: FrozenToptMarketFixture,
) -> tuple[D3E3PlannedInteraction, ...]:
    """Freeze both 21-symbol vintages before the first E3 network call."""

    plans: list[D3E3PlannedInteraction] = []
    for vintage in ("original", "changed"):
        for row in fixture.rows:
            request = YahooChartRequest(
                source_id="source.yahoo-chart-public",
                source_version="1.0.0",
                adapter_id="data_engine.d3.yahoo_chart_adapter",
                adapter_version="1.0.0",
                semantic_type_id="semantic.market-price",
                semantic_type_version="1.0.0",
                issuer_id=f"issuer:lei:{row.issuer_lei}",
                security_id=row.security_id,
                listing_id=row.listing_id,
                symbol=row.vendor_symbol,
                exchange_mic=row.exchange_mic,
                currency="USD",
                query_start=row.trading_date,
                query_end_exclusive=row.trading_date + timedelta(days=1),
                expected_trading_date=row.trading_date,
                maximum_attempts=3,
            )
            plans.append(
                D3E3PlannedInteraction(
                    vintage=vintage,
                    plan=freeze_yahoo_request_plan(
                        request,
                        capture_scope_id=scope.capture_scope_id,
                        capture_scope_sha256=scope.content_sha256,
                    ),
                )
            )
    if len(plans) != D3_E3_SOURCE_CALL_COUNT:
        raise ValueError("D3 E3 call plan does not contain both 21-symbol vintages")
    return tuple(plans)


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build_e3_context(
    repository_root: Path,
    parent: D2E3Evidence,
    *,
    environment: Literal["local", "ci"],
) -> tuple[RegistrySnapshot, D3E2CaptureContext]:
    parent_registry = parent.snapshots[0].registry_snapshot
    market_requirement = next(
        requirement
        for requirement in parent.capture_plan.scope.requirements
        if requirement.domain is DataDomain.MARKET_PRICES
    )
    market_type = next(
        entry
        for entry in parent_registry.semantic_types
        if entry.key == (market_requirement.semantic_type_id, market_requirement.semantic_type_version)
    )
    source_entry = SourceRegistryEntry(
        source_id=SOURCE_ID,
        version=SOURCE_VERSION,
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        normalizer_id="data_engine.d3.yahoo_persistence_normalizer",
        normalizer_version=SOURCE_VERSION,
        supported_domains=(DataDomain.MARKET_PRICES,),
        supported_type_ids=(market_type.semantic_type_id,),
        configuration_schema_sha256=canonical_sha256(YahooChartSourceConfig.model_json_schema()),
        mapping_schema_sha256=market_type.schema_fingerprint_sha256,
        adapter_implementation_sha256=_file_sha256(repository_root / E1_MODULE_PATH),
        normalizer_implementation_sha256=_file_sha256(repository_root / E3_MODULE_PATH),
    )
    registry = RegistrySnapshot(
        parent_snapshot_id=parent_registry.registry_snapshot_id,
        sources=(*parent_registry.sources, source_entry),
        semantic_types=parent_registry.semantic_types,
        identifier_types=parent_registry.identifier_types,
        required_type_ids=parent_registry.required_type_ids,
        required_identifier_type_ids=parent_registry.required_identifier_type_ids,
    )
    applicability = parent.capture_plan.applicability_mapping()
    source_coverage: SourceCoverageMapping = {
        key: (D3_E3_SOURCE_COVERAGE_ENTRY_ID,) if key[3] is DataDomain.MARKET_PRICES else values
        for key, values in parent.capture_plan.source_coverage_mapping().items()
    }
    parent_scope = parent.capture_plan.scope
    scope = CaptureScope(
        research_catalog_id=parent_scope.research_catalog_id,
        research_catalog_sha256=parent_scope.research_catalog_sha256,
        universe=parent_scope.universe,
        applicability_catalog_id=parent_scope.applicability_catalog_id,
        applicability_catalog_sha256=parent_scope.applicability_catalog_sha256,
        applicability_projection_sha256=parent_scope.applicability_projection_sha256,
        source_coverage_catalog_id=parent_scope.source_coverage_catalog_id,
        source_coverage_catalog_sha256=parent_scope.source_coverage_catalog_sha256,
        source_coverage_projection_sha256=canonical_source_coverage_projection_sha256(source_coverage),
        slo_catalog_id=parent_scope.slo_catalog_id,
        slo_catalog_sha256=parent_scope.slo_catalog_sha256,
        source_registry_id=registry.source_registry_snapshot_id,
        source_registry_sha256=registry.source_registry_sha256,
        semantic_type_registry_id=registry.semantic_type_registry_snapshot_id,
        semantic_type_registry_sha256=registry.semantic_type_registry_sha256,
        requirements=parent_scope.requirements,
        effective_at=parent_scope.effective_at,
        owner="batch-d3-staging-topt-capture",
    )
    capture_environment = {
        "local": CaptureEnvironment.LOCAL_TEST,
        "ci": CaptureEnvironment.GITHUB_CI,
    }[environment]
    return registry, D3E2CaptureContext(
        environment=capture_environment,
        registry=registry,
        source_entry=source_entry,
        requirement=market_requirement,
        scope=scope,
        applicability=applicability,
        source_coverage=source_coverage,
    )


def _persist_execution(
    connection: Connection[Any],
    raw_store: RawObjectStore,
    execution: D3E1TinyExecution,
    context: D3E2CaptureContext,
    *,
    vintage: Literal["original", "changed"],
    predecessor: NormalizedRecordRef | None,
    ticker: str,
    calendar_id: str,
) -> tuple[int, NormalizedRecordRef]:
    response = execution.landed_raw_responses[-1].response
    fetch_id = insert_fetch(
        connection,
        source=DataSource.YAHOO,
        source_record_id=f"{response.call_plan_id}:{vintage}:result",
        body=response.body,
        content_type=response.content_type,
        fetched_at=response.fetched_at,
        metadata={
            "source_id": response.source_id,
            "source_version": response.source_version,
            "adapter_id": response.adapter_id,
            "adapter_version": response.adapter_version,
            "call_plan_id": response.call_plan_id,
            "configuration_sha256": response.configuration_sha256,
            "attempt_number": response.attempt_number,
            "interaction_id": execution.result.interaction_id,
            "vintage": vintage,
        },
        store=raw_store,
        recorded_at=response.fetched_at + timedelta(seconds=2),
    )
    if get_payload(connection, fetch_id, store=raw_store) != response.body:
        raise ValueError("D3 E3 raw response failed checksum-verified readback")
    persisted_clock = connection.execute(
        "select fetched_at, recorded_at from raw.fetches where id = %s",
        (fetch_id,),
    ).fetchone()
    if persisted_clock is None:
        raise ValueError("D3 E3 raw response disappeared before normalization")
    payload, record = _normalize_execution(
        execution,
        context,
        predecessor=predecessor,
        fetched_at=persisted_clock[0],
        recorded_at=persisted_clock[1],
        ticker=ticker,
        calendar_id=calendar_id,
    )
    repository = PostgresMarketPriceRepository(connection)
    repository.put(record, payload, raw_reference=raw_ref(fetch_id))
    if repository.payload_for(record.normalized_record_id) != payload:
        raise ValueError("D3 E3 normalized price failed immutable readback")
    return fetch_id, record


def _market_evidence(
    context: D3E2CaptureContext,
    *,
    fetch_id: int,
    record: NormalizedRecordRef,
) -> CaptureRecordEvidence:
    requirement = context.requirement
    return CaptureRecordEvidence(
        source_coverage_entry_id=D3_E3_SOURCE_COVERAGE_ENTRY_ID,
        raw_id=raw_ref(fetch_id),
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
            requirement.freshness_policy_id: SOURCE_VERSION,
            requirement.partition_rule_id: SOURCE_VERSION,
        },
        quality_check_ids=requirement.quality_policy_ids,
        quality_status=QualityStatus.PASS,
        lineage_sha256=record.content_sha256,
    )


def _build_e3_manifest(
    parent: D2E3Evidence,
    parent_manifest: CaptureManifest,
    context: D3E2CaptureContext,
    persisted: dict[str, tuple[int, NormalizedRecordRef]],
) -> tuple[CaptureManifest, CaptureEvaluationReport]:
    plan_by_key = {cell.capture_key: cell for cell in parent.capture_plan.cells}
    cells: list[CaptureCell] = []
    for cell in parent_manifest.cells:
        if cell.domain is not DataDomain.MARKET_PRICES:
            cells.append(cell)
            continue
        plan_cell = plan_by_key[cell.key]
        fetch_id, record = persisted[plan_cell.instrument.id]
        cells.append(
            CaptureCell(
                subject=cell.subject,
                domain=cell.domain,
                partition_key=cell.partition_key,
                capture_requirement_id=cell.capture_requirement_id,
                applicability=cell.applicability,
                status="complete",
                evidence=(_market_evidence(context, fetch_id=fetch_id, record=record),),
            )
        )
    latest_recorded_at = max(record.recorded_at for _, record in persisted.values())
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
        partition_key=parent_manifest.partition_key,
        as_of=max(parent_manifest.as_of, max(record.draft.knowable_at for _, record in persisted.values())),
        started_at=parent_manifest.started_at,
        cells=tuple(cells),
        created_at=max(parent_manifest.created_at, latest_recorded_at) + timedelta(seconds=1),
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
        raise ValueError(f"D3 E3 manifest is not ready: {evaluation.blocking_reason_codes}")
    return manifest, evaluation


def _verify_bar(row: ToptMarketRow, result: D3E1TinyResult) -> None:
    bar = result.normalized_bar
    if (
        bar.issuer_id != f"issuer:lei:{row.issuer_lei}"
        or bar.security_id != row.security_id
        or bar.listing_id != row.listing_id
        or bar.symbol != row.vendor_symbol
        or bar.exchange_mic != row.exchange_mic
        or bar.trading_date != row.trading_date
        or bar.session_close_at != row.session_close_at
        or (bar.open, bar.high, bar.low, bar.close, bar.volume)
        != (row.open, row.high, row.low, row.close, row.volume)
    ):
        raise ValueError("D3 E3 Yahoo result drifted from the frozen TOPT coordinate")


def run_d3_e3(
    repository_root: Path,
    connection: Connection[Any],
    raw_store: RawObjectStore,
    client: httpx.Client,
    *,
    environment: Literal["local", "ci"],
) -> D3E3Evidence:
    """Run all E3 interactions atomically with the accepted 84-cell data plane."""

    with connection.transaction():
        parent = run_d2_e3(
            repository_root,
            connection,
            raw_store,
            environment=environment,
        )
        fixture = load_topt_market_fixture(repository_root, parent.denominator)
        registry, context = _build_e3_context(repository_root, parent, environment=environment)
        plans = build_e3_call_plans(context.scope, fixture)
        registry_repository = PostgresRegistrySnapshotRepository(connection)
        scope_repository = PostgresCaptureScopeRepository(connection)
        registry_repository.put(registry)
        scope_repository.put(context.scope)
        if (
            registry_repository.get(registry.registry_snapshot_id) != registry
            or scope_repository.get(context.scope.capture_scope_id) != context.scope
        ):
            raise ValueError("D3 E3 registry or scope failed immutable readback")
        rows_by_security = {row.security_id: row for row in fixture.rows}
        interactions: list[D3E3YahooInteraction] = []
        source_call_counts: Counter[str] = Counter()
        persisted_by_vintage: dict[str, dict[str, tuple[int, NormalizedRecordRef]]] = {
            "original": {},
            "changed": {},
        }
        for planned in plans:
            row = rows_by_security[planned.plan.request.security_id]
            fetched_at = fixture.price_source.retrieved_at + (
                timedelta(days=1) if planned.vintage == "changed" else timedelta()
            )

            def clock(fetched_at: datetime = fetched_at) -> datetime:
                return fetched_at

            execution = execute_e1_tiny_interaction(
                planned.plan,
                adapter=YahooRawHttpAdapter(client),
                normalizer=YahooDailyBarNormalizer(),
                raw_ledger=InMemoryRawResponseLedger(),
                clock=clock,
            )
            _verify_bar(row, execution.result)
            predecessor = (
                None
                if planned.vintage == "original"
                else persisted_by_vintage["original"][row.security_id][1]
            )
            fetch_id, record = _persist_execution(
                connection,
                raw_store,
                execution,
                context,
                vintage=planned.vintage,
                predecessor=predecessor,
                ticker=row.ticker,
                calendar_id=f"calendar.{row.exchange_mic.lower()}",
            )
            persisted_by_vintage[planned.vintage][row.security_id] = (fetch_id, record)
            source_call_counts[row.security_id] += execution.result.source_call_count
            interactions.append(
                D3E3YahooInteraction(
                    vintage=planned.vintage,
                    plan_id=planned.plan.call_plan_id,
                    raw_fetch_id=fetch_id,
                    normalized_record_id=record.normalized_record_id,
                    result=execution.result,
                )
            )
        if source_call_counts != Counter({row.security_id: 2 for row in fixture.rows}):
            raise ValueError("D3 E3 source-call counts do not match the frozen budget")
        parent_manifests = tuple(sorted(parent.capture_manifests, key=lambda item: item.as_of))
        manifests_and_evaluations = tuple(
            _build_e3_manifest(parent, parent_manifest, context, persisted_by_vintage[vintage])
            for vintage, parent_manifest in zip(("original", "changed"), parent_manifests, strict=True)
        )
        manifest_repository = PostgresCaptureManifestRepository(connection)
        evaluation_repository = PostgresCaptureEvaluationRepository(connection)
        for manifest, evaluation in manifests_and_evaluations:
            manifest_repository.put(manifest)
            evaluation_repository.put(evaluation)
            if (
                manifest_repository.get(manifest.capture_manifest_id) != manifest
                or evaluation_repository.get(evaluation.capture_evaluation_report_id) != evaluation
            ):
                raise ValueError("D3 E3 manifest or evaluation failed immutable readback")
        return D3E3Evidence(
            environment=environment,
            accepted_d2_evidence=parent,
            registry=registry,
            capture_scope=context.scope,
            capture_manifests=(manifests_and_evaluations[0][0], manifests_and_evaluations[1][0]),
            capture_evaluations=(manifests_and_evaluations[0][1], manifests_and_evaluations[1][1]),
            interactions=tuple(interactions),
            xom_predecessor_cik=fixture.xom_report_date_identity.predecessor_cik,
        )


__all__ = [
    "D3E3Evidence",
    "D3E3PlannedInteraction",
    "D3E3YahooInteraction",
    "build_e3_call_plans",
    "build_e3_yahoo_response",
    "run_d3_e3",
]
