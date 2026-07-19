"""Register a live capture run on the A1 evidence plane and advance the governed
pointer (#378 / #429 P2).

Called by the deployed pipeline after the quality report persists: appends the
run + release-manifest evidence nodes (idempotent), then advances
``mart.current_pointer`` so consumers resolve the head through
``mart.current_pointer_head`` (init.md rule 26) instead of an ORDER BY.
"""

from __future__ import annotations

from typing import Any

import psycopg
from truealpha_contracts import CaptureEnvironment
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

from data_engine.datahub.evidence_graph_repository import PostgresEvidenceGraphRepository

POINTER_FACTOR_ID = "gross_profit_per_employee"


def register_run_evidence(connection: psycopg.Connection[Any], *, run_id: str, release_manifest_id: str) -> int:
    """Append the run's evidence nodes and advance the pointer. Returns the pointer
    sequence now heading the governed read. Idempotent per run: appends dedupe on
    node identity, and an already-heading run advances nothing."""
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
    run_node = EvidenceNode(ref=run_ref, content_sha256=run_ref.content_sha256, stamp=stamp)
    manifest_node = EvidenceNode(ref=manifest_ref, content_sha256=manifest_ref.content_sha256, stamp=stamp)
    edge = EvidenceEdge(from_ref=run_ref, to_ref=manifest_ref, relation=EvidenceRelation.BOUND_TO, stamp=stamp)

    repo = PostgresEvidenceGraphRepository(connection)
    repo.append([run_node, manifest_node], [edge])

    key = CurrentPointerKey(
        environment=CaptureEnvironment.PRODUCTION,
        universe_id=universe_id,
        universe_version=universe_version,
        factor_id=POINTER_FACTOR_ID,
    )
    head = repo.head(key)
    if head is not None and head.target_run.node_id == run_id:
        return head.sequence  # retried tick: this run already heads the pointer
    pointer = CurrentPointer(
        key=key,
        target_run=run_ref,
        sequence=0 if head is None else head.sequence + 1,
        previous_run=None if head is None else head.target_run,
        advanced_at=cutoff,
    )
    return repo.advance(pointer).sequence
