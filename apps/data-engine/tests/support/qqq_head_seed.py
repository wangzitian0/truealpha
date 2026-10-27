"""Test support: a governed QQQ head and a fake Twelve Data vendor for the QQQ price lane (#1277).

`publish_qqq_head` lands one constituent refresh and publishes it through the functions the weekly
universe refresh runs (`universe_plane.publish_universe_list`). The lane then reads the head the way
the QQQ tick reads it (`resolve_universe_corpus`). `daily_series_client` answers both daily series of
any symbol and never touches the network.
"""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime

from data_engine import raw_store
from data_engine.datahub.market_prices import TwelveDataClient
from data_engine.datahub.production_topt.universe_plane import UNIVERSE_SOURCES, publish_universe_list
from truealpha_contracts.models import DataSource
from truealpha_runtime.testing import InMemoryRawObjectStore

#: The (unadjusted, split-adjusted) close the fake vendor answers: equal closes show no split.
FLAT_CLOSE = "40"


def _figi(ticker: str) -> str:
    """A twelve-character FIGI-shaped id, unique per ticker."""
    return "bbg" + ticker.lower().replace(".", "").rjust(9, "0")


def publish_qqq_head(
    connection,
    members: Sequence[tuple[str, int]],
    *,
    as_of: date = date(2026, 10, 16),
    figures: Mapping[str, tuple[str, str]] | None = None,
) -> None:
    """Publish (ticker, cik) members as the governed QQQ head. Two tickers with one CIK are one issuer.

    `figures` maps a ticker to the operator's (market cap, last sale price), as the parser reads them.
    """
    figures = figures or {}
    source = UNIVERSE_SOURCES["qqq"]
    fetched_at = datetime.combine(as_of, datetime.min.time(), tzinfo=UTC)
    body = json.dumps({"members": [ticker for ticker, _cik in members], "as_of": as_of.isoformat()}).encode()
    fetch_id = raw_store.insert_fetch(
        connection,
        source=DataSource.NASDAQ_INDEX,
        source_record_id=f"index-constituents:qqq:{as_of.isoformat()}",
        body=body,
        content_type="application/json",
        fetched_at=fetched_at,
        recorded_at=fetched_at,
        store=InMemoryRawObjectStore(),
    )
    for ticker, cik in members:
        connection.execute(
            """
            insert into staging.etf_constituent_facts
                (etf_symbol, as_of, source, raw_fetch_id, ticker, company_name, market_cap, last_sale_price,
                 cik, figi, knowable_at)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                source.etf,
                as_of,
                DataSource.NASDAQ_INDEX.value,
                fetch_id,
                ticker,
                ticker,
                figures.get(ticker, (None, None))[0],
                figures.get(ticker, (None, None))[1],
                cik,
                _figi(ticker),
                fetched_at,
            ),
        )
    publish_universe_list(connection, source, report_date=as_of, note="#1277 test head")


def daily_values(sessions: Sequence[date], close: str = FLAT_CLOSE) -> list[dict[str, str]]:
    """The `values` of a Twelve Data `/time_series` answer, newest first, one bar per session."""
    return [
        {"datetime": day.isoformat(), "open": close, "high": close, "low": close, "close": close, "volume": "1000"}
        for day in sorted(sessions, reverse=True)
    ]


def daily_series_client(
    requests: list[tuple[str, str, str]], sessions: Sequence[date], *, caller: str
) -> TwelveDataClient:
    """A fake vendor that answers every daily request with `sessions` and records (symbol, interval, adjust)."""

    def transport(url: str) -> tuple[int, bytes]:
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        requests.append((query["symbol"], query["interval"], query["adjust"]))
        return 200, json.dumps({"values": daily_values(sessions)}).encode()

    # The rate limiter never sleeps here: the tests count calls, they do not pace them.
    return TwelveDataClient(api_key="test", transport_fn=transport, sleep_fn=lambda _seconds: None, caller=caller)
