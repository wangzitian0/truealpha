"""The N-PORT holdings capture (#63 first tranche) against the committed real
QQQ filing: land, resolve through the KG, write weight-bearing line facts,
and insert nothing on a re-run of the same filing."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub.capture_core.nport_holdings import capture_fund_holdings
from data_engine.sources import nport
from factors.shared import entity_resolution as er
from truealpha_contracts.models import DataSource

SAMPLES = Path(__file__).resolve().parents[2] / "samples" / "nport"
_XML = (SAMPLES / "QQQ_NPORT_000106783926000024.xml").read_bytes()
_ACCESSION = "000106783926000024"
_FILING_DATE = "2026-01-29"
_SERIES = "S000006219"
_CIK = 1067839

_ATOM = f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry><content type="text/xml">
    <accession-number>0001067839-26-000024</accession-number>
    <filing-date>{_FILING_DATE}</filing-date>
  </content></entry>
</feed>""".encode()


class _Response:
    """An httpx response: `.content` is bytes, `.text` decodes them and `.json()` parses them."""

    status_code = 200

    def __init__(self, body: bytes | dict):
        self.content: bytes = body if isinstance(body, bytes) else json.dumps(body).encode()

    @property
    def text(self) -> str:
        return self.content.decode()

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return json.loads(self.content)


class _FakeSec:
    """The three URLs fund_series/latest_nport_accession/fetch_nport_xml hit,
    answered from committed bytes — the parse and write paths stay real."""

    def __init__(self):
        self.calls: list[str] = []

    def get(self, url: str) -> _Response:
        self.calls.append(url)
        if url == nport.MF_TICKERS_URL:
            return _Response({"data": [[_CIK, _SERIES, "C000017063", "QQQ"]]})
        if url == nport.BROWSE_URL.format(series_id=_SERIES):
            return _Response(_ATOM)
        if url == nport.ARCHIVE_URL.format(cik=_CIK, accession=_ACCESSION):
            return _Response(_XML)
        raise AssertionError(f"unexpected URL: {url}")


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


def test_capture_lands_filing_and_weighted_lines(connection) -> None:
    outcome = capture_fund_holdings(connection, "QQQ", http=_FakeSec())

    assert outcome.accession == _ACCESSION
    assert outcome.filing_date == _FILING_DATE
    assert outcome.report_period, "the filing's own as-of date must survive"
    assert outcome.equity_lines > 90
    assert outcome.facts_inserted + outcome.lines_skipped == outcome.equity_lines
    assert outcome.holdings_resolved + outcome.holdings_minted == outcome.facts_inserted

    landed = connection.execute("select id, source from raw.fetches where id = %s", (outcome.raw_id,)).fetchone()
    assert landed is not None and landed[1] == DataSource.NPORT.value

    fund_uuid = er.coerce_entity_uuid(connection, f"etf:series:{_SERIES}")
    assert fund_uuid is not None
    rows = connection.execute(
        """
        select holding_id, percent_of_net_assets, raw_ref, transaction_time
        from staging.fund_holding_facts where fund_id = %s and report_period = %s
        """,
        (fund_uuid, outcome.report_period),
    ).fetchall()
    assert len(rows) == outcome.facts_inserted
    assert all(r[1] is not None for r in rows), "every fact carries the filed weight"
    assert all(r[2] == f"raw.fetches:{outcome.raw_id}" for r in rows)
    # transaction time is the filing date — the knowable moment, not the fetch clock
    assert all(r[3] == datetime.fromisoformat(_FILING_DATE).replace(tzinfo=UTC) for r in rows)
    total = sum(float(r[1]) for r in rows)
    assert 80.0 < total < 105.0, f"filed weights should approximate the fund, got {total}"


def test_rerun_of_same_filing_inserts_nothing(connection) -> None:
    first = capture_fund_holdings(connection, "QQQ", http=_FakeSec())
    again = capture_fund_holdings(connection, "QQQ", http=_FakeSec())

    assert again.raw_id == first.raw_id, "content-addressed landing must dedupe"
    assert again.facts_inserted == 0
    assert again.holdings_minted == 0, "minted entities resolve from the ontology on rerun"
    assert again.holdings_resolved == first.facts_inserted


def test_known_isin_resolves_to_existing_entity_not_a_mint(connection) -> None:
    _, holdings = nport.parse_nport(_XML)
    target = next(h.isin for h in holdings if h.isin is not None and h.pct_val is not None and h.value_usd is not None)
    issuer_uuid = er.ensure_entity(connection, "company:cik:320193", "company", "Pre-seeded Issuer")
    er.assert_identifier(
        connection,
        entity_id=issuer_uuid,
        source="test",
        identifier_type="isin",
        identifier_value=target,
        confidence=0.98,
        transaction_time=datetime(2025, 1, 1, tzinfo=UTC),
        valid_from="2025-01-01",
        raw_ref="raw.fetches:0",
    )

    capture_fund_holdings(connection, "QQQ", http=_FakeSec())

    fund_uuid = er.coerce_entity_uuid(connection, f"etf:series:{_SERIES}")
    inst_uuid = er.coerce_entity_uuid(connection, f"security:isin:{target}")
    held_as = connection.execute(
        "select holding_id from staging.fund_holding_facts where fund_id = %s and isin = %s",
        (fund_uuid, target),
    ).fetchall()
    assert held_as and all(r[0] == inst_uuid for r in held_as)
    linked = connection.execute(
        "select from_entity_id from staging.entity_relations where to_entity_id = %s and relation_type = 'issues'",
        (inst_uuid,),
    ).fetchone()
    assert linked == (issuer_uuid,)


def test_edgar_browse_urls_use_a_page_size_that_edgar_accepts(monkeypatch: pytest.MonkeyPatch) -> None:
    """EDGAR answered 400 to `count=1` on 2026-11-21: "Count must be one of: 10, 20, ..., 100, 500"."""
    from urllib.parse import parse_qs, urlsplit

    from data_engine.datahub.capture_core import universe_plane

    accepted = {"10", "20", "30", "40", "50", "60", "70", "80", "90", "100", "500"}
    requested: list[str] = []

    def _capture(url: str, *_args: object, **_kwargs: object) -> tuple[int, bytes]:
        requested.append(url)
        return 404, b""

    monkeypatch.setattr(universe_plane, "_get", _capture)
    assert universe_plane._resolve_cik_via_edgar("AEP", user_agent="test-agent") is None
    requested.append(nport.BROWSE_URL.format(series_id=_SERIES))

    assert len(requested) == 2
    for url in requested:
        assert "browse-edgar" in url
        assert parse_qs(urlsplit(url).query)["count"][0] in accepted, url


def test_newest_filing_takes_the_latest_filing_date_of_a_multi_entry_feed() -> None:
    """A page of 10 entries replaced the page of 1. The reader must not depend on the entry order."""
    entries = (
        ("0001067839-26-000020", "2026-05-28"),
        ("0001067839-26-000030", "2026-08-28"),
        ("0001067839-26-000010", "2026-02-27"),
    )
    feed = (
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        + "".join(
            f'<entry><content type="text/xml"><accession-number>{acc}</accession-number>'
            f"<filing-date>{day}</filing-date></content></entry>"
            for acc, day in entries
        )
        + "</feed>"
    )

    assert nport.newest_filing(feed.encode()) == ("000106783926000030", "2026-08-28")
    assert nport.newest_filing(b'<feed xmlns="http://www.w3.org/2005/Atom"></feed>') is None
    assert nport.newest_filing(_ATOM) == (_ACCESSION, _FILING_DATE)
