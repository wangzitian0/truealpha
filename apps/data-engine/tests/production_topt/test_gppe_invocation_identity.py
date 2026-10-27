"""#1518: the pins of the labor-efficiency trees are part of the GPPE definition identity.

`mart.topt_gppe_invocations` holds `unique (snapshot_id, gppe_definition_id)` (migration 0026), and
`materialize` inserts with `on conflict (invocation_id) do nothing`. A snapshot that was materialized
before the pins existed holds an invocation row under the definition id of that time. After the deploy
the same snapshot is materialized again: a tick that resumes at `freeze_snapshot`, or any re-materialize
of a stored snapshot. If the new invocation carried the old definition id, the insert would break the
unique key and abort the tick. So the pins enter the definition (`GppeV0Definition.pinned_by`), and the
new invocation has a new definition id. No migration changes the constraint.

Skips without a local Postgres; ci-python runs it migrated.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub.production_topt import PostgresToptCoreRepository
from factors.production_topt import GppeV0Definition
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.strategy import load_live_strategy_definition

sys.path.insert(0, str(Path(__file__).parent))
from test_materialization import _seed_complete_production_run  # noqa: E402


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


def test_a_stored_snapshot_is_materialized_again_under_new_ids_after_the_pins(connection) -> None:
    seeded = _seed_complete_production_run(connection)
    run, release_manifest_id = seeded[1], seeded[3]
    repository = PostgresToptCoreRepository(connection)
    snapshot = repository.freeze_snapshot(run_id=run.run_id, release_manifest_id=release_manifest_id)

    # The invocation row as main wrote it: the payload names the snapshot and the definition, no pins.
    legacy = GppeV0Definition(risk_free_rate="0.05")
    payload = {"snapshot_id": snapshot.snapshot_id, "gppe_definition": legacy.model_dump(mode="json")}
    old_sha256 = canonical_sha256(payload)
    old_invocation_id = f"topt-gppe-invocation:{old_sha256}"
    connection.execute(
        """
        insert into mart.topt_gppe_invocations (
            invocation_id, content_sha256, snapshot_id, gppe_definition_id, gppe_definition_sha256, payload
        ) values (%s, %s, %s, %s, %s, %s::jsonb)
        """,
        (
            old_invocation_id,
            old_sha256,
            snapshot.snapshot_id,
            legacy.definition_id,
            legacy.content_sha256,
            psycopg.types.json.Jsonb(payload),
        ),
    )

    results = repository.materialize(snapshot, gppe_definition=legacy)

    rows = connection.execute(
        "select invocation_id, gppe_definition_id from mart.topt_gppe_invocations where snapshot_id = %s order by 1",
        (snapshot.snapshot_id,),
    ).fetchall()
    assert len(rows) == 2, "the old invocation stays and the new one is added"
    new_ids = {definition_id for invocation_id, definition_id in rows if invocation_id != old_invocation_id}
    assert new_ids and legacy.definition_id not in new_ids, (
        "the pins change the definition id, so the unique key differs"
    )
    assert {item.gppe_definition_id for item in results} == new_ids, (
        "each result names the definition that pinned its trees"
    )
    live = load_live_strategy_definition()
    assert new_ids == {GppeV0Definition(risk_free_rate="0.05").pinned_by(live).definition_id}
