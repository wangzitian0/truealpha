"""Terminal E3 validation of the D2 data plane on the exact TOPT denominator."""

import ast
import hashlib
import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast

import dagster as dg
from dagster import AssetExecutionContext
from data_engine.batches.mvp_medium_validation.e0_slice import SEMANTIC_TYPE_ID as MARKET_PRICE_TYPE_ID
from data_engine.batches.mvp_medium_validation.e1_slice import FrozenE1Corpus, load_e1_corpus
from data_engine.batches.mvp_medium_validation.e2_slice import (
    D2_E2_SCHEMA_EPOCH,
    FrozenMediumCaptureConfiguration,
    MvpMediumValidationHandoff,
    _capture,
    _draft,
    _policy_bindings,
    _raw_object_ref,
    run_d2_e2,
)
from data_engine.contract_repository import (
    PostgresCaptureEvaluationRepository,
    PostgresCaptureManifestRepository,
    PostgresCaptureScopeRepository,
    PostgresSnapshotRepository,
)
from data_engine.mvp_medium_models import MarketPricePayload
from data_engine.mvp_medium_pipeline import (
    LandedMediumCapture,
    MediumAdapterRegistration,
    MediumCaptureBatch,
    MediumCaptureWorkItem,
    MediumComponentCatalog,
    MediumNormalizerRegistration,
    land_medium_capture_plan,
    normalize_medium_capture_batch,
)
from data_engine.mvp_medium_registry import (
    ISSUER_SECURITY_TYPE_ID,
    MEDIUM_VERSION,
    SECURITY_LISTING_TYPE_ID,
    UNIVERSE_MEMBERSHIP_TYPE_ID,
)
from data_engine.mvp_medium_repository import (
    PostgresMediumSemanticRepository,
    build_medium_repository_registrations,
)
from data_engine.mvp_medium_snapshot import PostgresMediumSnapshotResolver, build_medium_snapshot
from data_engine.raw_store import get_payload
from psycopg import Connection
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from truealpha_contracts import (
    ApplicabilityMapping,
    CaptureCell,
    CaptureEvaluationReport,
    CaptureManifest,
    CaptureRecordEvidence,
    CaptureRequirement,
    CaptureScope,
    DataRequirement,
    DataSource,
    IssuerSecurityLink,
    ListingRole,
    PlannedDemandCell,
    RawObjectStore,
    RequirementLevel,
    SecurityKind,
    SecurityListingLink,
    SourceCoverageMapping,
    UniverseDefinitionKind,
    UniverseManifest,
    UniverseMembership,
    UsageStage,
    canonical_applicability_projection_sha256,
    canonical_source_coverage_projection_sha256,
    compile_capture_requirement_bindings,
    evaluate_capture_manifest,
)
from truealpha_contracts.common import CaptureEnvironment, canonical_sha256
from truealpha_contracts.data_quality import DataDomain, QualityStatus
from truealpha_contracts.execution import NormalizedRecordRef, SnapshotDemandCell, SnapshotManifest, SnapshotRequest
from truealpha_contracts.registries import RegistrySnapshot, SourceRegistryEntry
from truealpha_contracts.release import ReleaseManifest
from truealpha_contracts.universe import SubjectKind, SubjectRef

D2_E3_ASSET_NAME = "mvp_medium_validation_e3_terminal_evidence"
D2_E2_RUNTIME_HANDOFF_SHA256 = "e6c1206786f3dbbb79171f49e34555e7fadd96d7182752aa1e45a124825587a3"
D2_E2_RUNTIME_HANDOFF_ID = f"mvp-medium-validation-handoff:{D2_E2_RUNTIME_HANDOFF_SHA256}"
D2_E2_GOVERNANCE_HANDOFF_PATH = Path("governance/handoffs/D2-mvp-medium-validation.v1.json")
D2_E2_GOVERNANCE_HANDOFF_SHA256 = "0031707dbdf97b1a5d45a4ebafa5f1029bf8567e157e3d12f1ecde26b73390bb"
D2_E2_GOVERNANCE_HANDOFF_ID = (
    "handoff:d2-mvp-medium-validation:46162a55a54ba053b3effef97a95e6662c5da4052ca3ef656fd9440cb58b73be"
)
TOPT_ARTIFACT_SHA256 = "d0b2865cbde85181bb17801ac3be467c5049906f793876c8b6ac319b7525cc5a"
TOPT_UNIVERSE_ID = "universe:topt-us-2026-03-31"
TOPT_ACCESSION = "000207169126012475"
TOPT_PRIMARY_DOCUMENT_SHA256 = "7e46eb6babead70230986162349bb33f27d7af2a51a095b5850340aa0a534934"
TOPT_REPORT_DATE = date(2026, 3, 31)
TOPT_ISSUER_COUNT = 20
TOPT_INSTRUMENT_COUNT = 21
TOPT_REQUIRED_CELL_COUNT = 84
TOPT_NORMALIZED_RECORD_COUNT = 168
TOPT_MARKET_FIXTURE_PATH = Path("apps/data-engine/tests/fixtures/mvp_medium_validation/topt_market_2026-03-31.v1.json")
TOPT_MARKET_FIXTURE_SHA256 = "7a160dbd1a5816d0c31e20bd1f0e1ab8d2738e1fc744fc7bf96fa2903d19e038"
TOPT_ISSUER_SOURCE_ID = "source.fixture-topt-sec-issuer-security"
TOPT_LISTING_SOURCE_ID = "source.fixture-topt-sec-listing"
TOPT_MEMBERSHIP_SOURCE_ID = "source.fixture-topt-sec-membership"
TOPT_PRICE_SOURCE_ID = "source.fixture-topt-yahoo-market"
TOPT_CAPTURE_PARTITION = TOPT_UNIVERSE_ID
TOPT_CAPTURE_PARTITION_POLICY_ID = "partition.d2-e3-topt-universe:v1"
TOPT_CAPTURE_FRESHNESS_POLICY_ID = "freshness.d2-e3-topt-vintage:v1"
TOPT_CAPTURE_QUALITY_POLICY_IDS = (
    "quality.d2-e3-pit-lineage:v1",
    "quality.d2-e3-required-fields:v1",
)
TOPT_SOURCE_IMPLEMENTATION_SHA256 = "e0509b3bb93982bc0ed29776518612f1c470efd0095b2373eba0358032d8eac1"
TOPT_SOURCE_CODE_SHA256 = "8df0c1b834089c9aded19213b0fe3133fe69fa0787f26daa74f3c839911914de"


def _sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _capture_instrument_ref(instrument: SubjectRef) -> SubjectRef:
    if instrument.kind is not SubjectKind.SECURITY:
        raise ValueError("D2 E3 capture instruments must be canonical security subjects")
    return SubjectRef(
        kind=SubjectKind.SECURITY,
        id="security-ref:" + canonical_sha256(instrument.model_dump(mode="json")),
    )


class ToptInstrument(BaseModel):
    """One selected security line copied from the frozen TOPT filing context."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str = Field(min_length=1)
    cusip: str = Field(pattern=r"^[0-9A-Z]{9}$")
    issuer_lei: str = Field(pattern=r"^[0-9A-Z]{20}$")
    filing_weight_percent: Decimal = Field(gt=0, le=100)

    @field_validator("ticker")
    @classmethod
    def validate_ticker(cls, value: str) -> str:
        if value != value.strip() or any(character.isspace() for character in value):
            raise ValueError("TOPT ticker must be non-empty and cannot contain whitespace")
        return value

    @field_validator("filing_weight_percent", mode="before")
    @classmethod
    def reject_binary_float(cls, value: Any) -> Any:
        if isinstance(value, (float, bool)):
            raise ValueError("TOPT filing weights must be exact Decimal literals")
        return value

    @property
    def issuer_id(self) -> str:
        return f"issuer:lei:{self.issuer_lei}"

    @property
    def security_id(self) -> str:
        return f"security:cusip:{self.cusip}"

    @property
    def identity_document_id(self) -> str:
        return f"identity-link:{self.issuer_id}:{self.security_id}"

    @property
    def membership_id(self) -> str:
        return f"membership:{TOPT_UNIVERSE_ID}:{self.security_id}"


class ToptMarketScope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    universe_id: Literal["universe:topt-us-2026-03-31"]
    accession: Literal["000207169126012475"]
    report_date: date
    issuer_count: Literal[20]
    instrument_count: Literal[21]

    @model_validator(mode="after")
    def validate_report_date(self) -> "ToptMarketScope":
        if self.report_date != TOPT_REPORT_DATE:
            raise ValueError("TOPT market fixture report date drifted")
        return self


class ToptListingSource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: Literal["sec"]
    url: Literal["https://www.sec.gov/files/company_tickers_exchange.json"]
    retrieved_at: datetime
    response_sha256: Literal["9e7294ccfe77ceeae03b3d59040fc8e24dbd78c9df286a36238b58ebbadf6106"]


class ToptPriceSource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: Literal["yahoo"]
    client: Literal["yfinance"]
    client_version: Literal["1.5.1"]
    query_start: date
    query_end_exclusive: date
    retrieved_at: datetime
    auto_adjust: Literal[False]
    price_basis: Literal["unadjusted"]

    @model_validator(mode="after")
    def validate_query_window(self) -> "ToptPriceSource":
        if self.query_start != TOPT_REPORT_DATE or self.query_end_exclusive != date(2026, 4, 2):
            raise ValueError("TOPT price query window drifted")
        return self


class ToptXomReportDateIdentity(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    predecessor_cik: Literal["0000034088"]
    successor_cik: Literal["0002115436"]
    successor_effective_date: date
    successor_trading_date: date
    predecessor_filing: Literal["https://www.sec.gov/Archives/edgar/data/34088/000119312526291986/d70995d8k.htm"]
    successor_filing: Literal["https://www.sec.gov/Archives/edgar/data/2115436/000119312526291990/d71068d8k12b.htm"]

    @model_validator(mode="after")
    def validate_transition(self) -> "ToptXomReportDateIdentity":
        if (
            self.successor_effective_date != date(2026, 7, 1)
            or self.successor_trading_date != date(2026, 7, 2)
            or self.successor_effective_date <= TOPT_REPORT_DATE
        ):
            raise ValueError("XOM report-date predecessor interval drifted")
        return self


class ToptMarketRow(BaseModel):
    """One exact report-date listing and unadjusted daily price observation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cusip: str = Field(pattern=r"^[0-9A-Z]{9}$")
    issuer_lei: str = Field(pattern=r"^[0-9A-Z]{20}$")
    issuer_cik_at_report_date: str = Field(pattern=r"^[0-9]{10}$")
    ticker: str = Field(pattern=r"^[A-Z.]+$")
    vendor_symbol: str = Field(pattern=r"^[A-Z.-]+$")
    exchange: Literal["Nasdaq", "NYSE"]
    exchange_mic: Literal["XNAS", "XNYS"]
    listing_id: str = Field(pattern=r"^listing:(?:xnas|xnys):[a-z.]+$")
    trading_date: date
    session_close_at: datetime
    open: Decimal = Field(gt=0)
    high: Decimal = Field(gt=0)
    low: Decimal = Field(gt=0)
    close: Decimal = Field(gt=0)
    volume: int = Field(ge=0)

    @field_validator("open", "high", "low", "close", mode="before")
    @classmethod
    def reject_binary_float(cls, value: Any) -> Any:
        if isinstance(value, (float, bool)):
            raise ValueError("TOPT prices must be exact Decimal literals")
        return value

    @model_validator(mode="after")
    def validate_listing_and_price(self) -> "ToptMarketRow":
        expected_mic = {"Nasdaq": "XNAS", "NYSE": "XNYS"}[self.exchange]
        if self.exchange_mic != expected_mic:
            raise ValueError("TOPT exchange and MIC disagree")
        if self.listing_id != f"listing:{self.exchange_mic.lower()}:{self.ticker.lower()}":
            raise ValueError("TOPT listing identity drifted from MIC and ticker")
        expected_vendor_symbol = "BRK-B" if self.ticker == "BRK.B" else self.ticker
        if self.vendor_symbol != expected_vendor_symbol:
            raise ValueError("TOPT Yahoo symbol mapping drifted")
        if self.trading_date != TOPT_REPORT_DATE:
            raise ValueError("TOPT market row is outside the frozen report date")
        if self.session_close_at != datetime(2026, 3, 31, 20, tzinfo=UTC):
            raise ValueError("TOPT market row is not bound to the U.S. session close")
        if self.high < max(self.open, self.close, self.low) or self.low > min(
            self.open,
            self.close,
            self.high,
        ):
            raise ValueError("TOPT OHLC ordering is invalid")
        return self

    @property
    def security_id(self) -> str:
        return f"security:cusip:{self.cusip}"

    @property
    def listing_document_id(self) -> str:
        return f"identity-link:{self.security_id}:{self.listing_id}"

    @property
    def price_document_id(self) -> str:
        return f"price:{self.listing_id}:{self.trading_date.isoformat()}"


class FrozenToptMarketFixture(BaseModel):
    """Content-addressed 21-listing/21-price packet for D2 Local/CI."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1]
    fixture_id: Literal["d2-topt-market-2026-03-31-v1"]
    scope: ToptMarketScope
    listing_source: ToptListingSource
    price_source: ToptPriceSource
    xom_report_date_identity: ToptXomReportDateIdentity
    rows: tuple[ToptMarketRow, ...] = Field(
        min_length=TOPT_INSTRUMENT_COUNT,
        max_length=TOPT_INSTRUMENT_COUNT,
    )

    @model_validator(mode="after")
    def validate_exact_market_denominator(self) -> "FrozenToptMarketFixture":
        for retrieved_at in (self.listing_source.retrieved_at, self.price_source.retrieved_at):
            if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
                raise ValueError("TOPT source retrieval times must be timezone-aware")
        rows = tuple(sorted(self.rows, key=lambda item: item.cusip))
        if (
            len({row.cusip for row in rows}) != TOPT_INSTRUMENT_COUNT
            or len({row.listing_id for row in rows}) != TOPT_INSTRUMENT_COUNT
            or len({row.issuer_lei for row in rows}) != TOPT_ISSUER_COUNT
        ):
            raise ValueError("TOPT market fixture denominator is incomplete or duplicated")
        xom = next((row for row in rows if row.ticker == "XOM"), None)
        if (
            xom is None
            or xom.issuer_cik_at_report_date != self.xom_report_date_identity.predecessor_cik
            or any(row.issuer_cik_at_report_date == self.xom_report_date_identity.successor_cik for row in rows)
        ):
            raise ValueError("TOPT market fixture leaked the post-report XOM successor")
        object.__setattr__(self, "rows", rows)
        return self


class FrozenToptDenominator(BaseModel):
    """Exact identity-only TOPT denominator consumed by terminal D2 validation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_state: Literal["candidate_unapproved"]
    universe_id: Literal["universe:topt-us-2026-03-31"]
    accession: Literal["000207169126012475"]
    report_date: date
    primary_document_sha256: Literal["7e46eb6babead70230986162349bb33f27d7af2a51a095b5850340aa0a534934"]
    issuer_names: tuple[str, ...] = Field(min_length=TOPT_ISSUER_COUNT, max_length=TOPT_ISSUER_COUNT)
    selected_instrument_cusips: tuple[str, ...] = Field(
        min_length=TOPT_INSTRUMENT_COUNT,
        max_length=TOPT_INSTRUMENT_COUNT,
    )
    instruments: tuple[ToptInstrument, ...] = Field(
        min_length=TOPT_INSTRUMENT_COUNT,
        max_length=TOPT_INSTRUMENT_COUNT,
    )

    @model_validator(mode="after")
    def validate_exact_denominator(self) -> "FrozenToptDenominator":
        if self.artifact_sha256 != TOPT_ARTIFACT_SHA256:
            raise ValueError("TOPT denominator artifact bytes drifted")
        if self.report_date != TOPT_REPORT_DATE:
            raise ValueError("TOPT denominator report date drifted")
        if len(set(self.issuer_names)) != TOPT_ISSUER_COUNT:
            raise ValueError("TOPT denominator must retain exactly 20 issuer names")
        instruments = tuple(sorted(self.instruments, key=lambda item: item.cusip))
        if len({item.cusip for item in instruments}) != TOPT_INSTRUMENT_COUNT:
            raise ValueError("TOPT denominator must retain exactly 21 distinct instruments")
        if len({item.issuer_lei for item in instruments}) != TOPT_ISSUER_COUNT:
            raise ValueError("TOPT denominator must retain exactly 20 distinct issuers")
        declared_cusips = tuple(sorted(self.selected_instrument_cusips))
        if declared_cusips != tuple(item.cusip for item in instruments):
            raise ValueError("TOPT selected CUSIPs do not match its instrument rows")
        alphabet = {item.ticker: item for item in instruments if item.ticker in {"GOOG", "GOOGL"}}
        if (
            set(alphabet) != {"GOOG", "GOOGL"}
            or alphabet["GOOG"].issuer_lei != alphabet["GOOGL"].issuer_lei
            or alphabet["GOOG"].cusip == alphabet["GOOGL"].cusip
        ):
            raise ValueError("Alphabet Class A and C must remain separate instruments under one issuer")
        object.__setattr__(self, "issuer_names", tuple(sorted(self.issuer_names)))
        object.__setattr__(self, "selected_instrument_cusips", declared_cusips)
        object.__setattr__(self, "instruments", instruments)
        return self


class D2E3CapturePlanCell(BaseModel):
    """One pre-run capture cell and its native snapshot coordinate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    instrument: SubjectRef
    capture_demand: PlannedDemandCell
    snapshot_demand: SnapshotDemandCell
    document_id: str = Field(min_length=1)
    applicable_at: datetime
    source_registry_entry_id: str = Field(pattern=r"^source-registry-entry:[0-9a-f]{64}$")
    source_registry_entry_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    local_test_source_coverage_entry_id: str = Field(pattern=r"^source-coverage-entry:[0-9a-f]{64}$")
    github_ci_source_coverage_entry_id: str = Field(pattern=r"^source-coverage-entry:[0-9a-f]{64}$")

    @field_validator("applicable_at")
    @classmethod
    def validate_applicable_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("D2 E3 capture applicability time must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_bindings(self) -> "D2E3CapturePlanCell":
        capture = self.capture_demand
        snapshot = self.snapshot_demand
        if (
            self.instrument.kind is not SubjectKind.SECURITY
            or capture.subject != _capture_instrument_ref(self.instrument)
            or capture.partition_key != TOPT_CAPTURE_PARTITION
        ):
            raise ValueError("D2 E3 capture demand must use the frozen security-instrument grain")
        if capture.expected_stages != frozenset({UsageStage.CAPTURE, UsageStage.NORMALIZATION}):
            raise ValueError("D2 E3 capture demand stages drifted")
        if (
            capture.requirement_id != snapshot.requirement_id
            or capture.capture_requirement_id != snapshot.capture_requirement_id
            or capture.semantic_type_id != snapshot.semantic_type_id
            or capture.domain is not snapshot.domain
        ):
            raise ValueError("D2 E3 capture and snapshot demand bindings drifted")
        return self

    @property
    def capture_key(self) -> tuple[SubjectKind, str, DataDomain, str, str]:
        demand = self.capture_demand
        return (
            demand.subject.kind,
            demand.subject.id,
            demand.domain,
            demand.partition_key,
            demand.capture_requirement_id,
        )

    def source_coverage_entry_id(self, environment: CaptureEnvironment) -> str:
        if environment is CaptureEnvironment.LOCAL_TEST:
            return self.local_test_source_coverage_entry_id
        if environment is CaptureEnvironment.GITHUB_CI:
            return self.github_ci_source_coverage_entry_id
        raise ValueError("D2 E3 capture coverage only permits Local Test or GitHub CI")


class D2E3CapturePlan(BaseModel):
    """Content-addressed pre-run denominator for both E3 vintages."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    capture_plan_id: str = Field(default="", pattern=r"^(?:|d2-e3-capture-plan:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    scope: CaptureScope
    data_requirements: tuple[DataRequirement, ...] = Field(min_length=4, max_length=4)
    cells: tuple[D2E3CapturePlanCell, ...] = Field(
        min_length=TOPT_REQUIRED_CELL_COUNT,
        max_length=TOPT_REQUIRED_CELL_COUNT,
    )

    def applicability_mapping(self) -> ApplicabilityMapping:
        return {cell.capture_key: ("required", cell.applicable_at) for cell in self.cells}

    def source_coverage_mapping(self) -> SourceCoverageMapping:
        return {
            (environment, *cell.capture_key): (cell.source_coverage_entry_id(environment),)
            for environment in (CaptureEnvironment.LOCAL_TEST, CaptureEnvironment.GITHUB_CI)
            for cell in self.cells
        }

    @model_validator(mode="after")
    def validate_predeclared_denominator(self) -> "D2E3CapturePlan":
        data_requirements = tuple(sorted(self.data_requirements, key=lambda item: item.requirement_id))
        cells = tuple(sorted(self.cells, key=lambda item: item.capture_demand.planned_cell_id))
        object.__setattr__(self, "data_requirements", data_requirements)
        object.__setattr__(self, "cells", cells)

        bindings = compile_capture_requirement_bindings(data_requirements, self.scope.requirements)
        if len(bindings) != 4:
            raise ValueError("D2 E3 capture plan must retain four source-neutral requirements")
        data_by_id = {item.requirement_id: item for item in data_requirements}
        capture_keys = [cell.capture_key for cell in cells]
        snapshot_ids = [cell.snapshot_demand.planned_cell_id for cell in cells]
        document_ids = [cell.document_id for cell in cells]
        if (
            len(set(capture_keys)) != TOPT_REQUIRED_CELL_COUNT
            or len(set(snapshot_ids)) != TOPT_REQUIRED_CELL_COUNT
            or len(set(document_ids)) != TOPT_REQUIRED_CELL_COUNT
        ):
            raise ValueError("D2 E3 capture plan has missing or duplicate cells")
        for cell in cells:
            data_requirement = data_by_id.get(cell.capture_demand.requirement_id)
            capture_requirement = self.scope.requirement_map().get(cell.capture_demand.capture_requirement_id)
            if data_requirement is None or capture_requirement is None:
                raise ValueError("D2 E3 capture cell references an unknown requirement")
            if (
                bindings[data_requirement.requirement_id] != capture_requirement
                or cell.capture_demand.semantic_type_id != capture_requirement.semantic_type_id
                or cell.capture_demand.domain is not capture_requirement.domain
                or cell.capture_demand.subject.kind not in capture_requirement.subject_kinds
                or cell.snapshot_demand.subject.kind not in capture_requirement.subject_kinds
                or cell.applicable_at != self.scope.effective_at
            ):
                raise ValueError("D2 E3 capture cell semantics drifted from its frozen scope")
        if len({cell.instrument.id for cell in cells}) != TOPT_INSTRUMENT_COUNT:
            raise ValueError("D2 E3 capture plan must retain 21 security instruments")
        if Counter(cell.capture_demand.semantic_type_id for cell in cells) != {
            ISSUER_SECURITY_TYPE_ID: TOPT_INSTRUMENT_COUNT,
            MARKET_PRICE_TYPE_ID: TOPT_INSTRUMENT_COUNT,
            SECURITY_LISTING_TYPE_ID: TOPT_INSTRUMENT_COUNT,
            UNIVERSE_MEMBERSHIP_TYPE_ID: TOPT_INSTRUMENT_COUNT,
        }:
            raise ValueError("D2 E3 capture plan domain counts drifted")

        applicability = self.applicability_mapping()
        source_coverage = self.source_coverage_mapping()
        if canonical_applicability_projection_sha256(applicability) != self.scope.applicability_projection_sha256:
            raise ValueError("D2 E3 applicability projection drifted from its scope")
        if canonical_source_coverage_projection_sha256(source_coverage) != self.scope.source_coverage_projection_sha256:
            raise ValueError("D2 E3 source coverage projection drifted from its scope")

        payload = self.model_dump(mode="json", exclude={"capture_plan_id", "content_sha256"})
        digest = canonical_sha256(payload)
        if self.content_sha256 and self.content_sha256 != digest:
            raise ValueError("D2 E3 capture plan content hash mismatch")
        if self.capture_plan_id and self.capture_plan_id != f"d2-e3-capture-plan:{digest}":
            raise ValueError("D2 E3 capture plan ID mismatch")
        object.__setattr__(self, "content_sha256", digest)
        object.__setattr__(self, "capture_plan_id", f"d2-e3-capture-plan:{digest}")
        return self


class D2E3RowCell(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    demand: SnapshotDemandCell
    document_id: str = Field(min_length=1)
    normalized_record_id: str = Field(pattern=r"^normalized-record:[0-9a-f]{64}$")


class D2E3RowCompleteManifest(BaseModel):
    """One cutoff's exact required-cell denominator and selected records."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row_manifest_id: str = Field(default="", pattern=r"^(?:|d2-e3-row-manifest:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    cutoff: Literal["original", "changed"]
    snapshot_id: str = Field(pattern=r"^snapshot:[0-9a-f]{64}$")
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_cell_ids: tuple[str, ...] = Field(
        min_length=TOPT_REQUIRED_CELL_COUNT,
        max_length=TOPT_REQUIRED_CELL_COUNT,
    )
    cells: tuple[D2E3RowCell, ...] = Field(
        min_length=TOPT_REQUIRED_CELL_COUNT,
        max_length=TOPT_REQUIRED_CELL_COUNT,
    )

    @model_validator(mode="after")
    def validate_row_completeness(self) -> "D2E3RowCompleteManifest":
        cells = tuple(sorted(self.cells, key=lambda item: item.demand.planned_cell_id))
        cell_ids = tuple(cell.demand.planned_cell_id for cell in cells)
        expected = tuple(sorted(self.expected_cell_ids))
        if len(set(cell_ids)) != TOPT_REQUIRED_CELL_COUNT or cell_ids != expected:
            raise ValueError("D2 E3 row manifest has missing or duplicate required cells")
        if len({cell.normalized_record_id for cell in cells}) != TOPT_REQUIRED_CELL_COUNT:
            raise ValueError("D2 E3 required cells must select distinct normalized records")
        counts = Counter(cell.demand.semantic_type_id for cell in cells)
        if counts != {
            ISSUER_SECURITY_TYPE_ID: TOPT_INSTRUMENT_COUNT,
            MARKET_PRICE_TYPE_ID: TOPT_INSTRUMENT_COUNT,
            SECURITY_LISTING_TYPE_ID: TOPT_INSTRUMENT_COUNT,
            UNIVERSE_MEMBERSHIP_TYPE_ID: TOPT_INSTRUMENT_COUNT,
        }:
            raise ValueError("D2 E3 row manifest domain counts drifted")
        object.__setattr__(self, "cells", cells)
        object.__setattr__(self, "expected_cell_ids", expected)
        payload = self.model_dump(mode="json", exclude={"row_manifest_id", "content_sha256"})
        digest = canonical_sha256(payload)
        if self.content_sha256 and self.content_sha256 != digest:
            raise ValueError("D2 E3 row manifest content hash mismatch")
        if self.row_manifest_id and self.row_manifest_id != f"d2-e3-row-manifest:{digest}":
            raise ValueError("D2 E3 row manifest ID mismatch")
        object.__setattr__(self, "content_sha256", digest)
        object.__setattr__(self, "row_manifest_id", f"d2-e3-row-manifest:{digest}")
        return self


class D2E3Evidence(BaseModel):
    """Content-addressed terminal Local/CI evidence for issue #121 and #23."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str = Field(default="", pattern=r"^(?:|d2-e3-evidence:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    batch_id: Literal["D2-mvp-medium-validation"] = "D2-mvp-medium-validation"
    environment: Literal["local", "ci"]
    accepted_e2_handoff_id: str = Field(pattern=r"^mvp-medium-validation-handoff:[0-9a-f]{64}$")
    accepted_e2_handoff_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    governance_handoff_id: str = Field(pattern=r"^handoff:d2-mvp-medium-validation:[0-9a-f]{64}$")
    governance_handoff_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_epoch: Literal["staging.mvp-medium-domains.v1+0019+0021"] = "staging.mvp-medium-domains.v1+0019+0021"
    denominator: FrozenToptDenominator
    universe_manifest: UniverseManifest
    original_raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    changed_raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    market_original_raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    market_changed_raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    capture_plan: D2E3CapturePlan
    capture_manifests: tuple[CaptureManifest, CaptureManifest]
    capture_evaluations: tuple[CaptureEvaluationReport, CaptureEvaluationReport]
    normalized_record_ids: tuple[str, ...] = Field(
        min_length=TOPT_NORMALIZED_RECORD_COUNT,
        max_length=TOPT_NORMALIZED_RECORD_COUNT,
    )
    snapshots: tuple[SnapshotManifest, SnapshotManifest]
    row_manifests: tuple[D2E3RowCompleteManifest, D2E3RowCompleteManifest]
    pre_knowable_rejected: Literal[True] = True
    fixture_postgres_parity: Literal[True] = True
    stable_handoff: Literal[False] = False
    release_allowed: Literal[False] = False
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("D2 E3 evidence time must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_terminal_evidence(self) -> "D2E3Evidence":
        if (
            self.accepted_e2_handoff_id != D2_E2_RUNTIME_HANDOFF_ID
            or self.accepted_e2_handoff_sha256 != D2_E2_RUNTIME_HANDOFF_SHA256
            or self.governance_handoff_id != D2_E2_GOVERNANCE_HANDOFF_ID
            or self.governance_handoff_sha256 != D2_E2_GOVERNANCE_HANDOFF_SHA256
        ):
            raise ValueError("D2 E3 evidence does not bind the exact accepted E2 handoff")
        if self.original_raw_sha256 != self.denominator.artifact_sha256:
            raise ValueError("D2 E3 original raw bytes do not bind the frozen denominator")
        if self.original_raw_sha256 == self.changed_raw_sha256:
            raise ValueError("D2 E3 changed vintage must have distinct source bytes")
        if self.market_original_raw_sha256 != TOPT_MARKET_FIXTURE_SHA256:
            raise ValueError("D2 E3 market evidence does not bind the frozen fixture")
        if self.market_original_raw_sha256 == self.market_changed_raw_sha256:
            raise ValueError("D2 E3 changed market vintage must have distinct source bytes")
        if self.universe_manifest.ref.universe_id != TOPT_UNIVERSE_ID:
            raise ValueError("D2 E3 universe manifest drifted")
        if len(self.universe_manifest.membership_ids) != TOPT_INSTRUMENT_COUNT:
            raise ValueError("D2 E3 universe manifest does not retain 21 instruments")
        if self.capture_plan.scope.universe != self.universe_manifest.ref:
            raise ValueError("D2 E3 predeclared capture scope drifted from its universe")
        expected_security_ids = {item.security_id for item in self.denominator.instruments}
        if {cell.instrument.id for cell in self.capture_plan.cells} != expected_security_ids:
            raise ValueError("D2 E3 predeclared capture instruments drifted from TOPT")

        capture_manifests = tuple(sorted(self.capture_manifests, key=lambda item: item.as_of))
        capture_evaluations = tuple(sorted(self.capture_evaluations, key=lambda item: item.capture_manifest_id))
        expected_capture_keys = set(self.capture_plan.applicability_mapping())
        evaluation_by_manifest = {report.capture_manifest_id: report for report in capture_evaluations}
        if len(evaluation_by_manifest) != 2:
            raise ValueError("D2 E3 requires two distinct capture evaluations")
        expected_capture_environment = _capture_environment(self.environment)
        for manifest in capture_manifests:
            if (
                manifest.capture_scope_id != self.capture_plan.scope.capture_scope_id
                or manifest.capture_scope_sha256 != self.capture_plan.scope.content_sha256
                or manifest.environment is not expected_capture_environment
                or manifest.partition_key != TOPT_CAPTURE_PARTITION
                or len(manifest.cells) != TOPT_REQUIRED_CELL_COUNT
                or {cell.key for cell in manifest.cells} != expected_capture_keys
            ):
                raise ValueError("D2 E3 capture manifest drifted from its predeclared scope")
            report = evaluation_by_manifest.get(manifest.capture_manifest_id)
            if report is None or report.capture_manifest_sha256 != manifest.content_sha256 or not report.ready:
                raise ValueError("D2 E3 capture manifest is not blocker-free")

        snapshots = tuple(sorted(self.snapshots, key=lambda item: item.request.as_of))
        rows = tuple(sorted(self.row_manifests, key=lambda item: item.cutoff))
        if tuple(row.cutoff for row in rows) != ("changed", "original"):
            raise ValueError("D2 E3 requires original and changed row manifests")
        snapshot_by_id = {snapshot.snapshot_id: snapshot for snapshot in snapshots}
        if len(snapshot_by_id) != 2:
            raise ValueError("D2 E3 requires two distinct PIT snapshots")
        for row in rows:
            snapshot = snapshot_by_id.get(row.snapshot_id)
            if snapshot is None or snapshot.content_sha256 != row.snapshot_sha256:
                raise ValueError("D2 E3 row manifest does not bind its snapshot")
            if len(snapshot.universe_memberships) != TOPT_INSTRUMENT_COUNT:
                raise ValueError("D2 E3 snapshot denominator shrank")
            if len(snapshot.normalized_records) != TOPT_REQUIRED_CELL_COUNT:
                raise ValueError("D2 E3 snapshot is not row-complete")
        expected_sets = {row.expected_cell_ids for row in rows}
        if len(expected_sets) != 1:
            raise ValueError("D2 E3 cutoff demands drifted between vintages")
        selected_sets = tuple(frozenset(cell.normalized_record_id for cell in row.cells) for row in rows)
        if len(set(selected_sets)) != 2 or not selected_sets[0].isdisjoint(selected_sets[1]):
            raise ValueError("D2 E3 changed vintage did not replace every selected record")
        predeclared_snapshot_ids = {cell.snapshot_demand.planned_cell_id for cell in self.capture_plan.cells}
        if any(set(row.expected_cell_ids) != predeclared_snapshot_ids for row in rows):
            raise ValueError("D2 E3 snapshot denominator drifted from the predeclared plan")
        manifests_by_as_of = {manifest.as_of: manifest for manifest in capture_manifests}
        for snapshot in snapshots:
            capture_manifest = manifests_by_as_of.get(snapshot.request.as_of)
            if capture_manifest is None:
                raise ValueError("D2 E3 snapshot has no capture manifest at its cutoff")
            captured_ids = {
                evidence.normalized_id
                for cell in capture_manifest.cells
                for evidence in cell.evidence
                if evidence.normalized_id is not None
            }
            snapshot_ids = {record.normalized_record_id for record in snapshot.normalized_records}
            if captured_ids != snapshot_ids:
                raise ValueError("D2 E3 capture and snapshot selected different normalized rows")
        normalized_ids = tuple(sorted(set(self.normalized_record_ids)))
        if len(normalized_ids) != TOPT_NORMALIZED_RECORD_COUNT or set(normalized_ids) != set().union(*selected_sets):
            raise ValueError("D2 E3 normalized vintages do not reconcile")
        if self.created_at < max(snapshot.resolved_at for snapshot in snapshots):
            raise ValueError("D2 E3 evidence cannot predate its snapshots")
        object.__setattr__(self, "snapshots", snapshots)
        object.__setattr__(self, "row_manifests", rows)
        object.__setattr__(self, "capture_manifests", capture_manifests)
        object.__setattr__(self, "capture_evaluations", capture_evaluations)
        object.__setattr__(self, "normalized_record_ids", normalized_ids)
        payload = self.model_dump(mode="json", exclude={"evidence_id", "content_sha256"})
        digest = canonical_sha256(payload)
        if self.content_sha256 and self.content_sha256 != digest:
            raise ValueError("D2 E3 evidence content hash mismatch")
        if self.evidence_id and self.evidence_id != f"d2-e3-evidence:{digest}":
            raise ValueError("D2 E3 evidence ID mismatch")
        object.__setattr__(self, "content_sha256", digest)
        object.__setattr__(self, "evidence_id", f"d2-e3-evidence:{digest}")
        return self


def load_topt_denominator(repository_root: Path, corpus: FrozenE1Corpus | None = None) -> FrozenToptDenominator:
    """Load and validate identity context without approving candidate #59 semantics."""

    frozen_corpus = corpus or load_e1_corpus(repository_root)
    artifact = frozen_corpus.artifacts["topt-candidate-denominator"]
    if artifact.sha256 != TOPT_ARTIFACT_SHA256 or _sha256(artifact.body) != TOPT_ARTIFACT_SHA256:
        raise ValueError("D2 E3 TOPT artifact does not match the frozen E1 corpus")
    return _parse_topt_denominator_body(artifact.body)


def _parse_topt_denominator_body(body: bytes) -> FrozenToptDenominator:
    """Validate the exact TOPT denominator carried by one landed raw capture."""

    try:
        payload = json.loads(body, parse_float=Decimal)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError("D2 E3 TOPT denominator capture is not valid JSON") from error
    scope = payload.get("scope")
    if not isinstance(scope, dict):
        raise ValueError("D2 E3 TOPT scope is missing")
    source = scope.get("source")
    if not isinstance(source, dict):
        raise ValueError("D2 E3 TOPT source identity is missing")
    return FrozenToptDenominator.model_validate(
        {
            "artifact_sha256": TOPT_ARTIFACT_SHA256,
            "candidate_state": payload.get("state"),
            "universe_id": scope.get("universe_id"),
            "accession": source.get("accession"),
            "report_date": source.get("report_date"),
            "primary_document_sha256": source.get("primary_document_sha256"),
            "issuer_names": scope.get("issuers"),
            "selected_instrument_cusips": scope.get("selected_instrument_cusips"),
            "instruments": scope.get("selected_instruments"),
        }
    )


def load_topt_market_fixture(
    repository_root: Path,
    denominator: FrozenToptDenominator,
) -> FrozenToptMarketFixture:
    """Load the exact checked-in 21-listing/21-price E3 evidence packet."""

    body = (repository_root / TOPT_MARKET_FIXTURE_PATH).read_bytes()
    if _sha256(body) != TOPT_MARKET_FIXTURE_SHA256:
        raise ValueError("D2 E3 TOPT market fixture bytes drifted")
    return _parse_topt_market_fixture_body(body, denominator)


def _parse_topt_market_fixture_body(
    body: bytes,
    denominator: FrozenToptDenominator,
) -> FrozenToptMarketFixture:
    """Validate the exact market packet carried by one landed raw capture."""

    try:
        payload = json.loads(body, parse_float=Decimal)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError("D2 E3 TOPT market capture is not valid JSON") from error
    fixture = FrozenToptMarketFixture.model_validate(payload)
    denominator_coordinates = {(item.cusip, item.ticker, item.issuer_lei) for item in denominator.instruments}
    fixture_coordinates = {(item.cusip, item.ticker, item.issuer_lei) for item in fixture.rows}
    if fixture_coordinates != denominator_coordinates:
        raise ValueError("TOPT market capture does not match the frozen denominator")
    return fixture


def _topt_source_code_sha256() -> str:
    """Fingerprint only source-owned routes while retaining the accepted #157 identity."""

    function_names = {
        "_e3_catalog",
        "_e3_registry",
        "_parse_topt_denominator_body",
        "_parse_topt_market_fixture_body",
        "_payloads",
        "_work_items",
    }
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    functions = {
        node.name: ast.dump(node, annotate_fields=True, include_attributes=False)
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in function_names
    }
    if set(functions) != function_names:
        raise ValueError("D2 E3 TOPT source-owned function set drifted")
    return canonical_sha256(functions)


def _e3_registry(parent: RegistrySnapshot) -> RegistrySnapshot:
    """Add the TOPT fixture source without mutating the accepted E2 entries."""

    if _topt_source_code_sha256() != TOPT_SOURCE_CODE_SHA256:
        raise ValueError("D2 E3 TOPT source-owned implementation drifted without an explicit version change")
    implementation_sha256 = TOPT_SOURCE_IMPLEMENTATION_SHA256

    def source(
        *,
        source_id: str,
        component_suffix: str,
        domain: DataDomain,
        semantic_type_id: str,
        model_type: type[BaseModel],
        fixture_sha256: str,
    ) -> SourceRegistryEntry:
        return SourceRegistryEntry(
            source_id=source_id,
            version=MEDIUM_VERSION,
            adapter_id=f"data_engine.d2_e3:{component_suffix}_adapter",
            adapter_version=MEDIUM_VERSION,
            normalizer_id=f"data_engine.d2_e3:{component_suffix}_normalizer",
            normalizer_version=MEDIUM_VERSION,
            supported_domains=(domain,),
            supported_type_ids=(semantic_type_id,),
            configuration_schema_sha256=canonical_sha256(
                {
                    "source_id": source_id,
                    "fixture_sha256": fixture_sha256,
                    "network": False,
                    "credentials": False,
                }
            ),
            mapping_schema_sha256=canonical_sha256(model_type.model_json_schema(mode="validation")),
            adapter_implementation_sha256=implementation_sha256,
            normalizer_implementation_sha256=implementation_sha256,
        )

    return parent.extend(
        sources=(
            source(
                source_id=TOPT_ISSUER_SOURCE_ID,
                component_suffix="topt_sec_issuer_security",
                domain=DataDomain.INSTRUMENTS,
                semantic_type_id=ISSUER_SECURITY_TYPE_ID,
                model_type=IssuerSecurityLink,
                fixture_sha256=TOPT_ARTIFACT_SHA256,
            ),
            source(
                source_id=TOPT_LISTING_SOURCE_ID,
                component_suffix="topt_sec_listing",
                domain=DataDomain.INSTRUMENTS,
                semantic_type_id=SECURITY_LISTING_TYPE_ID,
                model_type=SecurityListingLink,
                fixture_sha256=TOPT_MARKET_FIXTURE_SHA256,
            ),
            source(
                source_id=TOPT_MEMBERSHIP_SOURCE_ID,
                component_suffix="topt_sec_membership",
                domain=DataDomain.UNIVERSE,
                semantic_type_id=UNIVERSE_MEMBERSHIP_TYPE_ID,
                model_type=UniverseMembership,
                fixture_sha256=TOPT_ARTIFACT_SHA256,
            ),
            source(
                source_id=TOPT_PRICE_SOURCE_ID,
                component_suffix="topt_yahoo_market",
                domain=DataDomain.MARKET_PRICES,
                semantic_type_id=MARKET_PRICE_TYPE_ID,
                model_type=MarketPricePayload,
                fixture_sha256=TOPT_MARKET_FIXTURE_SHA256,
            ),
        )
    )


def _verify_e2_governance_handoff(repository_root: Path) -> None:
    body = (repository_root / D2_E2_GOVERNANCE_HANDOFF_PATH).read_bytes()
    if _sha256(body) != D2_E2_GOVERNANCE_HANDOFF_SHA256:
        raise ValueError("accepted D2 E2 governance handoff bytes drifted")
    payload = json.loads(body)
    if (
        payload.get("handoff_id") != D2_E2_GOVERNANCE_HANDOFF_ID
        or payload.get("state") != "accepted"
        or payload.get("readiness_ceiling") != "E2"
        or payload.get("schema_epoch") != D2_E2_SCHEMA_EPOCH
        or "D2-mvp-medium-validation" not in payload.get("allowed_consumers", ())
        or tuple(sorted(payload.get("allowed_environments", ()))) != ("ci", "local")
        or any(payload.get("revocation", {}).values())
    ):
        raise ValueError("accepted D2 E2 governance handoff is not active for E3 Local/CI")


def _verify_e2_runtime_handoff(handoff: MvpMediumValidationHandoff) -> None:
    if handoff.handoff_id != D2_E2_RUNTIME_HANDOFF_ID or handoff.content_sha256 != D2_E2_RUNTIME_HANDOFF_SHA256:
        raise ValueError("D2 E3 did not reproduce the exact accepted E2 runtime handoff")


def _payloads(
    denominator: FrozenToptDenominator,
    market_fixture: FrozenToptMarketFixture,
    *,
    knowable_at: datetime,
    recorded_at: datetime,
    raw_reference: str,
) -> tuple[
    tuple[IssuerSecurityLink, ...],
    tuple[SecurityListingLink, ...],
    tuple[UniverseMembership, ...],
    tuple[MarketPricePayload, ...],
]:
    links: list[IssuerSecurityLink] = []
    listings: list[SecurityListingLink] = []
    memberships: list[UniverseMembership] = []
    prices: list[MarketPricePayload] = []
    market_by_cusip = {row.cusip: row for row in market_fixture.rows}
    for instrument in denominator.instruments:
        market = market_by_cusip[instrument.cusip]
        links.append(
            IssuerSecurityLink(
                input_id=instrument.identity_document_id,
                issuer_id=instrument.issuer_id,
                security_id=instrument.security_id,
                security_kind=SecurityKind.COMMON_STOCK,
                # The frozen candidate proves distinct CUSIPs, not legal class labels.
                share_class="unresolved",
                underlying_shares_per_security_unit=Decimal("1"),
                valid_from=denominator.report_date,
                valid_to=denominator.report_date,
                knowable_at=knowable_at,
                recorded_at=recorded_at,
                confidence=Decimal("0.99"),
                raw_ref=raw_reference,
            )
        )
        listings.append(
            SecurityListingLink(
                input_id=market.listing_document_id,
                security_id=instrument.security_id,
                listing_id=market.listing_id,
                exchange_mic=market.exchange_mic,
                ticker=market.ticker,
                listing_role=ListingRole.PRIMARY,
                currency="USD",
                timezone="America/New_York",
                trading_calendar_id="calendar:us-equities",
                trading_calendar_version="2026-03-31.fixture-v1",
                valid_from=denominator.report_date,
                valid_to=denominator.report_date,
                knowable_at=knowable_at,
                recorded_at=recorded_at,
                confidence=Decimal("0.99"),
                raw_ref=raw_reference,
            )
        )
        memberships.append(
            UniverseMembership(
                membership_id=instrument.membership_id,
                universe_id=denominator.universe_id,
                subject=SubjectRef(kind=SubjectKind.SECURITY, id=instrument.security_id),
                valid_from=denominator.report_date,
                valid_to=denominator.report_date,
                knowable_at=knowable_at,
                recorded_at=recorded_at,
                confidence=Decimal("0.99"),
                raw_ref=raw_reference,
            )
        )
        prices.append(
            MarketPricePayload(
                input_id=market.price_document_id,
                issuer_id=instrument.issuer_id,
                security_id=instrument.security_id,
                listing_id=market.listing_id,
                share_class="unresolved",
                exchange_mic=market.exchange_mic,
                ticker=market.ticker,
                calendar_id="calendar:us-equities",
                calendar_version="2026-03-31.fixture-v1",
                trading_date=market.trading_date,
                session_close_at=market.session_close_at,
                open=market.open,
                high=market.high,
                low=market.low,
                close=market.close,
                volume=market.volume,
                currency="USD",
                knowable_at=knowable_at,
                produced_at=knowable_at,
                recorded_at=recorded_at,
                confidence=Decimal("0.95"),
                confidence_policy_id="confidence.d2-e3-checked-in-fixture-v1",
                price_policy_id="price.d2-e3-unadjusted-v1",
            )
        )
    return tuple(links), tuple(listings), tuple(memberships), tuple(prices)


def _e3_catalog(
    registry: RegistrySnapshot,
    denominator: FrozenToptDenominator,
    market_fixture: FrozenToptMarketFixture,
) -> MediumComponentCatalog:
    sources = {(entry.source_id, entry.version): entry for entry in registry.sources}
    semantic_types = {(entry.semantic_type_id, entry.version): entry for entry in registry.semantic_types}
    raw_sources = {
        TOPT_ISSUER_SOURCE_ID: DataSource.SEC,
        TOPT_LISTING_SOURCE_ID: DataSource.SEC,
        TOPT_MEMBERSHIP_SOURCE_ID: DataSource.SEC,
        TOPT_PRICE_SOURCE_ID: DataSource.YAHOO,
    }
    adapters = tuple(
        MediumAdapterRegistration(
            source_id=source_id,
            source_version=MEDIUM_VERSION,
            adapter_id=sources[(source_id, MEDIUM_VERSION)].adapter_id,
            adapter_version=sources[(source_id, MEDIUM_VERSION)].adapter_version,
            adapter_implementation_sha256=sources[(source_id, MEDIUM_VERSION)].adapter_implementation_sha256,
            configuration_type=FrozenMediumCaptureConfiguration,
            raw_source=raw_source,
            capture=_capture,
        )
        for source_id, raw_source in raw_sources.items()
    )

    def issuer_links(capture: LandedMediumCapture):
        landed_denominator = _parse_topt_denominator_body(capture.body)
        links, _listings, _memberships, _prices = _payloads(
            landed_denominator,
            market_fixture,
            knowable_at=capture.fetched_at,
            recorded_at=capture.recorded_at,
            raw_reference=_raw_object_ref(capture),
        )
        return tuple(
            _draft(
                semantic_type_id=ISSUER_SECURITY_TYPE_ID,
                payload=link,
                subject=SubjectRef(kind=SubjectKind.ISSUER, id=link.issuer_id),
                valid_from=link.valid_from,
                valid_to=cast(date, link.valid_to),
                knowable_at=link.knowable_at,
                recorded_at=link.recorded_at,
                document_id=link.input_id,
                confidence=link.confidence,
                raw_reference=capture.raw_ref,
            )
            for link in links
        )

    def listings(capture: LandedMediumCapture):
        landed_fixture = _parse_topt_market_fixture_body(capture.body, denominator)
        _links, values, _memberships, _prices = _payloads(
            denominator,
            landed_fixture,
            knowable_at=capture.fetched_at,
            recorded_at=capture.recorded_at,
            raw_reference=_raw_object_ref(capture),
        )
        return tuple(
            _draft(
                semantic_type_id=SECURITY_LISTING_TYPE_ID,
                payload=listing,
                subject=SubjectRef(kind=SubjectKind.SECURITY, id=listing.security_id),
                valid_from=listing.valid_from,
                valid_to=cast(date, listing.valid_to),
                knowable_at=listing.knowable_at,
                recorded_at=listing.recorded_at,
                document_id=listing.input_id,
                confidence=listing.confidence,
                raw_reference=capture.raw_ref,
            )
            for listing in values
        )

    def memberships(capture: LandedMediumCapture):
        landed_denominator = _parse_topt_denominator_body(capture.body)
        _links, _listings, values, _prices = _payloads(
            landed_denominator,
            market_fixture,
            knowable_at=capture.fetched_at,
            recorded_at=capture.recorded_at,
            raw_reference=_raw_object_ref(capture),
        )
        return tuple(
            _draft(
                semantic_type_id=UNIVERSE_MEMBERSHIP_TYPE_ID,
                payload=membership,
                subject=membership.subject,
                valid_from=membership.valid_from,
                valid_to=cast(date, membership.valid_to),
                knowable_at=membership.knowable_at,
                recorded_at=membership.recorded_at,
                document_id=membership.membership_id,
                confidence=membership.confidence,
                raw_reference=capture.raw_ref,
            )
            for membership in values
        )

    def prices(capture: LandedMediumCapture):
        landed_fixture = _parse_topt_market_fixture_body(capture.body, denominator)
        _links, _listings, _memberships, values = _payloads(
            denominator,
            landed_fixture,
            knowable_at=capture.fetched_at,
            recorded_at=capture.recorded_at,
            raw_reference=_raw_object_ref(capture),
        )
        return tuple(
            _draft(
                semantic_type_id=MARKET_PRICE_TYPE_ID,
                payload=price,
                subject=SubjectRef(kind=SubjectKind.LISTING, id=price.listing_id),
                valid_from=price.trading_date,
                valid_to=price.trading_date,
                knowable_at=price.knowable_at,
                recorded_at=price.recorded_at,
                document_id=price.input_id,
                confidence=price.confidence,
                raw_reference=capture.raw_ref,
            )
            for price in values
        )

    routes = {
        (TOPT_ISSUER_SOURCE_ID, ISSUER_SECURITY_TYPE_ID): issuer_links,
        (TOPT_LISTING_SOURCE_ID, SECURITY_LISTING_TYPE_ID): listings,
        (TOPT_MEMBERSHIP_SOURCE_ID, UNIVERSE_MEMBERSHIP_TYPE_ID): memberships,
        (TOPT_PRICE_SOURCE_ID, MARKET_PRICE_TYPE_ID): prices,
    }
    normalizers = tuple(
        MediumNormalizerRegistration(
            source_id=source_id,
            source_version=MEDIUM_VERSION,
            semantic_type_id=semantic_type_id,
            semantic_type_version=semantic_types[(semantic_type_id, MEDIUM_VERSION)].version,
            normalizer_id=sources[(source_id, MEDIUM_VERSION)].normalizer_id,
            normalizer_version=sources[(source_id, MEDIUM_VERSION)].normalizer_version,
            normalizer_implementation_sha256=sources[(source_id, MEDIUM_VERSION)].normalizer_implementation_sha256,
            normalize=normalizer,
        )
        for (source_id, semantic_type_id), normalizer in routes.items()
    )
    return MediumComponentCatalog(registry=registry, adapters=adapters, normalizers=normalizers)


def _e3_repository_registrations(registry: RegistrySnapshot):
    def select_security(payload: BaseModel, partition_key: str) -> bool:
        link = cast(IssuerSecurityLink, payload)
        return partition_key == "all" or partition_key == f"security:{link.security_id}"

    def select_listing(payload: BaseModel, partition_key: str) -> bool:
        link = cast(SecurityListingLink, payload)
        return partition_key == "all" or partition_key == f"listing:{link.listing_id}"

    def select_universe(payload: BaseModel, partition_key: str) -> bool:
        membership = cast(UniverseMembership, payload)
        return partition_key == "all" or partition_key == f"universe:{membership.universe_id}"

    def rank_price_source(_payload: BaseModel, source_id: str) -> int | None:
        return 0 if source_id == TOPT_PRICE_SOURCE_ID else 1

    def rank_listing_source(_payload: BaseModel, source_id: str) -> int | None:
        return 0 if source_id == TOPT_LISTING_SOURCE_ID else 1

    def rank_issuer_source(_payload: BaseModel, source_id: str) -> int | None:
        return 0 if source_id == TOPT_ISSUER_SOURCE_ID else 1

    def rank_membership_source(_payload: BaseModel, source_id: str) -> int | None:
        return 0 if source_id == TOPT_MEMBERSHIP_SOURCE_ID else 1

    registrations = []
    for registration in build_medium_repository_registrations(registry):
        if registration.semantic_type_id == ISSUER_SECURITY_TYPE_ID:
            registration = replace(
                registration,
                partition_filter=select_security,
                source_rank=rank_issuer_source,
            )
        elif registration.semantic_type_id == MARKET_PRICE_TYPE_ID:
            source = next(
                entry
                for entry in registry.sources
                if entry.source_id == TOPT_PRICE_SOURCE_ID and MARKET_PRICE_TYPE_ID in entry.supported_type_ids
            )
            registration = replace(
                registration,
                mapping_versions={
                    **registration.mapping_versions,
                    TOPT_PRICE_SOURCE_ID: f"{source.normalizer_id}:{source.normalizer_version}",
                },
                source_rank=rank_price_source,
            )
        elif registration.semantic_type_id == SECURITY_LISTING_TYPE_ID:
            registration = replace(
                registration,
                partition_filter=select_listing,
                source_rank=rank_listing_source,
            )
        elif registration.semantic_type_id == UNIVERSE_MEMBERSHIP_TYPE_ID:
            registration = replace(
                registration,
                partition_filter=select_universe,
                source_rank=rank_membership_source,
            )
        registrations.append(registration)
    return tuple(registrations)


def _work_items(
    *,
    denominator_body: bytes,
    market_body: bytes,
    vintage: Literal["original", "changed"],
    knowable_at: datetime,
    recorded_at: datetime,
) -> tuple[
    MediumCaptureWorkItem,
    MediumCaptureWorkItem,
    MediumCaptureWorkItem,
    MediumCaptureWorkItem,
]:
    bodies = {
        "denominator": denominator_body,
        "market": market_body,
    }

    def configuration(
        source_id: str,
        *,
        artifact: Literal["denominator", "market"],
        source: DataSource,
        semantic_type_id: str,
    ) -> FrozenMediumCaptureConfiguration:
        body = bodies[artifact]
        return FrozenMediumCaptureConfiguration(
            artifact_id=f"topt-{artifact}-{vintage}-{semantic_type_id}",
            source=source,
            source_record_id=f"d2-e3:{TOPT_ACCESSION}:{vintage}:{source_id}:{semantic_type_id}",
            body=body,
            content_type="application/json",
            fetched_at=knowable_at,
            source_published_at=knowable_at,
            metadata={
                "accession": TOPT_ACCESSION,
                "artifact_sha256": _sha256(body),
                "artifact_kind": artifact,
                "candidate_state": "candidate_unapproved",
                "checked_in_fixture": True,
                "universe_id": TOPT_UNIVERSE_ID,
                "vintage": vintage,
            },
        )

    return (
        MediumCaptureWorkItem(
            source_id=TOPT_ISSUER_SOURCE_ID,
            source_version=MEDIUM_VERSION,
            semantic_type_ids=(ISSUER_SECURITY_TYPE_ID,),
            semantic_type_version=MEDIUM_VERSION,
            configuration=configuration(
                TOPT_ISSUER_SOURCE_ID,
                artifact="denominator",
                source=DataSource.SEC,
                semantic_type_id=ISSUER_SECURITY_TYPE_ID,
            ),
            recorded_at=recorded_at,
        ),
        MediumCaptureWorkItem(
            source_id=TOPT_LISTING_SOURCE_ID,
            source_version=MEDIUM_VERSION,
            semantic_type_ids=(SECURITY_LISTING_TYPE_ID,),
            semantic_type_version=MEDIUM_VERSION,
            configuration=configuration(
                TOPT_LISTING_SOURCE_ID,
                artifact="market",
                source=DataSource.SEC,
                semantic_type_id=SECURITY_LISTING_TYPE_ID,
            ),
            recorded_at=recorded_at,
        ),
        MediumCaptureWorkItem(
            source_id=TOPT_MEMBERSHIP_SOURCE_ID,
            source_version=MEDIUM_VERSION,
            semantic_type_ids=(UNIVERSE_MEMBERSHIP_TYPE_ID,),
            semantic_type_version=MEDIUM_VERSION,
            configuration=configuration(
                TOPT_MEMBERSHIP_SOURCE_ID,
                artifact="denominator",
                source=DataSource.SEC,
                semantic_type_id=UNIVERSE_MEMBERSHIP_TYPE_ID,
            ),
            recorded_at=recorded_at,
        ),
        MediumCaptureWorkItem(
            source_id=TOPT_PRICE_SOURCE_ID,
            source_version=MEDIUM_VERSION,
            semantic_type_ids=(MARKET_PRICE_TYPE_ID,),
            semantic_type_version=MEDIUM_VERSION,
            configuration=configuration(
                TOPT_PRICE_SOURCE_ID,
                artifact="market",
                source=DataSource.YAHOO,
                semantic_type_id=MARKET_PRICE_TYPE_ID,
            ),
            recorded_at=recorded_at,
        ),
    )


def _capture_environment(environment: str) -> CaptureEnvironment:
    try:
        return {
            "local": CaptureEnvironment.LOCAL_TEST,
            "ci": CaptureEnvironment.GITHUB_CI,
        }[environment]
    except KeyError as error:
        raise ValueError("D2 E3 capture only permits Local Test or GitHub CI") from error


def _capture_requirements() -> tuple[CaptureRequirement, ...]:
    specifications = (
        (
            ISSUER_SECURITY_TYPE_ID,
            DataDomain.INSTRUMENTS,
            (SubjectKind.ISSUER, SubjectKind.SECURITY),
            (
                "confidence",
                "input_id",
                "issuer_id",
                "security_id",
                "security_kind",
                "share_class",
                "underlying_shares_per_security_unit",
            ),
        ),
        (
            SECURITY_LISTING_TYPE_ID,
            DataDomain.INSTRUMENTS,
            (SubjectKind.SECURITY,),
            (
                "confidence",
                "currency",
                "exchange_mic",
                "input_id",
                "listing_id",
                "listing_role",
                "security_id",
                "ticker",
                "timezone",
                "trading_calendar_id",
                "trading_calendar_version",
            ),
        ),
        (
            UNIVERSE_MEMBERSHIP_TYPE_ID,
            DataDomain.UNIVERSE,
            (SubjectKind.SECURITY,),
            ("confidence", "membership_id", "subject", "universe_id"),
        ),
        (
            MARKET_PRICE_TYPE_ID,
            DataDomain.MARKET_PRICES,
            (SubjectKind.LISTING, SubjectKind.SECURITY),
            (
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
        ),
    )
    return tuple(
        CaptureRequirement(
            semantic_type_id=semantic_type_id,
            semantic_type_version=MEDIUM_VERSION,
            domain=domain,
            required_fields=required_fields,
            subject_kinds=subject_kinds,
            cadence=timedelta(days=1),
            partition_rule_id=TOPT_CAPTURE_PARTITION_POLICY_ID,
            freshness_policy_id=TOPT_CAPTURE_FRESHNESS_POLICY_ID,
            maximum_age=timedelta(days=2),
            quality_policy_ids=TOPT_CAPTURE_QUALITY_POLICY_IDS,
        )
        for semantic_type_id, domain, subject_kinds, required_fields in specifications
    )


def _capture_data_requirements(
    requirements: tuple[CaptureRequirement, ...],
) -> tuple[DataRequirement, ...]:
    return tuple(
        DataRequirement(
            capture_requirement_id=requirement.capture_requirement_id,
            semantic_type_id=requirement.semantic_type_id,
            domain=requirement.domain,
            subject_kinds=frozenset(requirement.subject_kinds),
            level=RequirementLevel.REQUIRED,
            lookback=timedelta(days=1),
            valid_period_rule_id=requirement.partition_rule_id,
            maximum_age=requirement.maximum_age,
            cadence=requirement.cadence,
        )
        for requirement in requirements
    )


def _source_coverage_entry_id(
    *,
    environment: CaptureEnvironment,
    demand: PlannedDemandCell,
    source: SourceRegistryEntry,
) -> str:
    return "source-coverage-entry:" + canonical_sha256(
        {
            "batch_id": "D2-mvp-medium-validation",
            "environment": environment.value,
            "planned_cell_id": demand.planned_cell_id,
            "source_registry_entry_id": source.source_registry_entry_id,
            "source_registry_entry_sha256": source.content_sha256,
        }
    )


def _build_e3_capture_plan(
    *,
    denominator: FrozenToptDenominator,
    market_fixture: FrozenToptMarketFixture,
    registry: RegistrySnapshot,
    universe_manifest: UniverseManifest,
    effective_at: datetime,
) -> D2E3CapturePlan:
    """Freeze all capture and snapshot demand before any TOPT bytes land."""

    requirements = _capture_requirements()
    data_requirements = _capture_data_requirements(requirements)
    compile_capture_requirement_bindings(data_requirements, requirements)
    requirement_by_type = {item.semantic_type_id: item for item in requirements}
    data_by_type = {item.semantic_type_id: item for item in data_requirements}
    source_by_id = {(item.source_id, item.version): item for item in registry.sources}
    type_by_id = {(item.semantic_type_id, item.version): item for item in registry.semantic_types}
    market_by_cusip = {row.cusip: row for row in market_fixture.rows}
    cells: list[D2E3CapturePlanCell] = []

    for instrument in denominator.instruments:
        market = market_by_cusip[instrument.cusip]
        security_subject = SubjectRef(kind=SubjectKind.SECURITY, id=instrument.security_id)
        capture_subject = _capture_instrument_ref(security_subject)
        specifications = (
            (
                ISSUER_SECURITY_TYPE_ID,
                TOPT_ISSUER_SOURCE_ID,
                instrument.identity_document_id,
                SubjectRef(kind=SubjectKind.ISSUER, id=instrument.issuer_id),
                f"security:{instrument.security_id}",
            ),
            (
                SECURITY_LISTING_TYPE_ID,
                TOPT_LISTING_SOURCE_ID,
                market.listing_document_id,
                security_subject,
                f"listing:{market.listing_id}",
            ),
            (
                UNIVERSE_MEMBERSHIP_TYPE_ID,
                TOPT_MEMBERSHIP_SOURCE_ID,
                instrument.membership_id,
                security_subject,
                f"universe:{denominator.universe_id}",
            ),
            (
                MARKET_PRICE_TYPE_ID,
                TOPT_PRICE_SOURCE_ID,
                market.price_document_id,
                SubjectRef(kind=SubjectKind.LISTING, id=market.listing_id),
                f"date:{market.trading_date.isoformat()}",
            ),
        )
        for semantic_type_id, source_id, document_id, snapshot_subject, snapshot_partition in specifications:
            requirement = requirement_by_type[semantic_type_id]
            data_requirement = data_by_type[semantic_type_id]
            semantic_type = type_by_id[(semantic_type_id, MEDIUM_VERSION)]
            if semantic_type.domain is not requirement.domain:
                raise ValueError("D2 E3 capture requirement drifted from the semantic registry")
            source = source_by_id[(source_id, MEDIUM_VERSION)]
            capture_demand = PlannedDemandCell(
                requirement_id=data_requirement.requirement_id,
                capture_requirement_id=requirement.capture_requirement_id,
                semantic_type_id=semantic_type_id,
                domain=requirement.domain,
                subject=capture_subject,
                partition_key=TOPT_CAPTURE_PARTITION,
                level=RequirementLevel.REQUIRED,
                expected_stages=frozenset({UsageStage.CAPTURE, UsageStage.NORMALIZATION}),
            )
            snapshot_demand = SnapshotDemandCell(
                requirement_id=data_requirement.requirement_id,
                capture_requirement_id=requirement.capture_requirement_id,
                semantic_type_id=semantic_type_id,
                semantic_type_version=MEDIUM_VERSION,
                domain=requirement.domain,
                subject=snapshot_subject,
                partition_key=snapshot_partition,
                level=RequirementLevel.REQUIRED,
            )
            cells.append(
                D2E3CapturePlanCell(
                    instrument=security_subject,
                    capture_demand=capture_demand,
                    snapshot_demand=snapshot_demand,
                    document_id=document_id,
                    applicable_at=effective_at,
                    source_registry_entry_id=source.source_registry_entry_id,
                    source_registry_entry_sha256=source.content_sha256,
                    local_test_source_coverage_entry_id=_source_coverage_entry_id(
                        environment=CaptureEnvironment.LOCAL_TEST,
                        demand=capture_demand,
                        source=source,
                    ),
                    github_ci_source_coverage_entry_id=_source_coverage_entry_id(
                        environment=CaptureEnvironment.GITHUB_CI,
                        demand=capture_demand,
                        source=source,
                    ),
                )
            )

    applicability: ApplicabilityMapping = {cell.capture_key: ("required", cell.applicable_at) for cell in cells}
    source_coverage: SourceCoverageMapping = {
        (environment, *cell.capture_key): (cell.source_coverage_entry_id(environment),)
        for environment in (CaptureEnvironment.LOCAL_TEST, CaptureEnvironment.GITHUB_CI)
        for cell in cells
    }
    applicability_projection_sha256 = canonical_applicability_projection_sha256(applicability)
    source_coverage_projection_sha256 = canonical_source_coverage_projection_sha256(source_coverage)
    research_catalog_sha256 = canonical_sha256(
        {
            "batch_id": "D2-mvp-medium-validation",
            "purpose": "batch-private-e3-capture-demand",
            "universe": universe_manifest.ref.model_dump(mode="json"),
            "data_requirements": [item.model_dump(mode="json") for item in data_requirements],
        }
    )
    applicability_catalog_sha256 = canonical_sha256(
        {
            "batch_id": "D2-mvp-medium-validation",
            "effective_at": effective_at.isoformat(),
            "projection_sha256": applicability_projection_sha256,
        }
    )
    source_coverage_catalog_sha256 = canonical_sha256(
        {
            "batch_id": "D2-mvp-medium-validation",
            "projection_sha256": source_coverage_projection_sha256,
            "source_registry_id": registry.source_registry_snapshot_id,
            "source_registry_sha256": registry.source_registry_sha256,
        }
    )
    slo_catalog_sha256 = canonical_sha256(
        {
            "batch_id": "D2-mvp-medium-validation",
            "requirements": [item.model_dump(mode="json") for item in requirements],
        }
    )
    scope = CaptureScope(
        research_catalog_id=f"research-catalog:{research_catalog_sha256}",
        research_catalog_sha256=research_catalog_sha256,
        universe=universe_manifest.ref,
        applicability_catalog_id=f"applicability:{applicability_catalog_sha256}",
        applicability_catalog_sha256=applicability_catalog_sha256,
        applicability_projection_sha256=applicability_projection_sha256,
        source_coverage_catalog_id=f"source-coverage:{source_coverage_catalog_sha256}",
        source_coverage_catalog_sha256=source_coverage_catalog_sha256,
        source_coverage_projection_sha256=source_coverage_projection_sha256,
        slo_catalog_id=f"module-slo:{slo_catalog_sha256}",
        slo_catalog_sha256=slo_catalog_sha256,
        source_registry_id=registry.source_registry_snapshot_id,
        source_registry_sha256=registry.source_registry_sha256,
        semantic_type_registry_id=registry.semantic_type_registry_snapshot_id,
        semantic_type_registry_sha256=registry.semantic_type_registry_sha256,
        requirements=requirements,
        effective_at=effective_at,
        owner="D2-mvp-medium-validation:E3",
    )
    return D2E3CapturePlan(
        scope=scope,
        data_requirements=data_requirements,
        cells=tuple(cells),
    )


def _records_by_document(records: tuple[NormalizedRecordRef, ...]) -> dict[str, NormalizedRecordRef]:
    by_document = {record.document_id: record for record in records}
    if len(by_document) != len(records):
        raise ValueError("D2 E3 normalized documents are duplicated")
    return by_document


def _universe_manifest(denominator: FrozenToptDenominator, *, effective_at: datetime) -> UniverseManifest:
    return UniverseManifest.create(
        universe_id=denominator.universe_id,
        universe_version="topt-2026-03-31-d2-e3-v1",
        definition_kind=UniverseDefinitionKind.FIXED_COHORT,
        effective_at=effective_at,
        owner="D2-mvp-medium-validation:E3",
        membership_ids=tuple(instrument.membership_id for instrument in denominator.instruments),
    )


def _capture_manifest(
    *,
    capture_plan: D2E3CapturePlan,
    capture_batch: MediumCaptureBatch,
    records_by_document: dict[str, NormalizedRecordRef],
    repository: PostgresMediumSemanticRepository,
    connection: Connection[Any],
    raw_store: RawObjectStore,
    environment: CaptureEnvironment,
    as_of: datetime,
    created_at: datetime,
) -> tuple[CaptureManifest, CaptureEvaluationReport]:
    """Left-join one observed vintage onto the immutable 84-cell plan."""

    expected_documents = {cell.document_id for cell in capture_plan.cells}
    unexpected_documents = set(records_by_document) - expected_documents
    if unexpected_documents:
        raise ValueError(f"D2 E3 normalization emitted unplanned documents: {sorted(unexpected_documents)}")
    requirements = capture_plan.scope.requirement_map()
    captures_by_source = {capture.source_registry_entry_id: capture for capture in capture_batch.captures}
    if len(captures_by_source) != len(capture_batch.captures):
        raise ValueError("D2 E3 capture batch duplicated a source registry entry")
    raw_identity_by_source: dict[str, tuple[str, str]] = {}
    for source_entry_id, capture in captures_by_source.items():
        persisted_body = get_payload(connection, capture.fetch_id, store=raw_store)
        persisted_sha256 = _sha256(persisted_body)
        if persisted_sha256 != capture.raw_object_sha256:
            raise ValueError("D2 E3 persisted raw bytes drifted from their landed checksum")
        raw_identity_by_source[source_entry_id] = capture.raw_ref, persisted_sha256
    cells: list[CaptureCell] = []
    for planned in capture_plan.cells:
        demand = planned.capture_demand
        record = records_by_document.get(planned.document_id)
        if record is None:
            cells.append(
                CaptureCell(
                    subject=demand.subject,
                    domain=demand.domain,
                    partition_key=demand.partition_key,
                    capture_requirement_id=demand.capture_requirement_id,
                    applicability="required",
                    status="missing",
                    reason_codes=("normalized-record-missing",),
                )
            )
            continue
        stored = repository.get(record.normalized_record_id)
        if stored is None or stored.record != record:
            cells.append(
                CaptureCell(
                    subject=demand.subject,
                    domain=demand.domain,
                    partition_key=demand.partition_key,
                    capture_requirement_id=demand.capture_requirement_id,
                    applicability="required",
                    status="error",
                    reason_codes=("normalized-record-readback-failed",),
                )
            )
            continue

        requirement = requirements[demand.capture_requirement_id]
        populated_fields = tuple(stored.payload.model_dump(mode="python", exclude_none=True))
        raw_identity = raw_identity_by_source.get(planned.source_registry_entry_id)
        if raw_identity is None:
            raise ValueError("D2 E3 capture plan source has no landed raw row")
        raw_id, raw_sha256 = raw_identity
        quality_pass = (
            set(requirement.required_fields).issubset(populated_fields)
            and record.document_id == planned.document_id
            and record.source_registry_entry_id == planned.source_registry_entry_id
            and record.source_registry_entry_sha256 == planned.source_registry_entry_sha256
            and record.raw_object_sha256 == raw_sha256
            and record.draft.knowable_at <= as_of
            and record.recorded_at <= created_at
        )
        valid_to = None if record.draft.valid_to is None else datetime.combine(record.draft.valid_to, time.max, UTC)
        evidence = CaptureRecordEvidence(
            source_coverage_entry_id=planned.source_coverage_entry_id(environment),
            raw_id=raw_id,
            raw_sha256=raw_sha256,
            normalized_id=record.normalized_record_id,
            semantic_type_id=record.draft.semantic_type_id,
            semantic_type_version=record.draft.semantic_type_version,
            populated_fields=populated_fields,
            knowable_at=record.draft.knowable_at,
            recorded_at=record.recorded_at,
            valid_from=datetime.combine(record.draft.valid_from, time.min, UTC),
            valid_to=valid_to,
            confidence=record.confidence,
            mapping_version=record.mapping_version,
            policy_versions={
                requirement.freshness_policy_id: MEDIUM_VERSION,
                requirement.partition_rule_id: MEDIUM_VERSION,
            },
            quality_check_ids=requirement.quality_policy_ids,
            quality_status=QualityStatus.PASS if quality_pass else QualityStatus.FAIL,
            lineage_sha256=record.content_sha256,
        )
        cells.append(
            CaptureCell(
                subject=demand.subject,
                domain=demand.domain,
                partition_key=demand.partition_key,
                capture_requirement_id=demand.capture_requirement_id,
                applicability="required",
                status="complete",
                evidence=(evidence,),
            )
        )

    scope = capture_plan.scope
    manifest = CaptureManifest(
        capture_scope_id=scope.capture_scope_id,
        capture_scope_sha256=scope.content_sha256,
        environment=environment,
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
        partition_key=TOPT_CAPTURE_PARTITION,
        as_of=as_of,
        started_at=as_of,
        cells=tuple(cells),
        created_at=created_at,
    )
    evaluation = evaluate_capture_manifest(
        scope,
        manifest,
        applicability_catalog_id=scope.applicability_catalog_id,
        applicability_catalog_sha256=scope.applicability_catalog_sha256,
        applicability=capture_plan.applicability_mapping(),
        source_coverage=capture_plan.source_coverage_mapping(),
        evaluated_at=created_at + timedelta(microseconds=1),
    )
    return manifest, evaluation


def _snapshot_plan(
    *,
    capture_plan: D2E3CapturePlan,
    registry: RegistrySnapshot,
    records_by_document: dict[str, NormalizedRecordRef],
    universe_manifest: UniverseManifest,
    as_of: datetime,
) -> tuple[
    SnapshotRequest,
    dict[str, tuple[NormalizedRecordRef, ...]],
    dict[str, str],
]:
    demands: list[SnapshotDemandCell] = []
    selected: dict[str, tuple[NormalizedRecordRef, ...]] = {}
    document_by_cell: dict[str, str] = {}
    for cell in capture_plan.cells:
        record = records_by_document[cell.document_id]
        demand = cell.snapshot_demand
        if (
            record.draft.semantic_type_id != demand.semantic_type_id
            or record.draft.semantic_type_version != demand.semantic_type_version
            or record.draft.subject != demand.subject
        ):
            raise ValueError("D2 E3 normalized record drifted from predeclared snapshot demand")
        demands.append(demand)
        selected[demand.planned_cell_id] = (record,)
        document_by_cell[demand.planned_cell_id] = record.document_id
    request = SnapshotRequest(
        universe=universe_manifest.ref,
        as_of=as_of,
        valid_on=TOPT_REPORT_DATE,
        registry_snapshot_id=registry.registry_snapshot_id,
        registry_snapshot_sha256=registry.content_sha256,
        source_registry_id=registry.source_registry_snapshot_id,
        source_registry_sha256=registry.source_registry_sha256,
        semantic_type_registry_id=registry.semantic_type_registry_snapshot_id,
        semantic_type_registry_sha256=registry.semantic_type_registry_sha256,
        policy_bindings=_policy_bindings(include_membership=True),
        demand_cells=tuple(demands),
    )
    return request, selected, document_by_cell


def _stored_memberships(
    repository: PostgresMediumSemanticRepository,
    records_by_document: dict[str, NormalizedRecordRef],
    denominator: FrozenToptDenominator,
) -> tuple[UniverseMembership, ...]:
    stored = tuple(
        repository.get(records_by_document[item.membership_id].normalized_record_id) for item in denominator.instruments
    )
    if any(item is None or not isinstance(item.payload, UniverseMembership) for item in stored):
        raise ValueError("D2 E3 membership payload disappeared after normalization")
    return tuple(cast(UniverseMembership, item.payload) for item in stored if item is not None)


def _row_manifest(
    *,
    cutoff: Literal["original", "changed"],
    snapshot: SnapshotManifest,
    document_by_cell: dict[str, str],
) -> D2E3RowCompleteManifest:
    cells = tuple(
        D2E3RowCell(
            demand=selection.demand,
            document_id=document_by_cell[selection.demand.planned_cell_id],
            normalized_record_id=selection.normalized_record_ids[0],
        )
        for selection in snapshot.selections
        if len(selection.normalized_record_ids) == 1
    )
    return D2E3RowCompleteManifest(
        cutoff=cutoff,
        snapshot_id=snapshot.snapshot_id,
        snapshot_sha256=snapshot.content_sha256,
        expected_cell_ids=tuple(document_by_cell),
        cells=cells,
    )


def _resolve_e3_snapshot_pair(
    *,
    resolver: PostgresMediumSnapshotResolver,
    request: SnapshotRequest,
    registry: RegistrySnapshot,
    fixture_records: dict[str, tuple[NormalizedRecordRef, ...]],
    resolved_at: datetime,
    universe_manifest: UniverseManifest,
    universe_memberships: tuple[UniverseMembership, ...],
) -> SnapshotManifest:
    fixture = build_medium_snapshot(
        request,
        registry=registry,
        selected_records=fixture_records,
        resolved_at=resolved_at,
        universe_manifest=universe_manifest,
        universe_memberships=universe_memberships,
    )
    postgres = resolver.resolve(
        request,
        registry=registry,
        resolved_at=resolved_at,
        universe_manifest=universe_manifest,
    )
    if postgres != fixture:
        fixture_ids = {record.normalized_record_id for record in fixture.normalized_records}
        postgres_ids = {record.normalized_record_id for record in postgres.normalized_records}
        raise ValueError(
            "D2 E3 fixture/Postgres snapshot parity failed: "
            f"missing={sorted(fixture_ids - postgres_ids)}, unexpected={sorted(postgres_ids - fixture_ids)}, "
            f"memberships_equal={fixture.universe_memberships == postgres.universe_memberships}"
        )
    return postgres


FailurePoint = Literal[
    "after-capture-plan",
    "after-original-normalization",
    "after-original-vintage",
]
FailureInjector = Callable[[FailurePoint], None]


def run_d2_e3(
    repository_root: Path,
    connection: Connection[Any],
    raw_store: RawObjectStore,
    *,
    environment: str,
    failure_injector: FailureInjector | None = None,
) -> D2E3Evidence:
    """Run the exact 20-issuer/21-instrument terminal corpus atomically."""

    if environment not in {"local", "ci"}:
        raise ValueError("D2 E3 only permits Local/CI execution")
    with connection.transaction():
        # PostgreSQL renders timestamptz values in the session timezone. Pin UTC so
        # rehydrated records retain one content identity across Local and CI.
        connection.execute("set local time zone 'UTC'")
        _verify_e2_governance_handoff(repository_root)
        e2_handoff = run_d2_e2(repository_root, connection, raw_store, environment=environment)
        _verify_e2_runtime_handoff(e2_handoff)
        corpus = load_e1_corpus(repository_root)
        denominator = load_topt_denominator(repository_root, corpus)
        market_fixture = load_topt_market_fixture(repository_root, denominator)
        artifact = corpus.artifacts["topt-candidate-denominator"]
        original_body = artifact.body
        changed_body = original_body + b"\n"
        market_original_body = (repository_root / TOPT_MARKET_FIXTURE_PATH).read_bytes()
        market_changed_body = market_original_body + b"\n"
        source_retrieved_at = max(
            market_fixture.listing_source.retrieved_at,
            market_fixture.price_source.retrieved_at,
        )
        original_at = max(e2_handoff.created_at + timedelta(seconds=1), source_retrieved_at)
        changed_at = original_at + timedelta(days=1)
        registry = _e3_registry(e2_handoff.registry_snapshot)
        universe = _universe_manifest(denominator, effective_at=original_at)
        capture_plan = _build_e3_capture_plan(
            denominator=denominator,
            market_fixture=market_fixture,
            registry=registry,
            universe_manifest=universe,
            effective_at=original_at - timedelta(microseconds=1),
        )
        capture_environment = _capture_environment(environment)
        capture_scope_repository = PostgresCaptureScopeRepository(connection)
        capture_manifest_repository = PostgresCaptureManifestRepository(connection)
        capture_evaluation_repository = PostgresCaptureEvaluationRepository(connection)
        capture_scope_repository.put(capture_plan.scope)
        if capture_scope_repository.get(capture_plan.scope.capture_scope_id) != capture_plan.scope:
            raise ValueError("D2 E3 capture scope failed immutable readback")
        if failure_injector is not None:
            failure_injector("after-capture-plan")

        repository = PostgresMediumSemanticRepository(
            connection,
            registry=registry,
            registrations=_e3_repository_registrations(registry),
        )
        catalog = _e3_catalog(registry, denominator, market_fixture)

        original_batch = land_medium_capture_plan(
            connection,
            object_store=raw_store,
            catalog=catalog,
            work_items=_work_items(
                denominator_body=original_body,
                market_body=market_original_body,
                vintage="original",
                knowable_at=original_at,
                recorded_at=original_at + timedelta(microseconds=1),
            ),
        )
        original = normalize_medium_capture_batch(batch=original_batch, catalog=catalog, repository=repository)
        original_by_document = _records_by_document(original.normalized_records)
        if failure_injector is not None:
            failure_injector("after-original-normalization")
        original_capture_manifest, original_capture_evaluation = _capture_manifest(
            capture_plan=capture_plan,
            capture_batch=original_batch,
            records_by_document=original_by_document,
            repository=repository,
            connection=connection,
            raw_store=raw_store,
            environment=capture_environment,
            as_of=original_at,
            created_at=original_at + timedelta(seconds=1),
        )
        capture_manifest_repository.put(original_capture_manifest)
        capture_evaluation_repository.put(original_capture_evaluation)
        if (
            capture_manifest_repository.get(original_capture_manifest.capture_manifest_id) != original_capture_manifest
            or capture_evaluation_repository.get(original_capture_evaluation.capture_evaluation_report_id)
            != original_capture_evaluation
        ):
            raise ValueError("D2 E3 original capture contracts failed immutable readback")
        if not original_capture_evaluation.ready:
            raise ValueError(
                "D2 E3 original capture manifest is incomplete: "
                + ", ".join(original_capture_evaluation.blocking_reason_codes)
            )
        if failure_injector is not None:
            failure_injector("after-original-vintage")

        changed_batch = land_medium_capture_plan(
            connection,
            object_store=raw_store,
            catalog=catalog,
            work_items=_work_items(
                denominator_body=changed_body,
                market_body=market_changed_body,
                vintage="changed",
                knowable_at=changed_at,
                recorded_at=changed_at + timedelta(microseconds=1),
            ),
        )
        changed = normalize_medium_capture_batch(batch=changed_batch, catalog=catalog, repository=repository)
        changed_by_document = _records_by_document(changed.normalized_records)
        changed_capture_manifest, changed_capture_evaluation = _capture_manifest(
            capture_plan=capture_plan,
            capture_batch=changed_batch,
            records_by_document=changed_by_document,
            repository=repository,
            connection=connection,
            raw_store=raw_store,
            environment=capture_environment,
            as_of=changed_at,
            created_at=changed_at + timedelta(seconds=1),
        )
        capture_manifest_repository.put(changed_capture_manifest)
        capture_evaluation_repository.put(changed_capture_evaluation)
        if (
            capture_manifest_repository.get(changed_capture_manifest.capture_manifest_id) != changed_capture_manifest
            or capture_evaluation_repository.get(changed_capture_evaluation.capture_evaluation_report_id)
            != changed_capture_evaluation
        ):
            raise ValueError("D2 E3 changed capture contracts failed immutable readback")
        if not changed_capture_evaluation.ready:
            raise ValueError(
                "D2 E3 changed capture manifest is incomplete: "
                + ", ".join(changed_capture_evaluation.blocking_reason_codes)
            )

        resolver = PostgresMediumSnapshotResolver(
            semantic_records=repository,
            snapshots=PostgresSnapshotRepository(connection),
        )
        original_request, original_fixture, original_documents = _snapshot_plan(
            capture_plan=capture_plan,
            registry=registry,
            records_by_document=original_by_document,
            universe_manifest=universe,
            as_of=original_at,
        )
        pre_knowable_request, _pre_fixture, _pre_documents = _snapshot_plan(
            capture_plan=capture_plan,
            registry=registry,
            records_by_document=original_by_document,
            universe_manifest=universe,
            as_of=original_at - timedelta(microseconds=1),
        )
        try:
            resolver.resolve(
                pre_knowable_request,
                registry=registry,
                resolved_at=changed_at + timedelta(seconds=1),
                universe_manifest=universe,
            )
        except ValueError as error:
            if "membership rows do not match" not in str(error):
                raise
        else:
            raise ValueError("D2 E3 future membership leaked before knowability")
        original_snapshot = _resolve_e3_snapshot_pair(
            resolver=resolver,
            request=original_request,
            registry=registry,
            fixture_records=original_fixture,
            resolved_at=changed_at + timedelta(seconds=1),
            universe_manifest=universe,
            universe_memberships=_stored_memberships(repository, original_by_document, denominator),
        )

        changed_request, changed_fixture, changed_documents = _snapshot_plan(
            capture_plan=capture_plan,
            registry=registry,
            records_by_document=changed_by_document,
            universe_manifest=universe,
            as_of=changed_at,
        )
        changed_snapshot = _resolve_e3_snapshot_pair(
            resolver=resolver,
            request=changed_request,
            registry=registry,
            fixture_records=changed_fixture,
            resolved_at=changed_at + timedelta(seconds=1),
            universe_manifest=universe,
            universe_memberships=_stored_memberships(repository, changed_by_document, denominator),
        )
        original_rows = _row_manifest(
            cutoff="original",
            snapshot=original_snapshot,
            document_by_cell=original_documents,
        )
        changed_rows = _row_manifest(
            cutoff="changed",
            snapshot=changed_snapshot,
            document_by_cell=changed_documents,
        )
        return D2E3Evidence(
            environment=cast(Literal["local", "ci"], environment),
            accepted_e2_handoff_id=D2_E2_RUNTIME_HANDOFF_ID,
            accepted_e2_handoff_sha256=D2_E2_RUNTIME_HANDOFF_SHA256,
            governance_handoff_id=D2_E2_GOVERNANCE_HANDOFF_ID,
            governance_handoff_sha256=D2_E2_GOVERNANCE_HANDOFF_SHA256,
            denominator=denominator,
            universe_manifest=universe,
            original_raw_sha256=_sha256(original_body),
            changed_raw_sha256=_sha256(changed_body),
            market_original_raw_sha256=_sha256(market_original_body),
            market_changed_raw_sha256=_sha256(market_changed_body),
            capture_plan=capture_plan,
            capture_manifests=(original_capture_manifest, changed_capture_manifest),
            capture_evaluations=(original_capture_evaluation, changed_capture_evaluation),
            normalized_record_ids=tuple(
                record.normalized_record_id for record in (*original.normalized_records, *changed.normalized_records)
            ),
            snapshots=(original_snapshot, changed_snapshot),
            row_manifests=(original_rows, changed_rows),
            created_at=changed_at + timedelta(seconds=2),
        )


class D2E3Activation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    batch_id: Literal["D2-mvp-medium-validation"] = "D2-mvp-medium-validation"
    environment: Literal["local", "ci"]
    expected_e2_handoff_id: str = Field(
        default=D2_E2_RUNTIME_HANDOFF_ID,
        pattern=r"^mvp-medium-validation-handoff:[0-9a-f]{64}$",
    )
    expected_e2_handoff_sha256: str = Field(
        default=D2_E2_RUNTIME_HANDOFF_SHA256,
        pattern=r"^[0-9a-f]{64}$",
    )
    release_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_handoff(self) -> "D2E3Activation":
        if (
            self.expected_e2_handoff_id != D2_E2_RUNTIME_HANDOFF_ID
            or self.expected_e2_handoff_sha256 != D2_E2_RUNTIME_HANDOFF_SHA256
        ):
            raise ValueError("D2 E3 activation must bind the accepted E2 runtime handoff")
        return self


@dataclass(frozen=True)
class D2E3RunnerResource:
    repository_root: Path
    connection: Connection[Any]
    raw_store: RawObjectStore
    activation: D2E3Activation

    def run(self) -> D2E3Evidence:
        evidence = run_d2_e3(
            self.repository_root,
            self.connection,
            self.raw_store,
            environment=self.activation.environment,
        )
        if (
            evidence.accepted_e2_handoff_id != self.activation.expected_e2_handoff_id
            or evidence.accepted_e2_handoff_sha256 != self.activation.expected_e2_handoff_sha256
        ):
            raise ValueError("D2 E3 materialization escaped its accepted E2 parent")
        return evidence


@dg.asset(
    name=D2_E3_ASSET_NAME,
    group_name="mvp_medium_validation_e3",
    required_resource_keys={"mvp_medium_validation_e3_runner"},
    description="Validate the exact TOPT denominator through the accepted D2 Local/CI data plane.",
)
def materialize_mvp_medium_validation_e3(context: AssetExecutionContext) -> dg.Output[D2E3Evidence]:
    runner = cast(D2E3RunnerResource, context.resources.mvp_medium_validation_e3_runner)
    evidence = runner.run()
    return dg.Output(
        evidence,
        metadata={
            "evidence_id": evidence.evidence_id,
            "issuer_count": TOPT_ISSUER_COUNT,
            "instrument_count": TOPT_INSTRUMENT_COUNT,
            "required_cell_count": TOPT_REQUIRED_CELL_COUNT,
            "environment": runner.activation.environment,
            "release_allowed": False,
        },
        data_version=dg.DataVersion(evidence.content_sha256),
    )


def build_d2_e3_definitions(
    *,
    repository_root: Path,
    connection: Connection[Any],
    raw_store: RawObjectStore,
    activation: D2E3Activation | ReleaseManifest,
) -> dg.Definitions:
    if not isinstance(activation, D2E3Activation):
        raise ValueError("D2 E3 is restricted to explicit Local/CI activation")
    return dg.Definitions(
        assets=[materialize_mvp_medium_validation_e3],
        resources={
            "mvp_medium_validation_e3_runner": cast(
                Any,
                D2E3RunnerResource(
                    repository_root=repository_root,
                    connection=connection,
                    raw_store=raw_store,
                    activation=activation,
                ),
            )
        },
    )


__all__ = [
    "D2_E3_ASSET_NAME",
    "D2E3Activation",
    "D2E3CapturePlan",
    "D2E3Evidence",
    "D2E3RowCompleteManifest",
    "FailurePoint",
    "FrozenToptDenominator",
    "FrozenToptMarketFixture",
    "ToptInstrument",
    "build_d2_e3_definitions",
    "load_topt_denominator",
    "load_topt_market_fixture",
    "materialize_mvp_medium_validation_e3",
    "run_d2_e3",
]
