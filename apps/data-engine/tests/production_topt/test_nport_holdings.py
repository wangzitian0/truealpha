"""The N-PORT holdings capture (#63 first tranche) against the committed real
QQQ filing: land, resolve through the KG, write weight-bearing line facts,
and insert nothing on a re-run of the same filing."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub.production_topt.nport_holdings import capture_fund_holdings
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
    def __init__(self, body: bytes | dict):
        self._body = body

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._body

    @property
    def content(self) -> bytes:
        return self._body


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

    rows = connection.execute(
        """
        select holding_id, percent_of_net_assets, raw_ref, transaction_time
        from staging.fund_holding_facts where fund_id = %s and report_period = %s
        """,
        (f"etf:series:{_SERIES}", outcome.report_period),
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
    assert again.holdings_minted == 0, "minted entities resolve from the KG on rerun"
    assert again.holdings_resolved == first.facts_inserted


def test_known_isin_resolves_to_existing_entity_not_a_mint(connection) -> None:
    _, holdings = nport.parse_nport(_XML)
    target = next(h.isin for h in holdings if h.isin and h.pct_val and h.value_usd)
    er.ensure_entity(connection, "company:cik:320193", "company", "Pre-seeded Issuer")
    er.assert_identifier(
        connection,
        entity_id="company:cik:320193",
        source="test",
        identifier_type="isin",
        identifier_value=target,
        confidence=0.98,
        transaction_time=datetime(2025, 1, 1, tzinfo=UTC),
        valid_from="2025-01-01",
        raw_ref="raw.fetches:0",
    )

    capture_fund_holdings(connection, "QQQ", http=_FakeSec())

    held_as = connection.execute(
        "select holding_id from staging.fund_holding_facts where fund_id = %s and isin = %s",
        (f"etf:series:{_SERIES}", target),
    ).fetchall()
    assert held_as and all(r[0] == "company:cik:320193" for r in held_as)
