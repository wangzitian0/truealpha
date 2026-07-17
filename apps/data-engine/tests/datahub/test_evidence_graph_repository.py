from __future__ import annotations

import os
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path

import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub.evidence_graph_repository import PostgresEvidenceGraphRepository
from truealpha_contracts import (
    BitemporalStamp,
    CaptureEnvironment,
    CurrentPointer,
    CurrentPointerKey,
    EvidenceEdge,
    EvidenceNode,
    EvidenceNodeKind,
    EvidenceNodeRef,
    EvidenceRelation,
)

_CONTRACT_SQL = Path(__file__).resolve().parents[4] / "db" / "tests" / "evidence_graph_contract.sql"

_RAW = "1" * 64
_OBS = "2" * 64
_RUN1 = "3" * 64
_RUN2 = "4" * 64
_STAMP = BitemporalStamp(
    valid_from=date(2026, 3, 31),
    transaction_time=datetime(2026, 4, 1, tzinfo=UTC),
    recorded_at=datetime(2026, 4, 1, 12, tzinfo=UTC),
)


@pytest.fixture
def connection():
    try:
        active = psycopg.connect(settings.database_url, connect_timeout=3, autocommit=False)
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    try:
        active.execute("select 1")
        yield active
    finally:
        active.rollback()
        active.close()


def _node(kind: EvidenceNodeKind, digest: str) -> EvidenceNode:
    prefix = {
        EvidenceNodeKind.RAW_FETCH: "raw-fetch",
        EvidenceNodeKind.NORMALIZED_OBSERVATION: "normalized-observation",
        EvidenceNodeKind.CAPTURE_RUN: "capture-run",
    }[kind]
    ref = EvidenceNodeRef(kind=kind, node_id=f"{prefix}:{digest}")
    return EvidenceNode(ref=ref, content_sha256=digest, stamp=_STAMP)


def _key() -> CurrentPointerKey:
    return CurrentPointerKey(
        environment=CaptureEnvironment.LOCAL_TEST,
        universe_id="universe:topt-us-2026-03-31",
        universe_version="v1",
        factor_id="gross_profit_per_employee",
    )


def test_append_is_idempotent_and_closure_traverses(connection) -> None:
    repo = PostgresEvidenceGraphRepository(connection)
    raw = _node(EvidenceNodeKind.RAW_FETCH, _RAW)
    obs = _node(EvidenceNodeKind.NORMALIZED_OBSERVATION, _OBS)
    edge = EvidenceEdge(from_ref=obs.ref, to_ref=raw.ref, relation=EvidenceRelation.DERIVED_FROM, stamp=_STAMP)
    repo.append([raw, obs], [edge])
    repo.append([raw, obs], [edge])  # idempotent re-append

    (node_count,) = connection.execute(
        "select count(*) from staging.evidence_nodes where node_id = any(%s)",
        ([raw.ref.node_id, obs.ref.node_id],),
    ).fetchone()
    assert node_count == 2

    forward = repo.closure(obs.ref)
    assert {n.ref for n in forward.nodes} == {obs.ref, raw.ref}
    assert [e.relation for e in forward.edges] == [EvidenceRelation.DERIVED_FROM]
    assert forward.truncated is False

    reverse = repo.closure(raw.ref, reverse=True)
    assert {n.ref for n in reverse.nodes} == {obs.ref, raw.ref}


def test_nodes_are_append_only(connection) -> None:
    repo = PostgresEvidenceGraphRepository(connection)
    raw = _node(EvidenceNodeKind.RAW_FETCH, _RAW)
    repo.append([raw], [])
    with pytest.raises(psycopg.errors.RaiseException):
        with connection.transaction():
            connection.execute(
                "update staging.evidence_nodes set kind = 'snapshot' where node_id = %s",
                (raw.ref.node_id,),
            )
    with pytest.raises(psycopg.errors.RaiseException):
        with connection.transaction():
            connection.execute("delete from staging.evidence_nodes where node_id = %s", (raw.ref.node_id,))


def test_pointer_advances_forward_only(connection) -> None:
    repo = PostgresEvidenceGraphRepository(connection)
    run1 = _node(EvidenceNodeKind.CAPTURE_RUN, _RUN1)
    run2 = _node(EvidenceNodeKind.CAPTURE_RUN, _RUN2)
    repo.append([run1, run2], [])
    key = _key()
    assert repo.head(key) is None

    first = CurrentPointer(key=key, target_run=run1.ref, sequence=0, advanced_at=datetime(2026, 4, 1, tzinfo=UTC))
    repo.advance(first)
    head = repo.head(key)
    assert head is not None and head.target_run == run1.ref and head.sequence == 0

    second = CurrentPointer(
        key=key,
        target_run=run2.ref,
        sequence=1,
        previous_run=run1.ref,
        advanced_at=datetime(2026, 4, 2, tzinfo=UTC),
    )
    repo.advance(second)
    assert repo.resolve_pointer(key).target_run == run2.ref

    # A stale advance naming the wrong previous_run (run1, not the head run2) is rejected.
    stale = CurrentPointer(
        key=key,
        target_run=run2.ref,
        sequence=2,
        previous_run=run1.ref,
        advanced_at=datetime(2026, 4, 3, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="current head as previous_run"):
        repo.advance(stale)


def test_db_contract_executes(connection) -> None:
    assert not connection.closed
    completed = subprocess.run(
        ["psql", settings.database_url, "-v", "ON_ERROR_STOP=1", "-f", str(_CONTRACT_SQL)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
