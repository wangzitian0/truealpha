"""#495 (1/3): mart.entity_display_resolution — migration 0033.

The member table is content-addressed with a full dependency spine (snapshot →
capture run → release manifest), so synthetic rows cannot be inserted directly;
this test reuses the SAME seeding machinery as
production_topt/test_materialization.py to produce a real frozen snapshot
(20 members, one per issuer in the frozen corpus), then asserts the view's
contract against it:

  * exactly one row per issuer, 20/20, every ticker non-null and parsed from
    `listing:<mic>:<symbol>` uppercased (issue #495 acceptance #1, corrected
    denominator — see the 2026-07-27 issue comment);
  * display_name is NULL without a KG entity (staging.kg_entities is empty in
    production today) and fills through the LEFT JOIN when one exists;
  * mart_readonly can select the view but still cannot touch staging directly
    (view-owner permission semantics keep the consumer boundary intact).

Skips without a local Postgres; ci-python/ci-db run it against the migrated
service container. Everything rolls back — nothing persists.
"""

import os
import sys
from pathlib import Path

import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub.capture_core import PostgresToptCoreRepository

sys.path.insert(0, str(Path(__file__).parent))
from capture_core.test_materialization import _seed_complete_production_run  # noqa: E402


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


@pytest.fixture
def frozen_members(connection):
    seeded = _seed_complete_production_run(connection)
    run, release_manifest_id = seeded[1], seeded[3]
    repository = PostgresToptCoreRepository(connection)
    snapshot = repository.freeze_snapshot(run_id=run.run_id, release_manifest_id=release_manifest_id)
    return connection, snapshot


def test_view_resolves_every_corpus_issuer_with_non_null_ticker(frozen_members) -> None:
    connection, snapshot = frozen_members
    rows = connection.execute(
        "select issuer_id, listing_id, ticker, display_name from mart.entity_display_resolution"
    ).fetchall()

    corpus_issuers = {member.issuer_id for member in snapshot.members}
    assert len(corpus_issuers) == 20, "frozen corpus: 20 issuers (21 members — Alphabet lists twice)"
    assert len(rows) == len(corpus_issuers), "exactly one view row per corpus issuer (20/20)"
    assert {issuer for issuer, *_ in rows} == corpus_issuers, "every issuer resolves, none invented"
    for issuer_id, listing_id, ticker, _display in rows:
        expected = listing_id.split(":", 2)[2].upper()
        assert ticker == expected and ticker, f"{issuer_id}: ticker {ticker!r} != parsed {expected!r}"


def test_display_name_is_null_without_alias_and_fills_through_the_join(frozen_members) -> None:
    from factors.shared import entity_resolution as er

    connection, snapshot = frozen_members
    member = snapshot.members[0]
    issuer, ticker = member.issuer_id, member.listing_id.split(":", 2)[2].upper()

    before = connection.execute(
        "select display_name from mart.entity_display_resolution where issuer_id = %s", (issuer,)
    ).fetchone()
    assert before == (None,), "name alias is empty in this transaction — name must be NULL, not invented"

    issuer_uuid = er.ensure_entity(connection, issuer, "issuer", "")
    connection.execute(
        "insert into staging.entity_aliases "
        "(entity_id, scheme, value, valid_from, transaction_time, source, raw_ref, method, confidence, mapping_version) "
        "values (%s::uuid, 'name', %s, '-infinity', '2026-06-01T00:00:00Z', 'test', 'raw:0', 'asserted', 0.8, 'v1')",
        (issuer_uuid, ticker),
    )
    bare_ticker = connection.execute(
        "select display_name from mart.entity_display_resolution where issuer_id = %s", (issuer,)
    ).fetchone()
    assert bare_ticker == (None,), "a bare ticker in entity_aliases(name) must not masquerade as a company name"

    connection.execute(
        "insert into staging.entity_aliases "
        "(entity_id, scheme, value, valid_from, transaction_time, source, raw_ref, method, confidence, mapping_version) "
        "values (%s::uuid, 'name', %s, '-infinity', '2026-06-02T00:00:00Z', 'test', 'raw:1', 'asserted', 0.99, 'v1')",
        (issuer_uuid, "Resolution Probe Co"),
    )
    after = connection.execute(
        "select display_name from mart.entity_display_resolution where issuer_id = %s", (issuer,)
    ).fetchone()
    assert after == ("Resolution Probe Co",)


def test_mart_readonly_reads_the_view_but_not_staging(frozen_members) -> None:
    connection, _snapshot = frozen_members
    connection.execute("set local role mart_readonly")
    row = connection.execute("select issuer_id, ticker from mart.entity_display_resolution limit 1").fetchone()
    assert row is not None and row[1]
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        connection.execute("select 1 from staging.core_snapshot_members limit 1")
