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

import psycopg
import pytest
from data_engine.config import settings

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
    """Why the wrong column was silent, pinned as a fact rather than left as a comment. If
    the envelope ever gains business fields this test goes red and the reasoning above needs
    revisiting — which is the point."""
    row = connection.execute(
        "select payload from staging.capture_normalized_observations where semantic_type = 'financial-fact' limit 1"
    ).fetchone()
    if row is None:
        pytest.skip("no financial-fact observation in this database")
    keys = set(row[0])
    assert ENVELOPE_KEYS <= keys, f"the envelope's shape changed: {sorted(keys)}"
    assert "vintage" not in keys, "the envelope gained a vintage; the projection should read it directly"
    for business_field in ("revenue", "gross_profit", "total_assets", "headcount"):
        assert business_field not in keys, (
            f"the envelope now carries {business_field!r}; this test's premise needs revisiting"
        )


def test_a_financial_observation_has_a_vintage_in_the_payload_table(connection) -> None:
    """The other half: the data the view must reach is really there, so a green projection
    test cannot be green because nothing has a vintage at all."""
    row = connection.execute(
        """
        select p.normalized_payload -> 'vintage'
        from staging.capture_normalized_observations o
        join staging.capture_observation_payloads p using (observation_id)
        where o.semantic_type = 'financial-fact'
          and p.normalized_payload ? 'vintage'
        limit 1
        """
    ).fetchone()
    if row is None:
        pytest.skip("no financial-fact observation carries a vintage in this database (pre-parser-v9)")
    vintage = row[0]
    assert isinstance(vintage, dict) and vintage, "a vintage must name at least one input"
    sample = next(iter(vintage.values()))
    assert {"accession", "form", "filed"} <= set(sample), f"a vintage entry must name its filing: {sample}"
