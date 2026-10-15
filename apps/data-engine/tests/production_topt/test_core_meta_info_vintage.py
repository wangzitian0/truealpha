"""`mart.topt_core_meta_info` projects the vintage from the payload table (#530 item 4).

20260908T1014 added `'vintage', observation.payload -> 'vintage'` so a served number could
name its filing, and read the wrong column. `capture_normalized_observations.payload` is the
observation ENVELOPE — no business field is in it — so the expression was always NULL and no
join objected. Measured on the 2026-09-09 governed TOPT head: 0 of 84 lineage items carried a
vintage while the payload table had one on every financial-fact observation.

These assert the two halves of that: the envelope really does lack business fields (so a
future reader does not "fix" this back), and the view really does read the payload table.

#1241 adds `inputs` to every lineage item: the input values (gross_profit, total_assets,
headcount, revenue, shares_outstanding, close), their `currency` and the `listing_id` that owns
them, copied from the same payload table through a fixed whitelist. The last tests in this file
assert that, on a seeded run.
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from production_topt.test_materialization import _seed_complete_production_run  # noqa: E402

#: The columns of the view, in order. 20261012T1130 (#1241) and 20261015T0720 (#1331) must not change one of them.
VIEW_COLUMNS = [
    "result_id",
    "invocation_id",
    "snapshot_id",
    "run_id",
    "release_manifest_id",
    "universe_id",
    "universe_version",
    "universe_sha256",
    "cutoff",
    "issuer_id",
    "instrument_id",
    "listing_id",
    "input_observation_ids",
    "gppe_invocation_id",
    "gppe_result_id",
    "gppe_definition_id",
    "gppe_definition_sha256",
    "tier_definition_id",
    "tier_definition_sha256",
    "confidence",
    "freshness",
    "created_at",
    "lineage",
]

#: The keys of one lineage item before #1241, and `inputs` which #1241 adds.
LINEAGE_KEYS_BEFORE_INPUTS = {
    "observation_id",
    "semantic_type",
    "semantic_version",
    "source_vintage_id",
    "source_request_id",
    "source_registry_entry_id",
    "source_policy_id",
    "parser_version",
    "mapping_version",
    "normalized_payload_sha256",
    "confidence",
    "freshness",
    "knowable_at",
    "recorded_at",
    "vintage",
}

#: The payload keys that reach `inputs`. A payload key outside this set must not reach it.
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

ENVELOPE_KEYS = {
    "content_sha256",
    "knowable_at",
    "observation_id",
    "parser_version",
    "semantic_type",
    "source_vintage_id",
}


@pytest.fixture
def connection():
    """One transaction per test. The view tests only read. The seeded tests write, and the
    rollback removes every row they seed."""
    try:
        active = psycopg.connect(settings.database_url, connect_timeout=3, autocommit=False)
    except psycopg.OperationalError as error:
        # The repository's convention, which this file did not follow when it landed
        # (review on #798): an UNCONDITIONAL skip silently drops this coverage in CI, where
        # a Postgres service exists and a connection failure is a real failure. A guard that
        # cannot go red where production runs is the shape AGENTS.md rule 7 forbids.
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    try:
        active.execute("select 1")
        yield active
    finally:
        active.rollback()
        active.close()


def _viewdef(connection) -> str:
    # `to_regclass` returns NULL for a missing relation; `'…'::regclass` RAISES, which made
    # the assertion below dead code and the failure message misleading (review on #798).
    row = connection.execute("select pg_get_viewdef(to_regclass('mart.topt_core_meta_info'), true)").fetchone()
    assert row is not None and row[0] is not None, (
        "mart.topt_core_meta_info does not exist — apply db/migrations before running this"
    )
    return " ".join(row[0].split())


def test_the_view_reads_the_payload_table_for_vintage(connection) -> None:
    definition = _viewdef(connection)
    assert "capture_observation_payloads" in definition, (
        "the view does not join the payload table, so `vintage` cannot resolve to anything but NULL (#530 item 4)"
    )
    assert "normalized_payload -> 'vintage'" in definition, (
        "the vintage must come from the business payload, not the observation envelope"
    )


def test_the_view_does_not_read_vintage_from_the_envelope(connection) -> None:
    """The specific regression. Stated separately from the positive above so a view that
    reads BOTH — which would look green on the first assertion — still fails."""
    definition = _viewdef(connection)
    assert "observation.payload -> 'vintage'" not in definition, (
        "`capture_normalized_observations.payload` is the envelope; reading `-> 'vintage'` "
        "from it silently yields NULL under a key that exists, which reads as 'this filing "
        "is unknown' rather than 'this projection is broken'"
    )


def test_the_envelope_carries_no_business_fields(connection) -> None:
    """Why the wrong column was silent, pinned as a fact and not left as a comment. If the
    envelope gains business fields, this test goes red and the reasoning above needs a new
    review. The run is seeded. So the test examines 21 envelopes in every database, including
    the empty database that CI starts with."""
    (_, run, *_rest) = _seed_complete_production_run(connection)
    envelopes = connection.execute(
        """
        select observation.payload
        from staging.capture_normalized_observations observation
        join staging.capture_observation_obligations usage using (observation_id)
        join raw.capture_obligations obligation on obligation.obligation_id = usage.capture_obligation_id
        where obligation.run_id = %s and observation.semantic_type = 'financial-fact'
        """,
        (run.run_id,),
    ).fetchall()
    assert len(envelopes) >= 21, "the run must expose one financial-fact envelope per listing"
    for (envelope,) in envelopes:
        keys = set(envelope)
        assert ENVELOPE_KEYS <= keys, f"the envelope's shape changed: {sorted(keys)}"
        assert "vintage" not in keys, "the envelope gained a vintage; the projection should read it directly"
        for business_field in ("revenue", "gross_profit", "total_assets", "headcount"):
            assert business_field not in keys, (
                f"the envelope now carries {business_field!r}; this test's premise needs revisiting"
            )


def test_the_vintage_in_the_payload_table_reaches_the_lineage_of_the_served_row(connection) -> None:
    """The other half: a vintage in the payload table reaches `mart.topt_core_meta_info`.
    The seeded financial-fact payloads carry a vintage. So a green projection test cannot be
    green because no row has a vintage. test_sec_financial_adapter.py covers the adapter side.
    `test_the_bundle_names_the_filing_behind_each_input` pins the entry shape.
    `test_the_headcounts_evidence_travels_on_the_row` pins the payload."""
    filing = {
        "accession": "0000320193-26-000010",
        "document": None,
        "filed": "2026-02-01",
        "form": "10-K",
        "fp": "FY",
        "fy": 2025,
        "period_end": "2025-12-31",
        "statement_form": True,
    }
    vintage = {"revenue": filing, "total_assets": filing}
    (_, run, _, release_manifest_id, *_rest) = _seed_complete_production_run(connection, financial_vintage=vintage)
    core = PostgresToptCoreRepository(connection)
    snapshot = core.freeze_snapshot(run_id=run.run_id, release_manifest_id=release_manifest_id)
    assert len(core.materialize(snapshot, gppe_definition=GppeV0Definition(risk_free_rate="0.05"))) == 20

    lineages = connection.execute(
        "select lineage from mart.topt_core_meta_info where run_id = %s", (run.run_id,)
    ).fetchall()
    items = [item for (lineage,) in lineages for item in lineage]
    financial = [item for item in items if item["semantic_type"] == "financial-fact"]
    assert len(financial) == 21, "every listing's financial-fact observation must appear in the lineage"
    assert all(item["vintage"] == vintage for item in financial)
    sample = next(iter(financial[0]["vintage"].values()))
    assert {"accession", "form", "filed"} <= set(sample), f"a vintage entry must name its filing: {sample}"
    assert all(item["vintage"] is None for item in items if item["semantic_type"] != "financial-fact")


def test_the_view_keeps_every_column_when_inputs_is_added(connection) -> None:
    """20261012T1130 and 20261015T0720 replace the view. A consumer selects these 23 columns by
    name, so a dropped, renamed or reordered column breaks it."""
    columns = [
        name
        for (name,) in connection.execute(
            """
            select attname
            from pg_attribute
            where attrelid = to_regclass('mart.topt_core_meta_info') and attnum > 0 and not attisdropped
            order by attnum
            """
        ).fetchall()
    ]
    assert columns == VIEW_COLUMNS


def _seed_and_materialize(connection):
    (_, run, _, release_manifest_id, *_rest) = _seed_complete_production_run(connection)
    core = PostgresToptCoreRepository(connection)
    snapshot = core.freeze_snapshot(run_id=run.run_id, release_manifest_id=release_manifest_id)
    assert len(core.materialize(snapshot, gppe_definition=GppeV0Definition(risk_free_rate="0.05"))) == 20
    return run


def test_the_inputs_of_a_served_row_carry_the_values_of_the_payload_table(connection) -> None:
    """#1241: the lineage names the filing (`vintage`) and now also carries the value read from
    it (`inputs`). The oracle is the payload table, read directly. The view must copy the
    whitelisted keys and nothing else, with a key kept absent when the payload lacks it."""
    run = _seed_and_materialize(connection)
    served = connection.execute(
        """
        select item
        from mart.topt_core_meta_info info, jsonb_array_elements(info.lineage) item
        where info.run_id = %s
        """,
        (run.run_id,),
    ).fetchall()
    items = [item for (item,) in served]
    assert len(items) >= 84, f"the run must serve one lineage item per observation, got {len(items)}"
    payloads = dict(
        connection.execute(
            "select observation_id, normalized_payload from staging.capture_observation_payloads "
            "where observation_id = any(%s)",
            ([item["observation_id"] for item in items],),
        ).fetchall()
    )
    assert len(payloads) == len(items)
    for item in items:
        assert set(item) == LINEAGE_KEYS_BEFORE_INPUTS | {"inputs"}, sorted(item)
        payload = payloads[item["observation_id"]]
        expected = {key: payload[key] for key in INPUT_WHITELIST if key in payload}
        assert item["inputs"] == expected, item["observation_id"]
        # Every payload key outside the whitelist stays out: identity, currency, vintage, bar data.
        assert set(item["inputs"]) <= set(INPUT_WHITELIST)

    by_type = {
        semantic: [item for item in items if item["semantic_type"] == semantic]
        for semantic in {item["semantic_type"] for item in items}
    }
    # The values the seeder wrote, as literals: the oracle above reads the same table the view
    # reads, so this pins the numbers themselves.
    assert {item["inputs"]["close"] for item in by_type["market-price"]} == {"40"}
    assert all(set(item["inputs"]) == {"close", "currency", "listing_id"} for item in by_type["market-price"])
    # Identity and membership hold no input. They hold the listing, which the whitelist also names.
    assert all(set(item["inputs"]) == {"listing_id"} for item in by_type["listing-identity"])
    assert all(set(item["inputs"]) == {"listing_id"} for item in by_type["universe-membership"])
    financial = by_type["financial-fact"]
    non_bank = [item for item in financial if item["inputs"]["gross_profit"] is not None]
    assert non_bank, "the seeded run must hold a non-financial issuer"
    for item in non_bank:
        listing_id = payloads[item["observation_id"]]["listing_id"]
        assert item["inputs"] == {
            "gross_profit": "210000000",
            "total_assets": "200000000",
            "headcount": "100",
            "revenue": "100000000",
            "shares_outstanding": "10000000",
            "currency": "USD",
            "listing_id": listing_id,
        }
    # A bank's payload holds gross_profit as JSON null. The key stays, with null: "no value".
    banks = [item for item in financial if item["inputs"]["gross_profit"] is None]
    assert banks, "the seeded run must hold a financial issuer, so a null input is covered"
    assert all("pre_provision_profit" not in item["inputs"] for item in financial)


def test_an_input_the_payload_lacks_stays_absent(connection) -> None:
    """The view never defaults a missing value. A key outside the payload must not appear."""
    run = _seed_and_materialize(connection)
    (item,) = connection.execute(
        """
        select item
        from mart.topt_core_meta_info info, jsonb_array_elements(info.lineage) item
        where info.run_id = %s and item ->> 'semantic_type' = 'market-price'
        limit 1
        """,
        (run.run_id,),
    ).fetchone()
    # `gross_profit` is on the whitelist but not in a price payload: it must stay absent, not null.
    assert "gross_profit" not in item["inputs"]
