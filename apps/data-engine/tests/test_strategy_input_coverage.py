"""#496 (metric first): mart.strategy_input_coverage — migration 0035 + writer.

Reuses the materialization suite's real seeding spine (same pattern as
test_entity_display_resolution_view.py): seed a complete production run,
freeze + materialize, seed strategy inputs, then persist coverage and assert:

  * one row per issuer of the run, required_count == 6 (derived from the
    frozen definition's required_input_keys(), not hardcoded in the writer);
  * present/missing arithmetic holds (check constraint mirrors it in the DB);
  * the writer's (complete, total) return matches the persisted rows;
  * rows are append-only (UPDATE/DELETE trigger-rejected) and re-running the
    writer is idempotent (on conflict do nothing).

Skips without a local Postgres; ci-python/ci-db run it migrated.
"""

import os
import sys
from pathlib import Path

import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub.production_topt import PostgresToptCoreRepository
from data_engine.datahub.strategy_bridge import (
    persist_strategy_input_coverage,
    seed_strategy_inputs_from_capture,
)
from factors.production_topt import GppeV0Definition

sys.path.insert(0, str(Path(__file__).parent))
from production_topt.test_materialization import CUTOFF, _seed_complete_production_run  # noqa: E402


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


def test_coverage_rows_land_per_issuer_and_arithmetic_holds(connection) -> None:
    seeded = _seed_complete_production_run(connection)
    run, release_manifest_id = seeded[1], seeded[3]
    repository = PostgresToptCoreRepository(connection)
    snapshot = repository.freeze_snapshot(run_id=run.run_id, release_manifest_id=release_manifest_id)
    repository.materialize(snapshot, gppe_definition=GppeV0Definition(risk_free_rate="0.05"))
    seed_strategy_inputs_from_capture(connection, run.run_id, cutoff=CUTOFF)

    complete, total = persist_strategy_input_coverage(connection, run.run_id, cutoff=CUTOFF)

    rows = connection.execute(
        "select issuer_id, required_count, present_count, missing_keys "
        "from mart.strategy_input_coverage where run_id = %s order by issuer_id",
        (run.run_id,),
    ).fetchall()
    issuers = {member.issuer_id for member in snapshot.members}
    assert total == len(rows) == len(issuers), "one coverage row per issuer of the run"
    assert all(required == 6 for _, required, *_ in rows), "6 required keys derived from the frozen definition"
    assert complete == sum(1 for _, req, present, _ in rows if present == req)
    for _issuer, required, present, missing in rows:
        assert len(missing) == required - present

    # Idempotent re-run: no duplicate rows, same counts.
    again = persist_strategy_input_coverage(connection, run.run_id, cutoff=CUTOFF)
    assert again == (complete, total)
    assert connection.execute(
        "select count(*) from mart.strategy_input_coverage where run_id = %s", (run.run_id,)
    ).fetchone() == (total,)

    # Append-only: the mart mutation trigger rejects rewrites of the metric.
    with pytest.raises(psycopg.errors.RaiseException):
        connection.execute(
            "update mart.strategy_input_coverage set present_count = required_count where run_id = %s",
            (run.run_id,),
        )
