"""Bind a captured run to its release manifest and advance the governed pointer
*only when the run's quality report meets the accepted service objectives*
(#378 / #429 P2 / #536).

Called by the deployed pipeline after the quality report persists. The capture executor
already appended this run's own evidence — the run node plus every raw-fetch and
normalized-observation node it produced — so this step adds only what capture cannot
know: the release-manifest node and the `bound_to` edge, then advances
``mart.current_pointer`` so consumers resolve the head through
``mart.current_pointer_head`` (init.md rule 26) instead of an ORDER BY.

#536: the advance used to be guarded by retry idempotency alone. The report was built
and persisted two statements earlier in the same transaction and its numbers were never
read, so a run that corroborated nothing still became the head App, MCP and `/chat`
serve — three consecutive Production heads did exactly that. The advance is now gated on
``ACCEPTED_SERVICE_OBJECTIVES``, the thresholds `docs/datahub-service-demand.md` already
declares.

A refused advance is a first-class outcome, not an exception. The run, its report and its
evidence nodes still persist — that is the record of the bad day, and discarding it would
destroy what explains the stall. Only the governed head stays put, so consumers keep
serving the last accepted run and the stuck head becomes the visible signal
(`topt-gppe-repository.ts` already falls back, and the funnel view reports pointer age).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import psycopg
from truealpha_contracts.common import CaptureEnvironment
from truealpha_contracts.evidence_graph import (
    BitemporalStamp,
    CurrentPointer,
    CurrentPointerKey,
    EvidenceEdge,
    EvidenceNode,
    EvidenceNodeKind,
    EvidenceNodeRef,
    EvidenceRelation,
)
from truealpha_contracts.reconciliation import ReconciliationOutcome

from data_engine.datahub.evidence_graph_repository import PostgresEvidenceGraphRepository

POINTER_FACTOR_ID = "gross_profit_per_employee"


@dataclass(frozen=True)
class ServiceObjectives:
    """The quality objectives a run must meet before it may head the governed pointer.

    These mirror `truealpha_contracts.service_demand.DataQualityObjective` threshold for
    threshold; they are held here as plain numbers because the deployed path must not
    depend on a checked-in demand fixture (fixture data lives in tests only, #429 I2) and
    because the demand's confidence *policy* identity is #207's to pin, not this gate's
    to invent.
    """

    minimum_coverage: Decimal
    minimum_availability: Decimal
    # On the 0-100 presentation scale that `ConfidenceEvaluation.score_100` defines
    # (confidence x 100). Report ratios themselves persist on the 0-1 scale.
    minimum_confidence_score: Decimal
    minimum_independent_origin_groups: int
    # Share of graded reconciliation cells that must reach the `high` band (agreed
    # with at least `minimum_independent_origin_groups` origin groups) for the
    # pointer to advance. The per-cell band requirement stays absolute; what this
    # replaces is weakest-cell gating, where one symbol's abstain froze every other
    # symbol's fresher, fully corroborated data behind the previous head (#623).
    minimum_corroborated_share: Decimal


# The accepted demand, `docs/datahub-service-demand.md` ("Its service objective is"):
# 100% denominator coverage and at least 95% availability; continuous confidence of at
# least 70 on the 0-100 presentation scale; the `high` target band, which requires at
# least two canonical original-source groups, mirrors and resellers of one origin not
# counting. Every number here is quoted from that document; none is invented here.
ACCEPTED_SERVICE_OBJECTIVES = ServiceObjectives(
    minimum_coverage=Decimal("1"),
    minimum_availability=Decimal("0.95"),
    minimum_confidence_score=Decimal("70"),
    minimum_independent_origin_groups=2,
    minimum_corroborated_share=Decimal("0.95"),
)


@dataclass(frozen=True)
class UnmetObjective:
    """One declared objective the run missed, carrying the number that missed it."""

    objective: str
    required: str
    observed: str

    def __str__(self) -> str:
        return f"{self.objective}: required >= {self.required}, observed {self.observed}"


@dataclass(frozen=True)
class PointerRegistration:
    """Outcome of one registration.

    `sequence` is the governed head's sequence *after* this call — the newly advanced one
    when the run was accepted, the untouched incumbent's when it was withheld, and `None`
    when the pointer has no head at all yet. `unmet` is empty exactly when this run heads
    the pointer.
    """

    run_id: str
    sequence: int | None
    unmet: tuple[UnmetObjective, ...]

    @property
    def accepted(self) -> bool:
        """True when this run heads the governed pointer after this call."""
        return not self.unmet

    @property
    def summary(self) -> str:
        return "; ".join(str(item) for item in self.unmet) if self.unmet else "every objective met"


def unmet_objectives(
    report: Mapping[str, Any], objectives: ServiceObjectives = ACCEPTED_SERVICE_OBJECTIVES
) -> tuple[UnmetObjective, ...]:
    """Judge one quality report against the accepted objectives.

    Fails closed: a missing or unreadable figure counts as zero, so a malformed report
    withholds the pointer instead of advancing it by omission.
    """
    measured = {
        "denominator_coverage": (_ratio(report.get("terminal_coverage")), objectives.minimum_coverage),
        "availability": (_ratio(report.get("availability")), objectives.minimum_availability),
        # The report keeps the mean on the 0-1 ratio scale; the demand pins the threshold
        # on the 0-100 presentation scale.
        "continuous_confidence": (
            _ratio(report.get("denominator_mean_confidence")) * 100,
            objectives.minimum_confidence_score,
        ),
        "corroborated_share": (
            _corroborated_share(
                report.get("reconciliation_cells"),
                minimum_origin_groups=objectives.minimum_independent_origin_groups,
            ),
            objectives.minimum_corroborated_share,
        ),
    }
    return tuple(
        UnmetObjective(objective=name, required=_plain(required), observed=_plain(observed))
        for name, (observed, required) in measured.items()
        if observed < required
    )


def _ratio(value: Any) -> Decimal:
    """Report figures persist as strings. Anything unreadable is zero — fail closed."""
    if value is None:
        return Decimal(0)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def _plain(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _corroborated_share(cells: Any, *, minimum_origin_groups: int) -> Decimal:
    """Share of graded cells whose served value reaches the `high` band.

    A cell reaches the band only when independent origins agree — exactly
    `ReconciliationOutcome.AGREED` with at least the policy's minimum origin groups;
    an abstained cell has origins but no corroborated value, so it contributes
    nothing to the numerator (counting its disagreeing origins would be the raw
    origin count `quality_report` itself rules out). The DENOMINATOR is every graded
    cell: the objective judges the share, not the weakest cell, so one symbol's
    abstain no longer freezes every other symbol's corroborated data behind the
    previous head (#623 — weakest-cell gating held 20/21-agreed TOPT and the first
    QQQ head on single-cell misses). A report that graded no cells corroborates
    nothing — fail closed.
    """
    if not isinstance(cells, Mapping) or not cells:
        return Decimal(0)
    # Floor the bar at 1: `_cell_origin_groups` scores every non-AGREED cell 0, and a
    # caller with `minimum_origin_groups=0` (the single-origin corpus objectives) must
    # not turn that 0 into "corroborated" via `0 >= 0` (Copilot on #626).
    minimum = max(minimum_origin_groups, 1)
    corroborated = sum(1 for cell in cells.values() if _cell_origin_groups(cell) >= minimum)
    return Decimal(corroborated) / Decimal(len(cells))


def _cell_origin_groups(cell: Any) -> int:
    if not isinstance(cell, Mapping) or cell.get("outcome") != ReconciliationOutcome.AGREED.value:
        return 0
    try:
        return int(cell.get("origin_groups", 0))
    except (TypeError, ValueError):
        return 0


def register_run_evidence(
    connection: psycopg.Connection[Any],
    *,
    run_id: str,
    release_manifest_id: str,
    quality_report: Mapping[str, Any],
    objectives: ServiceObjectives = ACCEPTED_SERVICE_OBJECTIVES,
) -> PointerRegistration:
    """Bind the run to its release manifest, then advance the pointer only when the run's
    report meets `objectives`.

    Idempotent per run: appends dedupe on node identity, and an already-heading run
    advances nothing. The release-manifest node and its `bound_to` edge are appended
    whether or not the pointer advances — a withheld run is still a run that happened,
    and its evidence is what explains the stall.
    """
    status = connection.execute(
        "select universe_id, universe_version, cutoff from mart.topt_capture_status where run_id = %s",
        (run_id,),
    ).fetchone()
    if status is None:
        raise ValueError(f"no capture status for run {run_id}")
    universe_id, universe_version, cutoff = status

    stamp = BitemporalStamp(valid_from=cutoff.date(), transaction_time=cutoff, recorded_at=cutoff)
    run_ref = EvidenceNodeRef(kind=EvidenceNodeKind.CAPTURE_RUN, node_id=run_id)
    manifest_ref = EvidenceNodeRef(kind=EvidenceNodeKind.RELEASE_MANIFEST, node_id=release_manifest_id)
    # The run node is the executor's to append — it owns the capture that created it.
    # Re-appending it here would claim authorship of a node this step never produced.
    manifest_node = EvidenceNode(ref=manifest_ref, content_sha256=manifest_ref.content_sha256, stamp=stamp)
    edge = EvidenceEdge(from_ref=run_ref, to_ref=manifest_ref, relation=EvidenceRelation.BOUND_TO, stamp=stamp)

    repo = PostgresEvidenceGraphRepository(connection)
    repo.append([manifest_node], [edge])

    key = CurrentPointerKey(
        environment=CaptureEnvironment.PRODUCTION,
        universe_id=universe_id,
        universe_version=universe_version,
        factor_id=POINTER_FACTOR_ID,
    )
    head = repo.head(key)
    if head is not None and head.target_run.node_id == run_id:
        # Retried tick: this run already heads the pointer. The report is a function of
        # the same run, so re-judging it could only reach the same verdict.
        return PointerRegistration(run_id=run_id, sequence=head.sequence, unmet=())

    unmet = unmet_objectives(quality_report, objectives)
    if unmet:
        # Refusal, not an exception: the caller still commits the run, its report and its
        # evidence; only the head stays where it is.
        return PointerRegistration(run_id=run_id, sequence=None if head is None else head.sequence, unmet=unmet)

    pointer = CurrentPointer(
        key=key,
        target_run=run_ref,
        sequence=0 if head is None else head.sequence + 1,
        previous_run=None if head is None else head.target_run,
        advanced_at=cutoff,
    )
    return PointerRegistration(run_id=run_id, sequence=repo.advance(pointer).sequence, unmet=())
