"""One producer of the primary listing: every engine and both views follow it (#1518, step 2).

The deployed Dagster op `run_topt_live_tick` runs end to end. The vendor answers are offline and pass
through the deployed adapters. The capture, the coordinate resolver, the snapshot freeze, the core
materialization, the strategy seed and the strategy replay are real.

Alphabet has two listings in the TOPT corpus: GOOGL and GOOG. Its entities carry PRODUCTION-shaped ids.
The store holds them as the entity backfill minted them (the birth alias is the legacy id), so the ids
are the ones of production (#1347 item 5). The ids that a fresh test store mints (the birth alias is the
CUSIP) are not used. With production ids the GOOG instrument id sorts before the GOOGL instrument id,
and the old id sets of `canonical_issuer` matched neither. The old core chose GOOG.

Two ticks run. In the first, the classification file `primary_listings.v1.json` declares GOOGL, as it
does in the package. In the second, the file declares no listing, so no `primary_listing` row exists.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from data_engine import raw_store
from data_engine.config import settings
from data_engine.datahub.production_topt import composition
from data_engine.datahub.production_topt.composition import PlannedRun
from data_engine.datahub.production_topt.executor import SourceFetchPort
from data_engine.datahub.production_topt.headcount import PostgresHeadcountExtractor, record_headcount
from data_engine.datahub.production_topt.market_price_adapter import (
    MarketPriceAdapter,
    MarketPriceQuote,
    MarketPriceTarget,
)
from data_engine.datahub.production_topt.release_derived_adapter import ReleaseDerivedAdapter, ReleaseDerivedRecord
from data_engine.datahub.production_topt.sec_financial_adapter import SecFinancialFactAdapter, SecTarget
from factors.production_topt import OperatingBranch
from truealpha_runtime.testing import InMemoryRawObjectStore, isolated_test_database

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "support"))
from cover_fixture import plain_share_ratio  # noqa: E402
from topt_symbols import TOPT_SYMBOLS  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_live_input_age as age  # noqa: E402
from macro_seed import seed_rate_for_cutoff  # noqa: E402
from split_series_seed import store_flat_daily_series  # noqa: E402

SESSION = age.SESSION
TICK = age.TICK
SERIES_START = age.SERIES_START
#: The close of each Alphabet listing. The two differ, so a value shows which listing supplied it.
CLOSES = {"GOOGL": Decimal("40"), "GOOG": Decimal("41")}
DEFAULT_CLOSE = Decimal("40")
#: The company-wide share count and revenue of `age._bundle`. Market value / revenue gives the P/S.
SHARES = age.FILED_SHARES
REVENUE = Decimal("100000000")

ALPHABET_ISSUER = "issuer:lei:5493006MHB84DD0ZWV18"
ALPHABET_LEI = "5493006MHB84DD0ZWV18"
INSTRUMENT_LEGACY = {"GOOGL": "security:cusip:02079K305", "GOOG": "security:cusip:02079K107"}
INSTRUMENT_CUSIP = {"GOOGL": "02079K305", "GOOG": "02079K107"}
LISTING_LEGACY = {"GOOGL": "listing:xnas:googl", "GOOG": "listing:xnas:goog"}
#: The instrument ids of production (#1347 item 5). A fixture check below proves the store derives them.
PRODUCTION_INSTRUMENT_IDS = {
    "GOOGL": "33afbf95-13be-5fa9-b64c-a9cd05901592",
    "GOOG": "063d58ea-0cc6-590d-86e0-b870746eabb0",
}
BACKFILLED_AT = datetime(2026, 1, 1, tzinfo=UTC)

_ALIAS_SQL = """
insert into staging.entity_aliases
    (entity_id, scheme, value, valid_from, transaction_time, source, raw_ref, method, confidence, mapping_version)
values (%s, %s, %s, '-infinity', %s, 'entity-backfill', 'backfill:test', 'asserted', 1, 'backfill:test')
"""


@dataclass(frozen=True)
class Alphabet:
    """The entity ids of Alphabet in the store, by ticker."""

    issuer: str
    instruments: dict[str, str]
    listings: dict[str, str]


def _mint_backfilled(connection: psycopg.Connection, kind: str, legacy_id: str, typed: tuple[str, str]) -> str:
    """One entity as the backfill mints it: the legacy id is the birth alias, and the typed alias follows."""
    row = connection.execute(
        "select staging.entity_mint(%s, 'legacy-id', %s, 'entity-backfill')::text", (kind, legacy_id)
    ).fetchone()
    assert row is not None
    for scheme, value in (("legacy-id", legacy_id), typed):
        connection.execute(_ALIAS_SQL, (row[0], scheme, value, BACKFILLED_AT))
    return str(row[0])


def seed_backfilled_alphabet(connection: psycopg.Connection) -> Alphabet:
    issuer = _mint_backfilled(connection, "issuer", ALPHABET_ISSUER, ("lei", ALPHABET_LEI))
    instruments = {
        ticker: _mint_backfilled(
            connection, "instrument", INSTRUMENT_LEGACY[ticker], ("cusip", INSTRUMENT_CUSIP[ticker])
        )
        for ticker in ("GOOGL", "GOOG")
    }
    listings = {
        ticker: _mint_backfilled(connection, "listing", LISTING_LEGACY[ticker], ("mic-ticker", f"XNAS:{ticker}"))
        for ticker in ("GOOGL", "GOOG")
    }
    connection.commit()
    return Alphabet(issuer=issuer, instruments=instruments, listings=listings)


def _routes(plan: PlannedRun, connection) -> dict[str, SourceFetchPort]:
    """The deployed adapters over fixed vendor answers, routed as the composition root routes them.

    It follows `test_live_input_age._routes`. Each listing closes at its own price (`CLOSES`).
    """
    cutoff_date = plan.timeline.partition_start.date()
    price_targets: dict[str, MarketPriceTarget] = {}
    sec_targets: dict[str, SecTarget] = {}
    release_targets: dict[str, ReleaseDerivedRecord] = {}
    cik_by_ticker: dict[str, int] = {}
    for work_item_id, binding in plan.bindings.items():
        semantic_type = binding.obligation.capture_requirement_id.removesuffix(":v1")
        subject_id = binding.obligation.subject.id
        issuer_id, instrument_id, listing_id, ticker = plan.coordinates[subject_id]
        cik_by_ticker.setdefault(ticker, 100000 + sorted(plan.coordinates).index(subject_id))
        if semantic_type == "market-price":
            price_targets[work_item_id] = MarketPriceTarget(
                symbol=ticker,
                cutoff=cutoff_date,
                issuer_id=issuer_id,
                instrument_id=instrument_id,
                listing_id=listing_id,
            )
        elif semantic_type == "financial-fact":
            sec_targets[work_item_id] = SecTarget(
                cik=cik_by_ticker[ticker],
                cutoff=cutoff_date,
                issuer_id=issuer_id,
                instrument_id=instrument_id,
                listing_id=listing_id,
                operating_branch=OperatingBranch.FINANCIAL if ticker == "JPM" else OperatingBranch.NON_FINANCIAL,
                ticker=ticker,
            )
        else:
            release_targets[work_item_id] = ReleaseDerivedRecord(
                semantic_type=semantic_type,
                subject_id=listing_id,
                payload={
                    "issuer_id": issuer_id,
                    "instrument_id": instrument_id,
                    "listing_id": listing_id,
                    "ticker": ticker,
                },
                knowable_at=plan.timeline.partition_start,
            )
    ticker_by_cik = {cik: ticker for ticker, cik in cik_by_ticker.items()}
    for ticker, cik in sorted(cik_by_ticker.items()):
        record_headcount(
            connection,
            cik=cik,
            headcount=Decimal("164000"),
            knowable_at=age._filed_at(ticker) - timedelta(days=1),
            source="test-fixture",
            evidence_ref=f"test:{ticker}",
            confidence=Decimal("0.7"),
        )

    def quote(symbol: str, cutoff: date) -> MarketPriceQuote:
        close = CLOSES.get(symbol, DEFAULT_CLOSE)
        return MarketPriceQuote(
            raw_bytes=f"bar:{symbol}:{cutoff.isoformat()}:{close}".encode(),
            close=close,
            as_of=cutoff,
            knowable_at=datetime.combine(cutoff, datetime.min.time(), tzinfo=UTC),
        )

    financial = SecFinancialFactAdapter(
        sec_targets,
        lambda cik, cutoff, branch: age._bundle(branch, cik=cik, ticker=ticker_by_cik[cik]),
        headcount_extractor=PostgresHeadcountExtractor(connection),
        listing_ratio_reader=plain_share_ratio,
    )
    routes: dict[str, SourceFetchPort] = {}
    routes.update(dict.fromkeys(price_targets, MarketPriceAdapter(price_targets, quote)))
    routes.update(dict.fromkeys(sec_targets, financial))
    routes.update(dict.fromkeys(release_targets, ReleaseDerivedAdapter(release_targets, cutoff=cutoff_date)))
    return routes


@dataclass(frozen=True)
class Tick:
    url: str
    metadata: dict
    alphabet: Alphabet


def _run_tick(*, declared: bool) -> Iterator[Tick]:
    """One deployed tick over the TOPT corpus in an isolated database, offline.

    `declared` False gives the resolver a classification file with no listing, so it writes no row.
    """
    import dagster as dg
    from data_engine.datahub import a1_evidence, resolve_coordinates
    from data_engine.lanes.capture import ToptLiveTickConfig, run_topt_live_tick
    from truealpha_runtime import EnvironmentTier as RuntimeEnvironmentTier

    with isolated_test_database("primarylisting") as url, pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(settings, "database_url", url)
        monkeypatch.setattr(settings, "environment_tier", RuntimeEnvironmentTier.PRODUCTION)
        for name, value in {
            "twelve_data_api_key": "",
            "moomoo_kline_origin_enabled": False,
            "moomoo_financials_origin_enabled": False,
        }.items():
            monkeypatch.setattr(settings, name, value)
        store = InMemoryRawObjectStore()
        monkeypatch.setattr(raw_store, "object_store", lambda: store)
        monkeypatch.setattr(composition, "build_routes", _routes)
        monkeypatch.setattr(a1_evidence, "unmet_objectives", lambda *_args, **_kwargs: ())
        monkeypatch.setattr(age, "AGE_DAYS", {})
        if not declared:
            monkeypatch.setattr(resolve_coordinates, "primary_listing_ids", lambda: frozenset())
        with psycopg.connect(url) as connection:
            alphabet = seed_backfilled_alphabet(connection)
            store_flat_daily_series(connection, TOPT_SYMBOLS, first=SERIES_START, last=SESSION, close=DEFAULT_CLOSE)
            seed_rate_for_cutoff(connection, TICK)
            connection.commit()
        context = dg.build_op_context()
        run_topt_live_tick(context, ToptLiveTickConfig(executed_at=TICK.isoformat()))
        yield Tick(url, context.get_output_metadata("result"), alphabet)


@pytest.fixture(scope="module")
def declared_tick() -> Iterator[Tick]:
    yield from _run_tick(declared=True)


@pytest.fixture(scope="module")
def undeclared_tick() -> Iterator[Tick]:
    yield from _run_tick(declared=False)


def _core_row(tick: Tick, issuer_id: str) -> tuple:
    with psycopg.connect(tick.url) as reader:
        row = reader.execute(
            """
            select instrument_id, listing_id, availability, reason_codes, current_ps
            from mart.topt_core_results where run_id = %s and issuer_id = %s
            """,
            (tick.metadata["capture_run_id"], issuer_id),
        ).fetchone()
    assert row is not None, f"the tick wrote no core row for {issuer_id}"
    return row


def _decision(tick: Tick) -> tuple:
    with psycopg.connect(tick.url) as reader:
        row = reader.execute(
            "select outcome, exclusion_reason from mart.strategy_decisions where strategy_run_id = %s and issuer_id = %s",
            (tick.metadata["strategy_run_id"], tick.alphabet.issuer),
        ).fetchone()
    assert row is not None, "the strategy run wrote no decision for Alphabet"
    return row


def test_the_store_holds_the_production_ids_of_alphabet(declared_tick: Tick) -> None:
    """The fixture ids are the ones of production, and the old id order picks GOOG by them (#1347 item 5)."""
    instruments = declared_tick.alphabet.instruments

    assert instruments == PRODUCTION_INSTRUMENT_IDS
    assert min(instruments.values()) == instruments["GOOG"], "id text alone puts GOOG first"
    with psycopg.connect(declared_tick.url) as reader:
        minted_by_the_tick = reader.execute(
            "select count(*) from staging.entities where birth_scheme = 'cusip' and birth_value = any(%s)",
            (sorted(INSTRUMENT_CUSIP.values()),),
        ).fetchone()
    assert minted_by_the_tick == (0,), "the tick resolved Alphabet to the stored entities and minted no second id"


def test_the_core_values_alphabet_at_its_declared_primary_listing(declared_tick: Tick) -> None:
    """Red on main: the id sets matched nothing in the store, and the core took GOOG by instrument id text."""
    alphabet = declared_tick.alphabet

    instrument_id, listing_id, availability, reasons, current_ps = _core_row(declared_tick, alphabet.issuer)

    assert (instrument_id, listing_id) == (alphabet.instruments["GOOGL"], alphabet.listings["GOOGL"])
    assert (availability, list(reasons)) == ("available", [])
    # The close of GOOGL (40) times the company share count, over revenue. GOOG's close would give 4.1.
    assert current_ps == CLOSES["GOOGL"] * SHARES / REVENUE


def test_the_strategy_seed_takes_the_close_of_the_same_listing(declared_tick: Tick) -> None:
    """The decision engine reads the close of the declared listing: the core and the decision agree."""
    with psycopg.connect(declared_tick.url) as reader:
        closes = reader.execute(
            "select value from staging.strategy_backtest_inputs where issuer_id = %s and cutoff_at = %s and input_key = 'last_close'",
            (declared_tick.alphabet.issuer, TICK),
        ).fetchall()

    assert closes == [(CLOSES["GOOGL"],)]


def test_the_mart_identity_view_names_the_declared_listing(declared_tick: Tick) -> None:
    with psycopg.connect(declared_tick.url) as reader:
        row = reader.execute(
            "select current_ticker, listing_id from mart.entity_identity where entity_id = %s::uuid",
            (declared_tick.alphabet.issuer,),
        ).fetchone()

    assert row == ("GOOGL", "listing:xnas:googl")


def test_the_display_view_prefers_the_declared_listing_over_a_newer_snapshot(declared_tick: Tick) -> None:
    """A newer snapshot of GOOG does not outrank the older snapshot of the declared GOOGL. Retract the row: it does."""
    alphabet = declared_tick.alphabet
    with psycopg.connect(declared_tick.url) as connection:
        # Replica role: the insert guards and foreign keys of the snapshot tables stand aside for a copy.
        connection.execute("set local session_replication_role = replica")
        connection.execute(
            """
            create temp table cloned_snapshot on commit drop as
            select * from staging.topt_core_snapshots
            """
        )
        connection.execute(
            """
            update cloned_snapshot set snapshot_id = 'topt-core-snapshot:' || repeat('a', 64),
                content_sha256 = repeat('a', 64), run_id = 'capture-run:' || repeat('a', 64)
            """
        )
        connection.execute("insert into staging.topt_core_snapshots select * from cloned_snapshot")
        connection.execute(
            """
            insert into staging.topt_core_snapshot_members
                (snapshot_id, instrument_id, issuer_id, listing_id, observation_ids, member_sha256, factor_input, created_at)
            select 'topt-core-snapshot:' || repeat('a', 64), %s, issuer_id, %s, observation_ids, repeat('b', 64),
                   factor_input, clock_timestamp() + interval '1 hour'
            from staging.topt_core_snapshot_members where issuer_id = %s
            """,
            (alphabet.instruments["GOOG"], alphabet.listings["GOOG"], alphabet.issuer),
        )

        def display_ticker() -> str:
            row = connection.execute(
                "select ticker from mart.entity_display_resolution where issuer_id = %s", (alphabet.issuer,)
            ).fetchone()
            assert row is not None
            return str(row[0])

        assert display_ticker() == "GOOGL"
        connection.execute(
            """
            insert into staging.entity_retractions (relation_id, valid_to, reason, transaction_time, source, raw_ref)
            select relation_id, '2026-01-01', 'test', clock_timestamp(), 'test', 'test'
            from staging.entity_relations where relation_type = 'primary_listing'
            """
        )
        assert display_ticker() == "GOOG"
        connection.rollback()


def test_an_issuer_with_two_listings_and_no_row_is_unavailable(undeclared_tick: Tick) -> None:
    """No `primary_listing` row: the core and the decision name the reason. No ticker order chooses a listing."""
    alphabet = undeclared_tick.alphabet
    with psycopg.connect(undeclared_tick.url) as reader:
        rows = reader.execute(
            "select count(*) from staging.entity_relations where relation_type = 'primary_listing'"
        ).fetchone()
    assert rows == (0,)

    _instrument, _listing, availability, reasons, current_ps = _core_row(undeclared_tick, alphabet.issuer)

    assert availability == "unavailable"
    assert list(reasons) == ["primary_listing_undeclared"]
    assert current_ps is None
    assert _decision(undeclared_tick) == ("excluded", "primary_listing_undeclared")


def test_an_undeclared_issuer_leaves_the_other_issuers_alone(undeclared_tick: Tick) -> None:
    """One issuer without a declared listing does not touch Apple, which has one listing."""
    with psycopg.connect(undeclared_tick.url) as reader:
        row = reader.execute(
            """
            select availability, reason_codes from mart.topt_core_results r
            join mart.entity_identity i on i.entity_id::text = r.issuer_id
            where r.run_id = %s and i.current_ticker = 'AAPL'
            """,
            (undeclared_tick.metadata["capture_run_id"],),
        ).fetchone()

    assert row == ("available", [])


def _stored_snapshot_payload(tick: Tick) -> dict:
    with psycopg.connect(tick.url) as reader:
        row = reader.execute(
            "select payload from staging.topt_core_snapshots where run_id = %s", (tick.metadata["capture_run_id"],)
        ).fetchone()
    assert row is not None
    return dict(row[0])


def test_the_frozen_snapshot_records_the_primary_listing_on_that_member_only(declared_tick: Tick) -> None:
    """`freeze_snapshot` records `is_primary` on the declared listing. The key is absent on every other member."""
    members = _stored_snapshot_payload(declared_tick)["members"]

    flagged = [member for member in members if "is_primary" in member]

    assert [(member["listing_id"], member["is_primary"]) for member in flagged] == [
        (declared_tick.alphabet.listings["GOOGL"], True)
    ]
    assert len(members) == 21, "the TOPT corpus holds 21 listings"


def test_a_snapshot_stored_before_the_flag_reloads_and_leaves_only_alphabet_undeclared(declared_tick: Tick) -> None:
    """A stored snapshot without `is_primary` reloads. Its sole-listing issuers keep their values. Alphabet has two
    listings and no flag, so its factor input is unavailable (a replay of such a run reads no relation)."""
    from data_engine.datahub.production_topt.materialization import ToptCoreSnapshot

    payload = _stored_snapshot_payload(declared_tick)
    for member in payload["members"]:
        member.pop("is_primary", None)

    snapshot = ToptCoreSnapshot.model_validate(payload)

    undeclared = [item.issuer_id for item in snapshot.factor_inputs() if item.primary_listing_undeclared]
    assert undeclared == [declared_tick.alphabet.issuer]
