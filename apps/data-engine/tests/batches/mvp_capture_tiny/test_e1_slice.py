from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

import dagster as dg
import psycopg
import pytest
from data_engine.batches.mvp_capture_tiny.e0_slice import run_e0_slice
from data_engine.batches.mvp_capture_tiny.e1_slice import (
    E1_ASSET_NAME,
    EphemeralPostgresRecordRepository,
    _base_probe,
    build_e1_definitions,
    evaluate_evidence_variant,
    validate_boundary_probe,
)
from data_engine.config import settings
from pydantic import ValidationError
from truealpha_contracts.execution import SnapshotCellSelection

REPOSITORY_ROOT = next(
    parent
    for parent in Path(__file__).resolve().parents
    if (parent / "governance" / "vision-issue-graph.json").is_file()
)


@pytest.fixture
def connection():
    try:
        active_connection = psycopg.connect(settings.database_url, connect_timeout=3, autocommit=True)
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    with active_connection:
        yield active_connection


def test_dagster_executes_all_terminal_e1_cases(connection):
    definitions = build_e1_definitions(repository_root=REPOSITORY_ROOT, connection=connection)
    dg.Definitions.validate_loadable(definitions)

    result = definitions.get_implicit_global_asset_job_def().execute_in_process()
    evidence = result.output_for_node(E1_ASSET_NAME)

    assert result.success
    assert evidence.fixture_snapshot_id == evidence.postgres_snapshot_id
    assert evidence.fixture_runner_selection_id == evidence.postgres_runner_selection_id
    assert {case.case_id for case in evidence.cases} == {
        "applicable-success",
        "missing-raw-evidence",
        "missing-normalized-evidence",
        "append-only-restatement",
        "wrong-listing-identity",
        "publication-boundary",
        "look-ahead-sentinel",
        "reordered-and-repeated-execution",
    }
    assert all(case.passed for case in evidence.cases)
    assert evidence.stable_handoff is False


def test_ephemeral_repository_rejects_update_and_delete(connection):
    base = run_e0_slice(REPOSITORY_ROOT)
    repository = EphemeralPostgresRecordRepository(connection)
    assert repository.put(base.record)

    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        connection.execute(
            "update mvp_capture_tiny_normalized_records set subject_id = %s",
            ("issuer.changed",),
        )
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        connection.execute("delete from mvp_capture_tiny_normalized_records")


def test_partial_write_rolls_back_and_retry_succeeds():
    base = run_e0_slice(REPOSITORY_ROOT)
    try:
        active_connection = psycopg.connect(settings.database_url, connect_timeout=3, autocommit=True)
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    with active_connection as connection:
        repository = EphemeralPostgresRecordRepository(connection)
        with pytest.raises(RuntimeError, match="simulated interruption"):
            with connection.transaction():
                assert repository.put(base.record)
                raise RuntimeError("simulated interruption")

        assert repository.count() == 0
        assert repository.put(base.record)
        assert repository.put(base.record) is False


@pytest.mark.parametrize(
    ("updates", "blocker"),
    [
        ({"confidence": None}, "evidence.missing_confidence"),
        ({"policy_versions": {}}, "evidence.missing_policy"),
        ({"knowable_at": None}, "evidence.missing_knowable_at"),
    ],
)
def test_capture_evidence_fails_closed(updates, blocker):
    evaluation = evaluate_evidence_variant(run_e0_slice(REPOSITORY_ROOT), **updates)

    assert not evaluation.ready
    assert any(code.startswith(blocker) for code in evaluation.blocking_reason_codes)


def test_stale_evidence_fails_closed():
    base = run_e0_slice(REPOSITORY_ROOT)
    evaluation = evaluate_evidence_variant(
        base,
        knowable_at=base.capture_manifest.as_of - base.capture_requirement.maximum_age - timedelta(seconds=1),
    )

    assert not evaluation.ready
    assert any("stale" in code for code in evaluation.blocking_reason_codes)


def test_required_snapshot_cell_cannot_omit_records():
    base = run_e0_slice(REPOSITORY_ROOT)

    with pytest.raises(ValidationError):
        SnapshotCellSelection(demand=base.snapshot.selections[0].demand)


@pytest.mark.parametrize(
    ("updates", "error"),
    [
        ({"observed_subject": {"kind": "issuer", "id": "issuer.changed"}}, "identity mismatch"),
        ({"observed_listing_id": "listing.nyse.nvda"}, "wrong listing"),
        ({"observed_share_class": "preferred"}, "wrong share class"),
        ({"price_basis": "adjusted"}, "double count"),
        ({"fx_knowable_at": None}, "future-known"),
        ({"maximum_fx_age": timedelta(seconds=1)}, "stale"),
        ({"observed_source_entry_id": "source.changed:1"}, "source registry binding drift"),
        ({"observed_semantic_type_id": "semantic.changed"}, "semantic type binding drift"),
    ],
)
def test_boundary_controls_reject_unsafe_bindings(updates, error):
    probe = _base_probe(run_e0_slice(REPOSITORY_ROOT))
    if updates.get("fx_knowable_at") is None and "fx_knowable_at" in updates:
        updates = {"fx_knowable_at": probe.as_of + timedelta(seconds=1)}

    with pytest.raises(ValueError, match=error):
        validate_boundary_probe(probe.model_copy(update=updates))
