"""The data-driven universe plane (#539): parse real bytes, land, publish, resolve.

The cassette is the exact byte string the index operator answered on 2026-08-17
(sha-anchored, #569's pattern); the round-trip test drives the same functions the
refresh job runs, against the real database, and proves the capture pipeline can
resolve its universe from the governed head — the property that replaced the
checked-in corpus file this plane exists to eliminate.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from pathlib import Path

import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub.production_topt.universe_corpus import corpus_list_version
from data_engine.datahub.production_topt.universe_plane import (
    UNIVERSE_SOURCES,
    build_denominator,
    current_head_mapping_sha,
    parse_nasdaq_index_rows,
    publish_universe_list,
    resolve_universe_corpus,
)
from truealpha_contracts.models import DataSource

_CASSETTE = Path(__file__).parent / "cassettes" / "nasdaq100_index.bcc3fb15.json"
_CASSETTE_SHA = "bcc3fb150a3988b699eba12e798e950e0ec0e7d55f4d99765c69dd528f087518"


@pytest.fixture
def connection():
    try:
        active = psycopg.connect(settings.database_url, connect_timeout=3, autocommit=False)
    except psycopg.OperationalError as error:
        pytest.skip(f"no local database: {error}")
    try:
        yield active
    finally:
        active.rollback()
        active.close()


def _bytes() -> bytes:
    body = _CASSETTE.read_bytes()
    assert hashlib.sha256(body).hexdigest() == _CASSETTE_SHA, "cassette is not the operator's bytes"
    return body


def test_the_operator_bytes_parse_to_the_full_index() -> None:
    rows = parse_nasdaq_index_rows(_bytes())
    assert len(rows) == 102
    tickers = {row["ticker"] for row in rows}
    assert {"AAPL", "NVDA", "GOOG", "GOOGL"} <= tickers
    assert all(row["name"] for row in rows)
    # The operator's own market caps ride the same bytes (weights proper are
    # #63's N-PORT plane): AAPL parses to its actual dollar figure, not a string.
    aapl = next(row for row in rows if row["ticker"] == "AAPL")
    assert aapl["market_cap"] == "4464797487400"
    assert sum(1 for row in rows if row["market_cap"]) >= 100


def test_a_truncated_response_refuses_to_parse() -> None:
    import json

    payload = json.loads(_bytes())
    payload["data"]["data"]["rows"] = payload["data"]["data"]["rows"][:40]
    with pytest.raises(ValueError, match="only 40 rows"):
        parse_nasdaq_index_rows(json.dumps(payload).encode())


def test_land_publish_resolve_round_trip(connection) -> None:
    """Rows land with lineage; the published head resolves as a validated corpus."""
    source = UNIVERSE_SOURCES["qqq"]
    body = _bytes()
    fetched_at = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
    from data_engine import raw_store

    fetch_id = raw_store.insert_fetch(
        connection,
        source=DataSource.NASDAQ_INDEX,
        source_record_id="index-constituents:qqq:test",
        body=body,
        content_type="application/json",
        fetched_at=fetched_at,
        recorded_at=fetched_at,
    )
    for i, row in enumerate(parse_nasdaq_index_rows(body)):
        connection.execute(
            """
            insert into staging.etf_constituent_facts
                (etf_symbol, as_of, source, raw_fetch_id, ticker, company_name, market_cap, cik, figi, knowable_at)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                "qqq",
                date(2026, 8, 17),
                DataSource.NASDAQ_INDEX.value,
                fetch_id,
                row["ticker"],
                row["name"],
                row.get("market_cap"),
                100000 + i,  # resolution is exercised live by the refresh; here the
                f"bbg{i:09d}",  # plane's own shape and the publish/resolve legs are under test
                fetched_at,
            ),
        )

    denominator = build_denominator(connection, source, report_date=date(2026, 6, 30))
    assert denominator["instrument_count"] == 102
    assert denominator["obligation_count"] == 408

    assert current_head_mapping_sha(connection, source) != denominator["instrument_mapping_sha256"]
    contract_id, sequence = publish_universe_list(
        connection, source, report_date=date(2026, 6, 30), note="round-trip test"
    )
    assert contract_id.startswith("universe-list:")
    assert current_head_mapping_sha(connection, source) == denominator["instrument_mapping_sha256"]

    corpus = resolve_universe_corpus(connection, source.head_kind)
    version = corpus_list_version(corpus)
    assert len(version.members) == 102
    assert version.universe.universe_id == "universe:qqq-us-2026-06-30"


def test_resolving_an_unpublished_universe_fails_loud(connection) -> None:
    with pytest.raises(LookupError, match="no published universe head"):
        resolve_universe_corpus(connection, "universe-list:never-published")


def test_a_same_day_second_refresh_does_not_smear_the_denominator(connection) -> None:
    """Two landings on one as_of: the denominator is the newest refresh alone
    (Copilot Medium on #606 — max(as_of) alone unioned both)."""
    source = UNIVERSE_SOURCES["qqq"]
    body = _bytes()
    from data_engine import raw_store

    for hour, marker in ((10, "first"), (14, "second")):
        fetched_at = datetime(2026, 8, 17, hour, 0, tzinfo=UTC)
        fetch_id = raw_store.insert_fetch(
            connection,
            source=DataSource.NASDAQ_INDEX,
            source_record_id=f"index-constituents:qqq:smear-{marker}",
            body=body + marker.encode(),
            content_type="application/json",
            fetched_at=fetched_at,
            recorded_at=fetched_at,
        )
        for i, row in enumerate(parse_nasdaq_index_rows(body)):
            connection.execute(
                """
                insert into staging.etf_constituent_facts
                    (etf_symbol, as_of, source, raw_fetch_id, ticker, company_name, market_cap, cik, figi, knowable_at)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    "qqq",
                    date(2026, 8, 17),
                    DataSource.NASDAQ_INDEX.value,
                    fetch_id,
                    row["ticker"],
                    row["name"],
                    row.get("market_cap"),
                    200000 + i,
                    f"smr{marker}{i:06d}",
                    fetched_at,
                ),
            )
    denominator = build_denominator(connection, source, report_date=date(2026, 6, 30))
    assert denominator["instrument_count"] == 102, "one refresh's rows, not the union"
    assert all(row[1].startswith("security:figi:smrsecond") for row in denominator["instruments"])


def test_the_edgar_atom_cik_parses() -> None:
    """The fallback's parse leg on EDGAR's real answer shape: SEC's crosswalk files
    miss AEP entirely while browse-edgar resolves it (verified live 2026-08-17)."""
    from data_engine.datahub.production_topt.universe_plane import _parse_edgar_cik

    assert (
        _parse_edgar_cik(b'<link href="...cgi-bin/browse-edgar?action=getcompany&amp;CIK=0000004904&amp;type=10-K"/>')
        == 4904
    )
    assert _parse_edgar_cik(b"<feed>no cik here</feed>") is None


def test_build_routes_trusts_the_universes_embedded_ciks(connection, monkeypatch) -> None:
    """A plane universe must never re-derive identities the governed head already
    carries: the first QQQ run failed in build_routes on the same SEC crosswalk
    hole (AEP) the refresh had already resolved via EDGAR. With issuer:cik ids in
    the plan, the crosswalk is not even consulted."""
    from data_engine.datahub.production_topt import composition
    from data_engine.datahub.production_topt.composition import build_routes, plan_and_persist

    source = UNIVERSE_SOURCES["qqq"]
    body = _bytes()
    fetched_at = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
    from data_engine import raw_store

    fetch_id = raw_store.insert_fetch(
        connection,
        source=DataSource.NASDAQ_INDEX,
        source_record_id="index-constituents:qqq:routes",
        body=body,
        content_type="application/json",
        fetched_at=fetched_at,
        recorded_at=fetched_at,
    )
    for i, row in enumerate(parse_nasdaq_index_rows(body)):
        connection.execute(
            """
            insert into staging.etf_constituent_facts
                (etf_symbol, as_of, source, raw_fetch_id, ticker, company_name, market_cap, cik, figi, knowable_at)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                "qqq",
                date(2026, 8, 17),
                DataSource.NASDAQ_INDEX.value,
                fetch_id,
                row["ticker"],
                row["name"],
                row.get("market_cap"),
                300000 + i,
                f"rte{i:09d}",
                fetched_at,
            ),
        )
    publish_universe_list(connection, source, report_date=date(2026, 6, 30), note="routes test")

    def crosswalk_must_not_be_needed():
        raise AssertionError("plane universes must not consult the SEC crosswalk")

    monkeypatch.setattr(composition.sec, "ticker_cik_index", crosswalk_must_not_be_needed)
    # Branch classification fetches SEC submissions per CIK — real network, not
    # this test's subject; the embedded-CIK path is.
    monkeypatch.setattr(composition, "resolve_issuer_classifications", lambda cik_by_ticker: {})
    plan = plan_and_persist(
        connection,
        cutoff=datetime(2026, 8, 17, 12, 30, tzinfo=UTC),
        version="test-routes",
        universe_head_kind=source.head_kind,
        label_prefix="test-qqq",
    )
    routes = build_routes(plan, connection)
    assert len(routes) == 102 * 4


def test_latest_quarter_end_is_the_completed_quarter() -> None:
    from data_engine.datahub.production_topt.universe_plane import latest_quarter_end

    assert latest_quarter_end(date(2026, 8, 17)) == date(2026, 6, 30)
    assert latest_quarter_end(date(2026, 1, 2)) == date(2025, 12, 31)
    # A quarter end publishes the PRIOR quarter — 06-30's own data is not settled on 06-30.
    assert latest_quarter_end(date(2026, 6, 30)) == date(2026, 3, 31)
    assert latest_quarter_end(date(2026, 7, 1)) == date(2026, 6, 30)


def test_snapshot_invariants_are_self_consistent_not_universe_literals() -> None:
    """#539: the first QQQ run captured all 408 obligations and died on a hardcoded
    84 in freeze/snapshot validation. Invariants are now derived from the snapshot
    itself — any universe size freezes through the same checks, and the
    four-distinct-observations-per-member consistency still refuses a malformed set."""
    from datetime import UTC as _UTC
    from datetime import datetime as _dt
    from decimal import Decimal as _D

    import pytest as _pytest
    from data_engine.datahub.production_topt.materialization import SnapshotMember, ToptCoreSnapshot
    from factors.production_topt import (
        MetricAvailability,
        MetricFreshness,
        OperatingBranch,
        ToptCellQualityInput,
        ToptMetricInput,
    )

    def oid(member: int, k: int) -> str:
        return "normalized-observation:" + f"{member:x}{k:x}".ljust(64, "0")

    def metric(name: str, member: int, k: int) -> ToptMetricInput:
        return ToptMetricInput(
            input_id=oid(member, k),
            metric=name,
            value=_D("100"),
            unit="USD",
            confidence=_D("0.9"),
            knowable_at=_dt(2026, 8, 1, tzinfo=_UTC),
            freshness=MetricFreshness.FRESH,
            availability=MetricAvailability.AVAILABLE,
        )

    def member(i: int) -> SnapshotMember:
        observation_ids = tuple(oid(i, k) for k in range(4))
        cells = tuple(
            ToptCellQualityInput(
                input_id=one,
                confidence=_D("0.9"),
                knowable_at=_dt(2026, 8, 1, tzinfo=_UTC),
                freshness=MetricFreshness.FRESH,
            )
            for one in observation_ids
        )
        return SnapshotMember(
            issuer_id=f"issuer:cik:{i:010d}",
            instrument_id=f"security:figi:test{i:08d}",
            listing_id=f"listing:xnas:t{i:03d}",
            operating_branch=OperatingBranch.NON_FINANCIAL,
            observation_ids=observation_ids,
            cell_inputs=cells,
            gross_profit=metric("gross_profit", i, 0),
            total_assets=metric("total_assets", i, 1),
            headcount=metric("headcount", i, 2),
            revenue=metric("revenue", i, 3),
            pre_provision_profit=None,
            shares_outstanding=metric("shares_outstanding", i, 0),
            market_price=metric("market_price", i, 1),
        )

    snapshot = ToptCoreSnapshot(
        run_id="capture-run:" + "a" * 64,
        release_manifest_id="release-manifest:" + "b" * 64,
        universe_id="universe:test-2026-06-30",
        universe_version="test-2026-06-30-v1",
        universe_sha256="c" * 64,
        cutoff=_dt(2026, 8, 17, tzinfo=_UTC),
        members=(member(1), member(2)),
    )
    assert snapshot.snapshot_id.startswith("topt-core-snapshot:")

    twin = member(3)
    with _pytest.raises(ValueError, match="four distinct observations per member"):
        ToptCoreSnapshot(
            run_id="capture-run:" + "a" * 64,
            release_manifest_id="release-manifest:" + "b" * 64,
            universe_id="universe:test-2026-06-30",
            universe_version="test-2026-06-30-v1",
            universe_sha256="c" * 64,
            cutoff=_dt(2026, 8, 17, tzinfo=_UTC),
            members=(
                twin,
                twin.model_copy(update={"instrument_id": "security:figi:other000", "listing_id": "listing:xnas:oth"}),
            ),
        )
