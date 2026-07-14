"""Terminal E3 validation of the D2 data plane on the exact TOPT denominator."""

import hashlib
import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast

import dagster as dg
from dagster import AssetExecutionContext
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
    RawObjectStore,
    SecurityKind,
    UniverseDefinitionKind,
    UniverseManifest,
    UniverseMembership,
)
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.execution import NormalizedRecordRef, SnapshotDemandCell, SnapshotManifest, SnapshotRequest
from truealpha_contracts.registries import RegistrySnapshot
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
TOPT_REQUIRED_CELL_COUNT = 42
TOPT_NORMALIZED_RECORD_COUNT = 84


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
    *,
    knowable_at: datetime,
    recorded_at: datetime,
    raw_reference: str,
) -> tuple[tuple[IssuerSecurityLink, ...], tuple[UniverseMembership, ...]]:
    links: list[IssuerSecurityLink] = []
    memberships: list[UniverseMembership] = []
    for instrument in denominator.instruments:
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
    return tuple(links), tuple(memberships)


def _e3_catalog(registry: RegistrySnapshot, denominator: FrozenToptDenominator) -> MediumComponentCatalog:
    sources = {(entry.source_id, entry.version): entry for entry in registry.sources}
    semantic_types = {(entry.semantic_type_id, entry.version): entry for entry in registry.semantic_types}

    adapters = tuple(
        MediumAdapterRegistration(
            source_id=source_id,
            source_version=MEDIUM_VERSION,
            adapter_id=sources[(source_id, MEDIUM_VERSION)].adapter_id,
            adapter_version=sources[(source_id, MEDIUM_VERSION)].adapter_version,
            adapter_implementation_sha256=sources[(source_id, MEDIUM_VERSION)].adapter_implementation_sha256,
            configuration_type=FrozenMediumCaptureConfiguration,
            raw_source=DataSource.SEC,
            capture=_capture,
        )
        for source_id in (IDENTITY_SOURCE_ID, MEMBERSHIP_SOURCE_ID)
    )

    def issuer_links(capture: LandedMediumCapture):
        links, _memberships = _payloads(
            denominator,
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

    def unsupported_listing(_capture: LandedMediumCapture):
        raise ValueError("TOPT candidate context does not establish exchange listing semantics")

    def memberships(capture: LandedMediumCapture):
        _links, values = _payloads(
            denominator,
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

    routes = {
        (IDENTITY_SOURCE_ID, ISSUER_SECURITY_TYPE_ID): issuer_links,
        (IDENTITY_SOURCE_ID, SECURITY_LISTING_TYPE_ID): unsupported_listing,
        (MEMBERSHIP_SOURCE_ID, UNIVERSE_MEMBERSHIP_TYPE_ID): memberships,
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

    def select_universe(payload: BaseModel, partition_key: str) -> bool:
        membership = cast(UniverseMembership, payload)
        return partition_key == "all" or partition_key == f"universe:{membership.universe_id}"

    return tuple(
        replace(registration, partition_filter=select_security)
        if registration.semantic_type_id == ISSUER_SECURITY_TYPE_ID
        else replace(registration, partition_filter=select_universe)
        if registration.semantic_type_id == UNIVERSE_MEMBERSHIP_TYPE_ID
        else registration
        for registration in build_medium_repository_registrations(registry)
    )


def _work_items(
    *,
    body: bytes,
    vintage: Literal["original", "changed"],
    knowable_at: datetime,
    recorded_at: datetime,
) -> tuple[MediumCaptureWorkItem, MediumCaptureWorkItem]:
    body_sha256 = _sha256(body)

    def configuration(source_id: str) -> FrozenMediumCaptureConfiguration:
        return FrozenMediumCaptureConfiguration(
            artifact_id=f"topt-candidate-denominator-{vintage}-{source_id}",
            source=DataSource.SEC,
            source_record_id=f"d2-e3:{TOPT_ACCESSION}:{vintage}:{source_id}",
            body=body,
            content_type="application/json",
            fetched_at=knowable_at,
            source_published_at=knowable_at,
            metadata={
                "accession": TOPT_ACCESSION,
                "artifact_sha256": body_sha256,
                "candidate_state": "candidate_unapproved",
                "identity_context_only": True,
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
            configuration=configuration(IDENTITY_SOURCE_ID),
            recorded_at=recorded_at,
        ),
        MediumCaptureWorkItem(
            source_id=MEMBERSHIP_SOURCE_ID,
            source_version=MEDIUM_VERSION,
            semantic_type_ids=(UNIVERSE_MEMBERSHIP_TYPE_ID,),
            semantic_type_version=MEDIUM_VERSION,
            configuration=configuration(MEMBERSHIP_SOURCE_ID),
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
    for instrument in denominator.instruments:
        identity_record = records_by_document[instrument.identity_document_id]
        membership_record = records_by_document[instrument.membership_id]
        for record, partition, label in (
            (
                identity_record,
                f"security:{instrument.security_id}",
                f"topt:{instrument.cusip}:issuer-security",
            ),
            (
                membership_record,
                f"universe:{denominator.universe_id}",
                f"topt:{instrument.cusip}:membership",
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
        artifact = corpus.artifacts["topt-candidate-denominator"]
        original_body = artifact.body
        changed_body = original_body + b"\n"
        original_at = e2_handoff.created_at + timedelta(seconds=1)
        changed_at = original_at + timedelta(days=1)
        registry = e2_handoff.registry_snapshot
        repository = PostgresMediumSemanticRepository(
            connection,
            registry=registry,
            registrations=_e3_repository_registrations(registry),
        )
        catalog = _e3_catalog(registry, denominator)

        original_batch = land_medium_capture_plan(
            connection,
            object_store=raw_store,
            catalog=catalog,
            work_items=_work_items(
                body=original_body,
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
                body=changed_body,
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
            registry=registry,
            records_by_document=original_by_document,
            universe_manifest=universe,
            as_of=original_at,
        )
        pre_knowable_request, _pre_fixture, _pre_documents = _snapshot_plan(
            denominator=denominator,
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
    "ToptInstrument",
    "build_d2_e3_definitions",
    "load_topt_denominator",
    "materialize_mvp_medium_validation_e3",
    "run_d2_e3",
]
