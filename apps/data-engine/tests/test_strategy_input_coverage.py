"""#496 (metric first), #1852: count_complete_strategy_inputs computes readiness in memory without writing mart.strategy_input_coverage.

Reuses the materialization suite's real seeding spine (same pattern as
test_entity_display_resolution_view.py): seed a complete production run,
freeze + materialize, seed strategy inputs, then verify:

  * required inputs derive from the trees that the frozen definition pins for the issuer's
    class (`required_input_keys`): 6 for a non-financial issuer or insurer, 9 for a bank;
  * returned (complete, total) counts match the seeded rows;
  * zero rows are persisted into retired `mart.strategy_input_coverage`.

Skips without a local Postgres; ci-python/ci-db run it migrated.
"""

import os
import sys
from pathlib import Path

import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub.capture_core import PostgresCoreRepository
from data_engine.datahub.strategy_bridge import (
    count_complete_strategy_inputs,
    seed_strategy_inputs_from_capture,
)
from factors.composite.strategy_evaluator import required_input_keys
from factors.panel import IssuerClass
from factors.snapshot_inputs import GppeV0Definition
from truealpha_contracts.strategy import load_live_strategy_definition

sys.path.insert(0, str(Path(__file__).parent))
from capture_core.test_materialization import CUTOFF, _seed_complete_production_run  # noqa: E402


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


def test_complete_strategy_input_count_derives_required_keys_without_writing_table(connection) -> None:
    seeded = _seed_complete_production_run(connection)
    run, release_manifest_id = seeded[1], seeded[3]
    repository = PostgresCoreRepository(connection)
    snapshot = repository.freeze_snapshot(run_id=run.run_id, release_manifest_id=release_manifest_id)
    repository.materialize(snapshot, gppe_definition=GppeV0Definition(risk_free_rate="0.05"))
    seed_strategy_inputs_from_capture(
        connection, run.run_id, cutoff=CUTOFF, parser_version="production-topt-integration-parser:v1"
    )

    complete, total = count_complete_strategy_inputs(connection, run.run_id, cutoff=CUTOFF)

    definition = load_live_strategy_definition()
    hand_count = {"financial": 9, "non_financial": 6, "insurance": 6}
    present_rows = connection.execute(
        "select distinct on (issuer_id, input_key) issuer_id, input_key from staging.strategy_backtest_inputs "
        "where cutoff_at = %s order by issuer_id, input_key, recorded_at desc",
        (CUTOFF,),
    ).fetchall()
    present_by_issuer: dict[str, set[str]] = {}
    for issuer_id, input_key in present_rows:
        present_by_issuer.setdefault(issuer_id, set()).add(input_key)

    branches = {member.issuer_id: member.operating_branch.value for member in snapshot.members}
    assert any(branch == "financial" for branch in branches.values()), "the seed holds a bank, so the 9 is exercised"
    for issuer_id, branch in branches.items():
        assert len(required_input_keys(definition, IssuerClass(branch))) == hand_count[branch]

    expected_complete = sum(
        1
        for issuer_id, branch in branches.items()
        if not (set(required_input_keys(definition, IssuerClass(branch))) - present_by_issuer.get(issuer_id, set()))
    )
    assert total == len(branches)
    assert present_by_issuer, "expected seeded inputs in staging.strategy_backtest_inputs"
    assert complete == expected_complete
    assert connection.execute(
        "select count(*) from mart.strategy_input_coverage where run_id = %s", (run.run_id,)
    ).fetchone() == (0,)

    incomplete = [
        (issuer_id, set(required_input_keys(definition, IssuerClass(branch))) - present_by_issuer.get(issuer_id, set()))
        for issuer_id, branch in branches.items()
    ]
    target_issuer, missing_keys = next((issuer_id, keys) for issuer_id, keys in incomplete if keys)
    for key in sorted(missing_keys):
        connection.execute(
            "insert into staging.strategy_backtest_inputs "
            "(issuer_id, cutoff_at, input_key, value, confidence, knowable_at) "
            "values (%s, %s, %s, 1, 0.9, %s)",
            (target_issuer, CUTOFF, key, CUTOFF),
        )
    complete_after, total_after = count_complete_strategy_inputs(connection, run.run_id, cutoff=CUTOFF)
    assert total_after == total
    assert complete_after == complete + 1
