"""`mart.topt_core_meta_info` projects the vintage from the payload table (#530 item 4).

20260908T1014 added `'vintage', observation.payload -> 'vintage'` so a served number could
name its filing, and read the wrong column. `capture_normalized_observations.payload` is the
observation ENVELOPE — no business field is in it — so the expression was always NULL and no
join objected. Measured on the 2026-09-09 governed TOPT head: 0 of 84 lineage items carried a
vintage while the payload table had one on every financial-fact observation.

These assert the two halves of that: the envelope really does lack business fields (so a
future reader does not "fix" this back), and the view really does read the payload table.
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
    try:
        active = psycopg.connect(settings.database_url, connect_timeout=3, autocommit=True)
    except psycopg.OperationalError as error:
        # The repository's convention, which this file did not follow when it landed
        # (review on #798): an UNCONDITIONAL skip silently drops this coverage in CI, where
        # a Postgres service exists and a connection failure is a real failure. A guard that
        # cannot go red where production runs is the shape AGENTS.md rule 7 forbids.
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    try:
        yield active
    finally:
        active.close()


@pytest.fixture
def seeded_connection():
    """A transaction for tests that seed a run. The rollback removes every seeded row."""
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


def test_the_envelope_carries_no_business_fields(seeded_connection) -> None:
    """Why the wrong column was silent, pinned as a fact rather than left as a comment. If
    the envelope ever gains business fields this test goes red and the reasoning above needs
    revisiting — which is the point. The run is seeded, so the test examines 21 envelopes in
    every database, including the empty one CI starts with."""
    (_, run, *_rest) = _seed_complete_production_run(seeded_connection)
    envelopes = seeded_connection.execute(
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


def test_the_vintage_in_the_payload_table_reaches_the_lineage_of_the_served_row(seeded_connection) -> None:
    """The other half: a vintage written to the payload table reaches `mart.topt_core_meta_info`.
    The seeded financial-fact payloads carry a vintage, so a green projection test cannot be
    green because nothing has a vintage at all. The adapter side is covered by
    test_sec_financial_adapter.py: `test_the_bundle_names_the_filing_behind_each_input` pins the
    entry shape and `test_the_headcounts_evidence_travels_on_the_row` pins the payload."""
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
    (_, run, _, release_manifest_id, *_rest) = _seed_complete_production_run(
        seeded_connection, financial_vintage=vintage
    )
    core = PostgresToptCoreRepository(seeded_connection)
    snapshot = core.freeze_snapshot(run_id=run.run_id, release_manifest_id=release_manifest_id)
    assert len(core.materialize(snapshot, gppe_definition=GppeV0Definition(risk_free_rate="0.05"))) == 20

    lineages = seeded_connection.execute(
        "select lineage from mart.topt_core_meta_info where run_id = %s", (run.run_id,)
    ).fetchall()
    items = [item for (lineage,) in lineages for item in lineage]
    financial = [item for item in items if item["semantic_type"] == "financial-fact"]
    assert len(financial) == 21, "every listing's financial-fact observation must appear in the lineage"
    assert all(item["vintage"] == vintage for item in financial)
    sample = next(iter(financial[0]["vintage"].values()))
    assert {"accession", "form", "filed"} <= set(sample), f"a vintage entry must name its filing: {sample}"
    assert all(item["vintage"] is None for item in items if item["semantic_type"] != "financial-fact")
