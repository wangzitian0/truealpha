"""Cutoff-specific snapshot bundles for the D2 shared medium-domain path."""

from __future__ import annotations

from datetime import datetime
from typing import cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.data_quality import DataDomain
from truealpha_contracts.execution import (
    NormalizedRecordRef,
    SnapshotCellSelection,
    SnapshotManifest,
    SnapshotRequest,
)
from truealpha_contracts.registries import RegistrySnapshot
from truealpha_contracts.universe import UniverseManifest, UniverseMembership
from truealpha_contracts.usage import RequirementLevel

from data_engine.contract_repository import PostgresSnapshotRepository
from data_engine.mvp_medium_registry import UNIVERSE_MEMBERSHIP_TYPE_ID
from data_engine.mvp_medium_repository import PostgresMediumSemanticRepository

RESOLVER_ID = "resolver.mvp-medium-postgres"
RESOLVER_VERSION = "1.0.0"
RESOLVER_IMPLEMENTATION_SHA256 = canonical_sha256({"component": RESOLVER_ID, "version": RESOLVER_VERSION})


class MediumSnapshotBundle(BaseModel):
    """Ordered PIT manifests whose distinct valid dates cannot be collapsed safely."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshot_bundle_id: str = Field(default="", pattern=r"^(?:|mvp-medium-snapshot-bundle:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    snapshots: tuple[SnapshotManifest, ...] = Field(min_length=4)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def identify(self) -> MediumSnapshotBundle:
        snapshots = tuple(
            sorted(
                self.snapshots,
                key=lambda item: (item.request.valid_on, item.request.as_of, item.snapshot_id),
            )
        )
        if len({snapshot.snapshot_id for snapshot in snapshots}) != len(snapshots):
            raise ValueError("medium snapshot bundle contains duplicate snapshots")
        if len({snapshot.request.valid_on for snapshot in snapshots}) < 4:
            raise ValueError("medium snapshot bundle must retain its distinct domain dates")
        if len({snapshot.registry_snapshot.registry_snapshot_id for snapshot in snapshots}) != 1:
            raise ValueError("medium snapshot bundle must use one exact registry snapshot")
        if any(
            record.draft.semantic_type_id == "semantic.corporate-action"
            for snapshot in snapshots
            for record in snapshot.normalized_records
        ):
            raise ValueError("corporate actions belong to the monotonic market-event bundle")
        if self.created_at < max(snapshot.resolved_at for snapshot in snapshots):
            raise ValueError("snapshot bundle cannot predate a member snapshot")
        object.__setattr__(self, "snapshots", snapshots)
        payload = self.model_dump(
            mode="json",
            exclude={"snapshot_bundle_id", "content_sha256"},
        )
        expected_hash = canonical_sha256(payload)
        expected_id = f"mvp-medium-snapshot-bundle:{expected_hash}"
        if self.content_sha256 and self.content_sha256 != expected_hash:
            raise ValueError("snapshot bundle content_sha256 mismatch")
        if self.snapshot_bundle_id and self.snapshot_bundle_id != expected_id:
            raise ValueError("snapshot bundle ID mismatch")
        object.__setattr__(self, "content_sha256", expected_hash)
        object.__setattr__(self, "snapshot_bundle_id", expected_id)
        return self


def build_medium_snapshot(
    request: SnapshotRequest,
    *,
    registry: RegistrySnapshot,
    selected_records: dict[str, tuple[NormalizedRecordRef, ...]],
    resolved_at: datetime,
    universe_manifest: UniverseManifest | None = None,
    universe_memberships: tuple[UniverseMembership, ...] = (),
) -> SnapshotManifest:
    """Build a canonical manifest from already selected fixture or Postgres rows."""

    records: dict[str, NormalizedRecordRef] = {}
    selections: list[SnapshotCellSelection] = []
    for demand in request.demand_cells:
        selected = selected_records.get(demand.planned_cell_id, ())
        if demand.level is RequirementLevel.REQUIRED and not selected:
            raise ValueError(f"required medium demand is empty: {demand.planned_cell_id}")
        for record in selected:
            records[record.normalized_record_id] = record
        selections.append(
            SnapshotCellSelection(
                demand=demand,
                normalized_record_ids=tuple(record.normalized_record_id for record in selected),
                reason_codes=() if selected else ("no_visible_record",),
            )
        )
    resolved_subjects = (
        request.subjects
        if request.universe is None
        else tuple(
            sorted(
                {membership.subject for membership in universe_memberships},
                key=lambda subject: (subject.kind.value, subject.id),
            )
        )
    )
    return SnapshotManifest(
        request=request,
        registry_snapshot=registry,
        resolved_subjects=resolved_subjects,
        universe_manifest=universe_manifest,
        universe_memberships=universe_memberships,
        normalized_records=tuple(records.values()),
        selections=tuple(selections),
        resolved_at=resolved_at,
        resolver_id=RESOLVER_ID,
        resolver_version=RESOLVER_VERSION,
        resolver_implementation_sha256=RESOLVER_IMPLEMENTATION_SHA256,
    )


class PostgresMediumSnapshotResolver:
    """Resolve one row-complete snapshot from a single Postgres transaction view."""

    def __init__(
        self,
        *,
        semantic_records: PostgresMediumSemanticRepository,
        snapshots: PostgresSnapshotRepository,
    ) -> None:
        self.semantic_records = semantic_records
        self.snapshots = snapshots

    def resolve(
        self,
        request: SnapshotRequest,
        *,
        registry: RegistrySnapshot,
        resolved_at: datetime,
        universe_manifest: UniverseManifest | None = None,
    ) -> SnapshotManifest:
        if (
            request.registry_snapshot_id != registry.registry_snapshot_id
            or request.registry_snapshot_sha256 != registry.content_sha256
            or request.source_registry_id != registry.source_registry_snapshot_id
            or request.source_registry_sha256 != registry.source_registry_sha256
            or request.semantic_type_registry_id != registry.semantic_type_registry_snapshot_id
            or request.semantic_type_registry_sha256 != registry.semantic_type_registry_sha256
        ):
            raise ValueError("medium snapshot request does not bind the registry exactly")
        if resolved_at < request.as_of:
            raise ValueError("medium snapshot cannot resolve before its cutoff")

        memberships: tuple[UniverseMembership, ...] = ()
        if request.universe is None:
            if universe_manifest is not None:
                raise ValueError("explicit-subject snapshot cannot accept a universe manifest")
        else:
            if universe_manifest is None or universe_manifest.ref != request.universe:
                raise ValueError("universe snapshot requires the exact requested manifest")
            visible = self.semantic_records.all_visible_records(
                semantic_type_id=UNIVERSE_MEMBERSHIP_TYPE_ID,
                semantic_type_version="1.0.0",
                as_of=request.as_of,
                valid_on=request.valid_on,
            )
            memberships = tuple(
                sorted(
                    (
                        cast(UniverseMembership, item.payload)
                        for item in visible
                        if cast(UniverseMembership, item.payload).universe_id == request.universe.universe_id
                    ),
                    key=lambda item: item.membership_id,
                )
            )
            if tuple(membership.membership_id for membership in memberships) != universe_manifest.membership_ids:
                raise ValueError("PIT membership rows do not match the fixed universe manifest")

        selected_records = {
            demand.planned_cell_id: tuple(
                item.record
                for item in self.semantic_records.visible_records(
                    demand,
                    as_of=request.as_of,
                    valid_on=request.valid_on,
                )
            )
            for demand in request.demand_cells
        }
        snapshot = build_medium_snapshot(
            request,
            registry=registry,
            selected_records=selected_records,
            resolved_at=resolved_at,
            universe_manifest=universe_manifest,
            universe_memberships=memberships,
        )
        self.snapshots.put(snapshot)
        return snapshot


def snapshot_domains(snapshot: SnapshotManifest) -> tuple[DataDomain, ...]:
    """Return the distinct registry domains selected into one manifest."""

    domain_by_type = {entry.semantic_type_id: entry.domain for entry in snapshot.registry_snapshot.semantic_types}
    return tuple(
        sorted(
            {domain_by_type[record.draft.semantic_type_id] for record in snapshot.normalized_records},
            key=lambda domain: domain.value,
        )
    )


__all__ = [
    "MediumSnapshotBundle",
    "PostgresMediumSnapshotResolver",
    "RESOLVER_ID",
    "RESOLVER_IMPLEMENTATION_SHA256",
    "RESOLVER_VERSION",
    "build_medium_snapshot",
    "snapshot_domains",
]
