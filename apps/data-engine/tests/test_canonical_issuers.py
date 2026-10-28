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
from cover_fixture import plain_share_ratio  # noqa: E402
from financial_lineage_seed import (  # noqa: E402
    MemoryStore,
    Observed,
    ShareClass,
    annual_company_facts,
    at,
    backfill_issuer,
    mint_issuer,
    seed_financial_lineage,
    seed_tick,
)

TICK = datetime(2026, 10, 9, 6, 15, tzinfo=UTC)
MINTED_AT = at(2025, 1, 1)
LIVE = at(2025, 9, 2)


@pytest.fixture(autouse=True)
def _plain_share_cover_for_history(monkeypatch: pytest.MonkeyPatch) -> None:
    import data_engine.datahub.strategy_history as history

    monkeypatch.setattr(history, "CoverRatioReader", lambda: plain_share_ratio)


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
    """The duplicate was self-renewing: each run re-read the legacy ids. Two runs write none."""
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
    # Review round 3, item 2: every count here is a us-gaap balance-sheet count, so each is a statement count.
    assert summary.share_basis_statement > 0
    assert (summary.share_basis_cover, summary.share_basis_unrecognised) == (0, 0)


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


# -- review round 3: which concept a share count came from, and how often each rule fires ------


def _project_shares(document: dict, *, ruleset=None):
    """Project one issuer at the 2024-06-30 quarter end with a 10:1 split on 2024-06-10. No database.

    Returns (the shares row's value, the projection). The close is the 2024-06-28 session.
    """
    from datetime import date

    from data_engine.datahub.production_topt.concept_mapping import DEFAULT_RULESET
    from data_engine.datahub.strategy_history import HistoryIssuer, LastClose, project_issuer_rows
    from factors.backtest.corporate_actions import SplitEvent
    from factors.production_topt import OperatingBranch

    issuer = HistoryIssuer(
        "issuer:h1257:basis", "", "listing:xnas:h1257v", "H1257V", 1_257_701, OperatingBranch.NON_FINANCIAL
    )
    close = LastClose(Decimal("120"), Decimal("0.9"), datetime(2024, 6, 28, 20, 0, tzinfo=UTC))
    projection = project_issuer_rows(
        issuer,
        [datetime(2024, 6, 30, 23, 59, 59, tzinfo=UTC)],
        load_facts=lambda _cik: (json.dumps(document).encode(), document),
        headcount_extractor=lambda _cik, _as_of: None,
        last_close=lambda _symbol, _cutoff: close,
        split_events=lambda _symbol: (SplitEvent("H1257V", date(2024, 6, 10), 10, 1),),
        ruleset=ruleset if ruleset is not None else DEFAULT_RULESET,
    )
    (shares,) = [row.value for row in projection.rows if row.input_key == "shares_outstanding"]
    return shares, projection


def _annual_report_document(*, cover_val: int, balance_val: int) -> dict:
    """A 20-F for the year to 2024-03-31, filed 2024-06-14, after the 2024-06-10 split.

    Its cover count and its balance-sheet count share one date and one accession (NICE's shape).
    The cover count is as of 2024-03-31, so pre-split; the balance-sheet count is restated.
    """
    document = annual_company_facts(base=5000, years=range(2018, 2024))
    del document["facts"]["us-gaap"]["CommonStockSharesOutstanding"]
    common = {
        "end": "2024-03-31",
        "filed": "2024-06-14",
        "form": "20-F",
        "fy": 2024,
        "fp": "FY",
        "accn": "0001-nice-20f",
    }
    document["facts"]["us-gaap"]["CommonStockSharesOutstanding"] = {
        "units": {"shares": [{**common, "val": balance_val}]}
    }
    document["facts"]["dei"] = {
        "EntityCommonStockSharesOutstanding": {"units": {"shares": [{**common, "val": cover_val}]}}
    }
    return document


def _statement_first_ruleset():
    """The governed ruleset with the share-count synonyms in the other order: us-gaap before dei.

    A pointer advance can publish such a ruleset with no deploy, so the code must not rely on the order.
    """
    from data_engine.datahub.production_topt.concept_mapping import DEFAULT_RULESET
    from truealpha_contracts.concept_mapping import ConceptMappingRuleset

    data = DEFAULT_RULESET.model_dump(mode="json")
    for mapping in data["mappings"]:
        if mapping["field"] == "shares_outstanding":
            mapping["concepts"] = list(reversed(mapping["concepts"]))
    data["ruleset_id"] = ""
    data.pop("content_sha256", None)
    return ConceptMappingRuleset.model_validate(data)


def test_a_restated_balance_count_beside_a_cover_count_of_one_date_is_not_classed_as_cover() -> None:
    """Review round 3, item 1: with us-gaap listed first, the restated balance-sheet count (70,000,000)
    wins the shared date. Matching on accession and period end alone finds the cover entry beside it
    and classes the count as a cover count: 700,000,000. Red until the match also needs the value."""
    shares, projection = _project_shares(
        _annual_report_document(cover_val=7_000_000, balance_val=70_000_000), ruleset=_statement_first_ruleset()
    )

    assert shares == 70_000_000
    assert (projection.share_basis_cover, projection.share_basis_statement) == (0, 1)


def test_a_cover_count_beside_a_balance_count_of_one_date_is_put_on_the_basis_of_the_close() -> None:
    """The same 20-F under the deployed order (dei first): the pre-split cover count wins the date
    and is put on the basis of the post-split close."""
    shares, projection = _project_shares(_annual_report_document(cover_val=7_000_000, balance_val=70_000_000))

    assert shares == 70_000_000
    assert (projection.share_basis_cover, projection.share_basis_statement) == (1, 0)


def test_a_weighted_average_fallback_count_is_on_the_basis_of_its_filing() -> None:
    """Review round 3, item 3a: no point-in-time count at all, so the weighted average answers. It is a
    statement figure, restated for the split before its 2024-06-14 filing: it is not adjusted again."""
    document = annual_company_facts(base=5000, years=range(2018, 2024))
    del document["facts"]["us-gaap"]["CommonStockSharesOutstanding"]
    document["facts"]["us-gaap"]["WeightedAverageNumberOfSharesOutstandingBasic"] = {
        "units": {
            "shares": [
                {
                    "start": "2023-04-01",
                    "end": "2024-03-31",
                    "filed": "2024-06-14",
                    "form": "10-K",
                    "fy": 2024,
                    "fp": "FY",
                    "accn": "0001-wavg",
                    "val": 70_000_000,
                }
            ]
        }
    }

    shares, projection = _project_shares(document)

    assert shares == 70_000_000
    assert (projection.share_basis_cover, projection.share_basis_statement, projection.share_basis_unrecognised) == (
        0,
        1,
        0,
    )


def test_a_cover_count_dated_on_the_ex_date_already_holds_the_split() -> None:
    """Review round 3, item 3b: a cover count as of 2024-06-10, the ex-date, counts the post-split
    shares. The window starts after the count's date, so the split is not applied a second time."""
    document = annual_company_facts(base=5000, years=range(2018, 2024))
    document["facts"]["dei"] = {
        "EntityCommonStockSharesOutstanding": {
            "units": {
                "shares": [
                    {
                        "end": "2024-06-10",
                        "filed": "2024-06-14",
                        "form": "10-Q",
                        "fy": 2024,
                        "fp": "Q2",
                        "accn": "0001-exdate",
                        "val": 70_000_000,
                    }
                ]
            }
        }
    }

    shares, projection = _project_shares(document)

    assert shares == 70_000_000
    assert projection.share_basis_cover == 1


def test_a_count_that_matches_no_stored_entry_falls_back_to_its_filing_date_and_is_counted() -> None:
    """Review round 3, item 2: a vintage whose accession names no stored entry cannot be classed. It
    falls back to the filing date, the rule that can under-adjust, and the summary counts it."""
    document = _annual_report_document(cover_val=7_000_000, balance_val=70_000_000)
    for concept in (
        document["facts"]["dei"]["EntityCommonStockSharesOutstanding"],
        document["facts"]["us-gaap"]["CommonStockSharesOutstanding"],
    ):
        concept["units"]["shares"][0]["accn"] = None

    _shares, projection = _project_shares(document)

    assert (projection.share_basis_cover, projection.share_basis_statement, projection.share_basis_unrecognised) == (
        0,
        0,
        1,
    )


def test_the_history_lane_reports_how_each_share_count_was_classed(monkeypatch) -> None:
    """Review round 3, item 2: the lane's reported summary carries the three counts, so a run shows
    how often the filing-date fallback, which can under-adjust, fires."""
    import dagster as dg
    from data_engine.datahub.strategy_history import HistorySummary
    from data_engine.lanes import strategy_history as lane

    summary = HistorySummary(
        issuers=20,
        cutoffs=48,
        inserted=0,
        already_present=0,
        no_outcome=0,
        undated=0,
        share_basis_cover=31,
        share_basis_statement=12,
        share_basis_unrecognised=5,
    )

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def commit(self) -> None:
            return None

    monkeypatch.setattr(lane.psycopg, "connect", lambda *_args, **_kwargs: _Connection())
    monkeypatch.setattr(lane, "project_deployed_history", lambda *_args, **_kwargs: summary)
    context = dg.build_op_context()

    lane.project_strategy_history_op(context, lane.StrategyHistoryConfig(executed_at="2026-10-11T10:07:00+00:00"))

    metadata = context.get_output_metadata("result")
    assert (metadata["share_basis_cover"], metadata["share_basis_statement"], metadata["share_basis_unrecognised"]) == (
        31,
        12,
        5,
    )


# -- #1347: the primary share class binds the ticker, whatever the observation ids ------------------

#: Alphabet as the universe corpus keys it: Class A trades as GOOGL (the primary class), Class C as GOOG.
ALPHABET_CLASSES = (ShareClass("GOOGL", "02079K305"), ShareClass("GOOG", "02079K107"))
#: The legacy capture that the entity backfill reads. Each later tick writes both listings at one instant.
BACKFILLED_AT = at(2025, 9, 1)
LEGACY_TICK = at(2025, 9, 20)
UUID_TICK = at(2026, 9, 25)


@pytest.fixture
def alphabet(connection):
    """Alphabet, minted by `staging.entity_backfill()` from its legacy ids, as production minted it."""
    store = MemoryStore()
    issuer = backfill_issuer(
        connection,
        lei="5493006MHB84DD0ZWV18",
        mic="XNAS",
        classes=ALPHABET_CLASSES,
        cik=1_652_044,
        known_at=BACKFILLED_AT,
        store=store,
    )
    return issuer, store


def _bound(connection, ids: list[str]) -> list[tuple[str, str]]:
    """The (issuer id, ticker) pairs that the backtest and the projector bind for `ids`."""
    from data_engine.datahub.strategy_history import canonical_history_issuers

    return [(item.issuer_id, item.ticker) for item in canonical_history_issuers(connection, ids, known_at=TICK).issuers]


def _preferred_listing(connection, entity_id: str, ids: list[str]) -> str:
    """The listing of the observation that `latest_financial_observations` selects for the entity."""
    from data_engine.datahub.strategy_history import latest_financial_observations

    found = latest_financial_observations(connection, {entity_id: ids}, known_at=TICK)
    return found[entity_id][0]["listing_id"]


@pytest.mark.parametrize("larger_id", ["GOOGL", "GOOG"])
def test_one_tick_of_uuid_listings_binds_the_primary_ticker_for_either_observation_id_order(
    connection, alphabet, larger_id: str
) -> None:
    """#1347. Production holds only entity UUIDs at the backtest cutoffs. One tick wrote both listings of
    Alphabet with one recorded_at and one knowable_at, so the old order fell to observation_id. The run
    of 2026-10-15 bound GOOG, which has no price bar. Red on the code before the fix when GOOG holds the
    larger observation id. The listing ids come from the entity backfill, as on production."""
    issuer, store = alphabet
    seed_tick(connection, issuer, legacy=False, knowable_at=UUID_TICK, larger_id_ticker=larger_id, store=store)
    ties = connection.execute(
        """
        select count(*), count(distinct (o.recorded_at, o.knowable_at))
        from staging.capture_normalized_observations o
        join staging.capture_observation_payloads p on p.observation_id = o.observation_id
        where p.normalized_payload ->> 'issuer_id' = %s
        """,
        (issuer.canonical_issuer_id,),
    ).fetchone()
    assert ties == (2, 1), "the two listings must tie on recorded_at and knowable_at, or this test proves nothing"

    assert _bound(connection, [issuer.canonical_issuer_id]) == [(issuer.canonical_issuer_id, "GOOGL")]
    assert (
        _preferred_listing(connection, issuer.canonical_issuer_id, [issuer.canonical_issuer_id])
        == issuer.listing("GOOGL").canonical_listing_id
    )


def test_a_newer_observation_of_the_secondary_class_does_not_displace_the_primary_class(connection, alphabet) -> None:
    """A later tick writes GOOG alone, so GOOG holds the newest observation of the issuer. The ticker decides
    before the age of an observation. Red on the code before the fix, and on an order that puts the age
    of an observation before the ticker."""
    issuer, store = alphabet
    seed_tick(connection, issuer, legacy=False, knowable_at=UUID_TICK, larger_id_ticker="GOOGL", store=store)
    goog, later = issuer.listing("GOOG"), UUID_TICK + timedelta(days=1)
    seed_financial_lineage(
        connection,
        [
            Observed(
                issuer.canonical_issuer_id,
                goog.canonical_listing_id,
                issuer.cik,
                "non_financial",
                later,
                instrument_id=goog.canonical_instrument_id,
            )
        ],
        company_facts={issuer.cik: {"facts": {}}},
        store=store,
        cutoff=later,
    )

    assert _bound(connection, [issuer.canonical_issuer_id]) == [(issuer.canonical_issuer_id, "GOOGL")]
    assert (
        _preferred_listing(connection, issuer.canonical_issuer_id, [issuer.canonical_issuer_id])
        == issuer.listing("GOOGL").canonical_listing_id
    )


@pytest.mark.parametrize("larger_id", ["GOOGL", "GOOG"])
def test_one_tick_of_legacy_listings_binds_the_primary_ticker_for_either_observation_id_order(
    connection, alphabet, larger_id: str
) -> None:
    """The old generation spells the ticker in its listing id (`listing:xnas:googl`). Staging still holds
    rows under it at the backtest cutoffs."""
    issuer, store = alphabet
    seed_tick(connection, issuer, legacy=True, knowable_at=LEGACY_TICK, larger_id_ticker=larger_id, store=store)

    assert _bound(connection, [issuer.legacy_issuer_id]) == [(issuer.canonical_issuer_id, "GOOGL")]
    assert (
        _preferred_listing(connection, issuer.canonical_issuer_id, [issuer.legacy_issuer_id])
        == issuer.listing("GOOGL").legacy_listing_id
    )


@pytest.mark.parametrize("larger_id", ["GOOGL", "GOOG"])
def test_both_generations_bind_the_primary_ticker_and_the_newest_observation_of_it(
    connection, alphabet, larger_id: str
) -> None:
    """Staging holds both generations at one cutoff. The ticker selects the class. Inside one ticker the
    newest observation wins: here the UUID tick, which landed after the legacy tick."""
    issuer, store = alphabet
    seed_tick(connection, issuer, legacy=True, knowable_at=LEGACY_TICK, larger_id_ticker=larger_id, store=store)
    seed_tick(connection, issuer, legacy=False, knowable_at=UUID_TICK, larger_id_ticker=larger_id, store=store)
    ids = [issuer.legacy_issuer_id, issuer.canonical_issuer_id]

    assert _bound(connection, ids) == [(issuer.canonical_issuer_id, "GOOGL")]
    assert (
        _preferred_listing(connection, issuer.canonical_issuer_id, ids) == issuer.listing("GOOGL").canonical_listing_id
    )
