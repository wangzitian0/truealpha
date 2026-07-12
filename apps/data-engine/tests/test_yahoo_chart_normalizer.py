import json
import uuid
from datetime import UTC, datetime

import pytest
from data_engine import instruments, raw_store
from data_engine.config import settings
from data_engine.normalizers import yahoo_chart
from factors.shared import entity_resolution as er
from truealpha_contracts import DataSource
from truealpha_runtime.testing import skip_or_fail

psycopg = pytest.importorskip("psycopg")

PAYLOAD = {
    "chart": {
        "error": None,
        "result": [
            {
                "meta": {"exchangeTimezoneName": "America/New_York"},
                "timestamp": [1750080600, 1750167000],
                "indicators": {
                    "quote": [
                        {
                            "open": [100.1, 101.2],
                            "high": [102.0, 103.0],
                            "low": [99.0, 100.0],
                            "close": [101.0, 102.5],
                            "volume": [1000, 1200],
                        }
                    ],
                    "adjclose": [{"adjclose": [100.8, 102.3]}],
                },
                "events": {
                    "dividends": {"1750080600": {"amount": 0.25, "date": 1750080600}},
                    "splits": {"1750167000": {"date": 1750167000, "numerator": 2, "denominator": 1}},
                },
            }
        ],
    }
}


@pytest.fixture
def conn():
    try:
        connection = psycopg.connect(settings.database_url, connect_timeout=3)
    except psycopg.OperationalError:
        skip_or_fail("no reachable Postgres (make runtime-up && make db-migrate)")
    yield connection
    connection.rollback()
    connection.close()


def test_yahoo_chart_normalizes_instrument_prices_and_non_double_counted_actions(conn):
    nonce = uuid.uuid4().hex[:10]
    issuer_id = f"company:test:{nonce}"
    instrument_id = f"instrument:isin:US{nonce}"
    listing_id = f"listing:vendor:US.TEST{nonce}"
    er.ensure_entity(conn, issuer_id, "company", "Yahoo Test")
    instruments.ensure_instrument(conn, instrument_id, "equity_common", "Yahoo Test")
    raw_id = raw_store.insert_fetch(
        conn,
        source=DataSource.YAHOO,
        source_record_id=f"chart:test:{nonce}",
        body=json.dumps(PAYLOAD).encode(),
        content_type="application/json",
        fetched_at=datetime.now(UTC),
    )
    prices, actions = yahoo_chart.normalize_fetch(
        conn,
        raw_fetch_id=raw_id,
        issuer_id=issuer_id,
        instrument_id=instrument_id,
        listing_id=listing_id,
        symbol=f"TEST{nonce}",
    )
    assert len(prices) == 2
    assert len(actions) == 2
    assert yahoo_chart.normalize_fetch(
        conn,
        raw_fetch_id=raw_id,
        issuer_id=issuer_id,
        instrument_id=instrument_id,
        listing_id=listing_id,
        symbol=f"TEST{nonce}",
    ) == (prices, actions)
    policies = conn.execute(
        "select distinct price_policy, confidence from staging.market_prices where instrument_id = %s",
        (instrument_id,),
    ).fetchall()
    assert policies == [("raw_plus_actions", yahoo_chart.PRICE_CONFIDENCE)]
    action_values = conn.execute(
        """
        select action_type, ratio, cash_amount, currency
        from staging.corporate_actions where instrument_id = %s order by action_type
        """,
        (instrument_id,),
    ).fetchall()
    assert action_values[0][0] == "cash_dividend"
    assert action_values[0][2] == pytest.approx(0.25)
    assert action_values[0][3] == "USD"
    assert action_values[1][0] == "split"
    assert action_values[1][1] == pytest.approx(2)


def test_yahoo_schema_drift_fails_loudly():
    with pytest.raises(ValueError, match="exactly one result"):
        yahoo_chart._result({"chart": {"error": None, "result": []}})
