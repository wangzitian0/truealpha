"""A reused observation keeps its lineage item in `mart.topt_core_meta_info` (#1331).

A TOPT tick reuses the observations that another run committed in the last 12 hours (#635).
Its financial-fact and market-price obligations end `unchanged`. Their work items name source
requests of the reuse run. A reused observation keeps the vintage, and so the source request, of
the run that fetched it. 20261012T1130 joined the source request on both requests, so the inner
join dropped every reused item. On staging, /research/trace then showed "6 input rows, 0 with a
value" after every deploy.

The seed is the deployed entry point. `run_topt_pipeline` runs twice at one cutoff, with offline
vendor routes. The first run fetches. The second run reuses, as the staging boot tick does. The
tests read the view that the migrations installed. The old definition comes from the text of
20261012T1130, so the fresh-run comparison reads the definition that production served before.
"""

from __future__ import annotations

import sys
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from data_engine.datahub.production_topt import PostgresToptCoreRepository, composition
from factors.production_topt import GppeV0Definition
from truealpha_contracts.common import CaptureEnvironment, canonical_sha256
from truealpha_contracts.datahub import SourceRequest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from production_topt.test_degraded_capture_reuse import (  # noqa: E402
    _REUSE_CUTOFF,
    _arm,
    _run_tick,
    tick_database_url,  # noqa: F401 - the database fixture of the reuse tests
)
from production_topt.test_materialization import _seed_complete_production_run  # noqa: E402

MIGRATIONS = Path(__file__).resolve().parents[4] / "db" / "migrations"
#: The definition before #1331. It owns the text of the old view.
INPUTS_MIGRATION = MIGRATIONS / "20261012T1130_web_topt_core_meta_info_inputs.sql"
#: The definition of #1331. It is the last file that defines the view.
REUSED_INPUTS_MIGRATION = MIGRATIONS / "20261015T0720_web_topt_core_meta_info_reused_inputs.sql"

REUSED_SEMANTICS = ("financial-fact", "market-price")
ALL_SEMANTICS = (*REUSED_SEMANTICS, "listing-identity", "universe-membership")
#: The payload keys that the view copies into `inputs` (20261012T1130).
INPUT_WHITELIST = (
    "gross_profit",
    "total_assets",
    "headcount",
    "revenue",
    "shares_outstanding",
    "close",
    "currency",
    "listing_id",
)
#: The source registration that the reuse tick plans its requests with.
BUMPED_REGISTRY_ENTRY_ID = f"source-registry-entry:{canonical_sha256({'source': 'lineage-1331-bumped'})}"
BUMPED_SOURCE_POLICY_ID = "source-policy:lineage-1331-bumped:v2"
#: The values that the offline vendor routes return (`_bundle`, `_quote`, `record_headcount`).
NON_BANK_FINANCIAL_VALUES = {
    "gross_profit": "210000000",
    "total_assets": "200000000",
    "headcount": "164000",
    "revenue": "100000000",
    "shares_outstanding": "10000000",
    "currency": "USD",
}


@pytest.fixture(scope="module")
def database(tick_database_url) -> str:  # noqa: F811
    """A scratch copy of the migrated database. The two ticks commit into it.

    The reuse tests own the setup of a database that a tick can run on. This module uses the same
    fixture, so the two modules cannot drift apart.
    """
    return str(tick_database_url)


_PLANNED_SOURCE_REQUEST = composition._source_request


def _bumped_source_request(obligation: Any, *, ordinal: int, source: str) -> SourceRequest:
    """The planned request of a later release that bumped the source registration.

    A bump of the source policy does not stop reuse: reuse compares parser versions (#788). So
    a reuse run can name another registry entry and policy than the request that fetched the bytes.
    """
    planned = _PLANNED_SOURCE_REQUEST(obligation, ordinal=ordinal, source=source)
    return SourceRequest(
        source_registry_entry_id=BUMPED_REGISTRY_ENTRY_ID,
        source_policy_id=BUMPED_SOURCE_POLICY_ID,
        request_fingerprint_version=planned.request_fingerprint_version,
        canonical_request_sha256=planned.canonical_request_sha256,
        subject_refs=planned.subject_refs,
        capture_requirement_ids=planned.capture_requirement_ids,
        partition=planned.partition,
    )


@pytest.fixture(scope="module")
def runs(database: str) -> tuple[str, str]:
    """(fresh run, reuse run): two ticks at one cutoff. The second tick reuses the first.

    The second tick also plans its requests with a bumped source registration.
    """
    with pytest.MonkeyPatch.context() as patch:
        _arm(patch)
        fresh = _run_tick(database, version="lineage-1331-fresh", cutoff=_REUSE_CUTOFF)
        patch.setattr(composition, "_source_request", _bumped_source_request)
        reused = _run_tick(database, version="lineage-1331-reuse", cutoff=_REUSE_CUTOFF)
    assert fresh.run_id != reused.run_id
    return fresh.run_id, reused.run_id


@pytest.fixture
def conn(database: str) -> Iterator[psycopg.Connection]:
    """A connection in a transaction that is always rolled back."""
    with psycopg.connect(database) as connection:
        try:
            yield connection
        finally:
            connection.rollback()


def _served(conn: psycopg.Connection, run_id: str, relation: str = "mart.topt_core_meta_info") -> dict[str, Any]:
    """result_id -> (input_observation_ids, lineage) for every core result of one run."""
    rows = conn.execute(
        f"select result_id, input_observation_ids, lineage from {relation} where run_id = %s",  # noqa: S608
        (run_id,),
    ).fetchall()
    return {result_id: (list(inputs), lineage) for result_id, inputs, lineage in rows}


def _terminal_states(conn: psycopg.Connection, run_id: str) -> dict[tuple[str, str], int]:
    rows = conn.execute(
        """
        select regexp_replace(obligation.capture_requirement_id, ':v1$', ''), terminal.terminal_state, count(*)
        from raw.capture_obligations obligation
        join raw.capture_obligation_results terminal on terminal.capture_obligation_id = obligation.obligation_id
        where obligation.run_id = %s
        group by 1, 2
        """,
        (run_id,),
    ).fetchall()
    return {(semantic, state): count for semantic, state, count in rows}


def _reused_requests(conn: psycopg.Connection, run_id: str) -> dict[str, dict[str, str]]:
    """observation_id -> the request of its vintage and the request of the reuse work item.

    One entry for each unchanged obligation of the run.
    """
    rows = conn.execute(
        """
        select observation.observation_id,
               fetched.source_request_id, fetched.source_registry_entry_id, fetched.source_policy_id,
               work.source_request_id
        from raw.capture_obligations obligation
        join raw.capture_obligation_results terminal on terminal.capture_obligation_id = obligation.obligation_id
        join raw.capture_attempt_results attempt on attempt.attempt_id = terminal.final_attempt_id
        join raw.capture_source_vintages vintage on vintage.source_vintage_id = attempt.reused_source_vintage_id
        join raw.capture_source_requests fetched on fetched.source_request_id = vintage.source_request_id
        join staging.capture_normalized_observations observation
          on observation.source_vintage_id = vintage.source_vintage_id
        join raw.capture_obligation_work_bindings binding on binding.obligation_id = obligation.obligation_id
        join raw.capture_work_items work on work.work_item_id = binding.work_item_id
        where obligation.run_id = %s and terminal.terminal_state = 'unchanged'
        """,
        (run_id,),
    ).fetchall()
    return {
        observation_id: {
            "source_request_id": request_id,
            "source_registry_entry_id": registry_entry_id,
            "source_policy_id": policy_id,
            "work_request_id": work_request_id,
        }
        for observation_id, request_id, registry_entry_id, policy_id, work_request_id in rows
    }


def _seed_integration_run(conn: psycopg.Connection) -> str:
    """The hand-built run of test_materialization, frozen and materialized. Returns its run_id.

    The run takes the environment that the freeze governs here: the ticks of the module seed
    wrote `mart.environment_identity`.
    """
    identity = conn.execute("select environment from mart.environment_identity").fetchone()
    assert identity is not None, "the ticks of the module seed must declare the environment"
    filing = {"accession": "0000320193-26-000010", "filed": "2026-02-01", "form": "10-K"}
    (_, run, _, release_manifest_id, *_rest) = _seed_complete_production_run(
        conn,
        stale_unchanged_first_observation=True,
        financial_vintage={"revenue": filing},
        environment=CaptureEnvironment(identity[0]),
    )
    core = PostgresToptCoreRepository(conn)
    snapshot = core.freeze_snapshot(run_id=run.run_id, release_manifest_id=release_manifest_id)
    assert len(core.materialize(snapshot, gppe_definition=GppeV0Definition(risk_free_rate="0.05"))) == 20
    return run.run_id


def _old_definition() -> str:
    """The view text of 20261012T1130, the definition before #1331."""
    parts = INPUTS_MIGRATION.read_text(encoding="utf-8").split("$view$")
    assert len(parts) == 3, f"{INPUTS_MIGRATION.name} must hold exactly one $view$ body"
    return parts[1]


def test_a_reuse_run_lists_every_reused_input_with_its_values_and_filing(conn: psycopg.Connection, runs) -> None:
    """#1331 criterion 1. RED on 20261012T1130: each lineage held only the identity and membership items."""
    fresh_run, reuse_run = runs
    # The staging shape, asserted so that the test cannot pass on a run that did not reuse.
    assert _terminal_states(conn, reuse_run) == {
        ("financial-fact", "unchanged"): 21,
        ("market-price", "unchanged"): 21,
        ("listing-identity", "success"): 21,
        ("universe-membership", "success"): 21,
    }
    requests = _reused_requests(conn, reuse_run)
    assert len(requests) == 42
    assert all(request["source_request_id"] != request["work_request_id"] for request in requests.values())
    assert all(request["source_policy_id"] != BUMPED_SOURCE_POLICY_ID for request in requests.values())

    served = _served(conn, reuse_run)
    assert len(served) == 20
    for inputs, lineage in served.values():
        # Every input observation once, in the order of observation_id.
        assert [item["observation_id"] for item in lineage] == sorted(inputs)
    items = [item for _, lineage in served.values() for item in lineage]
    assert Counter(item["semantic_type"] for item in items) == dict.fromkeys(ALL_SEMANTICS, 21)

    payloads: dict[str, dict[str, Any]] = dict(
        conn.execute(
            "select observation_id, normalized_payload from staging.capture_observation_payloads "
            "where observation_id = any(%s)",
            ([item["observation_id"] for item in items],),
        ).fetchall()
    )
    fetched = {item["observation_id"]: item for _, lineage in _served(conn, fresh_run).values() for item in lineage}
    reused = [item for item in items if item["semantic_type"] in REUSED_SEMANTICS]
    assert {item["observation_id"] for item in reused} == set(requests)
    for item in reused:
        payload = payloads[item["observation_id"]]
        # The request that fetched the bytes, not the request of the reuse work item.
        fetched_request = requests[item["observation_id"]]
        for key in ("source_request_id", "source_registry_entry_id", "source_policy_id"):
            assert item[key] == fetched_request[key], key
        assert item["inputs"] == {key: payload[key] for key in INPUT_WHITELIST if key in payload}
        assert item["vintage"] == payload.get("vintage")
        # A reused observation keeps the item of the run that fetched it, key for key.
        assert item == fetched[item["observation_id"]]
    # The control: the observations that the reuse run fetched itself name the bumped registration.
    own = [item for item in items if item["semantic_type"] not in REUSED_SEMANTICS]
    assert {(item["source_registry_entry_id"], item["source_policy_id"]) for item in own} == {
        (BUMPED_REGISTRY_ENTRY_ID, BUMPED_SOURCE_POLICY_ID)
    }

    # The numbers themselves, so the oracle is not only the payload table that the view reads.
    financial = [item for item in reused if item["semantic_type"] == "financial-fact"]
    non_bank = [item for item in financial if payloads[item["observation_id"]]["operating_branch"] == "non_financial"]
    assert len(non_bank) == 20, "the corpus holds one bank (JPM) among 21 listings"
    for item in non_bank:
        listing_id = payloads[item["observation_id"]]["listing_id"]
        assert item["inputs"] == {**NON_BANK_FINANCIAL_VALUES, "listing_id": listing_id}
        assert item["vintage"]["headcount"]["source"] == "test-fixture"
    for item in (item for item in reused if item["semantic_type"] == "market-price"):
        listing_id = payloads[item["observation_id"]]["listing_id"]
        assert item["inputs"] == {"close": "40", "currency": "USD", "listing_id": listing_id}
        assert item["vintage"] is None


def _same_lineage_as_the_old_definition(conn: psycopg.Connection, run_id: str) -> list[dict[str, Any]]:
    """Assert that the old and the new definition give equal lineages for every result of one run.

    The equality covers the item count, the keys, the values and the order. Returns the items.
    """
    conn.execute("create temp view lineage_before_1331 as " + _old_definition())
    before = _served(conn, run_id, "pg_temp.lineage_before_1331")
    after = _served(conn, run_id)
    assert len(after) == 20
    # 21 listings, four observations each.
    assert sum(len(lineage) for _, lineage in after.values()) == 84
    assert after == before
    return [item for _, lineage in after.values() for item in lineage]


def test_a_fresh_run_keeps_the_lineage_of_the_previous_definition(conn: psycopg.Connection, runs) -> None:
    """#1331 criterion 2, on the fresh tick of the module seed."""
    fresh_run, _ = runs
    items = _same_lineage_as_the_old_definition(conn, fresh_run)
    assert Counter(item["semantic_type"] for item in items) == dict.fromkeys(ALL_SEMANTICS, 21)


def test_a_hand_built_fresh_run_keeps_the_lineage_of_the_previous_definition(conn: psycopg.Connection, runs) -> None:
    """#1331 criterion 2, on the hand-built run of test_materialization.

    One obligation of that run ends `unchanged` on its own request, and its item is stale. Tied
    and foreign observations are linked to the same obligation. The freeze selects none of them.
    """
    items = _same_lineage_as_the_old_definition(conn, _seed_integration_run(conn))
    assert Counter(item["freshness"] for item in items)["stale"] == 1


def test_a_dual_class_issuer_in_a_reuse_run_lists_each_input_once(conn: psycopg.Connection, runs) -> None:
    """#1331 criterion 3: Alphabet has two listings. Each of its eight inputs appears once.

    The second half links every input observation of one listing to the obligation of the other
    listing, with the same semantic, in the same run. Then one observation has two obligations
    in the run. The lineage must not change.
    """
    _, reuse_run = runs
    (dual,) = [entry for entry in _served(conn, reuse_run).values() if len(entry[0]) == 8]
    inputs, before = dual
    assert [item["observation_id"] for item in before] == sorted(inputs)
    assert Counter(item["semantic_type"] for item in before) == {
        "financial-fact": 2,
        "market-price": 2,
        "listing-identity": 2,
        "universe-membership": 2,
    }
    assert len({item["inputs"]["listing_id"] for item in before}) == 2

    linked = conn.execute(
        """
        insert into staging.capture_observation_obligations (capture_obligation_id, observation_id)
        select other.obligation_id, observation.observation_id
        from unnest(%s::text[]) selected(observation_id)
        join staging.capture_normalized_observations observation using (observation_id)
        join raw.capture_obligations other
          on other.run_id = %s
         and regexp_replace(other.capture_requirement_id, ':v1$', '') = observation.semantic_type
         and other.subject_id <> observation.subject_id
         and other.subject_id in (
             select peer.subject_id
             from staging.capture_normalized_observations peer
             where peer.observation_id = any(%s::text[])
         )
        returning observation_id
        """,
        (inputs, reuse_run, inputs),
    ).fetchall()
    assert len(linked) == 8, "each input must gain a link to the obligation of the other listing"
    (after,) = [lineage for result_inputs, lineage in _served(conn, reuse_run).values() if result_inputs == inputs]
    assert after == before


def _view_state(conn: psycopg.Connection) -> tuple[str, str]:
    """The transaction that last wrote the rewrite rule of the view, and its definition."""
    row = conn.execute(
        """
        select rule.xmin::text, pg_get_viewdef(rule.ev_class)
        from pg_rewrite rule
        where rule.ev_class = to_regclass('mart.topt_core_meta_info') and rule.rulename = '_RETURN'
        """
    ).fetchone()
    assert row is not None, "mart.topt_core_meta_info does not exist"
    return row[0], row[1]


@pytest.mark.parametrize("migration", [INPUTS_MIGRATION, REUSED_INPUTS_MIGRATION], ids=["inputs", "reused-inputs"])
def test_a_replay_keeps_the_view_and_takes_no_strong_lock(conn: psycopg.Connection, migration: Path) -> None:
    """#1331 criterion 4: a boot replays both files on a settled database.

    Neither file replaces the view: the rewrite rule keeps its xmin and its definition. On a mart
    relation, each file takes only ACCESS SHARE, ROW EXCLUSIVE or SHARE UPDATE EXCLUSIVE. A strong
    lock would queue the boot behind every open reader of /research/trace.
    """
    before = _view_state(conn)
    conn.execute(migration.read_text(encoding="utf-8"))
    held = conn.execute(
        """
        select format('%s.%s', n.nspname, c.relname), l.mode
        from pg_locks l
        join pg_class c on c.oid = l.relation
        join pg_namespace n on n.oid = c.relnamespace
        where l.pid = pg_backend_pid() and l.locktype = 'relation' and l.granted and n.nspname = 'mart'
        """
    ).fetchall()
    strong = sorted(
        (name, mode)
        for name, mode in held
        if mode not in {"AccessShareLock", "RowExclusiveLock", "ShareUpdateExclusiveLock"}
    )
    assert strong == []
    assert _view_state(conn) == before
