"""Shared Local/CI runtime handoff for the D2 E2 medium-domain boundary."""

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

import dagster as dg
from dagster import AssetExecutionContext
from data_engine.batches.mvp_medium_validation.e0_slice import (
    build_price_registry,
    run_price_pipeline,
)
from data_engine.batches.mvp_medium_validation.e1_slice import (
    D2E1Evidence,
    FrozenE1Artifact,
    FrozenE1Corpus,
    MembershipVintage,
    _action_clock,
    _aware,
    _corrected_price_artifact,
    _dividend_case,
    _financial_facts,
    _membership_vintage,
    _required_text,
    _split_case,
    load_e1_corpus,
    run_d2_e1,
)
from data_engine.contract_repository import PostgresSnapshotRepository
from data_engine.mvp_assets import MvpNormalizationHandoff, run_d1_e2
from data_engine.mvp_medium_models import MarketPricePayload, MvpNormalizationDraft
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
    ACTION_SOURCE_ID,
    CORPORATE_ACTION_TYPE_ID,
    FINANCIAL_FACT_TYPE_ID,
    FINANCIAL_SOURCE_ID,
    IDENTITY_SOURCE_ID,
    ISSUER_SECURITY_TYPE_ID,
    MEDIUM_VERSION,
    MEMBERSHIP_SOURCE_ID,
    SECURITY_LISTING_TYPE_ID,
    UNIVERSE_MEMBERSHIP_TYPE_ID,
    build_medium_registry,
)
from data_engine.mvp_medium_repository import (
    PostgresMediumSemanticRepository,
    build_medium_repository_registrations,
)
from data_engine.mvp_medium_snapshot import (
    MediumSnapshotBundle,
    PostgresMediumSnapshotResolver,
    build_medium_snapshot,
)
from data_engine.mvp_pipeline import run_filing_pipeline
from data_engine.mvp_registry import FILING_SEMANTIC_TYPE_ID
from data_engine.raw_store import raw_ref
from psycopg import Connection
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from truealpha_contracts import (
    CorporateAction,
    DataSource,
    FinancialFact,
    IssuerSecurityLink,
    RawCapture,
    RawObjectStore,
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
from truealpha_contracts.registries import RegistrySnapshot
from truealpha_contracts.release import ReleaseManifest
from truealpha_contracts.universe import SubjectKind, SubjectRef, UniverseRef
from truealpha_contracts.usage import RequirementLevel

D2_E2_ASSET_NAME = "mvp_medium_validation_e2_handoff"
D2_E2_SCHEMA_EPOCH = "staging.mvp-medium-domains.v1+0019+0021"
D1_GOVERNANCE_HANDOFF_PATH = Path("governance/handoffs/D1-mvp-normalization-handoff.v1.json")
D1_GOVERNANCE_HANDOFF_ID = (
    "handoff:d1-mvp-normalization-handoff:6b7dee09f06996dc1635695c94c9802735ba10744964abef65e4c9f5caead7e7"
)
D1_GOVERNANCE_HANDOFF_SHA256 = "d872123a10fa626a5f777182b1f0c822c4013c0b4375ad10c6ea93da00716137"
D1_RUNTIME_HANDOFF_SHA256 = "594dce80771bf98cf940f477ca9889d453a2ee8f66b8b6b51d4d10578c0a4a8c"
D1_RUNTIME_HANDOFF_ID = f"mvp-normalization-handoff:{D1_RUNTIME_HANDOFF_SHA256}"
D1_SCHEMA_EPOCH = "staging.filing-document.v1+0019"
MIGRATION_IDS = ("0019_mvp_filing_document.sql", "0021_mvp_medium_domains.sql")
D2_E2_CONSUMERS = ("D2-mvp-medium-validation",)
EXPECTED_PROJECTION_COUNTS = {
    "staging.filing_documents": 2,
    "staging.mvp_corporate_actions": 2,
    "staging.mvp_financial_facts": 2,
    "staging.mvp_issuer_security_links": 1,
    "staging.mvp_market_prices": 2,
    "staging.mvp_security_listing_links": 1,
    "staging.mvp_universe_memberships": 203,
}


def _sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _module_sha256() -> str:
    return _sha256(Path(__file__).read_bytes())


class FrozenMediumCaptureConfiguration(BaseModel):
    """One checked-in byte object supplied to a registry-selected adapter."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_id: str
    source: DataSource
    source_record_id: str
    body: bytes
    content_type: str
    fetched_at: datetime
    source_published_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("fetched_at", "source_published_at")
    @classmethod
    def validate_datetimes(cls, value: datetime | None, info) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError(f"{info.field_name} must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_bytes(self) -> "FrozenMediumCaptureConfiguration":
        expected = self.metadata.get("artifact_sha256")
        if not isinstance(expected, str) or _sha256(self.body) != expected:
            raise ValueError("frozen medium capture bytes do not match metadata")
        return self


class MediumMarketEventBundle(BaseModel):
    """Content-addressed corporate-action records kept outside factor snapshots."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_bundle_id: str = Field(default="", pattern=r"^(?:|mvp-medium-events:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    normalized_records: tuple[NormalizedRecordRef, ...] = Field(min_length=2, max_length=2)
    actions: tuple[CorporateAction, ...] = Field(min_length=2, max_length=2)
    action_clock_sha256s: dict[str, str]
    e1_observed_ids: tuple[str, ...] = Field(min_length=6)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def identify(self) -> "MediumMarketEventBundle":
        records = tuple(sorted(self.normalized_records, key=lambda item: item.normalized_record_id))
        actions = tuple(sorted(self.actions, key=lambda item: item.action_id))
        if len({record.normalized_record_id for record in records}) != 2:
            raise ValueError("market-event bundle requires two distinct normalized records")
        if len({action.action_id for action in actions}) != 2:
            raise ValueError("market-event bundle requires the split and dividend actions")
        record_payloads = {
            record.draft.payload_sha256: record
            for record in records
            if record.draft.semantic_type_id == CORPORATE_ACTION_TYPE_ID
        }
        if set(record_payloads) != {canonical_sha256(action.model_dump(mode="json")) for action in actions}:
            raise ValueError("market-event records do not bind the typed action payloads")
        clocks = dict(sorted(self.action_clock_sha256s.items()))
        if set(clocks) != {action.action_id for action in actions} or any(
            len(value) != 64 for value in clocks.values()
        ):
            raise ValueError("market-event clock hashes are incomplete")
        if self.created_at < max(record.recorded_at for record in records):
            raise ValueError("market-event bundle cannot predate its normalized records")
        object.__setattr__(self, "normalized_records", records)
        object.__setattr__(self, "actions", actions)
        object.__setattr__(self, "action_clock_sha256s", clocks)
        object.__setattr__(self, "e1_observed_ids", tuple(sorted(set(self.e1_observed_ids))))
        payload = self.model_dump(mode="json", exclude={"event_bundle_id", "content_sha256"})
        expected_hash = canonical_sha256(payload)
        if self.content_sha256 and self.content_sha256 != expected_hash:
            raise ValueError("market-event bundle content hash mismatch")
        if self.event_bundle_id and self.event_bundle_id != f"mvp-medium-events:{expected_hash}":
            raise ValueError("market-event bundle ID mismatch")
        object.__setattr__(self, "content_sha256", expected_hash)
        object.__setattr__(self, "event_bundle_id", f"mvp-medium-events:{expected_hash}")
        return self


class MvpMediumValidationHandoff(BaseModel):
    """Stable, content-addressed Local/CI input for D2 E3."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    handoff_id: str = Field(default="", pattern=r"^(?:|mvp-medium-validation-handoff:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    schema_version: Literal[1] = 1
    batch_id: Literal["D2-mvp-medium-validation"] = "D2-mvp-medium-validation"
    schema_epoch: Literal["staging.mvp-medium-domains.v1+0019+0021"] = "staging.mvp-medium-domains.v1+0019+0021"
    corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    d1_governance_handoff_id: str
    d1_governance_handoff_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    d1_runtime_handoff_id: str
    d1_runtime_handoff_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    d1_schema_epoch: str
    e1_evidence_id: str
    e1_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    migration_sha256s: dict[str, str]
    migration_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    registry_snapshot: RegistrySnapshot
    registry_history_ids: tuple[str, ...] = Field(min_length=3, max_length=3)
    registry_entry_sha256s: dict[str, str]
    projection_record_ids: dict[str, tuple[str, ...]]
    normalized_record_ids: tuple[str, ...] = Field(min_length=213, max_length=213)
    snapshot_bundle: MediumSnapshotBundle
    market_event_bundle: MediumMarketEventBundle
    allowed_consumers: tuple[str, ...] = D2_E2_CONSUMERS
    allowed_environments: tuple[Literal["local", "ci"], ...] = ("ci", "local")
    stable_handoff: Literal[True] = True
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def identify(self) -> "MvpMediumValidationHandoff":
        if (
            self.d1_governance_handoff_id != D1_GOVERNANCE_HANDOFF_ID
            or self.d1_governance_handoff_sha256 != D1_GOVERNANCE_HANDOFF_SHA256
            or self.d1_runtime_handoff_id != D1_RUNTIME_HANDOFF_ID
            or self.d1_runtime_handoff_sha256 != D1_RUNTIME_HANDOFF_SHA256
            or self.d1_schema_epoch != D1_SCHEMA_EPOCH
        ):
            raise ValueError("D2 E2 does not bind the exact accepted D1 handoff")
        migrations = dict(sorted(self.migration_sha256s.items()))
        if tuple(migrations) != MIGRATION_IDS or self.migration_set_sha256 != canonical_sha256(migrations):
            raise ValueError("D2 E2 migration set drifted")
        entries = {
            **{entry.source_registry_entry_id: entry.content_sha256 for entry in self.registry_snapshot.sources},
            **{
                entry.semantic_type_registry_entry_id: entry.content_sha256
                for entry in self.registry_snapshot.semantic_types
            },
        }
        if self.registry_entry_sha256s != dict(sorted(entries.items())):
            raise ValueError("D2 E2 registry entry identities are incomplete")
        projection_ids = {
            table: tuple(sorted(set(record_ids))) for table, record_ids in sorted(self.projection_record_ids.items())
        }
        if {table: len(ids) for table, ids in projection_ids.items()} != EXPECTED_PROJECTION_COUNTS:
            raise ValueError("D2 E2 typed projection counts are incomplete")
        normalized_ids = tuple(sorted(set(self.normalized_record_ids)))
        if len(normalized_ids) != 213 or set(normalized_ids) != {
            record_id for ids in projection_ids.values() for record_id in ids
        }:
            raise ValueError("D2 E2 normalized and projection identities do not reconcile")
        snapshot_ids = {
            record.normalized_record_id
            for snapshot in self.snapshot_bundle.snapshots
            for record in snapshot.normalized_records
        }
        event_ids = {record.normalized_record_id for record in self.market_event_bundle.normalized_records}
        if not snapshot_ids <= set(normalized_ids) or not event_ids <= set(normalized_ids):
            raise ValueError("D2 E2 bundle references records outside the persistent handoff")
        registry_ids = {snapshot.registry_snapshot.registry_snapshot_id for snapshot in self.snapshot_bundle.snapshots}
        if registry_ids != {self.registry_snapshot.registry_snapshot_id}:
            raise ValueError("D2 E2 snapshots do not use the composite registry")
        if tuple(sorted(set(self.allowed_consumers))) != D2_E2_CONSUMERS:
            raise ValueError("D2 E2 consumer allow-list drifted")
        if tuple(sorted(set(self.allowed_environments))) != ("ci", "local"):
            raise ValueError("D2 E2 is restricted to Local/CI")
        if self.created_at < max(
            self.snapshot_bundle.created_at,
            self.market_event_bundle.created_at,
        ):
            raise ValueError("D2 E2 handoff cannot predate its runtime bundles")
        object.__setattr__(self, "migration_sha256s", migrations)
        object.__setattr__(self, "registry_entry_sha256s", dict(sorted(entries.items())))
        object.__setattr__(self, "projection_record_ids", projection_ids)
        object.__setattr__(self, "normalized_record_ids", normalized_ids)
        object.__setattr__(self, "allowed_consumers", D2_E2_CONSUMERS)
        object.__setattr__(self, "allowed_environments", ("ci", "local"))
        payload = self.model_dump(mode="json", exclude={"handoff_id", "content_sha256"})
        expected_hash = canonical_sha256(payload)
        if self.content_sha256 and self.content_sha256 != expected_hash:
            raise ValueError("D2 E2 handoff content hash mismatch")
        expected_id = f"mvp-medium-validation-handoff:{expected_hash}"
        if self.handoff_id and self.handoff_id != expected_id:
            raise ValueError("D2 E2 handoff ID mismatch")
        object.__setattr__(self, "content_sha256", expected_hash)
        object.__setattr__(self, "handoff_id", expected_id)
        return self


def _verify_d1_governance_handoff(repository_root: Path) -> dict[str, Any]:
    handoff_path = repository_root / D1_GOVERNANCE_HANDOFF_PATH
    body = handoff_path.read_bytes()
    if _sha256(body) != D1_GOVERNANCE_HANDOFF_SHA256:
        raise ValueError("accepted D1 governance handoff bytes drifted")
    payload = json.loads(body)
    if (
        payload.get("handoff_id") != D1_GOVERNANCE_HANDOFF_ID
        or payload.get("state") != "accepted"
        or payload.get("schema_epoch") != D1_SCHEMA_EPOCH
        or "D2-mvp-medium-validation" not in payload.get("allowed_consumers", ())
        or tuple(sorted(payload.get("allowed_environments", ()))) != ("ci", "local")
        or any(payload.get("revocation", {}).values())
    ):
        raise ValueError("accepted D1 governance handoff is not active for D2 Local/CI")
    return cast(dict[str, Any], payload)


def _verify_d1_runtime_handoff(handoff: MvpNormalizationHandoff) -> None:
    if (
        handoff.handoff_id != D1_RUNTIME_HANDOFF_ID
        or handoff.content_sha256 != D1_RUNTIME_HANDOFF_SHA256
        or handoff.schema_epoch != D1_SCHEMA_EPOCH
        or "D2-mvp-medium-validation" not in handoff.allowed_consumers
        or tuple(sorted(handoff.allowed_environments)) != ("ci", "local")
    ):
        raise ValueError("D2 E2 did not reproduce the exact accepted D1 runtime handoff")


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


def _content_type(artifact: FrozenE1Artifact) -> str:
    suffix = Path(artifact.path).suffix.lower()
    try:
        return {
            ".html": "text/html",
            ".json": "application/json",
            ".xml": "application/xml",
        }[suffix]
    except KeyError as error:
        raise ValueError(f"unsupported D2 E2 fixture content type: {artifact.path}") from error


def _configuration(
    corpus: FrozenE1Corpus,
    artifact: FrozenE1Artifact,
    *,
    source: DataSource,
    fetched_at: datetime,
    published_at: datetime | None = None,
) -> FrozenMediumCaptureConfiguration:
    return FrozenMediumCaptureConfiguration(
        artifact_id=artifact.artifact_id,
        source=source,
        source_record_id=f"d2-e2:{artifact.artifact_id}",
        body=artifact.body,
        content_type=_content_type(artifact),
        fetched_at=fetched_at,
        source_published_at=published_at,
        metadata={
            "artifact_id": artifact.artifact_id,
            "artifact_sha256": artifact.sha256,
            "corpus_sha256": corpus.corpus_sha256,
            "checked_in_fixture": True,
        },
    )


def _typed_with_raw_ref[ModelT: BaseModel](
    model_type: type[ModelT],
    payload: ModelT,
    raw_reference: str,
    **updates: Any,
) -> ModelT:
    return model_type.model_validate(
        {
            **payload.model_dump(mode="python"),
            **updates,
            "raw_ref": raw_reference,
        }
    )


def _raw_object_ref(capture: LandedMediumCapture) -> str:
    return f"raw-object:{capture.raw_object_sha256}"


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
    confidence: Any,
    raw_reference: str,
    is_restatement: bool = False,
    supersedes_document_id: str | None = None,
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
        raw_ref=raw_reference,
        is_restatement=is_restatement,
        supersedes_document_id=supersedes_document_id,
    )


def _identity_normalizers(
    issuer_security: IssuerSecurityLink,
    security_listing: SecurityListingLink,
):
    def issuer(capture: LandedMediumCapture) -> tuple[MvpNormalizationDraft, ...]:
        payload = _typed_with_raw_ref(
            IssuerSecurityLink,
            issuer_security,
            _raw_object_ref(capture),
        )
        return (
            _draft(
                semantic_type_id=ISSUER_SECURITY_TYPE_ID,
                payload=payload,
                subject=SubjectRef(kind=SubjectKind.ISSUER, id=payload.issuer_id),
                valid_from=payload.valid_from,
                valid_to=payload.valid_to or date.max,
                knowable_at=payload.knowable_at,
                recorded_at=payload.recorded_at,
                document_id=payload.input_id,
                confidence=payload.confidence,
                raw_reference=capture.raw_ref,
            ),
        )

    def listing(capture: LandedMediumCapture) -> tuple[MvpNormalizationDraft, ...]:
        payload = _typed_with_raw_ref(
            SecurityListingLink,
            security_listing,
            _raw_object_ref(capture),
        )
        return (
            _draft(
                semantic_type_id=SECURITY_LISTING_TYPE_ID,
                payload=payload,
                subject=SubjectRef(kind=SubjectKind.SECURITY, id=payload.security_id),
                valid_from=payload.valid_from,
                valid_to=payload.valid_to or date.max,
                knowable_at=payload.knowable_at,
                recorded_at=payload.recorded_at,
                document_id=payload.input_id,
                confidence=payload.confidence,
                raw_reference=capture.raw_ref,
            ),
        )

    return issuer, listing


def _financial_normalizer(
    facts: tuple[FinancialFact, FinancialFact],
):
    def normalize(capture: LandedMediumCapture) -> tuple[MvpNormalizationDraft, ...]:
        original, amended = tuple(
            _typed_with_raw_ref(
                FinancialFact,
                fact,
                _raw_object_ref(capture),
                entity_id="issuer.plug",
            )
            for fact in facts
        )
        original_document = f"financial-fact:{original.accession}"
        return (
            _draft(
                semantic_type_id=FINANCIAL_FACT_TYPE_ID,
                payload=original,
                subject=SubjectRef(kind=SubjectKind.ISSUER, id=original.entity_id),
                valid_from=original.valid_from,
                valid_to=original.valid_to,
                knowable_at=original.knowable_at,
                recorded_at=original.recorded_at,
                document_id=original_document,
                confidence=original.confidence,
                raw_reference=capture.raw_ref,
            ),
            _draft(
                semantic_type_id=FINANCIAL_FACT_TYPE_ID,
                payload=amended,
                subject=SubjectRef(kind=SubjectKind.ISSUER, id=amended.entity_id),
                valid_from=amended.valid_from,
                valid_to=amended.valid_to,
                knowable_at=amended.knowable_at,
                recorded_at=amended.recorded_at,
                document_id=f"financial-fact:{amended.accession}",
                confidence=amended.confidence,
                raw_reference=capture.raw_ref,
                is_restatement=True,
                supersedes_document_id=original_document,
            ),
        )

    return normalize


def _action_normalizer(actions_by_artifact: dict[str, CorporateAction]):
    def normalize(capture: LandedMediumCapture) -> tuple[MvpNormalizationDraft, ...]:
        artifact_id = cast(str, capture.metadata["artifact_id"])
        try:
            action = _typed_with_raw_ref(
                CorporateAction,
                actions_by_artifact[artifact_id],
                _raw_object_ref(capture),
            )
        except KeyError as error:
            raise ValueError(f"unregistered action fixture: {artifact_id}") from error
        lifecycle = action.lifecycle_times().values()
        valid_from = min(action.declared_at, action.knowable_at, *lifecycle).date()
        valid_to = max(action.declared_at, action.knowable_at, *lifecycle).date()
        return (
            _draft(
                semantic_type_id=CORPORATE_ACTION_TYPE_ID,
                payload=action,
                subject=SubjectRef(kind=SubjectKind.SECURITY, id=action.security_id),
                valid_from=valid_from,
                valid_to=valid_to,
                knowable_at=action.knowable_at,
                recorded_at=action.recorded_at,
                document_id=action.action_id,
                confidence=action.confidence,
                raw_reference=capture.raw_ref,
            ),
        )

    return normalize


def _membership_normalizer(vintages_by_artifact: dict[str, MembershipVintage]):
    def normalize(capture: LandedMediumCapture) -> tuple[MvpNormalizationDraft, ...]:
        artifact_id = cast(str, capture.metadata["artifact_id"])
        try:
            vintage = vintages_by_artifact[artifact_id]
        except KeyError as error:
            raise ValueError(f"unregistered membership fixture: {artifact_id}") from error
        return tuple(
            _draft(
                semantic_type_id=UNIVERSE_MEMBERSHIP_TYPE_ID,
                payload=(
                    payload := _typed_with_raw_ref(
                        UniverseMembership,
                        membership,
                        _raw_object_ref(capture),
                    )
                ),
                subject=payload.subject,
                valid_from=payload.valid_from,
                valid_to=payload.valid_to or date.max,
                knowable_at=payload.knowable_at,
                recorded_at=payload.recorded_at,
                document_id=payload.membership_id,
                confidence=payload.confidence,
                raw_reference=capture.raw_ref,
            )
            for membership in vintage.records
        )

    return normalize


def _component_catalog(
    *,
    registry: RegistrySnapshot,
    implementation_sha256: str,
    issuer_security: IssuerSecurityLink,
    security_listing: SecurityListingLink,
    facts: tuple[FinancialFact, FinancialFact],
    actions_by_artifact: dict[str, CorporateAction],
    vintages_by_artifact: dict[str, MembershipVintage],
) -> MediumComponentCatalog:
    sources = {(entry.source_id, entry.version): entry for entry in registry.sources}
    semantic_types = {(entry.semantic_type_id, entry.version): entry for entry in registry.semantic_types}
    raw_sources = {
        IDENTITY_SOURCE_ID: DataSource.SEC,
        FINANCIAL_SOURCE_ID: DataSource.SEC,
        ACTION_SOURCE_ID: DataSource.YAHOO,
        MEMBERSHIP_SOURCE_ID: DataSource.SEC,
    }
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
        for source_id, raw_source in raw_sources.items()
    )
    issuer_normalizer, listing_normalizer = _identity_normalizers(
        issuer_security,
        security_listing,
    )
    routes = {
        (IDENTITY_SOURCE_ID, ISSUER_SECURITY_TYPE_ID): issuer_normalizer,
        (IDENTITY_SOURCE_ID, SECURITY_LISTING_TYPE_ID): listing_normalizer,
        (FINANCIAL_SOURCE_ID, FINANCIAL_FACT_TYPE_ID): _financial_normalizer(facts),
        (ACTION_SOURCE_ID, CORPORATE_ACTION_TYPE_ID): _action_normalizer(actions_by_artifact),
        (MEMBERSHIP_SOURCE_ID, UNIVERSE_MEMBERSHIP_TYPE_ID): _membership_normalizer(vintages_by_artifact),
    }
    normalizers = tuple(
        MediumNormalizerRegistration(
            source_id=source_id,
            source_version=MEDIUM_VERSION,
            semantic_type_id=semantic_type_id,
            semantic_type_version=semantic_types[(semantic_type_id, MEDIUM_VERSION)].version,
            normalizer_id=sources[(source_id, MEDIUM_VERSION)].normalizer_id,
            normalizer_version=sources[(source_id, MEDIUM_VERSION)].normalizer_version,
            normalizer_implementation_sha256=implementation_sha256,
            normalize=normalizer,
        )
        for (source_id, semantic_type_id), normalizer in routes.items()
    )
    return MediumComponentCatalog(
        registry=registry,
        adapters=adapters,
        normalizers=normalizers,
    )


def _membership_vintages(corpus: FrozenE1Corpus) -> tuple[MembershipVintage, MembershipVintage]:
    expected = cast(dict[str, Any], corpus.cases["qqq-membership-vintages"]["expected"])
    first = _membership_vintage(
        corpus.artifacts["qqq-membership-2025q4"],
        knowable_at=_aware(
            expected["first_knowable_at"],
            label="membership.first_knowable_at",
        ),
        selected_name=_required_text(expected, "removed_name"),
    )
    second = _membership_vintage(
        corpus.artifacts["qqq-membership-2026q1"],
        knowable_at=_aware(
            expected["second_knowable_at"],
            label="membership.second_knowable_at",
        ),
        selected_name=_required_text(expected, "added_name"),
    )
    if len(first.records) != 101 or len(second.records) != 102:
        raise ValueError("D2 E2 requires the complete 101/102-record QQQ vintages")
    return first, second


def _capture_work_items(
    *,
    corpus: FrozenE1Corpus,
    issuer_security: IssuerSecurityLink,
    facts: tuple[FinancialFact, FinancialFact],
    split: CorporateAction,
    dividend: CorporateAction,
    first_vintage: MembershipVintage,
    second_vintage: MembershipVintage,
) -> tuple[MediumCaptureWorkItem, ...]:
    identity = corpus.artifacts["nvda-listing-identity"]
    company_facts = corpus.artifacts["plug-company-facts"]
    split_events = corpus.artifacts["nvda-split-events"]
    dividend_events = corpus.artifacts["jpm-dividend-events"]
    first_membership = corpus.artifacts["qqq-membership-2025q4"]
    second_membership = corpus.artifacts["qqq-membership-2026q1"]
    return (
        MediumCaptureWorkItem(
            source_id=IDENTITY_SOURCE_ID,
            source_version=MEDIUM_VERSION,
            semantic_type_ids=(ISSUER_SECURITY_TYPE_ID, SECURITY_LISTING_TYPE_ID),
            semantic_type_version=MEDIUM_VERSION,
            configuration=_configuration(
                corpus,
                identity,
                source=DataSource.SEC,
                fetched_at=issuer_security.knowable_at,
                published_at=issuer_security.knowable_at,
            ),
            recorded_at=issuer_security.recorded_at,
        ),
        MediumCaptureWorkItem(
            source_id=FINANCIAL_SOURCE_ID,
            source_version=MEDIUM_VERSION,
            semantic_type_ids=(FINANCIAL_FACT_TYPE_ID,),
            semantic_type_version=MEDIUM_VERSION,
            configuration=_configuration(
                corpus,
                company_facts,
                source=DataSource.SEC,
                fetched_at=min(fact.recorded_at for fact in facts),
            ),
            recorded_at=min(fact.recorded_at for fact in facts),
        ),
        MediumCaptureWorkItem(
            source_id=ACTION_SOURCE_ID,
            source_version=MEDIUM_VERSION,
            semantic_type_ids=(CORPORATE_ACTION_TYPE_ID,),
            semantic_type_version=MEDIUM_VERSION,
            configuration=_configuration(
                corpus,
                split_events,
                source=DataSource.YAHOO,
                fetched_at=split.knowable_at,
            ),
            recorded_at=split.recorded_at,
        ),
        MediumCaptureWorkItem(
            source_id=ACTION_SOURCE_ID,
            source_version=MEDIUM_VERSION,
            semantic_type_ids=(CORPORATE_ACTION_TYPE_ID,),
            semantic_type_version=MEDIUM_VERSION,
            configuration=_configuration(
                corpus,
                dividend_events,
                source=DataSource.YAHOO,
                fetched_at=dividend.knowable_at,
            ),
            recorded_at=dividend.recorded_at,
        ),
        MediumCaptureWorkItem(
            source_id=MEMBERSHIP_SOURCE_ID,
            source_version=MEDIUM_VERSION,
            semantic_type_ids=(UNIVERSE_MEMBERSHIP_TYPE_ID,),
            semantic_type_version=MEDIUM_VERSION,
            configuration=_configuration(
                corpus,
                first_membership,
                source=DataSource.SEC,
                fetched_at=first_vintage.knowable_at,
                published_at=first_vintage.knowable_at,
            ),
            recorded_at=first_vintage.records[0].recorded_at,
        ),
        MediumCaptureWorkItem(
            source_id=MEMBERSHIP_SOURCE_ID,
            source_version=MEDIUM_VERSION,
            semantic_type_ids=(UNIVERSE_MEMBERSHIP_TYPE_ID,),
            semantic_type_version=MEDIUM_VERSION,
            configuration=_configuration(
                corpus,
                second_membership,
                source=DataSource.SEC,
                fetched_at=second_vintage.knowable_at,
                published_at=second_vintage.knowable_at,
            ),
            recorded_at=second_vintage.records[0].recorded_at,
        ),
    )


def _policy_bindings(*, include_membership: bool) -> tuple[PolicyBinding, ...]:
    roles = set(PolicyRole)
    if not include_membership:
        roles.remove(PolicyRole.MEMBERSHIP)
    return tuple(
        PolicyBinding(
            role=role,
            policy_id=f"policy.d2-e2-{role.value.replace('_', '-')}",
            policy_version=MEDIUM_VERSION,
            implementation_sha256=canonical_sha256(
                {"batch": "D2-mvp-medium-validation", "rung": "E2", "role": role.value}
            ),
        )
        for role in sorted(roles, key=lambda item: item.value)
    )


def _demand(
    record: NormalizedRecordRef,
    *,
    domain: DataDomain,
    partition_key: str,
    label: str,
) -> SnapshotDemandCell:
    coordinate = {
        "label": label,
        "semantic_type_id": record.draft.semantic_type_id,
        "semantic_type_version": record.draft.semantic_type_version,
        "subject": record.draft.subject.model_dump(mode="json"),
        "partition_key": partition_key,
    }
    return SnapshotDemandCell(
        requirement_id=f"data-requirement:{canonical_sha256({'kind': 'data', **coordinate})}",
        capture_requirement_id=(f"capture-requirement:{canonical_sha256({'kind': 'capture', **coordinate})}"),
        semantic_type_id=record.draft.semantic_type_id,
        semantic_type_version=record.draft.semantic_type_version,
        domain=domain,
        subject=record.draft.subject,
        partition_key=partition_key,
        level=RequirementLevel.REQUIRED,
    )


def _snapshot_request(
    *,
    registry: RegistrySnapshot,
    records: tuple[NormalizedRecordRef, ...],
    partitions: tuple[str, ...],
    label: str,
    as_of: datetime,
    valid_on: date,
    subjects: tuple[SubjectRef, ...] = (),
    universe: UniverseRef | None = None,
) -> tuple[SnapshotRequest, dict[str, tuple[NormalizedRecordRef, ...]]]:
    if len(records) != len(partitions):
        raise ValueError("snapshot record and partition plans differ")
    domain_by_type = {entry.semantic_type_id: entry.domain for entry in registry.semantic_types}
    demands = tuple(
        _demand(
            record,
            domain=domain_by_type[record.draft.semantic_type_id],
            partition_key=partition,
            label=f"{label}:{position}",
        )
        for position, (record, partition) in enumerate(
            zip(records, partitions, strict=True),
            start=1,
        )
    )
    request = SnapshotRequest(
        subjects=subjects,
        universe=universe,
        as_of=as_of,
        valid_on=valid_on,
        registry_snapshot_id=registry.registry_snapshot_id,
        registry_snapshot_sha256=registry.content_sha256,
        source_registry_id=registry.source_registry_snapshot_id,
        source_registry_sha256=registry.source_registry_sha256,
        semantic_type_registry_id=registry.semantic_type_registry_snapshot_id,
        semantic_type_registry_sha256=registry.semantic_type_registry_sha256,
        policy_bindings=_policy_bindings(include_membership=universe is not None),
        demand_cells=demands,
    )
    selected: dict[str, tuple[NormalizedRecordRef, ...]] = {
        demand.planned_cell_id: (record,) for demand, record in zip(demands, records, strict=True)
    }
    return request, selected


def _resolve_snapshot_pair(
    *,
    resolver: PostgresMediumSnapshotResolver,
    request: SnapshotRequest,
    registry: RegistrySnapshot,
    fixture_records: dict[str, tuple[NormalizedRecordRef, ...]],
    resolved_at: datetime,
    universe_manifest: UniverseManifest | None = None,
    universe_memberships: tuple[UniverseMembership, ...] = (),
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
    if postgres != fixture or postgres.snapshot_id != fixture.snapshot_id:
        raise ValueError("D2 E2 fixture/Postgres snapshot parity failed")
    return postgres


def _stored_memberships(
    repository: PostgresMediumSemanticRepository,
    records: tuple[NormalizedRecordRef, ...],
) -> tuple[UniverseMembership, ...]:
    rows = tuple(repository.get(record.normalized_record_id) for record in records)
    if any(row is None for row in rows):
        raise ValueError("D2 E2 membership payload disappeared after normalization")
    return tuple(cast(UniverseMembership, row.payload) for row in rows if row is not None)


def _projection_record_ids(
    *,
    filing_records: tuple[NormalizedRecordRef, ...],
    price_records: tuple[NormalizedRecordRef, ...],
    medium_records: tuple[NormalizedRecordRef, ...],
) -> dict[str, tuple[str, ...]]:
    table_by_type = {
        FILING_SEMANTIC_TYPE_ID: "staging.filing_documents",
        "semantic.market-price": "staging.mvp_market_prices",
        FINANCIAL_FACT_TYPE_ID: "staging.mvp_financial_facts",
        CORPORATE_ACTION_TYPE_ID: "staging.mvp_corporate_actions",
        UNIVERSE_MEMBERSHIP_TYPE_ID: "staging.mvp_universe_memberships",
        ISSUER_SECURITY_TYPE_ID: "staging.mvp_issuer_security_links",
        SECURITY_LISTING_TYPE_ID: "staging.mvp_security_listing_links",
    }
    grouped: dict[str, list[str]] = {table: [] for table in table_by_type.values()}
    for record in (*filing_records, *price_records, *medium_records):
        grouped[table_by_type[record.draft.semantic_type_id]].append(record.normalized_record_id)
    result = {table: tuple(sorted(record_ids)) for table, record_ids in grouped.items()}
    if {table: len(ids) for table, ids in result.items()} != EXPECTED_PROJECTION_COUNTS:
        raise ValueError("D2 E2 persistent record plan is incomplete")
    return result


def _verify_projection_rows(
    connection: Connection[Any],
    projection_record_ids: dict[str, tuple[str, ...]],
) -> None:
    for table, record_ids in projection_record_ids.items():
        schema, name = table.split(".", maxsplit=1)
        if schema != "staging" or not name.replace("_", "").isalnum():
            raise ValueError(f"unsafe D2 E2 projection table: {table}")
        row = connection.execute(
            f"select count(*) from {table} where normalized_record_id = any(%s)",
            (list(record_ids),),
        ).fetchone()
        if row is None or row[0] != len(record_ids):
            raise ValueError(f"D2 E2 projection row count drifted for {table}")


def _event_bundle(
    *,
    records_by_document: dict[str, NormalizedRecordRef],
    actions: tuple[CorporateAction, CorporateAction],
    e1: D2E1Evidence,
    created_at: datetime,
) -> MediumMarketEventBundle:
    records = tuple(records_by_document[action.action_id] for action in actions)
    action_clock_sha256s = {
        action.action_id: canonical_sha256(
            [tick.model_dump(mode="json") for tick in _action_clock(action, as_of=created_at)]
        )
        for action in actions
    }
    observed = tuple(
        observed_id
        for case in e1.cases
        if case.case_id in {"nvda-split-lifecycle", "jpm-dividend-lifecycle"}
        for observed_id in case.observed_ids
    )
    return MediumMarketEventBundle(
        normalized_records=records,
        actions=actions,
        action_clock_sha256s=action_clock_sha256s,
        e1_observed_ids=observed,
        created_at=created_at,
    )


def _snapshot_bundle(
    *,
    connection: Connection[Any],
    registry: RegistrySnapshot,
    records_by_document: dict[str, NormalizedRecordRef],
    amended_filing: NormalizedRecordRef,
    amended_fact: FinancialFact,
    issuer_security: IssuerSecurityLink,
    security_listing: SecurityListingLink,
    corrected_price: NormalizedRecordRef,
    first_vintage: MembershipVintage,
    second_vintage: MembershipVintage,
    resolved_at: datetime,
) -> MediumSnapshotBundle:
    resolver = PostgresMediumSnapshotResolver(
        semantic_records=PostgresMediumSemanticRepository(
            connection,
            registry=registry,
            registrations=build_medium_repository_registrations(registry),
        ),
        snapshots=PostgresSnapshotRepository(connection),
    )
    amended_fact_record = records_by_document[f"financial-fact:{amended_fact.accession}"]
    issuer_link_record = records_by_document[issuer_security.input_id]
    listing_link_record = records_by_document[security_listing.input_id]

    plug_records = (amended_filing, amended_fact_record)
    plug_request, plug_fixture = _snapshot_request(
        registry=registry,
        records=plug_records,
        partitions=("all", amended_fact.fiscal_period),
        label="plug-restatement",
        as_of=max(record.draft.knowable_at for record in plug_records),
        valid_on=date(2020, 12, 31),
        subjects=(SubjectRef(kind=SubjectKind.ISSUER, id="issuer.plug"),),
    )
    plug = _resolve_snapshot_pair(
        resolver=resolver,
        request=plug_request,
        registry=registry,
        fixture_records=plug_fixture,
        resolved_at=resolved_at,
    )

    identity_records = (issuer_link_record, listing_link_record)
    identity_request, identity_fixture = _snapshot_request(
        registry=registry,
        records=identity_records,
        partitions=("all", "all"),
        label="nvda-identity-at-publication",
        as_of=max(record.draft.knowable_at for record in identity_records),
        valid_on=date(2024, 6, 7),
        subjects=tuple(record.draft.subject for record in identity_records),
    )
    identity = _resolve_snapshot_pair(
        resolver=resolver,
        request=identity_request,
        registry=registry,
        fixture_records=identity_fixture,
        resolved_at=resolved_at,
    )

    first_records = tuple(records_by_document[membership.membership_id] for membership in first_vintage.records)
    first_request, first_fixture = _snapshot_request(
        registry=registry,
        records=first_records,
        partitions=("all",) * len(first_records),
        label="qqq-2025q4-membership",
        as_of=first_vintage.knowable_at,
        valid_on=first_vintage.report_date,
        universe=first_vintage.manifest.ref,
    )
    first_payloads = _stored_memberships(resolver.semantic_records, first_records)
    first_membership = _resolve_snapshot_pair(
        resolver=resolver,
        request=first_request,
        registry=registry,
        fixture_records=first_fixture,
        resolved_at=resolved_at,
        universe_manifest=first_vintage.manifest,
        universe_memberships=first_payloads,
    )

    price_records = (corrected_price, issuer_link_record, listing_link_record)
    price_request, price_fixture = _snapshot_request(
        registry=registry,
        records=price_records,
        partitions=("date:2026-03-31", "all", "all"),
        label="nvda-corrected-price-and-identity",
        as_of=max(record.draft.knowable_at for record in price_records),
        valid_on=date(2026, 3, 31),
        subjects=tuple(record.draft.subject for record in price_records),
    )
    price = _resolve_snapshot_pair(
        resolver=resolver,
        request=price_request,
        registry=registry,
        fixture_records=price_fixture,
        resolved_at=resolved_at,
    )

    second_records = tuple(records_by_document[membership.membership_id] for membership in second_vintage.records)
    second_request, second_fixture = _snapshot_request(
        registry=registry,
        records=second_records,
        partitions=("all",) * len(second_records),
        label="qqq-2026q1-membership",
        as_of=second_vintage.knowable_at,
        valid_on=second_vintage.report_date,
        universe=second_vintage.manifest.ref,
    )
    second_payloads = _stored_memberships(resolver.semantic_records, second_records)
    second_membership = _resolve_snapshot_pair(
        resolver=resolver,
        request=second_request,
        registry=registry,
        fixture_records=second_fixture,
        resolved_at=resolved_at,
        universe_manifest=second_vintage.manifest,
        universe_memberships=second_payloads,
    )

    return MediumSnapshotBundle(
        snapshots=(plug, identity, first_membership, price, second_membership),
        created_at=resolved_at,
    )


def run_d2_e2(
    repository_root: Path,
    connection: Connection[Any],
    raw_store: RawObjectStore,
    *,
    environment: str,
) -> MvpMediumValidationHandoff:
    """Execute the complete E1 domain set through the shared E2 boundary."""

    if environment not in {"local", "ci"}:
        raise ValueError("D2 E2 only permits Local/CI execution")
    _verify_d1_governance_handoff(repository_root)
    d1_handoff = run_d1_e2(repository_root, connection, raw_store)
    _verify_d1_runtime_handoff(d1_handoff)
    e1 = run_d2_e1(
        repository_root,
        connection,
        raw_store,
        environment=environment,
    )
    corpus = load_e1_corpus(repository_root)
    if e1.corpus_sha256 != corpus.corpus_sha256:
        raise ValueError("D2 E2 did not consume the exact accepted E1 corpus")

    filing_run = run_filing_pipeline(
        repository_root=repository_root,
        connection=connection,
        raw_store=raw_store,
    )
    original_price_run = run_price_pipeline(
        repository_root,
        connection,
        raw_store,
        environment=environment,
    )
    correction = _corrected_price_artifact(corpus, original_price_run)
    price_run = run_price_pipeline(
        repository_root,
        connection,
        raw_store,
        environment=environment,
        artifacts=(original_price_run.artifacts[0], correction),
    )
    if price_run.registry != build_price_registry():
        raise ValueError("D2 E2 price parent registry drifted")

    facts = _financial_facts(corpus)
    split, split_case = _split_case(corpus, price_run)
    dividend, dividend_case = _dividend_case(corpus)
    if not split_case.passed or not dividend_case.passed:
        raise ValueError("D2 E2 action semantics regressed from E1")
    first_vintage, second_vintage = _membership_vintages(corpus)
    issuer_security = price_run.case.issuer_security
    security_listing = price_run.case.security_listing

    implementation_sha256 = _module_sha256()
    registry, history = build_medium_registry(
        price_run.registry,
        source_implementation_sha256=implementation_sha256,
    )
    repository = PostgresMediumSemanticRepository(
        connection,
        registry=registry,
        registrations=build_medium_repository_registrations(registry),
    )

    for record, filing_payload, fetch_id in zip(
        filing_run.records,
        filing_run.payloads,
        filing_run.raw_fetch_ids,
        strict=True,
    ):
        repository.put(record, filing_payload, raw_ref=raw_ref(fetch_id))
    shared_price_payloads = tuple(
        MarketPricePayload.model_validate(payload.model_dump(mode="python")) for payload in price_run.payloads
    )
    for record, price_payload, fetch_id in zip(
        price_run.records,
        shared_price_payloads,
        price_run.raw_fetch_ids,
        strict=True,
    ):
        repository.put(record, price_payload, raw_ref=raw_ref(fetch_id))

    catalog = _component_catalog(
        registry=registry,
        implementation_sha256=implementation_sha256,
        issuer_security=issuer_security,
        security_listing=security_listing,
        facts=facts,
        actions_by_artifact={
            "nvda-split-events": split,
            "jpm-dividend-events": dividend,
        },
        vintages_by_artifact={
            "qqq-membership-2025q4": first_vintage,
            "qqq-membership-2026q1": second_vintage,
        },
    )
    capture_batch = land_medium_capture_plan(
        connection,
        object_store=raw_store,
        catalog=catalog,
        work_items=_capture_work_items(
            corpus=corpus,
            issuer_security=issuer_security,
            facts=facts,
            split=split,
            dividend=dividend,
            first_vintage=first_vintage,
            second_vintage=second_vintage,
        ),
    )
    normalization_batch = normalize_medium_capture_batch(
        batch=capture_batch,
        catalog=catalog,
        repository=repository,
    )
    if len(normalization_batch.normalized_records) != 209:
        raise ValueError("D2 E2 did not normalize the full medium-domain fixture set")

    records_by_document = {record.document_id: record for record in normalization_batch.normalized_records}
    if len(records_by_document) != len(normalization_batch.normalized_records):
        raise ValueError("D2 E2 normalized documents are not uniquely addressable")
    resolved_at = corpus.created_at
    snapshots = _snapshot_bundle(
        connection=connection,
        registry=registry,
        records_by_document=records_by_document,
        amended_filing=filing_run.records[-1],
        amended_fact=facts[-1],
        issuer_security=issuer_security,
        security_listing=security_listing,
        corrected_price=price_run.records[-1],
        first_vintage=first_vintage,
        second_vintage=second_vintage,
        resolved_at=resolved_at,
    )
    persisted_action_rows = tuple(
        repository.get(records_by_document[action.action_id].normalized_record_id) for action in (split, dividend)
    )
    if any(row is None for row in persisted_action_rows):
        raise ValueError("D2 E2 action projection disappeared after normalization")
    persisted_actions = tuple(cast(CorporateAction, row.payload) for row in persisted_action_rows if row is not None)
    events = _event_bundle(
        records_by_document=records_by_document,
        actions=cast(tuple[CorporateAction, CorporateAction], persisted_actions),
        e1=e1,
        created_at=resolved_at,
    )
    projection_ids = _projection_record_ids(
        filing_records=filing_run.records,
        price_records=price_run.records,
        medium_records=normalization_batch.normalized_records,
    )
    _verify_projection_rows(connection, projection_ids)

    migration_sha256s = {
        migration_id: _sha256((repository_root / "db" / "migrations" / migration_id).read_bytes())
        for migration_id in MIGRATION_IDS
    }
    registry_entry_sha256s = {
        **{entry.source_registry_entry_id: entry.content_sha256 for entry in registry.sources},
        **{entry.semantic_type_registry_entry_id: entry.content_sha256 for entry in registry.semantic_types},
    }
    normalized_ids = tuple(record_id for ids in projection_ids.values() for record_id in ids)
    return MvpMediumValidationHandoff(
        corpus_sha256=corpus.corpus_sha256,
        d1_governance_handoff_id=D1_GOVERNANCE_HANDOFF_ID,
        d1_governance_handoff_sha256=D1_GOVERNANCE_HANDOFF_SHA256,
        d1_runtime_handoff_id=d1_handoff.handoff_id,
        d1_runtime_handoff_sha256=d1_handoff.content_sha256,
        d1_schema_epoch=d1_handoff.schema_epoch,
        e1_evidence_id=e1.evidence_id,
        e1_evidence_sha256=e1.content_sha256,
        migration_sha256s=migration_sha256s,
        migration_set_sha256=canonical_sha256(migration_sha256s),
        registry_snapshot=registry,
        registry_history_ids=tuple(snapshot.registry_snapshot_id for snapshot in history.snapshots),
        registry_entry_sha256s=registry_entry_sha256s,
        projection_record_ids=projection_ids,
        normalized_record_ids=normalized_ids,
        snapshot_bundle=snapshots,
        market_event_bundle=events,
        created_at=resolved_at + timedelta(seconds=1),
    )


class D2E2Activation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    batch_id: Literal["D2-mvp-medium-validation"] = "D2-mvp-medium-validation"
    environment: Literal["local", "ci"]
    expected_d1_handoff_id: str = D1_RUNTIME_HANDOFF_ID
    expected_d1_handoff_sha256: str = D1_RUNTIME_HANDOFF_SHA256
    release_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_parent(self) -> "D2E2Activation":
        if (
            self.expected_d1_handoff_id != D1_RUNTIME_HANDOFF_ID
            or self.expected_d1_handoff_sha256 != D1_RUNTIME_HANDOFF_SHA256
        ):
            raise ValueError("D2 E2 activation must bind the accepted D1 runtime handoff")
        return self


@dataclass(frozen=True)
class D2E2RunnerResource:
    repository_root: Path
    connection: Connection[Any]
    raw_store: RawObjectStore
    activation: D2E2Activation

    def run(self) -> MvpMediumValidationHandoff:
        handoff = run_d2_e2(
            self.repository_root,
            self.connection,
            self.raw_store,
            environment=self.activation.environment,
        )
        if (
            handoff.d1_runtime_handoff_id != self.activation.expected_d1_handoff_id
            or handoff.d1_runtime_handoff_sha256 != self.activation.expected_d1_handoff_sha256
        ):
            raise ValueError("D2 E2 materialization escaped its activated D1 parent")
        return handoff


@dg.asset(
    name=D2_E2_ASSET_NAME,
    group_name="mvp_medium_validation_e2",
    required_resource_keys={"mvp_medium_validation_e2_runner"},
    description="Publish the shared D2 E2 Local/CI medium-domain handoff.",
)
def materialize_mvp_medium_validation_e2(
    context: AssetExecutionContext,
) -> dg.Output[MvpMediumValidationHandoff]:
    runner = cast(D2E2RunnerResource, context.resources.mvp_medium_validation_e2_runner)
    handoff = runner.run()
    return dg.Output(
        handoff,
        metadata={
            "handoff_id": handoff.handoff_id,
            "schema_epoch": handoff.schema_epoch,
            "snapshot_count": len(handoff.snapshot_bundle.snapshots),
            "normalized_record_count": len(handoff.normalized_record_ids),
            "environment": runner.activation.environment,
            "stable_handoff": handoff.stable_handoff,
        },
        data_version=dg.DataVersion(handoff.content_sha256),
    )


def build_d2_e2_definitions(
    *,
    repository_root: Path,
    connection: Connection[Any],
    raw_store: RawObjectStore,
    activation: D2E2Activation | ReleaseManifest,
) -> dg.Definitions:
    if not isinstance(activation, D2E2Activation):
        raise ValueError("D2 E2 is restricted to explicit Local/CI activation")
    return dg.Definitions(
        assets=[materialize_mvp_medium_validation_e2],
        resources={
            "mvp_medium_validation_e2_runner": cast(
                Any,
                D2E2RunnerResource(
                    repository_root=repository_root,
                    connection=connection,
                    raw_store=raw_store,
                    activation=activation,
                ),
            )
        },
    )


__all__ = [
    "D1_GOVERNANCE_HANDOFF_ID",
    "D1_GOVERNANCE_HANDOFF_SHA256",
    "D1_RUNTIME_HANDOFF_ID",
    "D1_RUNTIME_HANDOFF_SHA256",
    "D2_E2_ASSET_NAME",
    "D2_E2_SCHEMA_EPOCH",
    "D2E2Activation",
    "D2E2RunnerResource",
    "FrozenMediumCaptureConfiguration",
    "MediumMarketEventBundle",
    "MvpMediumValidationHandoff",
    "build_d2_e2_definitions",
    "materialize_mvp_medium_validation_e2",
    "run_d2_e2",
]
