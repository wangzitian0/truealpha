"""Bind capture runs to release manifests and advance pointers (#378, #536)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import psycopg
from truealpha_contracts.common import CaptureEnvironment
from truealpha_runtime.reconciliation import ReconciliationOutcome

from data_engine.contracts.evidence_graph import (
    BitemporalStamp,
    CurrentPointer,
    CurrentPointerKey,
    EvidenceEdge,
    EvidenceNode,
    EvidenceNodeKind,
    EvidenceNodeRef,
    EvidenceRelation,
)
from data_engine.datahub.evidence_graph_repository import PostgresEvidenceGraphRepository

POINTER_FACTOR_ID = "gross_profit_per_employee"


@dataclass(frozen=True)
class ServiceObjectives:
    minimum_coverage: Decimal
    minimum_availability: Decimal
    minimum_independent_origin_groups: int
    minimum_corroborated_share: Decimal


ACCEPTED_SERVICE_OBJECTIVES = ServiceObjectives(
    minimum_coverage=Decimal("1"),
    minimum_availability=Decimal("0.95"),
    minimum_independent_origin_groups=2,
    minimum_corroborated_share=Decimal("0.95"),
)


@dataclass(frozen=True)
class UnmetObjective:
    objective: str
    required: str
    observed: str

    def __str__(self) -> str:
        return f"{self.objective}: required >= {self.required}, observed {self.observed}"


@dataclass(frozen=True)
class PointerRegistration:
    run_id: str
    sequence: int | None
    unmet: tuple[UnmetObjective, ...]
    advanced: bool = True

    @property
    def accepted(self) -> bool:
        return (not self.unmet) if not self.advanced else (self.sequence is not None)

    @property
    def is_degraded(self) -> bool:
        return bool(self.unmet)

    @property
    def summary(self) -> str:
        return "; ".join(str(item) for item in self.unmet) if self.unmet else "every objective met"


def _ratio(value: Any) -> Decimal:
    if value is None:
        return Decimal(0)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def _cell_origin_groups(cell: Any) -> int:
    if not isinstance(cell, Mapping) or cell.get("outcome") != ReconciliationOutcome.AGREED.value:
        return 0
    try:
        return int(cell.get("origin_groups", 0))
    except (TypeError, ValueError):
        return 0


def _corroborated_share(cells: Any, *, minimum_origin_groups: int) -> Decimal:
    if not isinstance(cells, Mapping) or not cells:
        return Decimal(0)
    minimum = max(minimum_origin_groups, 1)
    return Decimal(sum(1 for cell in cells.values() if _cell_origin_groups(cell) >= minimum)) / Decimal(len(cells))


def unmet_objectives(
    report: Mapping[str, Any], objectives: ServiceObjectives = ACCEPTED_SERVICE_OBJECTIVES
) -> tuple[UnmetObjective, ...]:
    measured = {
        "denominator_coverage": (_ratio(report.get("terminal_coverage")), objectives.minimum_coverage),
        "availability": (_ratio(report.get("availability")), objectives.minimum_availability),
        "corroborated_share": (
            _corroborated_share(
                report.get("reconciliation_cells"), minimum_origin_groups=objectives.minimum_independent_origin_groups
            ),
            objectives.minimum_corroborated_share,
        ),
    }
    return tuple(
        UnmetObjective(name, format(required.normalize(), "f"), format(observed.normalize(), "f"))
        for name, (observed, required) in measured.items()
        if observed < required
    )


def register_run_evidence(
    connection: psycopg.Connection[Any],
    *,
    run_id: str,
    release_manifest_id: str,
    quality_report: Mapping[str, Any],
    objectives: ServiceObjectives = ACCEPTED_SERVICE_OBJECTIVES,
) -> PointerRegistration:
    status = connection.execute(
        "select universe_id, universe_version, cutoff, environment from mart.topt_capture_status where run_id = %s",
        (run_id,),
    ).fetchone()
    if status is None:
        raise ValueError(f"no capture status for run {run_id}")
    universe_id, universe_version, cutoff, raw_env = status
    stamp = BitemporalStamp(valid_from=cutoff.date(), transaction_time=cutoff, recorded_at=cutoff)
    run_ref = EvidenceNodeRef(kind=EvidenceNodeKind.CAPTURE_RUN, node_id=run_id)
    manifest_ref = EvidenceNodeRef(kind=EvidenceNodeKind.RELEASE_MANIFEST, node_id=release_manifest_id)
    repo = PostgresEvidenceGraphRepository(connection)
    repo.append(
        [EvidenceNode(ref=manifest_ref, content_sha256=manifest_ref.content_sha256, stamp=stamp)],
        [EvidenceEdge(from_ref=run_ref, to_ref=manifest_ref, relation=EvidenceRelation.BOUND_TO, stamp=stamp)],
    )
    key = CurrentPointerKey(
        environment=CaptureEnvironment(raw_env),
        universe_id=universe_id,
        universe_version=universe_version,
        factor_id=POINTER_FACTOR_ID,
    )
    head = repo.head(key)
    if head is not None and head.target_run.node_id == run_id:
        return PointerRegistration(run_id=run_id, sequence=head.sequence, unmet=(), advanced=False)
    pointer = CurrentPointer(
        key=key,
        target_run=run_ref,
        sequence=0 if head is None else head.sequence + 1,
        previous_run=None if head is None else head.target_run,
        advanced_at=cutoff,
    )
    return PointerRegistration(
        run_id=run_id,
        sequence=repo.advance(pointer).sequence,
        unmet=unmet_objectives(quality_report, objectives),
        advanced=True,
    )
