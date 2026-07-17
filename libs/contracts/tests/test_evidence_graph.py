from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError
from truealpha_contracts import (
    BitemporalStamp,
    CaptureEnvironment,
    CurrentPointer,
    CurrentPointerKey,
    EvidenceEdge,
    EvidenceGraphReader,
    EvidenceGraphWriter,
    EvidenceNode,
    EvidenceNodeKind,
    EvidenceNodeRef,
    EvidenceRelation,
    ProvenanceClosure,
)

_H1 = "a" * 64
_H2 = "b" * 64
_H3 = "c" * 64
_STAMP = BitemporalStamp(
    valid_from=date(2026, 3, 31),
    transaction_time=datetime(2026, 4, 1, tzinfo=UTC),
    recorded_at=datetime(2026, 4, 1, 12, tzinfo=UTC),
)


def _ref(kind: EvidenceNodeKind, digest: str) -> EvidenceNodeRef:
    prefix = {
        EvidenceNodeKind.RAW_FETCH: "raw-fetch",
        EvidenceNodeKind.NORMALIZED_OBSERVATION: "normalized-observation",
        EvidenceNodeKind.CAPTURE_RUN: "capture-run",
        EvidenceNodeKind.MATERIALIZED_RESULT: "materialized-result",
    }[kind]
    return EvidenceNodeRef(kind=kind, node_id=f"{prefix}:{digest}")


def _node(kind: EvidenceNodeKind, digest: str, supersedes=None) -> EvidenceNode:
    return EvidenceNode(ref=_ref(kind, digest), content_sha256=digest, stamp=_STAMP, supersedes=supersedes)


def test_node_ref_rejects_wrong_prefix() -> None:
    with pytest.raises(ValidationError):
        EvidenceNodeRef(kind=EvidenceNodeKind.CAPTURE_RUN, node_id=f"snapshot:{_H1}")


def test_node_integrity_column_must_match_id() -> None:
    with pytest.raises(ValidationError):
        EvidenceNode(ref=_ref(EvidenceNodeKind.CAPTURE_RUN, _H1), content_sha256=_H2, stamp=_STAMP)


def test_edge_is_content_identified_and_deterministic() -> None:
    raw = _ref(EvidenceNodeKind.RAW_FETCH, _H1)
    obs = _ref(EvidenceNodeKind.NORMALIZED_OBSERVATION, _H2)
    edge = EvidenceEdge(from_ref=obs, to_ref=raw, relation=EvidenceRelation.DERIVED_FROM, stamp=_STAMP)
    again = EvidenceEdge(from_ref=obs, to_ref=raw, relation=EvidenceRelation.DERIVED_FROM, stamp=_STAMP)
    assert edge.edge_id.startswith("evidence-edge:")
    assert edge.edge_id == again.edge_id
    with pytest.raises(ValidationError):
        EvidenceEdge(from_ref=raw, to_ref=raw, relation=EvidenceRelation.DERIVED_FROM, stamp=_STAMP)


def test_supersedes_requires_same_kind() -> None:
    restated = _node(
        EvidenceNodeKind.NORMALIZED_OBSERVATION, _H2, supersedes=_ref(EvidenceNodeKind.NORMALIZED_OBSERVATION, _H1)
    )
    assert restated.supersedes is not None
    with pytest.raises(ValidationError):
        _node(EvidenceNodeKind.NORMALIZED_OBSERVATION, _H2, supersedes=_ref(EvidenceNodeKind.CAPTURE_RUN, _H1))


def test_pointer_resolves_to_a_run_and_advances_forward() -> None:
    key = CurrentPointerKey(
        environment=CaptureEnvironment.STAGING,
        universe_id="universe:topt-us-2026-03-31",
        universe_version="v1",
        factor_id="gross_profit_per_employee",
    )
    run1 = _ref(EvidenceNodeKind.CAPTURE_RUN, _H1)
    run2 = _ref(EvidenceNodeKind.CAPTURE_RUN, _H2)
    head = CurrentPointer(key=key, target_run=run1, sequence=0, advanced_at=datetime(2026, 4, 1, tzinfo=UTC))
    assert head.pointer_id.startswith("current-pointer:")
    advanced = CurrentPointer(
        key=key, target_run=run2, sequence=1, previous_run=run1, advanced_at=datetime(2026, 4, 2, tzinfo=UTC)
    )
    assert advanced.target_run == run2
    # A non-run target is rejected.
    with pytest.raises(ValidationError):
        CurrentPointer(
            key=key,
            target_run=_ref(EvidenceNodeKind.MATERIALIZED_RESULT, _H3),
            sequence=0,
            advanced_at=datetime(2026, 4, 1, tzinfo=UTC),
        )
    # A later advance must name the previous run it supersedes.
    with pytest.raises(ValidationError):
        CurrentPointer(key=key, target_run=run2, sequence=2, advanced_at=datetime(2026, 4, 3, tzinfo=UTC))


def test_pointer_key_rejects_mutable_version_tokens() -> None:
    with pytest.raises(ValidationError):
        CurrentPointerKey(
            environment=CaptureEnvironment.STAGING,
            universe_id="universe:topt",
            universe_version="latest",
            factor_id="gross_profit_per_employee",
        )


def test_closure_requires_root_and_connected_edges() -> None:
    raw = _node(EvidenceNodeKind.RAW_FETCH, _H1)
    obs = _node(EvidenceNodeKind.NORMALIZED_OBSERVATION, _H2)
    edge = EvidenceEdge(from_ref=obs.ref, to_ref=raw.ref, relation=EvidenceRelation.DERIVED_FROM, stamp=_STAMP)
    closure = ProvenanceClosure(root=obs.ref, reverse=False, nodes=(obs, raw), edges=(edge,))
    assert closure.root == obs.ref
    # Root absent from nodes is rejected.
    with pytest.raises(ValidationError):
        ProvenanceClosure(root=_ref(EvidenceNodeKind.CAPTURE_RUN, _H3), nodes=(obs, raw), edges=())
    # An edge to a node outside the closure is rejected.
    with pytest.raises(ValidationError):
        ProvenanceClosure(root=obs.ref, nodes=(obs,), edges=(edge,))


def test_ports_are_runtime_checkable_protocols() -> None:
    class _Reader:
        def resolve_pointer(self, key):
            return None

        def closure(self, root, *, reverse=False, max_nodes=1000): ...

    class _Writer:
        def append(self, nodes, edges): ...

    assert isinstance(_Reader(), EvidenceGraphReader)
    assert isinstance(_Writer(), EvidenceGraphWriter)
