"""Postgres (RDS) adapter for the ADR A1 evidence graph.

Implements the three backend-neutral ports from `truealpha_contracts.evidence_graph`:
append-only `EvidenceGraphWriter` (one call is one atomic unit-of-work), `EvidenceGraphReader`
(governed-pointer resolution + bounded provenance closure over indexed edge keys), and the
forward-only `CurrentPointerRegistry`. A future graph-database adapter implements the same
ports without touching factors or consumers.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from psycopg import Connection
from truealpha_contracts import (
    BitemporalStamp,
    CurrentPointer,
    CurrentPointerKey,
    EvidenceEdge,
    EvidenceNode,
    EvidenceNodeKind,
    EvidenceNodeRef,
    EvidenceRelation,
    ProvenanceClosure,
)


class PostgresEvidenceGraphRepository:
    """One repository implementing all three evidence-graph ports over Postgres."""

    def __init__(self, connection: Connection[Any]) -> None:
        self._connection = connection

    # -- EvidenceGraphWriter ---------------------------------------------------------------

    def append(self, nodes: Sequence[EvidenceNode], edges: Sequence[EvidenceEdge]) -> None:
        """Atomically append nodes then edges. Re-appending identical content is idempotent;
        the append-only triggers reject any update/delete."""
        with self._connection.transaction():
            for node in nodes:
                self._append_node(node)
            for edge in edges:
                self._append_edge(edge)

    def _append_node(self, node: EvidenceNode) -> None:
        self._connection.execute(
            """
            insert into staging.evidence_nodes (
                node_id, kind, content_sha256, valid_from, valid_to,
                transaction_time, recorded_at, supersedes_node_id
            ) values (%s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (node_id) do nothing
            """,
            (
                node.ref.node_id,
                node.ref.kind.value,
                node.content_sha256,
                node.stamp.valid_from,
                node.stamp.valid_to,
                node.stamp.transaction_time,
                node.stamp.recorded_at,
                node.supersedes.node_id if node.supersedes is not None else None,
            ),
        )

    def _append_edge(self, edge: EvidenceEdge) -> None:
        self._connection.execute(
            """
            insert into staging.evidence_edges (
                edge_id, content_sha256, from_kind, from_id, to_kind, to_id,
                relation, valid_from, valid_to, transaction_time, recorded_at
            ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (edge_id) do nothing
            """,
            (
                edge.edge_id,
                edge.content_sha256,
                edge.from_ref.kind.value,
                edge.from_ref.node_id,
                edge.to_ref.kind.value,
                edge.to_ref.node_id,
                edge.relation.value,
                edge.stamp.valid_from,
                edge.stamp.valid_to,
                edge.stamp.transaction_time,
                edge.stamp.recorded_at,
            ),
        )

    # -- EvidenceGraphReader ---------------------------------------------------------------

    def resolve_pointer(self, key: CurrentPointerKey) -> CurrentPointer | None:
        return self._head(key)

    def closure(self, root: EvidenceNodeRef, *, reverse: bool = False, max_nodes: int = 1000) -> ProvenanceClosure:
        if max_nodes < 1:
            raise ValueError("max_nodes must be positive")
        join = "e.to_id = w.node_id" if reverse else "e.from_id = w.node_id"
        select_next = "e.from_id" if reverse else "e.to_id"
        # Reachable node ids from the root over indexed edge keys, bounded to max_nodes + 1
        # so we can detect and report truncation.
        rows = self._connection.execute(
            f"""
            with recursive walk(node_id) as (
                select %s
                union
                select {select_next}
                from staging.evidence_edges e
                join walk w on {join}
            )
            select node_id from walk limit %s
            """,
            (root.node_id, max_nodes + 1),
        ).fetchall()
        node_ids = [row[0] for row in rows]
        truncated = len(node_ids) > max_nodes
        node_ids = node_ids[:max_nodes]
        if root.node_id not in node_ids:
            node_ids = [root.node_id, *node_ids][:max_nodes]

        nodes = tuple(self._load_node(node_id) for node_id in node_ids)
        edge_rows = self._connection.execute(
            """
            select edge_id, from_kind, from_id, to_kind, to_id, relation,
                   valid_from, valid_to, transaction_time, recorded_at
            from staging.evidence_edges
            where from_id = any(%s) and to_id = any(%s)
            """,
            (node_ids, node_ids),
        ).fetchall()
        edges = tuple(self._edge_from_row(row) for row in edge_rows)
        return ProvenanceClosure(root=root, reverse=reverse, nodes=nodes, edges=edges, truncated=truncated)

    def _load_node(self, node_id: str) -> EvidenceNode:
        row = self._connection.execute(
            """
            select node_id, kind, content_sha256, valid_from, valid_to,
                   transaction_time, recorded_at, supersedes_node_id
            from staging.evidence_nodes where node_id = %s
            """,
            (node_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"evidence node not found: {node_id}")
        kind = EvidenceNodeKind(row[1])
        supersedes = EvidenceNodeRef(kind=kind, node_id=row[7]) if row[7] is not None else None
        return EvidenceNode(
            ref=EvidenceNodeRef(kind=kind, node_id=row[0]),
            content_sha256=row[2],
            stamp=BitemporalStamp(
                valid_from=row[3],
                valid_to=row[4],
                transaction_time=row[5],
                recorded_at=row[6],
            ),
            supersedes=supersedes,
        )

    @staticmethod
    def _edge_from_row(row: tuple[Any, ...]) -> EvidenceEdge:
        return EvidenceEdge(
            from_ref=EvidenceNodeRef(kind=EvidenceNodeKind(row[1]), node_id=row[2]),
            to_ref=EvidenceNodeRef(kind=EvidenceNodeKind(row[3]), node_id=row[4]),
            relation=EvidenceRelation(row[5]),
            stamp=BitemporalStamp(
                valid_from=row[6],
                valid_to=row[7],
                transaction_time=row[8],
                recorded_at=row[9],
            ),
        )

    # -- CurrentPointerRegistry ------------------------------------------------------------

    def head(self, key: CurrentPointerKey) -> CurrentPointer | None:
        return self._head(key)

    def advance(self, pointer: CurrentPointer) -> CurrentPointer:
        """Record a forward advance. Rejects a non-increasing sequence or a pointer whose
        `previous_run` does not match the current head."""
        with self._connection.transaction():
            head_row = self._connection.execute(
                """
                select target_run_id, sequence from mart.current_pointer
                where environment = %s and universe_id = %s
                  and universe_version = %s and factor_id = %s
                order by sequence desc limit 1
                for update
                """,
                (
                    pointer.key.environment.value,
                    pointer.key.universe_id,
                    pointer.key.universe_version,
                    pointer.key.factor_id,
                ),
            ).fetchone()
            if head_row is None:
                if pointer.sequence != 0 or pointer.previous_run is not None:
                    raise ValueError("the first advance must be sequence 0 with no previous run")
            else:
                head_run, head_sequence = head_row
                if pointer.sequence != head_sequence + 1:
                    raise ValueError("a pointer advance must increment the head sequence by one")
                if pointer.previous_run is None or pointer.previous_run.node_id != head_run:
                    raise ValueError("a pointer advance must name the current head as previous_run")
            self._connection.execute(
                """
                insert into mart.current_pointer (
                    pointer_id, content_sha256, environment, universe_id, universe_version,
                    factor_id, target_run_id, sequence, previous_run_id, advanced_at
                ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    pointer.pointer_id,
                    pointer.content_sha256,
                    pointer.key.environment.value,
                    pointer.key.universe_id,
                    pointer.key.universe_version,
                    pointer.key.factor_id,
                    pointer.target_run.node_id,
                    pointer.sequence,
                    pointer.previous_run.node_id if pointer.previous_run is not None else None,
                    pointer.advanced_at,
                ),
            )
        return pointer

    def _head(self, key: CurrentPointerKey) -> CurrentPointer | None:
        row = self._connection.execute(
            """
            select target_run_id, sequence, previous_run_id, advanced_at
            from mart.current_pointer_head
            where environment = %s and universe_id = %s
              and universe_version = %s and factor_id = %s
            """,
            (key.environment.value, key.universe_id, key.universe_version, key.factor_id),
        ).fetchone()
        if row is None:
            return None
        previous = EvidenceNodeRef(kind=EvidenceNodeKind.CAPTURE_RUN, node_id=row[2]) if row[2] is not None else None
        return CurrentPointer(
            key=key,
            target_run=EvidenceNodeRef(kind=EvidenceNodeKind.CAPTURE_RUN, node_id=row[0]),
            sequence=row[1],
            previous_run=previous,
            advanced_at=row[3],
        )
