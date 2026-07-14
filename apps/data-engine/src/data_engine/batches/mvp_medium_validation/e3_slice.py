"""Terminal E3 validation of the D2 data plane on the exact TOPT denominator."""

import hashlib
import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
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
    _demand,
    _draft,
    _policy_bindings,
    _raw_object_ref,
    run_d2_e2,
)
from data_engine.contract_repository import PostgresSnapshotRepository
from data_engine.mvp_medium_models import MarketPricePayload
from data_engine.mvp_medium_pipeline import (
    LandedMediumCapture,
    MediumAdapterRegistration,
    MediumCaptureWorkItem,
    MediumComponentCatalog,
    MediumNormalizerRegistration,
    land_medium_capture_plan,
    normalize_medium_capture_batch,
)
from data_engine.mvp_medium_registry import (
    IDENTITY_SOURCE_ID,
    ISSUER_SECURITY_TYPE_ID,
    MEDIUM_VERSION,
    MEMBERSHIP_SOURCE_ID,
    SECURITY_LISTING_TYPE_ID,
    UNIVERSE_MEMBERSHIP_TYPE_ID,
)
from data_engine.mvp_medium_repository import (
    PostgresMediumSemanticRepository,
    build_medium_repository_registrations,
)
from data_engine.mvp_medium_snapshot import PostgresMediumSnapshotResolver, build_medium_snapshot
from psycopg import Connection
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from truealpha_contracts import (
    DataSource,
    IssuerSecurityLink,
    ListingRole,
    RawObjectStore,
    SecurityKind,
    SecurityListingLink,
    UniverseDefinitionKind,
    UniverseManifest,
    UniverseMembership,
)
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.data_quality import DataDomain
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
TOPT_PRICE_SOURCE_ID = "source.fixture-topt-yahoo-market"


def _sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


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
        normalized_ids = tuple(sorted(set(self.normalized_record_ids)))
        if len(normalized_ids) != TOPT_NORMALIZED_RECORD_COUNT or set(normalized_ids) != set().union(*selected_sets):
            raise ValueError("D2 E3 normalized vintages do not reconcile")
        if self.created_at < max(snapshot.resolved_at for snapshot in snapshots):
            raise ValueError("D2 E3 evidence cannot predate its snapshots")
        object.__setattr__(self, "snapshots", snapshots)
        object.__setattr__(self, "row_manifests", rows)
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
    payload = json.loads(artifact.body, parse_float=Decimal)
    scope = payload.get("scope")
    if not isinstance(scope, dict):
        raise ValueError("D2 E3 TOPT scope is missing")
    source = scope.get("source")
    if not isinstance(source, dict):
        raise ValueError("D2 E3 TOPT source identity is missing")
    return FrozenToptDenominator.model_validate(
        {
            "artifact_sha256": artifact.sha256,
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
    fixture = FrozenToptMarketFixture.model_validate(json.loads(body, parse_float=Decimal))
    denominator_coordinates = {(item.cusip, item.ticker, item.issuer_lei) for item in denominator.instruments}
    fixture_coordinates = {(item.cusip, item.ticker, item.issuer_lei) for item in fixture.rows}
    if fixture_coordinates != denominator_coordinates:
        raise ValueError("TOPT market fixture does not match the frozen denominator")
    return fixture


def _e3_registry(parent: RegistrySnapshot) -> RegistrySnapshot:
    """Add the TOPT fixture source without mutating the accepted E2 entries."""

    implementation_sha256 = _sha256(Path(__file__).read_bytes())
    source = SourceRegistryEntry(
        source_id=TOPT_PRICE_SOURCE_ID,
        version=MEDIUM_VERSION,
        adapter_id="data_engine.d2_e3:topt_yahoo_market_adapter",
        adapter_version=MEDIUM_VERSION,
        normalizer_id="data_engine.d2_e3:topt_yahoo_market_normalizer",
        normalizer_version=MEDIUM_VERSION,
        supported_domains=(DataDomain.MARKET_PRICES,),
        supported_type_ids=(MARKET_PRICE_TYPE_ID,),
        configuration_schema_sha256=canonical_sha256(
            {
                "fixture_sha256": TOPT_MARKET_FIXTURE_SHA256,
                "network": False,
                "credentials": False,
            }
        ),
        mapping_schema_sha256=canonical_sha256(MarketPricePayload.model_json_schema(mode="validation")),
        adapter_implementation_sha256=implementation_sha256,
        normalizer_implementation_sha256=implementation_sha256,
    )
    return parent.extend(sources=(source,))


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
        IDENTITY_SOURCE_ID: DataSource.SEC,
        MEMBERSHIP_SOURCE_ID: DataSource.SEC,
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
        links, _listings, _memberships, _prices = _payloads(
            denominator,
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
        _links, values, _memberships, _prices = _payloads(
            denominator,
            market_fixture,
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
        _links, _listings, values, _prices = _payloads(
            denominator,
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
        _links, _listings, _memberships, values = _payloads(
            denominator,
            market_fixture,
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
        (IDENTITY_SOURCE_ID, ISSUER_SECURITY_TYPE_ID): issuer_links,
        (IDENTITY_SOURCE_ID, SECURITY_LISTING_TYPE_ID): listings,
        (MEMBERSHIP_SOURCE_ID, UNIVERSE_MEMBERSHIP_TYPE_ID): memberships,
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

    registrations = []
    for registration in build_medium_repository_registrations(registry):
        if registration.semantic_type_id == ISSUER_SECURITY_TYPE_ID:
            registration = replace(registration, partition_filter=select_security)
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
            registration = replace(registration, partition_filter=select_listing)
        elif registration.semantic_type_id == UNIVERSE_MEMBERSHIP_TYPE_ID:
            registration = replace(registration, partition_filter=select_universe)
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
            source_id=IDENTITY_SOURCE_ID,
            source_version=MEDIUM_VERSION,
            semantic_type_ids=(ISSUER_SECURITY_TYPE_ID,),
            semantic_type_version=MEDIUM_VERSION,
            configuration=configuration(
                IDENTITY_SOURCE_ID,
                artifact="denominator",
                source=DataSource.SEC,
                semantic_type_id=ISSUER_SECURITY_TYPE_ID,
            ),
            recorded_at=recorded_at,
        ),
        MediumCaptureWorkItem(
            source_id=IDENTITY_SOURCE_ID,
            source_version=MEDIUM_VERSION,
            semantic_type_ids=(SECURITY_LISTING_TYPE_ID,),
            semantic_type_version=MEDIUM_VERSION,
            configuration=configuration(
                IDENTITY_SOURCE_ID,
                artifact="market",
                source=DataSource.SEC,
                semantic_type_id=SECURITY_LISTING_TYPE_ID,
            ),
            recorded_at=recorded_at,
        ),
        MediumCaptureWorkItem(
            source_id=MEMBERSHIP_SOURCE_ID,
            source_version=MEDIUM_VERSION,
            semantic_type_ids=(UNIVERSE_MEMBERSHIP_TYPE_ID,),
            semantic_type_version=MEDIUM_VERSION,
            configuration=configuration(
                MEMBERSHIP_SOURCE_ID,
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


def _records_by_document(records: tuple[NormalizedRecordRef, ...]) -> dict[str, NormalizedRecordRef]:
    by_document = {record.document_id: record for record in records}
    if len(by_document) != TOPT_REQUIRED_CELL_COUNT:
        raise ValueError("D2 E3 normalized documents are missing or duplicated")
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


def _snapshot_plan(
    *,
    denominator: FrozenToptDenominator,
    market_fixture: FrozenToptMarketFixture,
    registry: RegistrySnapshot,
    records_by_document: dict[str, NormalizedRecordRef],
    universe_manifest: UniverseManifest,
    as_of: datetime,
) -> tuple[
    SnapshotRequest,
    dict[str, tuple[NormalizedRecordRef, ...]],
    dict[str, str],
]:
    domain_by_type = {entry.semantic_type_id: entry.domain for entry in registry.semantic_types}
    demands: list[SnapshotDemandCell] = []
    selected: dict[str, tuple[NormalizedRecordRef, ...]] = {}
    document_by_cell: dict[str, str] = {}
    market_by_cusip = {row.cusip: row for row in market_fixture.rows}
    for instrument in denominator.instruments:
        market = market_by_cusip[instrument.cusip]
        identity_record = records_by_document[instrument.identity_document_id]
        listing_record = records_by_document[market.listing_document_id]
        membership_record = records_by_document[instrument.membership_id]
        price_record = records_by_document[market.price_document_id]
        for record, partition, label in (
            (
                identity_record,
                f"security:{instrument.security_id}",
                f"topt:{instrument.cusip}:issuer-security",
            ),
            (
                listing_record,
                f"listing:{market.listing_id}",
                f"topt:{instrument.cusip}:security-listing",
            ),
            (
                membership_record,
                f"universe:{denominator.universe_id}",
                f"topt:{instrument.cusip}:membership",
            ),
            (
                price_record,
                f"date:{market.trading_date.isoformat()}",
                f"topt:{instrument.cusip}:market-price",
            ),
        ):
            demand = _demand(
                record,
                domain=domain_by_type[record.draft.semantic_type_id],
                partition_key=partition,
                label=label,
            )
            demands.append(demand)
            selected[demand.planned_cell_id] = (record,)
            document_by_cell[demand.planned_cell_id] = record.document_id
    request = SnapshotRequest(
        universe=universe_manifest.ref,
        as_of=as_of,
        valid_on=denominator.report_date,
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


FailureInjector = Callable[[Literal["after-original-vintage"]], None]


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
        if len(original.normalized_records) != TOPT_REQUIRED_CELL_COUNT:
            raise ValueError("D2 E3 original denominator normalization is incomplete")
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
        if len(changed.normalized_records) != TOPT_REQUIRED_CELL_COUNT:
            raise ValueError("D2 E3 changed denominator normalization is incomplete")

        original_by_document = _records_by_document(original.normalized_records)
        changed_by_document = _records_by_document(changed.normalized_records)
        universe = _universe_manifest(denominator, effective_at=original_at)
        resolver = PostgresMediumSnapshotResolver(
            semantic_records=repository,
            snapshots=PostgresSnapshotRepository(connection),
        )
        original_request, original_fixture, original_documents = _snapshot_plan(
            denominator=denominator,
            market_fixture=market_fixture,
            registry=registry,
            records_by_document=original_by_document,
            universe_manifest=universe,
            as_of=original_at,
        )
        pre_knowable_request, _pre_fixture, _pre_documents = _snapshot_plan(
            denominator=denominator,
            market_fixture=market_fixture,
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
            denominator=denominator,
            market_fixture=market_fixture,
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
    "D2E3Evidence",
    "D2E3RowCompleteManifest",
    "FrozenToptDenominator",
    "FrozenToptMarketFixture",
    "ToptInstrument",
    "build_d2_e3_definitions",
    "load_topt_denominator",
    "load_topt_market_fixture",
    "materialize_mvp_medium_validation_e3",
    "run_d2_e3",
]
