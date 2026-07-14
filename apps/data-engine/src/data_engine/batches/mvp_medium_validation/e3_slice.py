"""Terminal Local/CI TOPT row-completeness runner for the D2 E3 rung."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal, cast
from xml.etree import ElementTree

import dagster as dg
from data_engine.batches.mvp_medium_validation.e1_slice import load_e1_corpus
from data_engine.batches.mvp_medium_validation.e2_slice import (
    D2_E2_SCHEMA_EPOCH,
    FrozenMediumCaptureConfiguration,
    MvpMediumValidationHandoff,
    run_d2_e2,
)
from data_engine.contract_repository import PostgresSnapshotRepository
from data_engine.mvp_medium_models import MarketPricePayload, MvpNormalizationDraft
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
    MediumRepositoryRegistration,
    PostgresMediumSemanticRepository,
    build_medium_repository_registrations,
)
from data_engine.mvp_medium_snapshot import (
    PostgresMediumSnapshotResolver,
    build_medium_snapshot,
)
from psycopg import Connection
from psycopg import Error as PsycopgError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from truealpha_contracts import (
    DataSource,
    IssuerSecurityLink,
    ListingRole,
    RawCapture,
    RawIngestionEnvelope,
    RawObjectRef,
    RawObjectStore,
    SecurityKind,
    SecurityListingLink,
    UniverseManifest,
    UniverseMembership,
)
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.data_quality import DataDomain
from truealpha_contracts.execution import (
    NormalizedRecordRef,
    PolicyBinding,
    PolicyRole,
    SnapshotDemandCell,
    SnapshotManifest,
    SnapshotRequest,
)
from truealpha_contracts.market import PriceBasis
from truealpha_contracts.registries import RegistrySnapshot, SourceRegistryEntry
from truealpha_contracts.release import ReleaseManifest
from truealpha_contracts.universe import (
    SubjectKind,
    SubjectRef,
    UniverseDefinitionKind,
)
from truealpha_contracts.usage import RequirementLevel

D2_E3_ASSET_NAME = "mvp_medium_validation_e3_handoff"
E3_CORPUS_PATH = Path("apps/data-engine/tests/fixtures/mvp_medium_validation/e3_corpus.v1.json")
BATCH_MANIFEST_PATH = Path("governance/batches/D2-mvp-medium-validation.v1.json")
E2_GOVERNANCE_HANDOFF_ID = (
    "handoff:d2-mvp-medium-validation:"
    "46162a55a54ba053b3effef97a95e6662c5da4052ca3ef656fd9440cb58b73be"
)
E2_GOVERNANCE_HANDOFF_SHA256 = "0031707dbdf97b1a5d45a4ebafa5f1029bf8567e157e3d12f1ecde26b73390bb"
E2_RUNTIME_HANDOFF_SHA256 = "e6c1206786f3dbbb79171f49e34555e7fadd96d7182752aa1e45a124825587a3"
E2_RUNTIME_HANDOFF_ID = f"mvp-medium-validation-handoff:{E2_RUNTIME_HANDOFF_SHA256}"
E2_REGISTRY_SNAPSHOT_ID = "registry-snapshot:77d61c305dc6f12b128518e6cfa5effc03f109ae5d0c25d9473e0a52df29f282"
E3_NPORT_SOURCE_ID = "source.fixture-e3-topt-nport"
E3_YAHOO_SOURCE_ID = "source.fixture-e3-topt-yahoo"
MARKET_PRICE_TYPE_ID = "semantic.market-price"
TOPT_MEMBERSHIP_PARTITION: Literal["universe:universe:topt-us-2026-03-31"] = (
    "universe:universe:topt-us-2026-03-31"
)
EXPECTED_TERMINAL_COUNTS = {
    ISSUER_SECURITY_TYPE_ID: 21,
    MARKET_PRICE_TYPE_ID: 21,
    SECURITY_LISTING_TYPE_ID: 21,
    UNIVERSE_MEMBERSHIP_TYPE_ID: 21,
}
EXPECTED_ADDED_PROJECTION_COUNTS = {
    "staging.mvp_issuer_security_links": 20,
    "staging.mvp_market_prices": 20,
    "staging.mvp_security_listing_links": 20,
    "staging.mvp_universe_memberships": 21,
}
EXPECTED_RETAINED_E2_PROJECTION_COUNTS = {
    "staging.filing_documents": 2,
    "staging.mvp_corporate_actions": 2,
    "staging.mvp_financial_facts": 2,
    "staging.mvp_issuer_security_links": 1,
    "staging.mvp_market_prices": 2,
    "staging.mvp_security_listing_links": 1,
    "staging.mvp_universe_memberships": 203,
}
_TOP_LEVEL_KEYS = {
    "schema_version",
    "corpus_id",
    "parent_corpus",
    "source_ceiling",
    "producer_handoff",
    "policy_state",
    "rung_scope",
    "temporal_contract",
    "universe",
    "artifacts",
    "row_completeness_demand",
    "action_window_coverage_demand",
    "retained_e2_evidence",
    "controls",
}
_YAHOO_RESULT_REQUIRED_KEYS = frozenset({"meta", "timestamp", "indicators"})
_YAHOO_RESULT_OPTIONAL_KEYS = frozenset({"events"})
_YAHOO_META_KEYS = frozenset(
    {
        "chartPreviousClose",
        "currency",
        "currentTradingPeriod",
        "dataGranularity",
        "exchangeName",
        "exchangeTimezoneName",
        "fiftyTwoWeekHigh",
        "fiftyTwoWeekLow",
        "firstTradeDate",
        "fullExchangeName",
        "gmtoffset",
        "hasPrePostMarketData",
        "instrumentType",
        "longName",
        "priceHint",
        "range",
        "regularMarketDayHigh",
        "regularMarketDayLow",
        "regularMarketPrice",
        "regularMarketTime",
        "regularMarketVolume",
        "shortName",
        "symbol",
        "timezone",
        "validRanges",
    }
)
_YAHOO_TRADING_PERIOD_KEYS = frozenset({"pre", "regular", "post"})
_YAHOO_TRADING_SESSION_KEYS = frozenset({"timezone", "start", "end", "gmtoffset"})
_YAHOO_INDICATOR_KEYS = frozenset({"quote", "adjclose"})
_YAHOO_QUOTE_KEYS = frozenset({"open", "high", "low", "close", "volume"})
_YAHOO_ADJCLOSE_KEYS = frozenset({"adjclose"})
_YAHOO_DIVIDEND_KEYS = frozenset({"amount", "date"})
_YAHOO_SPLIT_KEYS = frozenset({"date", "numerator", "denominator", "splitRatio"})


def _sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _module_sha256() -> str:
    return _sha256(Path(__file__).read_bytes())


def _resolve_inside(root: Path, relative_path: str) -> Path:
    resolved_root = root.resolve()
    candidate = (root / relative_path).resolve()
    if not candidate.is_relative_to(resolved_root) or not candidate.is_file():
        raise ValueError(f"E3 artifact is outside the repository or missing: {relative_path}")
    return candidate


def _no_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _load_json_bytes(body: bytes, *, label: str) -> Any:
    try:
        return json.loads(
            body,
            parse_float=Decimal,
            object_pairs_hook=_no_duplicate_object,
        )
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError(f"{label} is not valid JSON") from error


def _required_mapping(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return cast(dict[str, Any], value)


def _required_list(value: Any, *, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return cast(list[Any], value)


def _require_frozen_keys(
    value: dict[str, Any],
    *,
    required: frozenset[str],
    label: str,
    optional: frozenset[str] = frozenset(),
) -> None:
    keys = frozenset(value)
    if not required <= keys or keys - required - optional:
        raise ValueError(f"{label} schema drifted")


def _required_text(value: dict[str, Any], key: str, *, label: str = "E3 corpus") -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result or result != result.strip():
        raise ValueError(f"{label}.{key} must be a non-empty string")
    return result


def _aware(value: Any, *, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an ISO datetime string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{label} must be an ISO datetime string") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return parsed


def _strict_decimal(value: Any, *, label: str) -> Decimal:
    if isinstance(value, (float, bool)):
        raise ValueError(f"{label} must not use a binary float")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{label} is not a Decimal literal") from error
    if not parsed.is_finite():
        raise ValueError(f"{label} must be finite")
    return parsed


class FrozenE3Instrument(BaseModel):
    """One frozen ticker/security/listing coordinate in the terminal denominator."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str = Field(pattern=r"^[A-Z.]+$")
    source_symbol: str = Field(pattern=r"^[A-Z.-]+$")
    cusip: str = Field(pattern=r"^[A-Z0-9]{9}$")
    issuer_lei: str = Field(pattern=r"^[A-Z0-9]{20}$")
    share_class: str = Field(min_length=1)
    exchange_mic: str = Field(pattern=r"^[A-Z0-9]{4}$")
    listing_id: str
    price_artifact_id: str

    @property
    def issuer_id(self) -> str:
        return f"issuer:lei:{self.issuer_lei}"

    @property
    def security_id(self) -> str:
        return f"security:cusip:{self.cusip}"

    @property
    def instrument_id(self) -> str:
        return self.listing_id


@dataclass(frozen=True)
class FrozenE3Artifact:
    artifact_id: str
    path: str
    sha256: str
    byte_length: int
    body: bytes
    metadata: dict[str, Any]


@dataclass(frozen=True)
class FrozenE3Corpus:
    corpus_id: str
    corpus_sha256: str
    instruments: tuple[FrozenE3Instrument, ...]
    artifacts: dict[str, FrozenE3Artifact]
    payload: dict[str, Any]
    valid_on: date
    session_close_at: datetime
    fetched_at: datetime
    knowable_at: datetime
    produced_at: datetime
    recorded_at: datetime
    nport_accepted_at: datetime


class ActionWindowCoverageCell(BaseModel):
    """Raw Yahoo response coverage, deliberately separate from action semantics."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str
    cusip: str
    instrument_id: str
    artifact_id: str
    raw_object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["captured_empty", "captured_event_observed"]
    response_event_status: Literal["empty", "observed"]
    observed_event_count: int = Field(ge=0)
    dividend_amounts: tuple[Decimal, ...] = ()
    split_ratios: tuple[str, ...] = ()

    @field_validator("dividend_amounts", mode="before")
    @classmethod
    def reject_float_amounts(cls, value: Any) -> Any:
        values = value if isinstance(value, (list, tuple)) else (value,)
        if any(isinstance(item, (float, bool)) for item in values):
            raise ValueError("action-window dividend amounts must use Decimal")
        return value

    @model_validator(mode="after")
    def validate_events(self) -> ActionWindowCoverageCell:
        if any(not value.is_finite() or value < 0 for value in self.dividend_amounts):
            raise ValueError("action-window dividend amounts must be finite and non-negative")
        observed = len(self.dividend_amounts) + len(self.split_ratios)
        if self.observed_event_count != observed:
            raise ValueError("action-window observed event count does not reconcile")
        if (self.response_event_status == "observed") != bool(observed):
            raise ValueError("action-window event status does not match parsed events")
        expected_status = "captured_event_observed" if observed else "captured_empty"
        if self.status != expected_status:
            raise ValueError("action-window corpus status does not match parsed events")
        return self


class D2E3RowCompletenessEvidence(BaseModel):
    """Content-addressed proof for the 84 decision cells and 21 raw windows."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str = Field(default="", pattern=r"^(?:|mvp-topt-row-evidence:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    required_cell_count: Literal[84] = 84
    observed_cell_count: Literal[84] = 84
    issuer_ids: tuple[str, ...] = Field(min_length=20, max_length=20)
    instrument_ids: tuple[str, ...] = Field(min_length=21, max_length=21)
    cell_keys: tuple[str, ...] = Field(min_length=84, max_length=84)
    domain_counts: dict[str, int]
    added_projection_counts: dict[str, int]
    fixture_snapshot_id: str
    fixture_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    postgres_snapshot_id: str
    postgres_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    action_window_cell_count: Literal[21] = 21
    action_window_coverage: tuple[ActionWindowCoverageCell, ...] = Field(
        min_length=21,
        max_length=21,
    )
    cutoff_as_of: dict[str, datetime]
    cutoff_domain_counts: dict[str, dict[str, int]]
    membership_partition_key: Literal[
        "universe:universe:topt-us-2026-03-31"
    ] = TOPT_MEMBERSHIP_PARTITION
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("row-evidence created_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def identify(self) -> D2E3RowCompletenessEvidence:
        issuers = tuple(sorted(set(self.issuer_ids)))
        instruments = tuple(sorted(set(self.instrument_ids)))
        cells = tuple(sorted(set(self.cell_keys)))
        coverage = tuple(sorted(self.action_window_coverage, key=lambda item: item.cusip))
        if len(issuers) != 20 or len(instruments) != 21 or len(cells) != 84:
            raise ValueError("E3 row-completeness denominator is incomplete or duplicated")
        if self.domain_counts != EXPECTED_TERMINAL_COUNTS:
            raise ValueError("E3 terminal domain counts drifted")
        if self.added_projection_counts != EXPECTED_ADDED_PROJECTION_COUNTS:
            raise ValueError("E3 added projection counts drifted")
        if (
            self.fixture_snapshot_id != self.postgres_snapshot_id
            or self.fixture_snapshot_sha256 != self.postgres_snapshot_sha256
        ):
            raise ValueError("E3 fixture/Postgres snapshot parity failed")
        if len({item.cusip for item in coverage}) != 21 or len({item.artifact_id for item in coverage}) != 21:
            raise ValueError("E3 action-window coverage is incomplete or duplicated")
        if Counter(item.response_event_status for item in coverage) != {"empty": 20, "observed": 1}:
            raise ValueError("E3 action-window response statuses drifted")
        observed = next(item for item in coverage if item.response_event_status == "observed")
        if observed.ticker != "MU" or observed.dividend_amounts != (Decimal("0.15"),):
            raise ValueError("E3 MU dividend observation drifted")
        expected_cutoffs = {
            "before_nport": {
                ISSUER_SECURITY_TYPE_ID: 1,
                MARKET_PRICE_TYPE_ID: 0,
                SECURITY_LISTING_TYPE_ID: 1,
                UNIVERSE_MEMBERSHIP_TYPE_ID: 0,
            },
            "before_prices": {
                ISSUER_SECURITY_TYPE_ID: 21,
                MARKET_PRICE_TYPE_ID: 1,
                SECURITY_LISTING_TYPE_ID: 21,
                UNIVERSE_MEMBERSHIP_TYPE_ID: 21,
            },
            "terminal": EXPECTED_TERMINAL_COUNTS,
        }
        if self.cutoff_domain_counts != expected_cutoffs or set(self.cutoff_as_of) != set(expected_cutoffs):
            raise ValueError("E3 multi-cutoff PIT evidence drifted")
        if not (
            self.cutoff_as_of["before_nport"]
            < self.cutoff_as_of["before_prices"]
            < self.cutoff_as_of["terminal"]
            <= self.created_at
        ):
            raise ValueError("E3 cutoff clocks are not monotonic")
        object.__setattr__(self, "issuer_ids", issuers)
        object.__setattr__(self, "instrument_ids", instruments)
        object.__setattr__(self, "cell_keys", cells)
        object.__setattr__(self, "action_window_coverage", coverage)
        payload = self.model_dump(mode="json", exclude={"evidence_id", "content_sha256"})
        expected_hash = canonical_sha256(payload)
        if self.content_sha256 and self.content_sha256 != expected_hash:
            raise ValueError("E3 row-evidence content hash mismatch")
        expected_id = f"mvp-topt-row-evidence:{expected_hash}"
        if self.evidence_id and self.evidence_id != expected_id:
            raise ValueError("E3 row-evidence ID mismatch")
        object.__setattr__(self, "content_sha256", expected_hash)
        object.__setattr__(self, "evidence_id", expected_id)
        return self


class MvpToptValidationHandoff(BaseModel):
    """Stable terminal E3 handoff, additive over the exact accepted E2 output."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    handoff_id: str = Field(default="", pattern=r"^(?:|mvp-topt-validation-handoff:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    schema_version: Literal[1] = 1
    batch_id: Literal["D2-mvp-medium-validation"] = "D2-mvp-medium-validation"
    rung: Literal["E3"] = "E3"
    schema_epoch: str = D2_E2_SCHEMA_EPOCH
    corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    e2_governance_handoff_id: str
    e2_governance_handoff_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    e2_handoff_id: str
    e2_handoff_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    registry_snapshot: RegistrySnapshot
    registry_history_ids: tuple[str, ...] = Field(min_length=4, max_length=4)
    registry_entry_sha256s: dict[str, str]
    raw_object_sha256s: tuple[str, ...] = Field(min_length=22, max_length=22)
    normalized_record_ids: tuple[str, ...] = Field(min_length=84, max_length=84)
    added_normalized_record_ids: tuple[str, ...] = Field(min_length=81, max_length=81)
    added_projection_record_ids: dict[str, tuple[str, ...]]
    snapshot: SnapshotManifest
    terminal_price_payloads: tuple[MarketPricePayload, ...] = Field(min_length=21, max_length=21)
    universe_manifest: UniverseManifest
    row_evidence: D2E3RowCompletenessEvidence
    retained_e2_projection_counts: dict[str, int]
    retained_e2_event_bundle_id: str
    retained_e2_action_record_ids: tuple[str, ...] = Field(min_length=2, max_length=2)
    retained_e1_case_ids: tuple[str, ...] = Field(min_length=7, max_length=7)
    changed_vintage_record_ids: tuple[str, ...] = Field(min_length=2, max_length=2)
    prior_vintage_record_ids: tuple[str, ...] = Field(min_length=1, max_length=1)
    terminal_nvda_price_record_id: str
    append_only_controls: dict[str, bool]
    closure_evidence_ids: dict[str, str]
    retry_safe: Literal[True] = True
    allowed_consumers: tuple[str, ...] = ("D2-mvp-medium-validation",)
    allowed_environments: tuple[Literal["local", "ci"], ...] = ("ci", "local")
    stable_handoff: Literal[True] = True
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("E3 handoff created_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def identify(self) -> MvpToptValidationHandoff:
        if (
            self.e2_governance_handoff_id != E2_GOVERNANCE_HANDOFF_ID
            or self.e2_governance_handoff_sha256 != E2_GOVERNANCE_HANDOFF_SHA256
            or self.e2_handoff_id != E2_RUNTIME_HANDOFF_ID
            or self.e2_handoff_sha256 != E2_RUNTIME_HANDOFF_SHA256
            or self.schema_epoch != D2_E2_SCHEMA_EPOCH
        ):
            raise ValueError("E3 handoff does not bind the exact accepted E2 producer")
        if self.registry_snapshot.parent_snapshot_id != E2_REGISTRY_SNAPSHOT_ID:
            raise ValueError("E3 registry does not extend the accepted E2 snapshot")
        if self.registry_history_ids[-1] != self.registry_snapshot.registry_snapshot_id:
            raise ValueError("E3 registry history does not terminate at its published snapshot")
        entries = {
            **{
                entry.source_registry_entry_id: entry.content_sha256
                for entry in self.registry_snapshot.sources
            },
            **{
                entry.semantic_type_registry_entry_id: entry.content_sha256
                for entry in self.registry_snapshot.semantic_types
            },
        }
        if self.registry_entry_sha256s != dict(sorted(entries.items())):
            raise ValueError("E3 registry entry hashes are incomplete")
        raw_hashes = tuple(sorted(set(self.raw_object_sha256s)))
        normalized_ids = tuple(sorted(set(self.normalized_record_ids)))
        added_ids = tuple(sorted(set(self.added_normalized_record_ids)))
        if len(raw_hashes) != 22 or len(normalized_ids) != 84 or len(added_ids) != 81:
            raise ValueError("E3 handoff object or record identities are incomplete")
        projection_ids = {
            table: tuple(sorted(set(record_ids)))
            for table, record_ids in sorted(self.added_projection_record_ids.items())
        }
        if {table: len(ids) for table, ids in projection_ids.items()} != EXPECTED_ADDED_PROJECTION_COUNTS:
            raise ValueError("E3 handoff added projection IDs are incomplete")
        if {record_id for ids in projection_ids.values() for record_id in ids} != set(added_ids):
            raise ValueError("E3 added normalized and projection IDs do not reconcile")
        snapshot_ids = tuple(
            sorted(record.normalized_record_id for record in self.snapshot.normalized_records)
        )
        if normalized_ids != snapshot_ids or not set(added_ids) < set(normalized_ids):
            raise ValueError("E3 normalized IDs do not reconcile with its terminal snapshot")
        if self.snapshot.snapshot_id != self.row_evidence.postgres_snapshot_id:
            raise ValueError("E3 handoff snapshot does not bind row-completeness evidence")
        price_payloads = tuple(sorted(self.terminal_price_payloads, key=lambda item: item.ticker))
        snapshot_price_records = tuple(
            record
            for record in self.snapshot.normalized_records
            if record.draft.semantic_type_id == MARKET_PRICE_TYPE_ID
        )
        if (
            len({item.listing_id for item in price_payloads}) != 21
            or len(snapshot_price_records) != 21
            or {
                canonical_sha256(item.model_dump(mode="json"))
                for item in price_payloads
            }
            != {record.draft.payload_sha256 for record in snapshot_price_records}
        ):
            raise ValueError("E3 typed terminal price payloads do not match the snapshot")
        if self.snapshot.universe_manifest != self.universe_manifest:
            raise ValueError("E3 handoff universe manifest drifted")
        if self.retained_e2_projection_counts != EXPECTED_RETAINED_E2_PROJECTION_COUNTS:
            raise ValueError("E3 handoff did not retain the exact E2 projection evidence")
        if len(set(self.retained_e2_action_record_ids)) != 2:
            raise ValueError("E3 handoff did not retain both E2 action sentinels")
        if self.terminal_nvda_price_record_id not in self.changed_vintage_record_ids:
            raise ValueError("E3 terminal NVDA selection is outside its changed-vintage evidence")
        if set(self.prior_vintage_record_ids) != (
            set(self.changed_vintage_record_ids) - {self.terminal_nvda_price_record_id}
        ):
            raise ValueError("E3 prior NVDA vintage is not retained exactly")
        if self.append_only_controls != {"delete_rejected": True, "update_rejected": True}:
            raise ValueError("E3 append-only mutation controls did not pass")
        expected_closure = {
            "e2_complete_domain_handoff": self.e2_handoff_id,
            "e3_row_completeness_evidence": self.row_evidence.evidence_id,
            "e3_terminal_snapshot": self.snapshot.snapshot_id,
        }
        if self.closure_evidence_ids != expected_closure:
            raise ValueError("E3 issue-23 closure evidence is incomplete")
        if tuple(sorted(set(self.allowed_consumers))) != ("D2-mvp-medium-validation",):
            raise ValueError("E3 consumer allow-list drifted")
        if tuple(sorted(set(self.allowed_environments))) != ("ci", "local"):
            raise ValueError("E3 execution must remain Local/CI only")
        if self.created_at < max(self.snapshot.resolved_at, self.row_evidence.created_at):
            raise ValueError("E3 handoff cannot predate its evidence")
        object.__setattr__(self, "registry_entry_sha256s", dict(sorted(entries.items())))
        object.__setattr__(self, "raw_object_sha256s", raw_hashes)
        object.__setattr__(self, "normalized_record_ids", normalized_ids)
        object.__setattr__(self, "added_normalized_record_ids", added_ids)
        object.__setattr__(self, "added_projection_record_ids", projection_ids)
        object.__setattr__(self, "terminal_price_payloads", price_payloads)
        object.__setattr__(self, "retained_e2_action_record_ids", tuple(sorted(self.retained_e2_action_record_ids)))
        object.__setattr__(self, "retained_e1_case_ids", tuple(sorted(set(self.retained_e1_case_ids))))
        if (
            self.changed_vintage_record_ids[-1] != self.terminal_nvda_price_record_id
            or self.prior_vintage_record_ids != self.changed_vintage_record_ids[:-1]
        ):
            raise ValueError("E3 changed-vintage IDs are not in transaction-time order")
        object.__setattr__(self, "allowed_consumers", ("D2-mvp-medium-validation",))
        object.__setattr__(self, "allowed_environments", ("ci", "local"))
        payload = self.model_dump(mode="json", exclude={"handoff_id", "content_sha256"})
        expected_hash = canonical_sha256(payload)
        if self.content_sha256 and self.content_sha256 != expected_hash:
            raise ValueError("E3 handoff content hash mismatch")
        expected_id = f"mvp-topt-validation-handoff:{expected_hash}"
        if self.handoff_id and self.handoff_id != expected_id:
            raise ValueError("E3 handoff ID mismatch")
        object.__setattr__(self, "content_sha256", expected_hash)
        object.__setattr__(self, "handoff_id", expected_id)
        return self


def _artifact_from_declaration(
    repository_root: Path,
    declaration: dict[str, Any],
) -> FrozenE3Artifact:
    artifact_id = _required_text(declaration, "artifact_id")
    path = _required_text(declaration, "path", label=artifact_id)
    expected_sha256 = _required_text(declaration, "sha256", label=artifact_id)
    byte_length = declaration.get("byte_length")
    if not isinstance(byte_length, int) or isinstance(byte_length, bool) or byte_length < 1:
        raise ValueError(f"{artifact_id}.byte_length must be a positive integer")
    body = _resolve_inside(repository_root, path).read_bytes()
    if len(body) != byte_length or _sha256(body) != expected_sha256:
        raise ValueError(f"E3 artifact checksum or byte length drifted: {artifact_id}")
    return FrozenE3Artifact(
        artifact_id=artifact_id,
        path=path,
        sha256=expected_sha256,
        byte_length=byte_length,
        body=body,
        metadata=dict(declaration),
    )


def _parse_nport_holdings(
    body: bytes,
) -> tuple[dict[str, str], tuple[dict[str, str], ...]]:
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as error:
        raise ValueError("E3 N-PORT artifact is malformed XML") from error
    gen_info = root.find(".//{*}genInfo")
    if gen_info is None:
        raise ValueError("E3 N-PORT genInfo is missing")

    def text(element: ElementTree.Element, name: str) -> str:
        value = element.findtext(f"{{*}}{name}")
        if value is None or not value.strip():
            raise ValueError(f"E3 N-PORT {name} is missing")
        return value.strip()

    source = {
        "registrant_cik": text(gen_info, "regCik"),
        "series": text(gen_info, "seriesId"),
        "report_date": text(gen_info, "repPdDate"),
    }
    holdings: list[dict[str, str]] = []
    for position in root.findall(".//{*}invstOrSec"):
        holdings.append(
            {
                "name": text(position, "name"),
                "lei": text(position, "lei"),
                "cusip": text(position, "cusip"),
                "asset_category": text(position, "assetCat"),
            }
        )
    return source, tuple(holdings)


def _yahoo_result(
    artifact: FrozenE3Artifact,
    instrument: FrozenE3Instrument,
    *,
    target_timestamp: int,
) -> dict[str, Any]:
    payload = _required_mapping(
        _load_json_bytes(artifact.body, label=artifact.artifact_id),
        label=artifact.artifact_id,
    )
    if set(payload) != {"chart"}:
        raise ValueError(f"{artifact.artifact_id} Yahoo root schema drifted")
    chart = _required_mapping(payload.get("chart"), label=f"{artifact.artifact_id}.chart")
    if set(chart) != {"error", "result"} or chart.get("error") is not None:
        raise ValueError(f"{artifact.artifact_id} Yahoo chart envelope drifted")
    results = _required_list(chart.get("result"), label=f"{artifact.artifact_id}.chart.result")
    if len(results) != 1:
        raise ValueError(f"{artifact.artifact_id} must contain exactly one Yahoo result")
    result = _required_mapping(results[0], label=f"{artifact.artifact_id}.chart.result[0]")
    _require_frozen_keys(
        result,
        required=_YAHOO_RESULT_REQUIRED_KEYS,
        optional=_YAHOO_RESULT_OPTIONAL_KEYS,
        label=f"{artifact.artifact_id} Yahoo result",
    )
    meta = _required_mapping(result.get("meta"), label=f"{artifact.artifact_id}.meta")
    _require_frozen_keys(
        meta,
        required=_YAHOO_META_KEYS,
        label=f"{artifact.artifact_id} Yahoo meta",
    )
    required_meta = {
        "currency": "USD",
        "symbol": instrument.source_symbol,
        "exchangeTimezoneName": "America/New_York",
        "instrumentType": "EQUITY",
        "dataGranularity": "1d",
    }
    if any(meta.get(key) != expected for key, expected in required_meta.items()):
        raise ValueError(f"{artifact.artifact_id} Yahoo subject or market metadata drifted")
    trading_period = _required_mapping(
        meta.get("currentTradingPeriod"),
        label=f"{artifact.artifact_id}.meta.currentTradingPeriod",
    )
    _require_frozen_keys(
        trading_period,
        required=_YAHOO_TRADING_PERIOD_KEYS,
        label=f"{artifact.artifact_id} Yahoo meta.currentTradingPeriod",
    )
    for session_name in _YAHOO_TRADING_PERIOD_KEYS:
        session = _required_mapping(
            trading_period.get(session_name),
            label=f"{artifact.artifact_id}.meta.currentTradingPeriod.{session_name}",
        )
        _require_frozen_keys(
            session,
            required=_YAHOO_TRADING_SESSION_KEYS,
            label=f"{artifact.artifact_id} Yahoo meta.currentTradingPeriod.{session_name}",
        )
    timestamps = _required_list(result.get("timestamp"), label=f"{artifact.artifact_id}.timestamp")
    if (
        len(timestamps) != 2
        or any(not isinstance(value, int) or isinstance(value, bool) for value in timestamps)
        or timestamps.count(target_timestamp) != 1
    ):
        raise ValueError(f"{artifact.artifact_id} Yahoo timestamp window drifted")
    indicators = _required_mapping(
        result.get("indicators"),
        label=f"{artifact.artifact_id}.indicators",
    )
    _require_frozen_keys(
        indicators,
        required=_YAHOO_INDICATOR_KEYS,
        label=f"{artifact.artifact_id} Yahoo indicators",
    )
    quotes = _required_list(indicators.get("quote"), label=f"{artifact.artifact_id}.quote")
    if len(quotes) != 1:
        raise ValueError(f"{artifact.artifact_id} must contain exactly one quote series")
    quote = _required_mapping(quotes[0], label=f"{artifact.artifact_id}.quote[0]")
    _require_frozen_keys(
        quote,
        required=_YAHOO_QUOTE_KEYS,
        label=f"{artifact.artifact_id} Yahoo quote",
    )
    for field_name in ("open", "high", "low", "close", "volume"):
        values = _required_list(
            quote.get(field_name),
            label=f"{artifact.artifact_id}.quote.{field_name}",
        )
        if len(values) != len(timestamps) or values[timestamps.index(target_timestamp)] is None:
            raise ValueError(f"{artifact.artifact_id} Yahoo {field_name} series drifted")
    adjusted_closes = _required_list(
        indicators.get("adjclose"),
        label=f"{artifact.artifact_id}.adjclose",
    )
    if len(adjusted_closes) != 1:
        raise ValueError(f"{artifact.artifact_id} must contain exactly one adjusted-close series")
    adjusted_close = _required_mapping(
        adjusted_closes[0],
        label=f"{artifact.artifact_id}.adjclose[0]",
    )
    _require_frozen_keys(
        adjusted_close,
        required=_YAHOO_ADJCLOSE_KEYS,
        label=f"{artifact.artifact_id} Yahoo adjclose",
    )
    adjusted_close_values = _required_list(
        adjusted_close.get("adjclose"),
        label=f"{artifact.artifact_id}.adjclose.adjclose",
    )
    if len(adjusted_close_values) != len(timestamps):
        raise ValueError(f"{artifact.artifact_id} Yahoo adjclose series drifted")
    return result


def _parsed_action_window(
    *,
    corpus_cell: dict[str, Any],
    artifact: FrozenE3Artifact,
    instrument: FrozenE3Instrument,
    result: dict[str, Any],
) -> ActionWindowCoverageCell:
    events = result.get("events")
    dividend_amounts: list[Decimal] = []
    split_ratios: list[str] = []
    observed_payloads: list[dict[str, Any]] = []
    if events is not None:
        event_map = _required_mapping(events, label=f"{artifact.artifact_id}.events")
        if set(event_map) - {"dividends", "splits"}:
            raise ValueError(f"{artifact.artifact_id} contains an unsupported Yahoo event type")
        dividends = event_map.get("dividends", {})
        if dividends is not None:
            for source_key, raw_event in _required_mapping(
                dividends,
                label=f"{artifact.artifact_id}.events.dividends",
            ).items():
                event = _required_mapping(raw_event, label=f"{artifact.artifact_id}.dividend")
                _require_frozen_keys(
                    event,
                    required=_YAHOO_DIVIDEND_KEYS,
                    label=f"{artifact.artifact_id} Yahoo dividend",
                )
                amount = _strict_decimal(event.get("amount"), label=f"{artifact.artifact_id}.amount")
                event_date = event.get("date")
                if not source_key.isdigit() or not isinstance(event_date, int) or isinstance(event_date, bool):
                    raise ValueError(f"{artifact.artifact_id} dividend timestamp schema drifted")
                dividend_amounts.append(amount)
                observed_payloads.append(
                    {
                        "event_type": "dividend",
                        "source_event_key": int(source_key),
                        "source_event_date": event_date,
                        "amount": str(amount),
                    }
                )
        splits = event_map.get("splits", {})
        if splits is not None:
            for source_key, raw_event in _required_mapping(
                splits,
                label=f"{artifact.artifact_id}.events.splits",
            ).items():
                event = _required_mapping(raw_event, label=f"{artifact.artifact_id}.split")
                _require_frozen_keys(
                    event,
                    required=_YAHOO_SPLIT_KEYS,
                    label=f"{artifact.artifact_id} Yahoo split",
                )
                ratio = event.get("splitRatio")
                event_date = event.get("date")
                if (
                    not source_key.isdigit()
                    or not isinstance(ratio, str)
                    or not ratio
                    or not isinstance(event_date, int)
                    or isinstance(event_date, bool)
                ):
                    raise ValueError(f"{artifact.artifact_id} split schema drifted")
                split_ratios.append(ratio)
                observed_payloads.append(
                    {
                        "event_type": "split",
                        "source_event_key": int(source_key),
                        "source_event_date": event_date,
                        "split_ratio": ratio,
                    }
                )
    expected_events = corpus_cell.get("observed_events", [])
    if expected_events != observed_payloads:
        raise ValueError(f"{artifact.artifact_id} action-window events drifted from the corpus")
    return ActionWindowCoverageCell(
        ticker=instrument.ticker,
        cusip=instrument.cusip,
        instrument_id=instrument.instrument_id,
        artifact_id=artifact.artifact_id,
        raw_object_sha256=artifact.sha256,
        status=cast(Any, corpus_cell.get("status")),
        response_event_status="observed" if observed_payloads else "empty",
        observed_event_count=len(observed_payloads),
        dividend_amounts=tuple(dividend_amounts),
        split_ratios=tuple(split_ratios),
    )


def _verify_producer_handoff(repository_root: Path, producer: dict[str, Any]) -> None:
    path = _required_text(producer, "path", label="producer_handoff")
    body = _resolve_inside(repository_root, path).read_bytes()
    if _sha256(body) != E2_GOVERNANCE_HANDOFF_SHA256 or producer.get("sha256") != _sha256(body):
        raise ValueError("accepted E2 governance handoff bytes drifted")
    handoff = _required_mapping(
        _load_json_bytes(body, label="E2 governance handoff"),
        label="E2 governance handoff",
    )
    if (
        producer.get("handoff_id") != E2_GOVERNANCE_HANDOFF_ID
        or producer.get("runtime_handoff_id") != E2_RUNTIME_HANDOFF_ID
        or producer.get("runtime_handoff_sha256") != E2_RUNTIME_HANDOFF_SHA256
        or producer.get("registry_snapshot_id") != E2_REGISTRY_SNAPSHOT_ID
        or producer.get("schema_epoch") != D2_E2_SCHEMA_EPOCH
        or producer.get("state") != "accepted"
        or producer.get("allowed_consumer") != "D2-mvp-medium-validation"
        or tuple(sorted(producer.get("allowed_environments", ()))) != ("ci", "local")
        or handoff.get("handoff_id") != E2_GOVERNANCE_HANDOFF_ID
        or handoff.get("state") != "accepted"
        or handoff.get("schema_epoch") != D2_E2_SCHEMA_EPOCH
        or "D2-mvp-medium-validation" not in handoff.get("allowed_consumers", ())
        or tuple(sorted(handoff.get("allowed_environments", ()))) != ("ci", "local")
        or any(handoff.get("revocation", {}).values())
    ):
        raise ValueError("accepted E2 producer handoff is not active for E3 Local/CI")


def load_e3_corpus(repository_root: Path) -> FrozenE3Corpus:
    """Load and fail-closed validate the exact additive E3 corpus."""

    corpus_file = _resolve_inside(repository_root, E3_CORPUS_PATH.as_posix())
    corpus_bytes = corpus_file.read_bytes()
    corpus_sha256 = _sha256(corpus_bytes)
    batch_manifest = _required_mapping(
        _load_json_bytes(
            _resolve_inside(repository_root, BATCH_MANIFEST_PATH.as_posix()).read_bytes(),
            label="D2 batch manifest",
        ),
        label="D2 batch manifest",
    )
    terminal = _required_mapping(
        batch_manifest.get("terminal_corpus"),
        label="D2 batch terminal_corpus",
    )
    if terminal != {"manifest_path": E3_CORPUS_PATH.as_posix(), "sha256": corpus_sha256}:
        raise ValueError("E3 corpus does not match the canonical batch terminal_corpus binding")
    corpus = _required_mapping(_load_json_bytes(corpus_bytes, label="E3 corpus"), label="E3 corpus")
    if (
        set(corpus) != _TOP_LEVEL_KEYS
        or corpus.get("schema_version") != 1
        or corpus.get("corpus_id") != "d2-mvp-medium-validation-e3-v1"
    ):
        raise ValueError("E3 corpus schema or scope drifted")

    parent = _required_mapping(corpus.get("parent_corpus"), label="parent_corpus")
    parent_path = _required_text(parent, "path", label="parent_corpus")
    parent_body = _resolve_inside(repository_root, parent_path).read_bytes()
    if (
        parent.get("relationship") != "additive-terminal-corpus"
        or parent.get("sha256") != _sha256(parent_body)
        or batch_manifest.get("corpus", {}).get("manifest_path") != parent_path
        or batch_manifest.get("corpus", {}).get("sha256") != _sha256(parent_body)
    ):
        raise ValueError("E3 parent corpus binding drifted")
    producer = _required_mapping(corpus.get("producer_handoff"), label="producer_handoff")
    _verify_producer_handoff(repository_root, producer)

    scope = _required_mapping(corpus.get("rung_scope"), label="rung_scope")
    if scope != {
        "frozen_target_rung": "E3",
        "terminal_denominator_frozen": True,
        "issuer_count": 20,
        "instrument_count": 21,
        "required_terminal_cell_count": 84,
        "reused_e2_cell_count": 3,
        "new_e3_cell_count": 81,
        "runtime": "offline-local-ci-only",
        "revision_rule": scope.get("revision_rule"),
    } or not isinstance(scope.get("revision_rule"), str):
        raise ValueError("E3 frozen rung scope drifted")

    temporal = _required_mapping(corpus.get("temporal_contract"), label="temporal_contract")
    valid_on = date.fromisoformat(_required_text(temporal, "valid_on", label="temporal_contract"))
    session_close_at = _aware(temporal.get("session_close_at"), label="session_close_at")
    fetched_at = _aware(temporal.get("fetched_at"), label="fetched_at")
    knowable_at = _aware(temporal.get("knowable_at"), label="knowable_at")
    produced_at = _aware(temporal.get("produced_at"), label="produced_at")
    recorded_at = _aware(temporal.get("recorded_at"), label="recorded_at")
    target_timestamp = temporal.get("target_source_timestamp")
    if (
        valid_on != date(2026, 3, 31)
        or not isinstance(target_timestamp, int)
        or isinstance(target_timestamp, bool)
        or datetime.fromtimestamp(target_timestamp, tz=UTC).date() != valid_on
        or not (session_close_at <= fetched_at == knowable_at == produced_at < recorded_at)
        or temporal.get("currency") != "USD"
        or temporal.get("timezone") != "America/New_York"
        or temporal.get("price_basis") != "unadjusted"
    ):
        raise ValueError("E3 temporal contract drifted")

    universe = _required_mapping(corpus.get("universe"), label="universe")
    raw_instruments = _required_list(universe.get("instruments"), label="universe.instruments")
    instruments = tuple(
        sorted(
            (
                FrozenE3Instrument.model_validate(
                    _required_mapping(item, label="universe.instrument")
                )
                for item in raw_instruments
            ),
            key=lambda item: item.ticker,
        )
    )
    if (
        len(instruments) != 21
        or len({item.ticker for item in instruments}) != 21
        or len({item.cusip for item in instruments}) != 21
        or len({item.listing_id for item in instruments}) != 21
        or len({item.issuer_lei for item in instruments}) != 20
        or len({item.price_artifact_id for item in instruments}) != 21
    ):
        raise ValueError("E3 instrument denominator shrank or contains duplicates")
    by_ticker = {item.ticker: item for item in instruments}
    if (
        set(by_ticker) != {
            "AAPL", "ABBV", "AMZN", "AVGO", "BRK.B", "COST", "GOOG", "GOOGL",
            "JNJ", "JPM", "LLY", "MA", "META", "MSFT", "MU", "NFLX", "NVDA",
            "TSLA", "V", "WMT", "XOM",
        }
        or by_ticker["GOOG"].issuer_lei != by_ticker["GOOGL"].issuer_lei
        or by_ticker["GOOG"].cusip == by_ticker["GOOGL"].cusip
    ):
        raise ValueError("E3 ticker or Alphabet subject invariants drifted")
    raw_issuers = _required_list(universe.get("issuers"), label="universe.issuers")
    declared_issuer_leis = {
        _required_text(_required_mapping(item, label="universe.issuer"), "issuer_lei")
        for item in raw_issuers
    }
    if len(raw_issuers) != 20 or declared_issuer_leis != {item.issuer_lei for item in instruments}:
        raise ValueError("E3 issuer denominator does not match its instruments")

    raw_artifacts = _required_list(corpus.get("artifacts"), label="artifacts")
    artifacts: dict[str, FrozenE3Artifact] = {}
    for raw_artifact in raw_artifacts:
        artifact = _artifact_from_declaration(
            repository_root,
            _required_mapping(raw_artifact, label="artifact"),
        )
        if artifact.artifact_id in artifacts:
            raise ValueError(f"duplicate E3 artifact: {artifact.artifact_id}")
        artifacts[artifact.artifact_id] = artifact
    expected_artifact_ids = {
        "topt-candidate-denominator",
        "topt-nport-primary-document",
        *(item.price_artifact_id for item in instruments),
    }
    if len(artifacts) != 23 or set(artifacts) != expected_artifact_ids:
        raise ValueError("E3 artifact set is missing, duplicated, or unexpected")

    candidate = _required_mapping(
        _load_json_bytes(
            artifacts["topt-candidate-denominator"].body,
            label="issue-59 candidate denominator",
        ),
        label="issue-59 candidate denominator",
    )
    candidate_scope = _required_mapping(candidate.get("scope"), label="issue-59 scope")
    candidate_rows = _required_list(
        candidate_scope.get("selected_instruments"),
        label="issue-59 selected_instruments",
    )
    candidate_pairs = {
        (
            _required_text(_required_mapping(item, label="issue-59 instrument"), "cusip"),
            _required_text(_required_mapping(item, label="issue-59 instrument"), "issuer_lei"),
        )
        for item in candidate_rows
    }
    frozen_pairs = {(item.cusip, item.issuer_lei) for item in instruments}
    if (
        candidate.get("state") != "candidate_unapproved"
        or candidate_scope.get("universe_id") != universe.get("universe_id")
        or len(candidate_rows) != 21
        or candidate_pairs != frozen_pairs
    ):
        raise ValueError("E3 issue-59 denominator cross-check failed")

    nport = artifacts["topt-nport-primary-document"]
    nport_source, holdings = _parse_nport_holdings(nport.body)
    universe_source = _required_mapping(universe.get("source"), label="universe.source")
    included = tuple(item for item in holdings if item["asset_category"] == "EC")
    excluded = Counter(item["asset_category"] for item in holdings if item["asset_category"] != "EC")
    included_pairs = {(item["cusip"], item["lei"]) for item in included}
    if (
        len(holdings) != 23
        or len(included) != 21
        or len({item["lei"] for item in included}) != 20
        or included_pairs != frozen_pairs
        or dict(sorted(excluded.items())) != {"DE": 1, "STIV": 1}
        or nport_source
        != {
            "registrant_cik": universe_source.get("registrant_cik"),
            "series": universe_source.get("series"),
            "report_date": universe_source.get("report_date"),
        }
        or universe_source.get("raw_holding_count") != 23
        or universe_source.get("included_holding_count") != 21
        or universe_source.get("included_asset_category") != "EC"
        or universe_source.get("primary_document_sha256") != nport.sha256
    ):
        raise ValueError("E3 N-PORT denominator or source metadata drifted")
    nport_accepted_at = _aware(universe_source.get("accepted_at"), label="N-PORT accepted_at")
    if not (session_close_at < nport_accepted_at < fetched_at):
        raise ValueError("E3 N-PORT acceptance clock is out of order")

    demand = _required_mapping(corpus.get("row_completeness_demand"), label="row demand")
    domains = _required_mapping(demand.get("domains"), label="row demand domains")
    expected_domain_plan = {
        "issuer_security_link": (21, 1, 20),
        "security_listing_link": (21, 1, 20),
        "market_price": (21, 1, 20),
        "universe_membership": (21, 0, 21),
    }
    for name, (terminal_count, reuse_count, add_count) in expected_domain_plan.items():
        plan = _required_mapping(domains.get(name), label=f"row demand {name}")
        if (
            plan.get("expected_terminal_count") != terminal_count
            or plan.get("reuse_e2_count") != reuse_count
            or plan.get("add_e3_count") != add_count
        ):
            raise ValueError(f"E3 {name} row demand drifted")
    if (
        demand.get("expected_terminal_cell_count") != 84
        or demand.get("expected_reused_e2_cell_count") != 3
        or demand.get("expected_new_e3_cell_count") != 81
        or len(demand.get("required_cusips", ())) != 21
        or set(demand.get("required_cusips", ())) != {item.cusip for item in instruments}
        or set(demand.get("excluded_from_topt_row_completeness", ()))
        != {"filing-document", "financial-fact", "corporate-action"}
    ):
        raise ValueError("E3 terminal row demand drifted")

    action_demand = _required_mapping(
        corpus.get("action_window_coverage_demand"),
        label="action_window_coverage_demand",
    )
    action_cells = _required_list(action_demand.get("cells"), label="action-window cells")
    cells_by_ticker: dict[str, dict[str, Any]] = {}
    for raw_cell in action_cells:
        cell = _required_mapping(raw_cell, label="action-window cell")
        ticker = _required_text(cell, "ticker", label="action-window cell")
        if ticker in cells_by_ticker:
            raise ValueError(f"duplicate E3 action-window ticker: {ticker}")
        cells_by_ticker[ticker] = cell
    if (
        action_demand.get("window_start") != "2026-03-31"
        or action_demand.get("window_end_exclusive") != "2026-04-02"
        or action_demand.get("requested_events") != ["div", "splits"]
        or action_demand.get("expected_coverage_cell_count") != 21
        or action_demand.get("empty_event_cell_count") != 20
        or action_demand.get("observed_event_cell_count") != 1
        or action_demand.get("decision_snapshot_cell_count") != 0
        or set(cells_by_ticker) != set(by_ticker)
    ):
        raise ValueError("E3 action-window demand drifted")
    parsed_cells: list[ActionWindowCoverageCell] = []
    for instrument in instruments:
        artifact = artifacts[instrument.price_artifact_id]
        if (
            artifact.metadata.get("source") != "yahoo-chart"
            or artifact.metadata.get("source_symbol") != instrument.source_symbol
            or artifact.metadata.get("semantic_type") != "market-price"
        ):
            raise ValueError(f"{artifact.artifact_id} artifact subject metadata drifted")
        result = _yahoo_result(
            artifact,
            instrument,
            target_timestamp=target_timestamp,
        )
        corpus_cell = cells_by_ticker[instrument.ticker]
        if (
            corpus_cell.get("cusip") != instrument.cusip
            or corpus_cell.get("artifact_id") != instrument.price_artifact_id
        ):
            raise ValueError(f"{instrument.ticker} action-window subject drifted")
        parsed_cells.append(
            _parsed_action_window(
                corpus_cell=corpus_cell,
                artifact=artifact,
                instrument=instrument,
                result=result,
            )
        )
    if Counter(item.response_event_status for item in parsed_cells) != {"empty": 20, "observed": 1}:
        raise ValueError("E3 action-window response coverage drifted")

    return FrozenE3Corpus(
        corpus_id=cast(str, corpus["corpus_id"]),
        corpus_sha256=corpus_sha256,
        instruments=instruments,
        artifacts=artifacts,
        payload=corpus,
        valid_on=valid_on,
        session_close_at=session_close_at,
        fetched_at=fetched_at,
        knowable_at=knowable_at,
        produced_at=produced_at,
        recorded_at=recorded_at,
        nport_accepted_at=nport_accepted_at,
    )


def _source_entry(
    source_id: str,
    *,
    domains: tuple[DataDomain, ...],
    semantic_type_ids: tuple[str, ...],
    implementation_sha256: str,
) -> SourceRegistryEntry:
    suffix = source_id.removeprefix("source.").replace("-", "_")
    return SourceRegistryEntry(
        source_id=source_id,
        version=MEDIUM_VERSION,
        adapter_id=f"data_engine.d2_e3:{suffix}_adapter",
        adapter_version=MEDIUM_VERSION,
        normalizer_id=f"data_engine.d2_e3:{suffix}_normalizer",
        normalizer_version=MEDIUM_VERSION,
        supported_domains=domains,
        supported_type_ids=semantic_type_ids,
        configuration_schema_sha256=canonical_sha256(
            {
                "configuration": "FrozenMediumCaptureConfiguration",
                "fixture_only": True,
                "source_id": source_id,
            }
        ),
        mapping_schema_sha256=canonical_sha256(
            {
                "source_id": source_id,
                "semantic_type_ids": semantic_type_ids,
                "rung": "E3",
                "membership_partition": (
                    TOPT_MEMBERSHIP_PARTITION
                    if source_id == E3_NPORT_SOURCE_ID
                    else None
                ),
            }
        ),
        adapter_implementation_sha256=implementation_sha256,
        normalizer_implementation_sha256=implementation_sha256,
    )


def _extend_registry(
    parent: RegistrySnapshot,
    *,
    implementation_sha256: str,
) -> RegistrySnapshot:
    if parent.registry_snapshot_id != E2_REGISTRY_SNAPSHOT_ID:
        raise ValueError("E3 registry parent is not the exact accepted E2 snapshot")
    additions = (
        _source_entry(
            E3_NPORT_SOURCE_ID,
            domains=(DataDomain.INSTRUMENTS, DataDomain.UNIVERSE),
            semantic_type_ids=(
                ISSUER_SECURITY_TYPE_ID,
                SECURITY_LISTING_TYPE_ID,
                UNIVERSE_MEMBERSHIP_TYPE_ID,
            ),
            implementation_sha256=implementation_sha256,
        ),
        _source_entry(
            E3_YAHOO_SOURCE_ID,
            domains=(DataDomain.MARKET_PRICES,),
            semantic_type_ids=(MARKET_PRICE_TYPE_ID,),
            implementation_sha256=implementation_sha256,
        ),
    )
    child = parent.extend(sources=additions)
    parent_sources = {entry.key: entry for entry in parent.sources}
    child_sources = {entry.key: entry for entry in child.sources}
    if (
        child.parent_snapshot_id != parent.registry_snapshot_id
        or child.semantic_types != parent.semantic_types
        or child.identifier_types != parent.identifier_types
        or child.required_type_ids != parent.required_type_ids
        or child.required_identifier_type_ids != parent.required_identifier_type_ids
        or any(child_sources.get(key) != entry for key, entry in parent_sources.items())
        or set(child_sources) - set(parent_sources) != {entry.key for entry in additions}
    ):
        raise ValueError("E3 registry extension mutated an inherited E2 entry")
    return child


def _capture(configuration: BaseModel) -> RawCapture:
    frozen = cast(FrozenMediumCaptureConfiguration, configuration)
    return RawCapture(
        source=frozen.source,
        source_record_id=frozen.source_record_id,
        body=frozen.body,
        content_type=frozen.content_type,
        fetched_at=frozen.fetched_at,
        source_published_at=frozen.source_published_at,
        metadata=frozen.metadata,
    )


def _configuration(
    corpus: FrozenE3Corpus,
    artifact: FrozenE3Artifact,
    *,
    source: DataSource,
    published_at: datetime | None = None,
) -> FrozenMediumCaptureConfiguration:
    content_type = "application/xml" if artifact.path.endswith(".xml") else "application/json"
    return FrozenMediumCaptureConfiguration(
        artifact_id=artifact.artifact_id,
        source=source,
        source_record_id=f"d2-e3:{artifact.artifact_id}",
        body=artifact.body,
        content_type=content_type,
        fetched_at=corpus.fetched_at,
        source_published_at=published_at,
        metadata={
            "artifact_id": artifact.artifact_id,
            "artifact_path": artifact.path,
            "artifact_sha256": artifact.sha256,
            "artifact_byte_length": artifact.byte_length,
            "corpus_sha256": corpus.corpus_sha256,
            "checked_in_fixture": True,
            "network_permitted": False,
        },
    )


def _raw_object_ref(capture: LandedMediumCapture) -> str:
    return f"raw-object:{capture.raw_object_sha256}"


def _with_raw_ref[ModelT: BaseModel](
    model_type: type[ModelT],
    payload: ModelT,
    raw_reference: str,
) -> ModelT:
    return model_type.model_validate(
        {
            **payload.model_dump(mode="python"),
            "raw_ref": raw_reference,
        }
    )


def _draft(
    *,
    semantic_type_id: str,
    payload: BaseModel,
    subject: SubjectRef,
    valid_from: date,
    valid_to: date,
    knowable_at: datetime,
    recorded_at: datetime,
    document_id: str,
    confidence: Decimal,
    raw_ref: str,
) -> MvpNormalizationDraft:
    return MvpNormalizationDraft(
        semantic_type_id=semantic_type_id,
        payload=payload,
        subject=subject,
        valid_from=valid_from,
        valid_to=valid_to,
        knowable_at=knowable_at,
        produced_at=knowable_at,
        recorded_at=recorded_at,
        document_id=document_id,
        confidence=confidence,
        raw_ref=raw_ref,
    )


def _issuer_link(
    corpus: FrozenE3Corpus,
    instrument: FrozenE3Instrument,
    raw_reference: str,
) -> IssuerSecurityLink:
    return IssuerSecurityLink(
        input_id=f"identity-link:{instrument.issuer_id}:{instrument.security_id}",
        issuer_id=instrument.issuer_id,
        security_id=instrument.security_id,
        security_kind=SecurityKind.COMMON_STOCK,
        share_class=instrument.share_class,
        underlying_security_id=None,
        underlying_shares_per_security_unit=Decimal("1"),
        valid_from=corpus.valid_on,
        valid_to=None,
        knowable_at=corpus.nport_accepted_at,
        recorded_at=corpus.recorded_at,
        confidence=Decimal("0.99"),
        raw_ref=raw_reference,
    )


def _listing_link(
    corpus: FrozenE3Corpus,
    instrument: FrozenE3Instrument,
    raw_reference: str,
) -> SecurityListingLink:
    temporal = _required_mapping(corpus.payload["temporal_contract"], label="temporal_contract")
    return SecurityListingLink(
        input_id=f"identity-link:{instrument.security_id}:{instrument.listing_id}",
        security_id=instrument.security_id,
        listing_id=instrument.listing_id,
        exchange_mic=instrument.exchange_mic,
        ticker=instrument.ticker,
        listing_role=ListingRole.PRIMARY,
        currency=cast(str, temporal["currency"]),
        timezone=cast(str, temporal["timezone"]),
        trading_calendar_id=cast(str, temporal["trading_calendar_id"]),
        trading_calendar_version=cast(str, temporal["trading_calendar_version"]),
        valid_from=corpus.valid_on,
        valid_to=None,
        knowable_at=corpus.nport_accepted_at,
        recorded_at=corpus.recorded_at,
        confidence=Decimal("0.99"),
        raw_ref=raw_reference,
    )


def _membership(
    corpus: FrozenE3Corpus,
    instrument: FrozenE3Instrument,
    raw_reference: str,
) -> UniverseMembership:
    universe = _required_mapping(corpus.payload["universe"], label="universe")
    return UniverseMembership(
        membership_id=f"membership:{universe['universe_id']}:{instrument.security_id}",
        universe_id=cast(str, universe["universe_id"]),
        subject=SubjectRef(kind=SubjectKind.SECURITY, id=instrument.security_id),
        valid_from=corpus.valid_on,
        valid_to=None,
        knowable_at=corpus.nport_accepted_at,
        recorded_at=corpus.recorded_at,
        confidence=Decimal("0.99"),
        raw_ref=raw_reference,
    )


def _nport_normalizer(
    corpus: FrozenE3Corpus,
    semantic_type_id: str,
):
    def normalize(capture: LandedMediumCapture) -> tuple[MvpNormalizationDraft, ...]:
        _source, holdings = _parse_nport_holdings(capture.body)
        pairs = {
            (item["cusip"], item["lei"])
            for item in holdings
            if item["asset_category"] == "EC"
        }
        if pairs != {(item.cusip, item.issuer_lei) for item in corpus.instruments}:
            raise ValueError("landed E3 N-PORT identities drifted before normalization")
        raw_reference = _raw_object_ref(capture)
        if semantic_type_id == ISSUER_SECURITY_TYPE_ID:
            payloads: tuple[BaseModel, ...] = tuple(
                _issuer_link(corpus, instrument, raw_reference)
                for instrument in corpus.instruments
                if instrument.ticker != "NVDA"
            )
            subjects = tuple(
                SubjectRef(kind=SubjectKind.ISSUER, id=cast(IssuerSecurityLink, item).issuer_id)
                for item in payloads
            )
            document_ids = tuple(cast(IssuerSecurityLink, item).input_id for item in payloads)
        elif semantic_type_id == SECURITY_LISTING_TYPE_ID:
            payloads = tuple(
                _listing_link(corpus, instrument, raw_reference)
                for instrument in corpus.instruments
                if instrument.ticker != "NVDA"
            )
            subjects = tuple(
                SubjectRef(kind=SubjectKind.SECURITY, id=cast(SecurityListingLink, item).security_id)
                for item in payloads
            )
            document_ids = tuple(cast(SecurityListingLink, item).input_id for item in payloads)
        elif semantic_type_id == UNIVERSE_MEMBERSHIP_TYPE_ID:
            payloads = tuple(
                _membership(corpus, instrument, raw_reference)
                for instrument in corpus.instruments
            )
            subjects = tuple(cast(UniverseMembership, item).subject for item in payloads)
            document_ids = tuple(cast(UniverseMembership, item).membership_id for item in payloads)
        else:
            raise ValueError(f"unsupported E3 N-PORT semantic route: {semantic_type_id}")
        return tuple(
            _draft(
                semantic_type_id=semantic_type_id,
                payload=payload,
                subject=subject,
                valid_from=corpus.valid_on,
                valid_to=date.max,
                knowable_at=corpus.nport_accepted_at,
                recorded_at=corpus.recorded_at,
                document_id=document_id,
                confidence=Decimal("0.99"),
                raw_ref=capture.raw_ref,
            )
            for payload, subject, document_id in zip(
                payloads,
                subjects,
                document_ids,
                strict=True,
            )
        )

    return normalize


def _price_payload(
    corpus: FrozenE3Corpus,
    artifact: FrozenE3Artifact,
    instrument: FrozenE3Instrument,
) -> MarketPricePayload:
    temporal = _required_mapping(corpus.payload["temporal_contract"], label="temporal_contract")
    target_timestamp = cast(int, temporal["target_source_timestamp"])
    result = _yahoo_result(artifact, instrument, target_timestamp=target_timestamp)
    timestamps = cast(list[int], result["timestamp"])
    position = timestamps.index(target_timestamp)
    indicators = cast(dict[str, Any], result["indicators"])
    quote = cast(list[dict[str, Any]], indicators["quote"])[0]
    prices = {
        name: _strict_decimal(
            cast(list[Any], quote[name])[position],
            label=f"{artifact.artifact_id}.{name}",
        )
        for name in ("open", "high", "low", "close")
    }
    volume = cast(list[Any], quote["volume"])[position]
    if not isinstance(volume, int) or isinstance(volume, bool) or volume < 0:
        raise ValueError(f"{artifact.artifact_id}.volume is not a non-negative integer")
    return MarketPricePayload(
        input_id=f"price-bar:{instrument.listing_id}:{corpus.valid_on.isoformat()}:unadjusted",
        issuer_id=instrument.issuer_id,
        security_id=instrument.security_id,
        listing_id=instrument.listing_id,
        share_class=instrument.share_class,
        exchange_mic=instrument.exchange_mic,
        ticker=instrument.ticker,
        calendar_id=cast(str, temporal["trading_calendar_id"]),
        calendar_version=cast(str, temporal["trading_calendar_version"]),
        trading_date=corpus.valid_on,
        session_close_at=corpus.session_close_at,
        open=prices["open"],
        high=prices["high"],
        low=prices["low"],
        close=prices["close"],
        volume=volume,
        currency=cast(str, temporal["currency"]),
        price_basis=PriceBasis.UNADJUSTED,
        knowable_at=corpus.knowable_at,
        produced_at=corpus.produced_at,
        recorded_at=corpus.recorded_at,
        confidence=Decimal("0.99"),
        confidence_policy_id="policy.d2-e3-reviewed-fixture-confidence",
        price_policy_id="policy.d2-e3-unadjusted-execution-price",
    )


def _price_normalizer(
    corpus: FrozenE3Corpus,
    instruments_by_artifact: dict[str, FrozenE3Instrument],
):
    def normalize(capture: LandedMediumCapture) -> tuple[MvpNormalizationDraft, ...]:
        artifact_id = cast(str, capture.metadata.get("artifact_id"))
        try:
            artifact = corpus.artifacts[artifact_id]
            instrument = instruments_by_artifact[artifact_id]
        except KeyError as error:
            raise ValueError(f"unregistered E3 Yahoo artifact: {artifact_id}") from error
        if capture.body != artifact.body or capture.raw_object_sha256 != artifact.sha256:
            raise ValueError(f"landed E3 Yahoo bytes drifted: {artifact_id}")
        payload = _price_payload(corpus, artifact, instrument)
        return (
            _draft(
                semantic_type_id=MARKET_PRICE_TYPE_ID,
                payload=payload,
                subject=SubjectRef(kind=SubjectKind.LISTING, id=instrument.listing_id),
                valid_from=corpus.valid_on,
                valid_to=corpus.valid_on,
                knowable_at=payload.knowable_at,
                recorded_at=payload.recorded_at,
                document_id=payload.input_id,
                confidence=payload.confidence,
                raw_ref=capture.raw_ref,
            ),
        )

    return normalize


def _component_catalog(
    *,
    corpus: FrozenE3Corpus,
    registry: RegistrySnapshot,
    implementation_sha256: str,
) -> MediumComponentCatalog:
    sources = {(entry.source_id, entry.version): entry for entry in registry.sources}
    adapters = tuple(
        MediumAdapterRegistration(
            source_id=source_id,
            source_version=MEDIUM_VERSION,
            adapter_id=sources[(source_id, MEDIUM_VERSION)].adapter_id,
            adapter_version=sources[(source_id, MEDIUM_VERSION)].adapter_version,
            adapter_implementation_sha256=implementation_sha256,
            configuration_type=FrozenMediumCaptureConfiguration,
            raw_source=raw_source,
            capture=_capture,
        )
        for source_id, raw_source in (
            (E3_NPORT_SOURCE_ID, DataSource.NPORT),
            (E3_YAHOO_SOURCE_ID, DataSource.YAHOO),
        )
    )
    price_normalizer = _price_normalizer(
        corpus,
        {item.price_artifact_id: item for item in corpus.instruments},
    )
    routes = {
        (E3_NPORT_SOURCE_ID, ISSUER_SECURITY_TYPE_ID): _nport_normalizer(
            corpus,
            ISSUER_SECURITY_TYPE_ID,
        ),
        (E3_NPORT_SOURCE_ID, SECURITY_LISTING_TYPE_ID): _nport_normalizer(
            corpus,
            SECURITY_LISTING_TYPE_ID,
        ),
        (E3_NPORT_SOURCE_ID, UNIVERSE_MEMBERSHIP_TYPE_ID): _nport_normalizer(
            corpus,
            UNIVERSE_MEMBERSHIP_TYPE_ID,
        ),
        (E3_YAHOO_SOURCE_ID, MARKET_PRICE_TYPE_ID): price_normalizer,
    }
    normalizers = tuple(
        MediumNormalizerRegistration(
            source_id=source_id,
            source_version=MEDIUM_VERSION,
            semantic_type_id=semantic_type_id,
            semantic_type_version=MEDIUM_VERSION,
            normalizer_id=sources[(source_id, MEDIUM_VERSION)].normalizer_id,
            normalizer_version=sources[(source_id, MEDIUM_VERSION)].normalizer_version,
            normalizer_implementation_sha256=implementation_sha256,
            normalize=normalize,
        )
        for (source_id, semantic_type_id), normalize in routes.items()
    )
    return MediumComponentCatalog(
        registry=registry,
        adapters=adapters,
        normalizers=normalizers,
    )


def _capture_work_items(corpus: FrozenE3Corpus) -> tuple[MediumCaptureWorkItem, ...]:
    nport = corpus.artifacts["topt-nport-primary-document"]
    items: list[MediumCaptureWorkItem] = [
        MediumCaptureWorkItem(
            source_id=E3_NPORT_SOURCE_ID,
            source_version=MEDIUM_VERSION,
            semantic_type_ids=(
                ISSUER_SECURITY_TYPE_ID,
                SECURITY_LISTING_TYPE_ID,
                UNIVERSE_MEMBERSHIP_TYPE_ID,
            ),
            semantic_type_version=MEDIUM_VERSION,
            configuration=_configuration(
                corpus,
                nport,
                source=DataSource.NPORT,
                published_at=corpus.nport_accepted_at,
            ),
            recorded_at=corpus.recorded_at,
        )
    ]
    items.extend(
        MediumCaptureWorkItem(
            source_id=E3_YAHOO_SOURCE_ID,
            source_version=MEDIUM_VERSION,
            semantic_type_ids=(MARKET_PRICE_TYPE_ID,),
            semantic_type_version=MEDIUM_VERSION,
            configuration=_configuration(
                corpus,
                corpus.artifacts[instrument.price_artifact_id],
                source=DataSource.YAHOO,
            ),
            recorded_at=corpus.recorded_at,
        )
        for instrument in corpus.instruments
    )
    return tuple(items)


def _repository_registrations(
    registry: RegistrySnapshot,
) -> tuple[MediumRepositoryRegistration, ...]:
    """Keep the E3 Yahoo JSON mapping distinct from the inherited CSV route."""

    source = next(
        entry
        for entry in registry.sources
        if (entry.source_id, entry.version) == (E3_YAHOO_SOURCE_ID, MEDIUM_VERSION)
    )
    registrations: list[MediumRepositoryRegistration] = []
    for registration in build_medium_repository_registrations(registry):
        if registration.semantic_type_id == UNIVERSE_MEMBERSHIP_TYPE_ID:
            registrations.append(
                replace(
                    registration,
                    partition_filter=_membership_partition,
                )
            )
            continue
        if registration.semantic_type_id != MARKET_PRICE_TYPE_ID:
            registrations.append(registration)
            continue
        mapping_versions = dict(registration.mapping_versions)
        mapping_versions[E3_YAHOO_SOURCE_ID] = (
            f"{source.normalizer_id}:{source.normalizer_version}"
        )
        registrations.append(
            replace(registration, mapping_versions=mapping_versions)
        )
    return tuple(registrations)


def _membership_partition(payload: BaseModel, partition_key: str) -> bool:
    membership = cast(UniverseMembership, payload)
    return partition_key == "all" or partition_key == f"universe:{membership.universe_id}"


def _policy_bindings() -> tuple[PolicyBinding, ...]:
    return tuple(
        PolicyBinding(
            role=role,
            policy_id=f"policy.d2-e3-{role.value.replace('_', '-')}",
            policy_version=MEDIUM_VERSION,
            implementation_sha256=canonical_sha256(
                {
                    "batch": "D2-mvp-medium-validation",
                    "role": role.value,
                    "rung": "E3",
                }
            ),
        )
        for role in sorted(PolicyRole, key=lambda item: item.value)
    )


def _partition_for(record: NormalizedRecordRef) -> str:
    if record.draft.semantic_type_id == MARKET_PRICE_TYPE_ID:
        return f"date:{record.draft.valid_from.isoformat()}"
    if record.draft.semantic_type_id == UNIVERSE_MEMBERSHIP_TYPE_ID:
        return TOPT_MEMBERSHIP_PARTITION
    return "all"


def _demand(
    record: NormalizedRecordRef,
    *,
    domain: DataDomain,
) -> SnapshotDemandCell:
    partition_key = _partition_for(record)
    coordinate = {
        "batch": "D2-mvp-medium-validation",
        "rung": "E3",
        "document_id": record.document_id,
        "semantic_type_id": record.draft.semantic_type_id,
        "semantic_type_version": record.draft.semantic_type_version,
        "subject": record.draft.subject.model_dump(mode="json"),
        "partition_key": partition_key,
    }
    return SnapshotDemandCell(
        requirement_id=f"data-requirement:{canonical_sha256({'kind': 'data', **coordinate})}",
        capture_requirement_id=(
            f"capture-requirement:{canonical_sha256({'kind': 'capture', **coordinate})}"
        ),
        semantic_type_id=record.draft.semantic_type_id,
        semantic_type_version=record.draft.semantic_type_version,
        domain=domain,
        subject=record.draft.subject,
        partition_key=partition_key,
        level=RequirementLevel.REQUIRED,
    )


def _e2_reused_records(
    handoff: MvpMediumValidationHandoff,
    repository: PostgresMediumSemanticRepository,
) -> dict[str, NormalizedRecordRef]:
    expected_identity_documents = {
        "identity-link:issuer:lei:549300S4KLFTLO7GSQ80:security:cusip:67066G104",
        "identity-link:security:cusip:67066G104:listing:xnas:nvda",
    }
    candidates = {
        record.document_id: record
        for snapshot in handoff.snapshot_bundle.snapshots
        for record in snapshot.normalized_records
        if record.document_id in expected_identity_documents
    }
    price_candidates = {
        record.normalized_record_id: record
        for snapshot in handoff.snapshot_bundle.snapshots
        for record in snapshot.normalized_records
        if record.draft.semantic_type_id == MARKET_PRICE_TYPE_ID
        and record.draft.subject
        == SubjectRef(kind=SubjectKind.LISTING, id="listing:xnas:nvda")
        and record.draft.valid_from == date(2026, 3, 31)
        and record.draft.valid_to == date(2026, 3, 31)
    }
    if set(candidates) != expected_identity_documents or len(price_candidates) != 1:
        raise ValueError("E3 could not recover the exact three accepted E2 terminal cells")
    price_record = next(iter(price_candidates.values()))
    stored_price = repository.get(price_record.normalized_record_id)
    expected_price_input = "price-bar:listing:xnas:nvda:2026-03-31:unadjusted"
    if (
        stored_price is None
        or not isinstance(stored_price.payload, MarketPricePayload)
        or stored_price.payload.input_id != expected_price_input
    ):
        raise ValueError("E3 accepted E2 corrected price payload drifted")
    candidates[expected_price_input] = price_record
    expected_types = {
        "identity-link:issuer:lei:549300S4KLFTLO7GSQ80:security:cusip:67066G104": (
            ISSUER_SECURITY_TYPE_ID
        ),
        "identity-link:security:cusip:67066G104:listing:xnas:nvda": (
            SECURITY_LISTING_TYPE_ID
        ),
        "price-bar:listing:xnas:nvda:2026-03-31:unadjusted": MARKET_PRICE_TYPE_ID,
    }
    if any(candidates[key].draft.semantic_type_id != value for key, value in expected_types.items()):
        raise ValueError("E3 accepted E2 terminal cell semantics drifted")
    return candidates


def _terminal_records(
    e2_records: dict[str, NormalizedRecordRef],
    added_records: tuple[NormalizedRecordRef, ...],
) -> tuple[NormalizedRecordRef, ...]:
    records = tuple(
        sorted(
            (*e2_records.values(), *added_records),
            key=lambda item: (
                item.draft.semantic_type_id,
                item.document_id,
                item.normalized_record_id,
            ),
        )
    )
    counts = Counter(record.draft.semantic_type_id for record in records)
    if len(records) != 84 or counts != EXPECTED_TERMINAL_COUNTS:
        raise ValueError("E3 terminal record plan is not the exact 84-cell denominator")
    if len({record.normalized_record_id for record in records}) != 84:
        raise ValueError("E3 terminal record plan contains duplicate normalized records")
    if any(record.draft.semantic_type_id == "semantic.corporate-action" for record in records):
        raise ValueError("E3 corporate actions must remain outside the decision snapshot")
    return records


def _stored_payloads[PayloadT: BaseModel](
    repository: PostgresMediumSemanticRepository,
    records: tuple[NormalizedRecordRef, ...],
    model_type: type[PayloadT],
) -> tuple[PayloadT, ...]:
    result: list[PayloadT] = []
    for record in records:
        row = repository.get(record.normalized_record_id)
        if row is None or not isinstance(row.payload, model_type):
            raise ValueError(f"E3 persisted {model_type.__name__} payload disappeared")
        result.append(row.payload)
    return tuple(result)


def _fixture_selections(
    records: tuple[NormalizedRecordRef, ...],
    demands: tuple[SnapshotDemandCell, ...],
) -> dict[str, tuple[NormalizedRecordRef, ...]]:
    selected: dict[str, tuple[NormalizedRecordRef, ...]] = {}
    for demand in demands:
        matches = tuple(
            record
            for record in records
            if record.draft.semantic_type_id == demand.semantic_type_id
            and record.draft.semantic_type_version == demand.semantic_type_version
            and record.draft.subject == demand.subject
            and (
                demand.partition_key == "all"
                or demand.partition_key == f"date:{record.draft.valid_from.isoformat()}"
                or (
                    demand.semantic_type_id == UNIVERSE_MEMBERSHIP_TYPE_ID
                    and demand.partition_key == TOPT_MEMBERSHIP_PARTITION
                )
            )
        )
        if not matches:
            raise ValueError(f"E3 fixture demand is empty: {demand.planned_cell_id}")
        selected[demand.planned_cell_id] = matches
    return selected


def _snapshot_pair(
    *,
    connection: Connection[Any],
    corpus: FrozenE3Corpus,
    registry: RegistrySnapshot,
    repository: PostgresMediumSemanticRepository,
    records: tuple[NormalizedRecordRef, ...],
) -> tuple[SnapshotManifest, SnapshotManifest, UniverseManifest]:
    membership_records = tuple(
        record
        for record in records
        if record.draft.semantic_type_id == UNIVERSE_MEMBERSHIP_TYPE_ID
    )
    memberships = _stored_payloads(repository, membership_records, UniverseMembership)
    universe_payload = _required_mapping(corpus.payload["universe"], label="universe")
    universe_manifest = UniverseManifest.create(
        universe_id=cast(str, universe_payload["universe_id"]),
        universe_version=cast(str, universe_payload["universe_version"]),
        definition_kind=UniverseDefinitionKind.FIXED_COHORT,
        membership_ids=tuple(item.membership_id for item in memberships),
        effective_at=corpus.nport_accepted_at,
        owner="D2-mvp-medium-validation:E3",
    )
    domain_by_type = {entry.semantic_type_id: entry.domain for entry in registry.semantic_types}
    demands = tuple(
        _demand(record, domain=domain_by_type[record.draft.semantic_type_id])
        for record in records
    )
    if len({demand.planned_cell_id for demand in demands}) != 84:
        raise ValueError("E3 snapshot demand contains duplicate required cells")
    request = SnapshotRequest(
        universe=universe_manifest.ref,
        as_of=corpus.knowable_at,
        valid_on=corpus.valid_on,
        registry_snapshot_id=registry.registry_snapshot_id,
        registry_snapshot_sha256=registry.content_sha256,
        source_registry_id=registry.source_registry_snapshot_id,
        source_registry_sha256=registry.source_registry_sha256,
        semantic_type_registry_id=registry.semantic_type_registry_snapshot_id,
        semantic_type_registry_sha256=registry.semantic_type_registry_sha256,
        policy_bindings=_policy_bindings(),
        demand_cells=demands,
    )
    resolved_at = corpus.recorded_at + timedelta(seconds=1)
    fixture = build_medium_snapshot(
        request,
        registry=registry,
        selected_records=_fixture_selections(records, demands),
        resolved_at=resolved_at,
        universe_manifest=universe_manifest,
        universe_memberships=memberships,
    )
    resolver = PostgresMediumSnapshotResolver(
        semantic_records=repository,
        snapshots=PostgresSnapshotRepository(connection),
    )
    postgres = resolver.resolve(
        request,
        registry=registry,
        resolved_at=resolved_at,
        universe_manifest=universe_manifest,
    )
    selections_by_cell = {
        selection.demand.planned_cell_id: selection
        for selection in postgres.selections
    }
    for demand, intended in zip(demands, records, strict=True):
        selection = selections_by_cell.get(demand.planned_cell_id)
        if (
            selection is None
            or intended.normalized_record_id not in selection.normalized_record_ids
        ):
            raise ValueError(
                f"E3 demand lost its intended document/CUSIP record: {intended.document_id}"
            )
    if fixture != postgres or fixture.snapshot_id != postgres.snapshot_id:
        fixture_cells = {
            selection.demand.planned_cell_id: selection.normalized_record_ids
            for selection in fixture.selections
        }
        postgres_cells = {
            selection.demand.planned_cell_id: selection.normalized_record_ids
            for selection in postgres.selections
        }
        mismatches = tuple(
            cell_id
            for cell_id in sorted(fixture_cells)
            if fixture_cells[cell_id] != postgres_cells.get(cell_id)
        )
        raise ValueError(
            "E3 fixture/Postgres 84-cell snapshot parity failed: "
            f"cell_mismatches={len(mismatches)}, "
            f"subjects_equal={fixture.resolved_subjects == postgres.resolved_subjects}, "
            f"memberships_equal={fixture.universe_memberships == postgres.universe_memberships}"
        )
    if (
        len(postgres.selections) != 84
        or len(postgres.normalized_records) != 84
        or len(postgres.universe_memberships) != 21
    ):
        raise ValueError("E3 terminal snapshot is not row-complete")
    return fixture, postgres, universe_manifest


def _cutoff_counts(
    repository: PostgresMediumSemanticRepository,
    snapshot: SnapshotManifest,
    *,
    as_of: datetime,
) -> dict[str, int]:
    counts = {semantic_type_id: 0 for semantic_type_id in EXPECTED_TERMINAL_COUNTS}
    for demand in snapshot.request.demand_cells:
        visible = repository.visible_records(
            demand,
            as_of=as_of,
            valid_on=snapshot.request.valid_on,
        )
        if visible:
            counts[demand.semantic_type_id] += 1
    return counts


def _projection_record_ids(
    records: tuple[NormalizedRecordRef, ...],
) -> dict[str, tuple[str, ...]]:
    table_by_type = {
        ISSUER_SECURITY_TYPE_ID: "staging.mvp_issuer_security_links",
        MARKET_PRICE_TYPE_ID: "staging.mvp_market_prices",
        SECURITY_LISTING_TYPE_ID: "staging.mvp_security_listing_links",
        UNIVERSE_MEMBERSHIP_TYPE_ID: "staging.mvp_universe_memberships",
    }
    grouped: dict[str, list[str]] = {table: [] for table in table_by_type.values()}
    for record in records:
        try:
            grouped[table_by_type[record.draft.semantic_type_id]].append(record.normalized_record_id)
        except KeyError as error:
            raise ValueError("E3 added record escaped the terminal domain plan") from error
    result = {table: tuple(sorted(record_ids)) for table, record_ids in sorted(grouped.items())}
    if {table: len(ids) for table, ids in result.items()} != EXPECTED_ADDED_PROJECTION_COUNTS:
        raise ValueError("E3 added projection record plan drifted")
    return result


def _verify_projection_rows(
    connection: Connection[Any],
    projection_ids: dict[str, tuple[str, ...]],
) -> None:
    for table, record_ids in projection_ids.items():
        row = connection.execute(
            f"select count(*) from {table} where normalized_record_id = any(%s)",
            (list(record_ids),),
        ).fetchone()
        if row is None or row[0] != len(record_ids):
            raise ValueError(f"E3 projection row count drifted for {table}")


def _expect_mutation_rejected(
    connection: Connection[Any],
    statement: str,
    record_id: str,
) -> bool:
    try:
        with connection.transaction():
            connection.execute(statement, (record_id,))
    except PsycopgError as error:
        if "point-in-time records are append-only" not in str(error):
            raise ValueError("E3 append-only control failed for an unexpected reason") from error
        return True
    raise ValueError("E3 append-only control unexpectedly permitted a mutation")


def _append_only_controls(
    connection: Connection[Any],
    projection_ids: dict[str, tuple[str, ...]],
) -> dict[str, bool]:
    issuer_id = projection_ids["staging.mvp_issuer_security_links"][0]
    membership_id = projection_ids["staging.mvp_universe_memberships"][0]
    return {
        "delete_rejected": _expect_mutation_rejected(
            connection,
            "delete from staging.mvp_universe_memberships where normalized_record_id = %s",
            membership_id,
        ),
        "update_rejected": _expect_mutation_rejected(
            connection,
            "update staging.mvp_issuer_security_links set confidence = confidence "
            "where normalized_record_id = %s",
            issuer_id,
        ),
    }


def _action_coverage(
    corpus: FrozenE3Corpus,
    capture_batch: MediumCaptureBatch,
) -> tuple[ActionWindowCoverageCell, ...]:
    captures = {
        cast(str, capture.metadata.get("artifact_id")): capture
        for capture in capture_batch.captures
        if capture.source_id == E3_YAHOO_SOURCE_ID
    }
    action_demand = _required_mapping(
        corpus.payload["action_window_coverage_demand"],
        label="action_window_coverage_demand",
    )
    raw_cells = _required_list(action_demand["cells"], label="action-window cells")
    cells_by_ticker = {
        cast(str, _required_mapping(item, label="action-window cell")["ticker"]): (
            _required_mapping(item, label="action-window cell")
        )
        for item in raw_cells
    }
    result: list[ActionWindowCoverageCell] = []
    target_timestamp = cast(
        int,
        _required_mapping(corpus.payload["temporal_contract"], label="temporal_contract")[
            "target_source_timestamp"
        ],
    )
    for instrument in corpus.instruments:
        artifact = corpus.artifacts[instrument.price_artifact_id]
        capture = captures.get(artifact.artifact_id)
        if capture is None or capture.raw_object_sha256 != artifact.sha256:
            raise ValueError(f"E3 action-window raw capture is missing: {artifact.artifact_id}")
        result.append(
            _parsed_action_window(
                corpus_cell=cells_by_ticker[instrument.ticker],
                artifact=artifact,
                instrument=instrument,
                result=_yahoo_result(
                    artifact,
                    instrument,
                    target_timestamp=target_timestamp,
                ),
            )
        )
    return tuple(result)


def _verify_e2_runtime(handoff: MvpMediumValidationHandoff) -> None:
    if (
        handoff.handoff_id != E2_RUNTIME_HANDOFF_ID
        or handoff.content_sha256 != E2_RUNTIME_HANDOFF_SHA256
        or handoff.schema_epoch != D2_E2_SCHEMA_EPOCH
        or handoff.registry_snapshot.registry_snapshot_id != E2_REGISTRY_SNAPSHOT_ID
        or handoff.allowed_consumers != ("D2-mvp-medium-validation",)
        or tuple(sorted(handoff.allowed_environments)) != ("ci", "local")
        or {table: len(ids) for table, ids in handoff.projection_record_ids.items()}
        != EXPECTED_RETAINED_E2_PROJECTION_COUNTS
    ):
        raise ValueError("E3 did not reproduce the exact accepted E2 runtime handoff")


class _ContentAddressedReplayStore:
    """Bridge fixture-store bucket changes without weakening byte identity."""

    def __init__(self, delegate: RawObjectStore) -> None:
        self._delegate = delegate
        self._bodies_by_sha256: dict[str, bytes] = {}

    def store(self, capture: RawCapture) -> RawIngestionEnvelope:
        envelope = self._delegate.store(capture)
        if envelope.object.sha256 != _sha256(capture.body):
            raise ValueError("fixture object store returned the wrong content hash")
        previous = self._bodies_by_sha256.setdefault(envelope.object.sha256, capture.body)
        if previous != capture.body:
            raise ValueError("fixture replay cache detected a SHA-256 collision")
        return envelope

    def get(self, ref: RawObjectRef) -> bytes:
        try:
            body = self._delegate.get(ref)
        except (FileNotFoundError, KeyError):
            try:
                body = self._bodies_by_sha256[ref.sha256]
            except KeyError as error:
                raise ValueError(f"fixture replay object is unavailable: {ref.sha256}") from error
        if len(body) != ref.byte_length or _sha256(body) != ref.sha256:
            raise ValueError("fixture replay object failed checksum or byte-length validation")
        return body


def _run_d2_e3(
    repository_root: Path,
    connection: Connection[Any],
    raw_store: RawObjectStore,
    *,
    environment: str,
    fail_after_normalized_records: int | None,
) -> MvpToptValidationHandoff:
    corpus = load_e3_corpus(repository_root)
    replay_store = _ContentAddressedReplayStore(raw_store)
    e2_handoff = run_d2_e2(
        repository_root,
        connection,
        replay_store,
        environment=environment,
    )
    _verify_e2_runtime(e2_handoff)
    implementation_sha256 = _module_sha256()
    registry = _extend_registry(
        e2_handoff.registry_snapshot,
        implementation_sha256=implementation_sha256,
    )
    repository = PostgresMediumSemanticRepository(
        connection,
        registry=registry,
        registrations=_repository_registrations(registry),
    )
    catalog = _component_catalog(
        corpus=corpus,
        registry=registry,
        implementation_sha256=implementation_sha256,
    )
    capture_batch = land_medium_capture_plan(
        connection,
        object_store=replay_store,
        catalog=catalog,
        work_items=_capture_work_items(corpus),
    )
    raw_hashes = tuple(capture.raw_object_sha256 for capture in capture_batch.captures)
    if len(raw_hashes) != 22 or len(set(raw_hashes)) != 22:
        raise ValueError("E3 did not land exactly one N-PORT and 21 Yahoo raw objects")

    nvda_artifact_id = next(
        item.price_artifact_id for item in corpus.instruments if item.ticker == "NVDA"
    )
    normalization_captures = tuple(
        capture
        for capture in capture_batch.captures
        if not (
            capture.source_id == E3_YAHOO_SOURCE_ID
            and capture.metadata.get("artifact_id") == nvda_artifact_id
        )
    )
    if len(normalization_captures) != 21:
        raise ValueError("E3 NVDA raw-only normalization exclusion drifted")
    added_records: list[NormalizedRecordRef] = []
    for capture in normalization_captures:
        normalized = normalize_medium_capture_batch(
            batch=MediumCaptureBatch(captures=(capture,)),
            catalog=catalog,
            repository=repository,
        )
        added_records.extend(normalized.normalized_records)
        if (
            fail_after_normalized_records is not None
            and len(added_records) >= fail_after_normalized_records
        ):
            raise RuntimeError(
                f"injected E3 failure after {len(added_records)} normalized records"
            )
    added = tuple(sorted(added_records, key=lambda item: item.normalized_record_id))
    if (
        len(added) != 81
        or len({record.normalized_record_id for record in added}) != 81
        or Counter(record.draft.semantic_type_id for record in added)
        != {
            ISSUER_SECURITY_TYPE_ID: 20,
            MARKET_PRICE_TYPE_ID: 20,
            SECURITY_LISTING_TYPE_ID: 20,
            UNIVERSE_MEMBERSHIP_TYPE_ID: 21,
        }
    ):
        raise ValueError("E3 additive normalization did not produce the exact 81 records")

    e2_records = _e2_reused_records(e2_handoff, repository)
    terminal_records = _terminal_records(e2_records, added)
    fixture_snapshot, postgres_snapshot, universe_manifest = _snapshot_pair(
        connection=connection,
        corpus=corpus,
        registry=registry,
        repository=repository,
        records=terminal_records,
    )
    projection_ids = _projection_record_ids(added)
    _verify_projection_rows(connection, projection_ids)
    mutation_controls = _append_only_controls(connection, projection_ids)
    coverage = _action_coverage(corpus, capture_batch)

    cutoff_as_of = {
        "before_nport": corpus.nport_accepted_at - timedelta(microseconds=1),
        "before_prices": corpus.knowable_at - timedelta(microseconds=1),
        "terminal": corpus.knowable_at,
    }
    cutoff_counts = {
        label: _cutoff_counts(
            repository,
            postgres_snapshot,
            as_of=as_of,
        )
        for label, as_of in cutoff_as_of.items()
    }
    row_evidence = D2E3RowCompletenessEvidence(
        issuer_ids=tuple(sorted({item.issuer_id for item in corpus.instruments})),
        instrument_ids=tuple(item.listing_id for item in corpus.instruments),
        cell_keys=tuple(
            demand.planned_cell_id for demand in postgres_snapshot.request.demand_cells
        ),
        domain_counts=dict(
            Counter(
                record.draft.semantic_type_id
                for record in postgres_snapshot.normalized_records
            )
        ),
        added_projection_counts={table: len(ids) for table, ids in projection_ids.items()},
        fixture_snapshot_id=fixture_snapshot.snapshot_id,
        fixture_snapshot_sha256=fixture_snapshot.content_sha256,
        postgres_snapshot_id=postgres_snapshot.snapshot_id,
        postgres_snapshot_sha256=postgres_snapshot.content_sha256,
        action_window_coverage=coverage,
        cutoff_as_of=cutoff_as_of,
        cutoff_domain_counts=cutoff_counts,
        membership_partition_key=TOPT_MEMBERSHIP_PARTITION,
        created_at=postgres_snapshot.resolved_at,
    )

    price_records = tuple(
        record
        for record in terminal_records
        if record.draft.semantic_type_id == MARKET_PRICE_TYPE_ID
    )
    terminal_prices = _stored_payloads(repository, price_records, MarketPricePayload)
    e2_price_ids = e2_handoff.projection_record_ids["staging.mvp_market_prices"]
    e2_price_rows = tuple(repository.get(record_id) for record_id in e2_price_ids)
    if any(row is None for row in e2_price_rows):
        raise ValueError("E3 changed-vintage E2 price evidence disappeared")
    changed_vintages = tuple(
        row.record.normalized_record_id
        for row in sorted(
            (row for row in e2_price_rows if row is not None),
            key=lambda item: (
                item.record.draft.knowable_at,
                item.record.recorded_at,
                item.record.normalized_record_id,
            ),
        )
    )
    terminal_nvda = e2_records[
        "price-bar:listing:xnas:nvda:2026-03-31:unadjusted"
    ].normalized_record_id
    if len(changed_vintages) != 2 or changed_vintages[-1] != terminal_nvda:
        raise ValueError("E3 changed-vintage evidence does not select the corrected NVDA row")

    retained_counts = {
        table: len(ids) for table, ids in e2_handoff.projection_record_ids.items()
    }
    e1_corpus = load_e1_corpus(repository_root)
    registry_entries = {
        **{
            entry.source_registry_entry_id: entry.content_sha256
            for entry in registry.sources
        },
        **{
            entry.semantic_type_registry_entry_id: entry.content_sha256
            for entry in registry.semantic_types
        },
    }
    return MvpToptValidationHandoff(
        corpus_sha256=corpus.corpus_sha256,
        e2_governance_handoff_id=E2_GOVERNANCE_HANDOFF_ID,
        e2_governance_handoff_sha256=E2_GOVERNANCE_HANDOFF_SHA256,
        e2_handoff_id=e2_handoff.handoff_id,
        e2_handoff_sha256=e2_handoff.content_sha256,
        registry_snapshot=registry,
        registry_history_ids=(
            *e2_handoff.registry_history_ids,
            registry.registry_snapshot_id,
        ),
        registry_entry_sha256s=registry_entries,
        raw_object_sha256s=raw_hashes,
        normalized_record_ids=tuple(
            record.normalized_record_id for record in terminal_records
        ),
        added_normalized_record_ids=tuple(
            record.normalized_record_id for record in added
        ),
        added_projection_record_ids=projection_ids,
        snapshot=postgres_snapshot,
        terminal_price_payloads=terminal_prices,
        universe_manifest=universe_manifest,
        row_evidence=row_evidence,
        retained_e2_projection_counts=retained_counts,
        retained_e2_event_bundle_id=e2_handoff.market_event_bundle.event_bundle_id,
        retained_e2_action_record_ids=tuple(
            record.normalized_record_id
            for record in e2_handoff.market_event_bundle.normalized_records
        ),
        retained_e1_case_ids=tuple(sorted(e1_corpus.cases)),
        changed_vintage_record_ids=changed_vintages,
        prior_vintage_record_ids=changed_vintages[:-1],
        terminal_nvda_price_record_id=terminal_nvda,
        append_only_controls=mutation_controls,
        closure_evidence_ids={
            "e2_complete_domain_handoff": e2_handoff.handoff_id,
            "e3_row_completeness_evidence": row_evidence.evidence_id,
            "e3_terminal_snapshot": postgres_snapshot.snapshot_id,
        },
        created_at=postgres_snapshot.resolved_at + timedelta(seconds=1),
    )


def run_d2_e3(
    repository_root: Path,
    connection: Connection[Any],
    raw_store: RawObjectStore,
    *,
    environment: str,
    fail_after_normalized_records: int | None = None,
) -> MvpToptValidationHandoff:
    """Execute the exact terminal TOPT corpus under one rollback boundary."""

    if environment not in {"local", "ci"}:
        raise ValueError("D2 E3 only permits Local/CI execution")
    if (
        fail_after_normalized_records is not None
        and (
            isinstance(fail_after_normalized_records, bool)
            or fail_after_normalized_records < 1
            or fail_after_normalized_records > 81
        )
    ):
        raise ValueError("E3 failure injection threshold must be between 1 and 81")
    with connection.transaction():
        return _run_d2_e3(
            repository_root,
            connection,
            raw_store,
            environment=environment,
            fail_after_normalized_records=fail_after_normalized_records,
        )


class D2E3Activation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    batch_id: Literal["D2-mvp-medium-validation"] = "D2-mvp-medium-validation"
    environment: Literal["local", "ci"]
    expected_e2_handoff_id: str = E2_RUNTIME_HANDOFF_ID
    expected_e2_handoff_sha256: str = E2_RUNTIME_HANDOFF_SHA256
    release_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_parent(self) -> D2E3Activation:
        if (
            self.expected_e2_handoff_id != E2_RUNTIME_HANDOFF_ID
            or self.expected_e2_handoff_sha256 != E2_RUNTIME_HANDOFF_SHA256
        ):
            raise ValueError("D2 E3 activation must bind the accepted E2 runtime handoff")
        return self


@dataclass(frozen=True)
class D2E3RunnerResource:
    repository_root: Path
    connection: Connection[Any]
    raw_store: RawObjectStore
    activation: D2E3Activation

    def run(self) -> MvpToptValidationHandoff:
        handoff = run_d2_e3(
            self.repository_root,
            self.connection,
            self.raw_store,
            environment=self.activation.environment,
        )
        if (
            handoff.e2_handoff_id != self.activation.expected_e2_handoff_id
            or handoff.e2_handoff_sha256
            != self.activation.expected_e2_handoff_sha256
        ):
            raise ValueError("D2 E3 materialization escaped its activated E2 parent")
        return handoff


@dg.asset(
    name=D2_E3_ASSET_NAME,
    group_name="mvp_medium_validation_e3",
    required_resource_keys={"mvp_medium_validation_e3_runner"},
    description="Publish the terminal D2 E3 Local/CI TOPT validation handoff.",
)
def materialize_mvp_medium_validation_e3(
    context,
) -> dg.Output[MvpToptValidationHandoff]:
    runner = cast(
        D2E3RunnerResource,
        context.resources.mvp_medium_validation_e3_runner,
    )
    handoff = runner.run()
    return dg.Output(
        handoff,
        metadata={
            "handoff_id": handoff.handoff_id,
            "required_cell_count": handoff.row_evidence.required_cell_count,
            "observed_cell_count": handoff.row_evidence.observed_cell_count,
            "action_window_cell_count": handoff.row_evidence.action_window_cell_count,
            "environment": runner.activation.environment,
            "stable_handoff": handoff.stable_handoff,
        },
        data_version=dg.DataVersion(handoff.content_sha256),
    )


def build_d2_e3_definitions(
    *,
    repository_root: Path,
    connection: Connection[Any],
    raw_store: RawObjectStore,
    activation: D2E3Activation | ReleaseManifest,
) -> dg.Definitions:
    if not isinstance(activation, D2E3Activation):
        raise ValueError("D2 E3 is restricted to explicit Local/CI activation; release is forbidden")
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
    "E3_CORPUS_PATH",
    "ActionWindowCoverageCell",
    "D2E3Activation",
    "D2E3RowCompletenessEvidence",
    "D2E3RunnerResource",
    "FrozenE3Artifact",
    "FrozenE3Corpus",
    "FrozenE3Instrument",
    "MvpToptValidationHandoff",
    "build_d2_e3_definitions",
    "load_e3_corpus",
    "materialize_mvp_medium_validation_e3",
    "run_d2_e3",
]
