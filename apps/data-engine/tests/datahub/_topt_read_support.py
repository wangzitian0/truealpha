from __future__ import annotations

import sys
from pathlib import Path

from data_engine.datahub import quality_report
from data_engine.datahub.a1_evidence import register_run_evidence
from data_engine.datahub.capture_core import PostgresCoreRepository
from factors.snapshot_inputs import GppeV0Definition

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from capture_core.test_materialization import _CORPUS_OBJECTIVES, _seed_complete_production_run  # noqa: E402


def _materialized_run(connection):
    """A complete production run, frozen and materialized, with no governed pointer in this
    transaction. Clearing the factor's pointers makes the head the same on any database. The
    delete bypasses the append-only trigger. The rollback restores every row."""
    connection.execute("set session_replication_role = replica")
    connection.execute(
        "delete from mart.current_pointer where environment = 'production' and factor_id = %s",
        ("gross_profit_per_employee",),
    )
    connection.execute("set session_replication_role = origin")
    _repository, run, _list_version, release_manifest_id, *_ = _seed_complete_production_run(connection)
    core = PostgresCoreRepository(connection)
    snapshot = core.freeze_snapshot(run_id=run.run_id, release_manifest_id=release_manifest_id)
    assert len(core.materialize(snapshot, gppe_definition=GppeV0Definition(risk_free_rate="0.05"))) == 20
    return run, release_manifest_id


def _accepted_run(connection):
    """The same run after its quality report persists and its pointer advances."""
    run, release_manifest_id = _materialized_run(connection)
    graded = quality_report.build_report(connection, run.run_id)
    quality_report.persist(connection, graded)
    registration = register_run_evidence(
        connection,
        run_id=run.run_id,
        release_manifest_id=release_manifest_id,
        quality_report=graded,
        objectives=_CORPUS_OBJECTIVES,
    )
    assert registration.accepted, registration.summary
    return run
