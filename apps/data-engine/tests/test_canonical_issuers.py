"""#1257: the history projector and the backtest resolve issuers to their canonical entity.

`staging.strategy_backtest_inputs` holds each issuer under two id generations: the legacy
`issuer:lei:` id of the old live seeds and the entity UUID of the seeds since the identity
unification. Old rows never leave the append-only table. Every test here resolves issuers through
the real lineage: minted entities with their aliases, and financial-fact observations under both
generations (`support/financial_lineage_seed.py`). Nothing in the resolution path is stubbed.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from data_engine.datahub.strategy_history import project_deployed_history

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
from financial_lineage_seed import (  # noqa: E402
    MemoryStore,
    Observed,
    annual_company_facts,
    at,
    mint_issuer,
    seed_financial_lineage,
)

TICK = datetime(2026, 10, 9, 6, 15, tzinfo=UTC)
MINTED_AT = at(2025, 1, 1)
LIVE = at(2025, 9, 2)


@pytest.fixture
def connection():
    from truealpha_runtime.testing import guard_db_write

    url = guard_db_write()
    try:
        active = psycopg.connect(url, connect_timeout=3, autocommit=False)
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    try:
        yield active
    finally:
        active.rollback()
        active.close()


@pytest.fixture
def two_generations(connection):
    """Issuer "both" under its legacy id and its UUID; issuer "legacy_only" under its legacy id only."""
    both = mint_issuer(
        connection, lei="H1257BOTHXXXXXXXXX00", mic="XNAS", ticker="H1257A", cik=1_257_101, known_at=MINTED_AT
    )
    legacy_only = mint_issuer(
        connection, lei="H1257LEGACYXXXXXXX00", mic="XNAS", ticker="H1257B", cik=1_257_102, known_at=MINTED_AT
    )
    store = MemoryStore()
    seed_financial_lineage(
        connection,
        [
            Observed(both.legacy_issuer_id, both.legacy_listing_id, both.cik, "non_financial", at(2025, 9, 1)),
            Observed(both.canonical_issuer_id, both.canonical_listing_id, both.cik, "non_financial", at(2026, 9, 25)),
            Observed(
                legacy_only.legacy_issuer_id,
                legacy_only.legacy_listing_id,
                legacy_only.cik,
                "non_financial",
                at(2025, 9, 1),
            ),
        ],
        company_facts={both.cik: annual_company_facts(base=3000), legacy_only.cik: annual_company_facts(base=4000)},
        store=store,
        cutoff=at(2026, 9, 25),
    )
    connection.execute("delete from staging.strategy_backtest_inputs")
    for issuer_id in (both.legacy_issuer_id, both.canonical_issuer_id, legacy_only.legacy_issuer_id):
        connection.execute(
            """
            insert into staging.strategy_backtest_inputs (issuer_id, cutoff_at, input_key, value, confidence, knowable_at)
            values (%s, %s, 'revenue', '1', '0.9', %s)
            """,
            (issuer_id, LIVE, LIVE - timedelta(days=1)),
        )
    return {"both": both, "legacy_only": legacy_only, "store": store}


def _projected_ids(connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "select distinct issuer_id from staging.strategy_backtest_inputs where cutoff_at <> %s", (LIVE,)
        ).fetchall()
    }


def test_the_projector_writes_history_under_canonical_ids_only(connection, two_generations) -> None:
    """#1257 (b). Red on the code before the fix: the projector writes a full history for each of
    the three ids, two of them legacy. After it: one history per canonical entity, and the
    legacy-only issuer's history lands under its canonical id."""
    both, legacy_only = two_generations["both"], two_generations["legacy_only"]

    summary = project_deployed_history(connection, tick=TICK, months=3, store=two_generations["store"])

    assert _projected_ids(connection) == {both.canonical_issuer_id, legacy_only.canonical_issuer_id}
    assert summary.issuers == 2
    assert summary.unresolved_issuers == ()


def test_a_rerun_of_the_projector_writes_no_legacy_row_either(connection, two_generations) -> None:
    """The duplicate was self-renewing: each weekly run re-read the legacy ids. Two runs write none."""
    store = two_generations["store"]
    project_deployed_history(connection, tick=TICK, months=3, store=store)
    second = project_deployed_history(connection, tick=TICK, months=3, store=store)

    assert second.inserted == 0, "a rerun on the same stored bytes adds no row"
    assert all(not issuer_id.startswith("issuer:") for issuer_id in _projected_ids(connection))


def test_a_legacy_id_that_names_no_entity_is_reported_and_never_projected(connection, two_generations) -> None:
    orphan = "issuer:lei:H1257ORPHANXXXXXXX00"
    connection.execute(
        """
        insert into staging.strategy_backtest_inputs (issuer_id, cutoff_at, input_key, value, confidence, knowable_at)
        values (%s, %s, 'revenue', '1', '0.9', %s)
        """,
        (orphan, LIVE, LIVE - timedelta(days=1)),
    )

    summary = project_deployed_history(connection, tick=TICK, months=3, store=two_generations["store"])

    assert orphan in summary.unresolved_issuers
    assert orphan not in _projected_ids(connection)


def test_a_signed_predecessor_keyed_by_the_legacy_id_still_applies_to_the_canonical_issuer(
    connection, two_generations
) -> None:
    """The owner-signed registry predates the entity UUIDs (ExxonMobil's row names `issuer:lei:`).
    Canonicalization must not lose it: every id of the entity is presented to the registry."""
    from data_engine.datahub.strategy_history import canonical_history_issuers

    both = two_generations["both"]
    connection.execute(
        """
        insert into staging.issuer_cik_predecessors (issuer_id, predecessor_cik, reason, approved_by)
        values (%s, 1257999, 'test: holdco reorganization', 'test')
        """,
        (both.legacy_issuer_id,),
    )

    resolved = canonical_history_issuers(connection, [both.legacy_issuer_id, both.canonical_issuer_id], known_at=TICK)

    (issuer,) = resolved.issuers
    assert issuer.issuer_id == both.canonical_issuer_id
    assert (issuer.predecessor_cik, issuer.predecessor_signed) == (1257999, True)
    assert resolved.merged == {both.legacy_issuer_id: both.canonical_issuer_id}
    assert resolved.canonical_ids == frozenset({both.canonical_issuer_id})


# -- #1257 replay finding: a filed share count and a later close must share one share basis -----


def _seed_split_series(connection, ticker: str, *, ex_date, sessions) -> None:
    """Two stored series of one symbol with a 10-for-1 split on ``ex_date``."""
    from decimal import Decimal

    from data_engine.datahub.market_prices import PriceBarRecord, insert_market_prices_daily

    unadjusted, adjusted = [], []
    for index, day in enumerate(sessions):
        close = Decimal(1000) * (1 + Decimal("0.001") * index)
        close = close / 10 if day >= ex_date else close
        factor = Decimal(10) if day < ex_date else Decimal(1)
        unadjusted.append(PriceBarRecord(ticker, day, close, close, close, close, Decimal(1000)))
        adjusted.append(
            PriceBarRecord(ticker, day, close / factor, close / factor, close / factor, close / factor, Decimal(1000))
        )
    insert_market_prices_daily(connection, unadjusted, adjust="none")
    insert_market_prices_daily(connection, adjusted, adjust="splits")


def test_a_share_count_filed_before_a_split_is_put_on_the_basis_of_the_close(connection) -> None:
    """The replay of the staging export found NVDA selected at 2024-06-30: its last close (2024-06-28)
    was after the 10:1 split of 2024-06-10, its share count was filed on 2024-05-29, before it. The
    product understated the market value tenfold. Red on the code before the fix: the projector
    writes the filed count unchanged. After it, the count at a cutoff after the split carries the
    factor, and becomes knowable with the close."""
    from datetime import date

    from data_engine.datahub.market_prices import is_xnys_trading_day

    issuer = mint_issuer(
        connection, lei="H1257SPLITXXXXXXXX00", mic="XNAS", ticker="H1257S", cik=1_257_301, known_at=MINTED_AT
    )
    document = annual_company_facts(base=5000, years=range(2018, 2025))
    # FY2024's count, filed 2025-02-20, is already on the post-split basis.
    for entry in document["facts"]["us-gaap"]["CommonStockSharesOutstanding"]["units"]["shares"]:
        if entry["fy"] == 2024:
            entry["val"] *= 10
    store = MemoryStore()
    seed_financial_lineage(
        connection,
        [
            Observed(
                issuer.canonical_issuer_id, issuer.canonical_listing_id, issuer.cik, "non_financial", at(2024, 3, 1)
            )
        ],
        company_facts={issuer.cik: document},
        store=store,
        cutoff=at(2024, 3, 1),
    )
    connection.execute("delete from staging.strategy_backtest_inputs")
    connection.execute(
        """
        insert into staging.strategy_backtest_inputs (issuer_id, cutoff_at, input_key, value, confidence, knowable_at)
        values (%s, '2024-03-02T00:00:00Z', 'revenue', '1', '0.9', '2024-03-01T00:00:00Z')
        """,
        (issuer.canonical_issuer_id,),
    )
    first, last = date(2024, 1, 2), date(2025, 3, 31)
    sessions = [first + timedelta(days=offset) for offset in range((last - first).days + 1)]
    _seed_split_series(
        connection, "H1257S", ex_date=date(2024, 6, 10), sessions=[d for d in sessions if is_xnys_trading_day(d)]
    )

    summary = project_deployed_history(connection, tick=at(2025, 4, 15), months=16, store=store)

    def shares_at(cutoff: str):
        return connection.execute(
            """
            select value, knowable_at from staging.strategy_backtest_inputs
            where issuer_id = %s and cutoff_at = %s and input_key = 'shares_outstanding'
            order by knowable_at desc, recorded_at desc limit 1
            """,
            (issuer.canonical_issuer_id, cutoff),
        ).fetchone()

    filed = 5_000_000 + 2023
    before, after, refiled = (
        shares_at("2024-03-31T23:59:59Z"),
        shares_at("2024-06-30T23:59:59Z"),
        shares_at("2025-03-31T23:59:59Z"),
    )
    assert before[0] == filed, "before the split, the filed count stands"
    assert after[0] == filed * 10, "after the split, the filed count is put on the basis of the close"
    assert after[1] == datetime(2024, 6, 28, 20, 0, tzinfo=UTC), "the product is knowable at the close's session close"
    assert refiled[0] == (5_000_000 + 2024) * 10, "a count filed after the split is already on the new basis"
    assert summary.split_adjusted > 0
