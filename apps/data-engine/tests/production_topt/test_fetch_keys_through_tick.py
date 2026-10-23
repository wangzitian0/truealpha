"""Ticks of one cutoff on one day make no repeated vendor request (#1411 K3/K5/K7, #1418 R2.B2).

These tests drive the deployed Dagster op `run_topt_live_tick` three times with the same cutoff.
The second tick cannot reuse the committed observations (#635 reuses them for twelve hours; the
window is set to zero here, as when one origin of a run was refused and the next tick has to ask
every origin again). The adapters then ask their vendors again. The third tick sets `force_fetch`,
as staging's boot canary and an operator do: it is a probe that must reach the vendors. The vendors
are a fake HTTP socket under the deployed clients: the real `sec` and `yahoo` clients, the real source
gateway, the real call ledger and the real index of landed keys, all in an isolated database.

What the tests judge is the socket and the ledger. A request that reaches the socket again for a key
that landed, or a fetch key with two successful ledger rows on one UTC day, fails them. The nightly
verdict `fetch_keys_once` reads the same ledger and names the key when a duplicate is injected.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import dagster as dg
import httpx
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
    MarketPriceTarget,
    yahoo_quote_fetcher,
)
from data_engine.datahub.production_topt.release_derived_adapter import ReleaseDerivedAdapter, ReleaseDerivedRecord
from data_engine.datahub.production_topt.sec_financial_adapter import (
    SecFinancialFactAdapter,
    SecTarget,
    sec_financial_fetcher,
)
from data_engine.lanes.capture import ToptLiveTickConfig, run_topt_live_tick
from data_engine.lanes.quality import FetchKeysOnceConfig, run_fetch_keys_once
from data_engine.quality import fetch_keys_once
from data_engine.sources import gateway
from factors.production_topt import OperatingBranch
from truealpha_runtime import EnvironmentTier as RuntimeEnvironmentTier
from truealpha_runtime.testing import InMemoryRawObjectStore, isolated_test_database

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "support"))
from macro_seed import store_risk_free_series  # noqa: E402
from split_series_seed import store_topt_series  # noqa: E402

#: The settled session every listing closes on, and the tick that follows it (22:15 UTC).
SESSION = date(2026, 3, 31)
TICK = datetime(2026, 4, 1, 22, 15, tzinfo=UTC)
CLOSE = Decimal("40")
SHARES = 10_000_000
FILED = "2026-02-01"
COVER = "2026-01-20"
SERIES_START = date(2025, 12, 1)


def _entry(end: str, value: int, *, start: str | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "end": end,
        "val": value,
        "accn": "0000000000-26-000001",
        "filed": FILED,
        "form": "10-K",
        "fy": 2025,
        "fp": "FY",
    }
    if start is not None:
        entry["start"] = start
    return entry


def _company_facts() -> bytes:
    annual = {"start": "2025-01-01"}
    return json.dumps(
        {
            "facts": {
                "dei": {
                    "EntityCommonStockSharesOutstanding": {"units": {"shares": [_entry(COVER, SHARES)]}},
                },
                "us-gaap": {
                    "Revenues": {"units": {"USD": [_entry("2025-12-31", 100_000_000, **{"start": annual["start"]})]}},
                    "GrossProfit": {"units": {"USD": [_entry("2025-12-31", 60_000_000, **{"start": annual["start"]})]}},
                    "Assets": {"units": {"USD": [_entry("2025-12-31", 200_000_000)]}},
                },
            }
        }
    ).encode()


def _yahoo_chart() -> bytes:
    """A daily chart that ends on the settled session, every bar at the close of 40."""
    days = [SESSION - timedelta(days=offset) for offset in range(10, -1, -1)]
    stamps = [int(datetime.combine(day, datetime.min.time(), tzinfo=UTC).timestamp()) for day in days]
    closes = [float(CLOSE)] * len(days)
    quote = {"open": closes, "high": closes, "low": closes, "close": closes, "volume": [1000] * len(days)}
    payload = {
        "chart": {
            "result": [{"timestamp": stamps, "indicators": {"quote": [quote], "adjclose": [{"adjclose": closes}]}}]
        }
    }
    return json.dumps(payload).encode()


class FakeVendors:
    """The vendors' side of the socket. It counts every request by URL path and answers fixed bytes."""

    def __init__(self) -> None:
        self.requests: Counter[str] = Counter()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests[request.url.path] += 1
        if request.url.host == "data.sec.gov" and "/companyfacts/" in request.url.path:
            return httpx.Response(200, content=_company_facts())
        if request.url.host.endswith("finance.yahoo.com") and "/chart/" in request.url.path:
            return httpx.Response(200, content=_yahoo_chart())
        return httpx.Response(404, content=b"{}")

    def count(self, marker: str) -> int:
        return sum(count for path, count in self.requests.items() if marker in path)


def _routes(plan: PlannedRun, connection) -> dict[str, SourceFetchPort]:
    """The deployed adapters over the deployed fetchers: the vendors are reached through the socket."""
    cutoff_date = plan.timeline.partition_start.date()
    price_targets: dict[str, MarketPriceTarget] = {}
    sec_targets: dict[str, SecTarget] = {}
    release_targets: dict[str, ReleaseDerivedRecord] = {}
    cik_by_ticker: dict[str, int] = {}
    for work_item_id, binding in plan.bindings.items():
        semantic_type = binding.obligation.capture_requirement_id.removesuffix(":v1")
        subject_id = binding.obligation.subject.id
        issuer_id, instrument_id, listing_id, ticker = plan.coordinates[subject_id]
        # Dual-class listings share one CIK, as GOOG and GOOGL do: one issuer, two listings.
        issuer_ticker = "GOOGL" if ticker == "GOOG" else ticker
        cik_by_ticker.setdefault(
            ticker, 100000 + sorted({c[3] for c in plan.coordinates.values()}).index(issuer_ticker)
        )
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
                operating_branch=OperatingBranch.NON_FINANCIAL,
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
    for ticker, cik in sorted(cik_by_ticker.items()):
        record_headcount(
            connection,
            cik=cik,
            headcount=Decimal("164000"),
            knowable_at=datetime(2026, 1, 1, tzinfo=UTC),
            source="test-fixture",
            evidence_ref=f"test:{ticker}",
            confidence=Decimal("0.7"),
        )
    price = MarketPriceAdapter(price_targets, yahoo_quote_fetcher)
    financial = SecFinancialFactAdapter(
        sec_targets, sec_financial_fetcher, headcount_extractor=PostgresHeadcountExtractor(connection)
    )
    release = ReleaseDerivedAdapter(release_targets, cutoff=cutoff_date)
    routes: dict[str, SourceFetchPort] = {}
    routes.update(dict.fromkeys(price_targets, price))
    routes.update(dict.fromkeys(sec_targets, financial))
    routes.update(dict.fromkeys(release_targets, release))
    return routes


def _tick(*, force_fetch: bool) -> None:
    run_topt_live_tick(dg.build_op_context(), ToptLiveTickConfig(executed_at=TICK.isoformat(), force_fetch=force_fetch))


@dataclass(frozen=True)
class Ticks:
    url: str
    day: date
    vendors: FakeVendors
    requests_after_first: Counter[str]
    requests_after_second: Counter[str]
    requests_after_third: Counter[str]


@pytest.fixture(scope="module")
def ticks() -> Iterator[Ticks]:
    """Three deployed ticks of one cutoff in an isolated database, with the deployed ledger and index."""
    from data_engine.datahub import a1_evidence

    with isolated_test_database("fetchkeys") as url, pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(settings, "database_url", url)
        monkeypatch.setattr(settings, "environment_tier", RuntimeEnvironmentTier.PRODUCTION)
        monkeypatch.setattr(settings, "sec_user_agent", "TrueAlpha test test@example.com")
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
        vendors = FakeVendors()
        real_client = httpx.Client
        monkeypatch.setattr(
            httpx, "Client", lambda *a, **kw: real_client(*a, **{**kw, "transport": httpx.MockTransport(vendors)})
        )
        # The deployed sinks: the warehouse ledger and the warehouse index of landed keys.
        monkeypatch.setattr(gateway, "_writer", gateway.postgres_writer)
        monkeypatch.setattr(gateway, "_landed_keys", gateway.PostgresLandedKeys(gateway._pg, gateway._pg_reset))
        day = gateway.utc_day()
        monkeypatch.setattr(gateway, "utc_day", lambda: day)
        gateway._pg_reset()
        store_topt_series(url)
        # The tick reads the risk-free rate of its cutoff from the stored series (#1255).
        store_risk_free_series(url)
        try:
            _tick(force_fetch=False)
            first = Counter(vendors.requests)
            monkeypatch.setattr(composition, "_REUSE_MAX_AGE", timedelta(0))
            _tick(force_fetch=False)
            second = Counter(vendors.requests)
            _tick(force_fetch=True)
            third = Counter(vendors.requests)
            yield Ticks(url, date.fromisoformat(day), vendors, first, second, third)
        finally:
            gateway._pg_reset()


def test_the_first_tick_asks_each_vendor_key_once(ticks: Ticks) -> None:
    """Control. The first tick does reach the socket, once per CIK and once per symbol.

    A test that passes because nothing was fetched proves nothing, so the count of the first
    tick is asserted before the count of the second.
    """
    assert ticks.requests_after_first, "the first tick made no vendor request; the fixture is empty"
    assert ticks.vendors.count("/companyfacts/") > 0 and ticks.vendors.count("/chart/") > 0
    repeated = {path: count for path, count in ticks.requests_after_first.items() if count > 1}
    assert repeated == {}, f"the first tick asked one vendor path twice: {repeated}"


def test_a_second_tick_of_the_same_cutoff_makes_zero_repeated_vendor_requests(ticks: Ticks) -> None:
    """Acceptance. The second tick asks its adapters again; every key is served from the index.

    Red without the index of landed keys: the second tick asked SEC for every company-facts
    document and Yahoo for every chart a second time.
    """
    added = ticks.requests_after_second - ticks.requests_after_first
    assert dict(added) == {}, f"the second tick reached the vendor socket again: {dict(added)}"


def test_a_forced_tick_is_a_probe_that_reaches_every_vendor_and_leaves_unkeyed_rows(ticks: Ticks) -> None:
    """The boot canary forces a fetch to prove that the release reaches its vendors (`release_fetch_proof`).

    Red when the index serves a forced tick: the release proof then passes with no vendor call."""
    probed = ticks.requests_after_third - ticks.requests_after_second
    first = ticks.requests_after_first
    assert set(probed) == set(first), "the probe must ask every path of the first tick again"
    # GOOG and GOOGL share one CIK: the first tick asks it once, and a probe has no index to share it by.
    assert all(probed[path] >= first[path] for path in first)
    with psycopg.connect(ticks.url) as connection:
        unkeyed = connection.execute(
            "select count(*) from staging.api_call_ledger "
            "where fetch_key is null and ok and source in ('sec', 'yahoo') and caller not like 'injected-%'"
        ).fetchone()[0]
        keyed = connection.execute(
            "select count(*) from staging.api_call_ledger where fetch_key is not null and ok and caller not like 'injected-%'"
        ).fetchone()[0]
    assert unkeyed == sum(probed.values()), "each probe request is a ledger row without a key"
    assert keyed == sum(ticks.requests_after_first.values()), "the first tick is the only keyed fetch of each key"


def test_no_fetch_key_has_two_successful_ledger_rows_on_one_day(ticks: Ticks) -> None:
    """The ledger of the environment shows each key once, and the keys carry the vendors' paths."""
    with psycopg.connect(ticks.url) as connection:
        rows = connection.execute(
            "select fetch_key, count(*) from staging.api_call_ledger "
            "where fetch_key is not null and ok and caller not like 'injected-%' group by 1"
        ).fetchall()
        reuse = connection.execute("select coalesce(sum(reuse_count), 0) from staging.landed_fetch_keys").fetchone()[0]
    keys = {key for key, _count in rows}
    assert {count for _key, count in rows} == {1}, f"a key was fetched more than once: {rows}"
    assert any(key.startswith("sec|companyfacts|issuer:cik:") for key in keys), keys
    assert any(key.startswith("yahoo|chart") and "|symbol:" in key for key in keys), keys
    assert reuse > 0, "the second tick read the index, and each read counts as a reuse"


def test_the_nightly_verdict_is_green_on_these_ticks_and_names_an_injected_duplicate(ticks: Ticks) -> None:
    """`fetch_keys_once` reads the ledger of the two ticks: green. One duplicate row turns it red, by key."""
    with psycopg.connect(ticks.url, autocommit=True) as connection:
        green = fetch_keys_once.evaluate(connection, day=ticks.day)
        assert green.keyed > 0 and green.duplicates == () and not green.red, green.summary
        key = connection.execute(
            "select fetch_key from staging.api_call_ledger where fetch_key like 'sec|companyfacts|%' limit 1"
        ).fetchone()[0]
        _inject(connection, "injected-duplicate", key)
        try:
            red = fetch_keys_once.evaluate(connection, day=ticks.day)
        finally:
            _clean(connection)
    assert red.red and dict(red.duplicates) == {key: 2}, red.summary
    assert key in red.summary


def test_the_dagster_check_records_a_red_verdict_for_a_duplicate_and_fails_the_run(ticks: Ticks) -> None:
    """The deployed op `run_fetch_keys_once` fails with the key named, and the verdict row is red."""
    key = "sec|companyfacts|issuer:cik:9999999999|" + ticks.day.isoformat()
    with psycopg.connect(ticks.url, autocommit=True) as connection:
        _inject(connection, "injected-first", key)
        _inject(connection, "injected-second", key)
        try:
            with pytest.raises(dg.Failure, match="fetched twice"):
                run_fetch_keys_once(dg.build_op_context(), FetchKeysOnceConfig(day=ticks.day.isoformat()))
            row = connection.execute(
                "select ok, summary from mart.nightly_verdicts where check_name = 'fetch_keys_once' "
                "order by verdict_id desc limit 1"
            ).fetchone()
        finally:
            _clean(connection)
    assert row is not None and row[0] is False and key in row[1], row


def _inject(connection: psycopg.Connection, caller: str, key: str) -> None:
    connection.execute(
        """
        insert into staging.api_call_ledger (source, endpoint, caller, called_at, ok, cost, fetch_key)
        values ('sec', 'companyfacts', %s, now(), true, 1, %s)
        """,
        (caller, key),
    )


def _clean(connection: psycopg.Connection) -> None:
    connection.execute("delete from staging.api_call_ledger where caller like 'injected-%'")
