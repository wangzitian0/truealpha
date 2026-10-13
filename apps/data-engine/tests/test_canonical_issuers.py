"""#1257: the history projector and the backtest resolve issuers to their canonical entity.

`staging.strategy_backtest_inputs` holds each issuer under two id generations: the legacy
`issuer:lei:` id of the old live seeds and the entity UUID of the seeds since the identity
unification. Old rows never leave the append-only table. Every test here resolves issuers through
the real lineage: minted entities with their aliases, and financial-fact observations under both
generations (`support/financial_lineage_seed.py`). Nothing in the resolution path is stubbed.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
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


def _sessions(first, last):
    from data_engine.datahub.market_prices import is_xnys_trading_day

    return [
        first + timedelta(days=offset)
        for offset in range((last - first).days + 1)
        if is_xnys_trading_day(first + timedelta(days=offset))
    ]


def _seed_split_issuer(connection, *, name: str, cik: int, document: dict, ex_date) -> tuple:
    """One issuer with stored lineage, stored company facts and both price series around a 10:1 split."""
    from datetime import date

    # Minted before every tick these tests use, so the aliases are knowable at each of them.
    issuer = mint_issuer(
        connection,
        lei=f"H1257{name}".ljust(18, "X") + "00",
        mic="XNAS",
        ticker=f"H1257{name}",
        cik=cik,
        known_at=at(2023, 1, 1),
    )
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
    _seed_split_series(
        connection, f"H1257{name}", ex_date=ex_date, sessions=_sessions(date(2024, 1, 2), date(2025, 3, 31))
    )
    return issuer, store


def _shares_at(connection, issuer_id: str, cutoff: str):
    return connection.execute(
        """
        select value, knowable_at from staging.strategy_backtest_inputs
        where issuer_id = %s and cutoff_at = %s and input_key = 'shares_outstanding'
        order by knowable_at desc, recorded_at desc limit 1
        """,
        (issuer_id, cutoff),
    ).fetchone()


def _split_document() -> dict:
    """FY2023's count (filed 2024-02-20) predates the 2024-06-10 split; FY2024's (filed 2025-02-20) is post-split."""
    document = annual_company_facts(base=5000, years=range(2018, 2025))
    for entry in document["facts"]["us-gaap"]["CommonStockSharesOutstanding"]["units"]["shares"]:
        if entry["fy"] == 2024:
            entry["val"] *= 10
    return document


def test_a_share_count_filed_before_a_split_is_put_on_the_basis_of_the_close(connection) -> None:
    """The replay of the staging export found NVDA selected at 2024-06-30: its last close (2024-06-28)
    was after the 10:1 split of 2024-06-10, its share count was filed on 2024-05-29, before it. The
    product understated the market value tenfold. Red on the code before the fix: the projector
    writes the filed count unchanged. After it, the count at a cutoff after the split carries the
    factor, and becomes knowable with the close."""
    from datetime import date

    issuer, store = _seed_split_issuer(
        connection, name="S", cik=1_257_301, document=_split_document(), ex_date=date(2024, 6, 10)
    )

    summary = project_deployed_history(connection, tick=at(2025, 4, 15), months=16, store=store)

    filed = 5_000_000 + 2023
    before = _shares_at(connection, issuer.canonical_issuer_id, "2024-03-31T23:59:59Z")
    after = _shares_at(connection, issuer.canonical_issuer_id, "2024-06-30T23:59:59Z")
    refiled = _shares_at(connection, issuer.canonical_issuer_id, "2025-03-31T23:59:59Z")
    assert before[0] == filed, "before the split, the filed count stands"
    assert after[0] == filed * 10, "after the split, the filed count is put on the basis of the close"
    assert after[1] == datetime(2024, 6, 28, 20, 0, tzinfo=UTC), "the product is knowable at the close's session close"
    assert refiled[0] == (5_000_000 + 2024) * 10, "a count filed after the split is already on the new basis"
    assert summary.split_adjusted > 0


def test_a_second_projection_with_a_split_in_the_window_adds_and_changes_nothing(connection) -> None:
    """Audit item 1: the split-adjusted rows are idempotent. A rerun on the same stored bytes adds no
    row and changes no stored value; the dedupe matches the adjusted value and its knowable_at."""
    from datetime import date

    issuer, store = _seed_split_issuer(
        connection, name="R", cik=1_257_302, document=_split_document(), ex_date=date(2024, 6, 10)
    )
    snapshot = """
        select cutoff_at, input_key, coalesce(fiscal_period, ''), value, knowable_at
        from staging.strategy_backtest_inputs where issuer_id = %s order by 1, 2, 3, 5, 4
    """
    first = project_deployed_history(connection, tick=at(2025, 4, 15), months=16, store=store)
    before = connection.execute(snapshot, (issuer.canonical_issuer_id,)).fetchall()
    second = project_deployed_history(connection, tick=at(2025, 4, 15), months=16, store=store)
    after = connection.execute(snapshot, (issuer.canonical_issuer_id,)).fetchall()

    assert first.split_adjusted > 0, "the window must hold an adjusted count, or this test proves nothing"
    assert second.inserted == 0
    assert after == before


def test_the_split_adjusted_row_wins_over_an_earlier_as_filed_row_in_the_reader(connection) -> None:
    """Audit item 1: the table already holds the as-filed count at the cutoff (an earlier projection
    wrote it), and nothing is ever deleted. The adjusted row is appended with a LATER knowable_at,
    and the gateway the backtest reads takes the latest knowable_at first, so the adjusted row wins."""
    from datetime import date

    from data_engine.strategy_backtest_gateway import StrategyBacktestGateway

    issuer, store = _seed_split_issuer(
        connection, name="W", cik=1_257_303, document=_split_document(), ex_date=date(2024, 6, 10)
    )
    cutoff = "2024-06-30T23:59:59Z"
    filed = 5_000_000 + 2023
    connection.execute(
        """
        insert into staging.strategy_backtest_inputs (issuer_id, cutoff_at, input_key, value, confidence, knowable_at)
        values (%s, %s, 'shares_outstanding', %s, '0.9', '2024-02-20T23:59:59.999999Z')
        """,
        (issuer.canonical_issuer_id, cutoff, filed),
    )

    project_deployed_history(connection, tick=at(2025, 4, 15), months=16, store=store)

    shares = {
        row[1]: row[2]
        for row in StrategyBacktestGateway(connection).rows_at(cutoff)
        if row[0] == issuer.canonical_issuer_id and row[1] == "shares_outstanding"
    }
    assert shares == {"shares_outstanding": filed * 10}


def _share_counts_document(*, cover: tuple | None, balance: tuple | None) -> dict:
    """Income facts through FY2023, plus share counts given as (period end, filed, value).

    The cover count (dei) is as of its own date. The balance-sheet count (us-gaap) is restated
    for a split before the filing is issued (SAB Topic 4C).
    """
    document = annual_company_facts(base=5000, years=range(2018, 2024))

    def entry(end: str, filed: str, value: int) -> dict:
        return {
            "end": end,
            "filed": filed,
            "form": "10-Q",
            "fy": 2024,
            "fp": "Q2",
            "accn": f"0001-{filed}",
            "val": value,
        }

    if balance is not None:
        document["facts"]["us-gaap"]["CommonStockSharesOutstanding"]["units"]["shares"].append(entry(*balance))
    if cover is not None:
        document["facts"]["dei"] = {"EntityCommonStockSharesOutstanding": {"units": {"shares": [entry(*cover)]}}}
    return document


def test_a_cover_count_dated_before_a_split_but_filed_after_it_is_put_on_the_basis_of_the_close(connection) -> None:
    """Audit item 3: the cover count is as of 2024-06-05, the split is 2024-06-10, the filing is
    2024-06-14. The count is pre-split. Red on the code that starts the window at the filing date:
    it misses the split. The window starts at the count's own date for a cover count."""
    from datetime import date

    # One 10-Q filed 2024-06-14: its restated balance-sheet count (2024-04-30) and its cover count (2024-06-05).
    document = _share_counts_document(
        cover=("2024-06-05", "2024-06-14", 7_000_000), balance=("2024-04-30", "2024-06-14", 70_000_000)
    )
    issuer, store = _seed_split_issuer(
        connection, name="C", cik=1_257_304, document=document, ex_date=date(2024, 6, 10)
    )

    project_deployed_history(connection, tick=at(2024, 7, 15), months=6, store=store)

    assert _shares_at(connection, issuer.canonical_issuer_id, "2024-06-30T23:59:59Z")[0] == 70_000_000


def test_a_balance_sheet_count_restated_before_its_filing_is_not_adjusted_twice(connection) -> None:
    """Audit item 3, the other concept: a balance-sheet count as of 2024-04-30 in a filing of
    2024-06-14 is already restated for the split of 2024-06-10. Starting its window at its period
    end would multiply it a second time; its window starts at the filing date."""
    from datetime import date

    # No cover count: the restated balance-sheet count of the 2024-06-14 filing is the latest count.
    document = _share_counts_document(cover=None, balance=("2024-04-30", "2024-06-14", 70_000_000))
    issuer, store = _seed_split_issuer(
        connection, name="B", cik=1_257_305, document=document, ex_date=date(2024, 6, 10)
    )

    project_deployed_history(connection, tick=at(2024, 7, 15), months=6, store=store)

    assert _shares_at(connection, issuer.canonical_issuer_id, "2024-06-30T23:59:59Z")[0] == 70_000_000


def test_two_signed_predecessors_that_disagree_stop_the_resolution(connection, two_generations) -> None:
    """Audit item 2: one entity is named by two legacy ids (its LEI id and its CIK id), and the
    registry, which holds legacy ids only, has a signed predecessor under each with different CIKs.
    Picking the first sorted one would be a silent choice; the resolver refuses with a named error."""
    from data_engine.datahub.strategy_history import ConflictingPredecessorError, canonical_history_issuers

    both = two_generations["both"]
    cik_id = "issuer:cik:0001257101"
    connection.execute(
        """
        insert into staging.entity_aliases (
            entity_id, scheme, value, valid_from, valid_to, transaction_time, source, raw_ref, method, confidence,
            mapping_version
        ) values (%s, 'cik', '0001257101', '-infinity'::date, null, %s, 'test', 'test:cik', 'asserted', 1.0, 'test:v1')
        """,
        (both.canonical_issuer_id, MINTED_AT),
    )
    for issuer_id, cik in ((both.legacy_issuer_id, 1257888), (cik_id, 1257999)):
        connection.execute(
            """
            insert into staging.issuer_cik_predecessors (issuer_id, predecessor_cik, reason, approved_by)
            values (%s, %s, 'test: two signed rows', 'test')
            """,
            (issuer_id, cik),
        )

    with pytest.raises(
        ConflictingPredecessorError, match=r"CIK 1257888 under issuer:lei:.*CIK 1257999 under issuer:cik:"
    ):
        canonical_history_issuers(connection, [both.legacy_issuer_id, cik_id, both.canonical_issuer_id], known_at=TICK)

    # Without the CIK id in the inputs, one signed row is left, and it applies to the canonical issuer.
    agreed = canonical_history_issuers(connection, [both.legacy_issuer_id, both.canonical_issuer_id], known_at=TICK)
    assert agreed.issuers[0].predecessor_cik == 1257888


def test_an_id_with_lineage_but_no_entity_is_never_projected(connection, two_generations) -> None:
    """Audit item 4: a legacy id with its own financial-fact lineage and no entity in the store.
    Red on the code before #1257: lineage alone resolved it, and its history was written under the
    legacy id. Now it is reported unresolved and nothing is written under it."""
    orphan_issuer, orphan_listing = "issuer:lei:H1257ORPHANXXXXXXX00", "listing:xnas:h1257o"
    seed_financial_lineage(
        connection,
        [Observed(orphan_issuer, orphan_listing, 1_257_401, "non_financial", at(2025, 9, 1))],
        company_facts={1_257_401: annual_company_facts(base=6000)},
        store=two_generations["store"],
        cutoff=at(2025, 9, 1),
    )
    connection.execute(
        """
        insert into staging.strategy_backtest_inputs (issuer_id, cutoff_at, input_key, value, confidence, knowable_at)
        values (%s, %s, 'revenue', '1', '0.9', %s)
        """,
        (orphan_issuer, LIVE, LIVE - timedelta(days=1)),
    )

    summary = project_deployed_history(connection, tick=TICK, months=3, store=two_generations["store"])

    assert orphan_issuer in summary.unresolved_issuers
    assert orphan_issuer not in _projected_ids(connection)
    assert summary.issuers == 2, "the two issuers with an entity are still projected"


def test_a_uuid_that_is_not_a_stored_entity_is_never_projected(connection, two_generations) -> None:
    """Audit item 7: `lookup_entity` answers any UUID with itself. A UUID with lineage but no entity
    in the store must not pass as canonical. Red on the resolver without the stored-entity guard."""
    stray = "0f0f0f0f-1257-4257-8257-0f0f0f0f0f0f"
    seed_financial_lineage(
        connection,
        [Observed(stray, "listing:xnas:h1257u", 1_257_402, "non_financial", at(2025, 9, 1))],
        company_facts={1_257_402: annual_company_facts(base=6500)},
        store=two_generations["store"],
        cutoff=at(2025, 9, 1),
    )
    connection.execute(
        """
        insert into staging.strategy_backtest_inputs (issuer_id, cutoff_at, input_key, value, confidence, knowable_at)
        values (%s, %s, 'revenue', '1', '0.9', %s)
        """,
        (stray, LIVE, LIVE - timedelta(days=1)),
    )

    summary = project_deployed_history(connection, tick=TICK, months=3, store=two_generations["store"])

    assert stray in summary.unresolved_issuers
    assert stray not in _projected_ids(connection)


def test_every_symbol_without_its_split_adjusted_series_is_named(connection) -> None:
    """Audit item 6: two symbols lack the split-adjusted series. The error names both, not the first."""
    from datetime import date

    from data_engine.datahub.market_prices import PriceBarRecord, insert_market_prices_daily
    from data_engine.datahub.strategy_history import HistoryIssuer, run_strategy_history
    from factors.backtest.corporate_actions import SplitDerivationError
    from factors.production_topt import OperatingBranch

    issuers = []
    for number, ticker in enumerate(("H1257X", "H1257Y", "H1257Z"), start=1):
        issuers.append(
            HistoryIssuer(
                f"issuer:h1257:{number}",
                "",
                f"listing:xnas:{ticker.lower()}",
                ticker,
                1_257_500 + number,
                OperatingBranch.NON_FINANCIAL,
            )
        )
        bars = [
            PriceBarRecord(ticker, day, Decimal(10), Decimal(10), Decimal(10), Decimal(10), Decimal(1))
            for day in _sessions(date(2024, 5, 1), date(2024, 6, 28))
        ]
        insert_market_prices_daily(connection, bars, adjust="none")
        if ticker == "H1257Z":
            insert_market_prices_daily(connection, bars, adjust="splits")
    documents = {issuer.cik: annual_company_facts(base=7000 + issuer.cik % 10) for issuer in issuers}

    with pytest.raises(SplitDerivationError) as raised:
        run_strategy_history(
            connection,
            tick=at(2024, 7, 15),
            issuers=issuers,
            months=2,
            load_facts=lambda cik: (json.dumps(documents[cik]).encode(), documents[cik]),
        )

    assert "H1257X" in str(raised.value) and "H1257Y" in str(raised.value)
    assert "H1257Z" not in str(raised.value)
