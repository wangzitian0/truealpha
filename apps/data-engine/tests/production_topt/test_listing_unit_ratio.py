"""The depositary ratio through the deployed tick (#1519, step 1a).

These tests drive the deployed Dagster op `run_topt_live_tick` over the deployed route builder
(`composition.build_routes` and `sec_financial_adapter.build_route`). The SEC host is a fake socket under
the deployed `sec` client, the deployed gateway and the deployed index of landed keys. The capture, the
freeze, the core engine, the strategy seed and the quality report are real. Only the company-facts bytes
and the vendors of prices come from fixed offline answers.

The fixture universe is the 21-listing TOPT corpus. Each listing has its own CIK. The fake SEC host answers
one cover per issuer. NVDA is PDD-shaped: its cover title says "each representing four Class A ordinary
shares". Five issuers show the failure shapes: an unfamiliar title, a refused document, no annual filing, a
cover of another ticker and a plain title. Every other issuer has a plain title.

Step 1a only measures. The tests below judge three things.

1. The payload of each issuer carries the outcome, and an `unparsed` outcome never aborts the capture of the
   issuer's numbers (criterion b).
2. The cover read adds no `submissions` request and one archive request for each issuer (the SEC calls that
   the issue estimates).
3. The core result and the frozen factor input of every issuer are the same with and without the reader
   (criterion c). The PDD-shaped listing still multiplies the ordinary count by the close: step 1b changes that.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from data_engine.datahub.production_topt import composition, sec_financial_adapter
from data_engine.datahub.production_topt.materialization import FinancialFactPayload
from data_engine.datahub.standards import listing_unit_ratio
from data_engine.datahub.standards.listing_unit_ratio import (
    PATTERN_DEPOSITARY,
    PATTERN_PLAIN_SHARE,
    PATTERN_UNPARSED,
    PAYLOAD_KEYS,
    REASON_NO_ANNUAL_FILING,
    REASON_TICKER_NOT_ON_COVER,
    REASON_UNFAMILIAR_TITLE,
    CoverRatioReader,
)
from data_engine.sources import gateway, sec
from data_engine.sources.landed_keys import MemoryLandedKeys
from truealpha_runtime.testing import isolated_test_database

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "support"))
from cover_fixture import cover_html  # noqa: E402
from macro_seed import store_risk_free_series  # noqa: E402
from production_topt.test_degraded_capture_forced import (  # noqa: E402
    _arm,
    _bundle,
    _live_topt_tick,
    _offline_routes,
    _quote,
)
from split_series_seed import store_topt_series  # noqa: E402

#: The settled session the tick prices on. Every filing of the fake host is older than it.
SESSION = date(2026, 4, 8)
FILED = "2026-02-20"
PERIOD = "2025-12-31"
_BANK_TICKER = "JPM"

#: The routes and the finder as the deployed code defines them. Tests of the repository replace both by default
#: (`conftest.no_annual_filing_over_http`), and a test module reads them before any replacement.
_REAL_BUILD_ROUTES = composition.build_routes
_DEPLOYED_FINDER = listing_unit_ratio.latest_annual_filing_over_http

PDD_TITLE = (
    "American Depositary Shares, each representing<br/>four Class A ordinary shares, par value US$0.00002 per share"
)
PDD_TITLE_PRINTED = (
    "American Depositary Shares, each representing four Class A ordinary shares, par value US$0.00002 per share"
)
PLAIN_TITLE = "Common stock, par value $0.01 per share"
UNFAMILIAR_TITLE = "Units, each consisting of one share of common stock and one-third of one redeemable warrant"

#: ticker -> what the fake SEC host holds for the issuer's latest annual filing. The default is a plain title.
#: `None` means the submissions index lists no annual filing. An int is the status of a refused document.
COVER_OF: dict[str, bytes | int | None] = {
    "NVDA": cover_html(("NVDA", PDD_TITLE)),
    "MSFT": cover_html(("MSFT", PLAIN_TITLE)),
    "AMZN": cover_html(("AMZN", UNFAMILIAR_TITLE)),
    "AAPL": 500,
    "GOOGL": None,
    "META": cover_html(("FB", PLAIN_TITLE)),
}
_PATH_OF_SUBMISSIONS = re.compile(r"^/submissions/CIK(\d{10})\.json$")
_PATH_OF_ARCHIVE = re.compile(r"^/Archives/edgar/data/(\d+)/(\d+)/(.+)$")


def _accession_of(cik: int) -> str:
    return f"{cik:010d}26000001"


def _dashed(accession: str) -> str:
    return f"{accession[:10]}-{accession[10:12]}-{accession[12:]}"


class FakeSecHost:
    """The SEC host as the route sees it: the crosswalk, one submissions index and one primary document per CIK.

    It counts every request by path. A request that no handler names fails the test."""

    def __init__(self) -> None:
        self.cik_by_ticker: dict[str, int] = {}
        self.requests: Counter[str] = Counter()

    def _ticker_of(self, cik: int) -> str:
        return next(ticker for ticker, number in self.cik_by_ticker.items() if number == cik)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests[request.url.path] += 1
        if str(request.url) == sec.TICKERS_URL:
            rows = {
                str(index): {"cik_str": cik, "ticker": ticker.replace(".", "-"), "title": ticker}
                for index, (ticker, cik) in enumerate(sorted(self.cik_by_ticker.items()))
            }
            return httpx.Response(200, json=rows)
        index = _PATH_OF_SUBMISSIONS.match(request.url.path)
        if index is not None:
            cik = int(index.group(1))
            ticker = self._ticker_of(cik)
            cover = COVER_OF.get(ticker, cover_html((ticker, PLAIN_TITLE)))
            recent: dict[str, list[str]] = {}
            if cover is not None:
                recent = {
                    "form": ["20-F" if ticker == "NVDA" else "10-K"],
                    "filingDate": [FILED],
                    "reportDate": [PERIOD],
                    "accessionNumber": [_dashed(_accession_of(cik))],
                    "primaryDocument": [f"{ticker.lower()}-20251231.htm"],
                }
            return httpx.Response(
                200, json={"sic": "6021" if ticker == _BANK_TICKER else "3674", "filings": {"recent": recent}}
            )
        archive = _PATH_OF_ARCHIVE.match(request.url.path)
        assert archive is not None, f"the route asked the fake SEC host for {request.url}"
        ticker = self._ticker_of(int(archive.group(1)))
        cover = COVER_OF.get(ticker, cover_html((ticker, PLAIN_TITLE)))
        if isinstance(cover, int):
            return httpx.Response(cover, content=b"server error")
        assert isinstance(cover, bytes), f"{ticker} has no annual filing, so nobody may ask for its document"
        return httpx.Response(200, content=cover)

    def count(self, pattern: re.Pattern[str]) -> Counter[int]:
        """Requests per CIK, for the paths that `pattern` matches. The CIK is the first group."""
        found: Counter[int] = Counter()
        for path, count in self.requests.items():
            match = pattern.match(path)
            if match is not None:
                found[int(match.group(1))] += count
        return found


@dataclass(frozen=True)
class Observed:
    """What one deployed tick wrote, read into plain data before its database is dropped."""

    payloads: dict[str, dict[str, Any]]  # ticker -> financial-fact payload
    listing_of: dict[str, str]  # ticker -> listing id
    factor_inputs: dict[str, Any]  # issuer id -> the frozen input of the core
    core: dict[str, tuple[Any, ...]]  # issuer id -> the value columns of mart.topt_core_results
    decisions: dict[str, str | None]  # issuer id -> exclusion reason
    status: tuple[int, int, bool]  # obligations, failed, complete
    report: dict[str, Any]  # the persisted quality report
    cik_of: dict[str, int]
    requests: Counter[str]
    submissions_per_cik: Counter[int]
    archive_per_cik: Counter[int]
    requests_after_a_second_reader: Counter[str]


#: The keys of a frozen factor input that name an observation, a run or a snapshot. The id of an observation is the
#: hash of its payload, so a payload with more keys has another id. The values of the inputs do not change.
_IDENTITY_KEYS = frozenset({"input_id", "observation_ids", "snapshot_id", "run_id", "release_manifest_id"})


def _without_identity(node: Any) -> Any:
    """The values of a frozen input with its identities removed. `cell_inputs` follow the order of the observation
    ids, which are hashes, so they are put in the order of their own values."""
    if isinstance(node, dict):
        kept = {key: _without_identity(value) for key, value in node.items() if key not in _IDENTITY_KEYS}
        if "cell_inputs" in kept:
            kept["cell_inputs"] = sorted(kept["cell_inputs"], key=lambda cell: json.dumps(cell, sort_keys=True))
        return kept
    if isinstance(node, list):
        return [_without_identity(item) for item in node]
    return node


_CORE_VALUE_COLUMNS = (
    "operating_branch, operating_metric, availability, operating_efficiency, capital_adjusted_gross_profit, gppe, "
    "tier, target_ps_lower, target_ps_upper, target_ps_midpoint, current_ps, valuation_gap, confidence, freshness, "
    "reason_codes, operating_period_end, revenue_period_end, shares_period_end, availability_status, "
    "source_evidence_status, factor_validation_status, current_pgp, target_pgp_lower, target_pgp_upper, "
    "target_pgp_midpoint"
)


def _arm_deployed_route(monkeypatch: pytest.MonkeyPatch, host: FakeSecHost, *, reader_on: bool) -> None:
    """The deployed route builder over the fake SEC host. Prices, company facts and the other semantics are offline.

    With `reader_on` False the route has no cover reader: that is the route before this change."""
    _arm(monkeypatch, quote=lambda: _quote(SESSION, Decimal("40")), price_cutoff=SESSION)

    def build(plan: Any, connection: Any = None) -> dict[str, Any]:
        offline = _offline_routes(plan, connection, quote=lambda: _quote(SESSION, Decimal("40")), price_cutoff=SESSION)
        # The numbering of `_offline_routes`: the headcount facts it records sit under these CIKs.
        for subject_id, (_issuer, _instrument, _listing, ticker) in plan.coordinates.items():
            host.cik_by_ticker.setdefault(ticker, 100000 + sorted(plan.coordinates).index(subject_id))
        routes = _REAL_BUILD_ROUTES(plan, connection)
        for work_item_id, binding in plan.bindings.items():
            if binding.obligation.capture_requirement_id != "financial-fact:v1":
                routes[work_item_id] = offline[work_item_id]
        return routes

    monkeypatch.setattr(composition, "build_routes", build)
    monkeypatch.setattr(sec, "client", lambda: httpx.Client(transport=httpx.MockTransport(host.handle)))
    monkeypatch.setattr(
        sec_financial_adapter, "sec_financial_fetcher", lambda cik, cutoff, branch, **_kwargs: _bundle(branch)
    )
    monkeypatch.setattr(listing_unit_ratio, "latest_annual_filing_over_http", _DEPLOYED_FINDER)
    if not reader_on:
        monkeypatch.setattr(listing_unit_ratio, "CoverRatioReader", lambda: None)
    # The deployed gateway and the deployed key index, in memory: a key that landed makes no request.
    day = "2026-04-08"
    monkeypatch.setattr(gateway, "utc_day", lambda: day)
    monkeypatch.setattr(gateway, "_writer", gateway.MemoryLedger())
    monkeypatch.setattr(gateway, "_landed_keys", MemoryLandedKeys())


def _observe(*, reader_on: bool) -> Observed:
    with isolated_test_database("ratio_on" if reader_on else "ratio_off") as url, pytest.MonkeyPatch.context() as mp:
        store_topt_series(url)
        store_risk_free_series(url)
        host = FakeSecHost()
        _arm_deployed_route(mp, host, reader_on=reader_on)
        executed_at = datetime.combine(SESSION, time(22, 15), tzinfo=UTC)
        metadata = _live_topt_tick(url, mp, executed_at=executed_at, accept=True)
        run_id = metadata["capture_run_id"]
        after_first = Counter(host.requests)
        # A second reader of the same run, as the standards lane is: its keys landed, so it makes no request.
        if reader_on:
            again = CoverRatioReader(_DEPLOYED_FINDER)(host.cik_by_ticker["NVDA"], SESSION, "NVDA")
            assert again.listing_units_per_share == Decimal(4)
        with psycopg.connect(url) as reader:
            listing_of = {
                ticker: listing
                for ticker, listing in reader.execute(
                    """
                    select p.normalized_payload->>'ticker', p.normalized_payload->>'listing_id'
                    from raw.capture_obligations ob
                    join staging.capture_observation_obligations oo on oo.capture_obligation_id = ob.obligation_id
                    join staging.capture_observation_payloads p on p.observation_id = oo.observation_id
                    where ob.run_id = %s and ob.capture_requirement_id = 'listing-identity:v1'
                    """,
                    (run_id,),
                ).fetchall()
            }
            by_listing = {
                listing: payload
                for listing, payload in reader.execute(
                    """
                    select p.normalized_payload->>'listing_id', p.normalized_payload
                    from raw.capture_obligations ob
                    join staging.capture_observation_obligations oo on oo.capture_obligation_id = ob.obligation_id
                    join staging.capture_observation_payloads p on p.observation_id = oo.observation_id
                    where ob.run_id = %s and ob.capture_requirement_id = 'financial-fact:v1'
                    """,
                    (run_id,),
                ).fetchall()
            }
            factor_inputs = dict(
                reader.execute(
                    """
                    select m.issuer_id, m.factor_input
                    from staging.topt_core_snapshot_members m
                    join staging.topt_core_snapshots s on s.snapshot_id = m.snapshot_id
                    where s.run_id = %s
                    """,
                    (run_id,),
                ).fetchall()
            )
            core = {
                row[0]: tuple(row[1:])
                for row in reader.execute(
                    f"select issuer_id, {_CORE_VALUE_COLUMNS} from mart.topt_core_results where run_id = %s",
                    (run_id,),
                ).fetchall()
            }
            decisions = dict(
                reader.execute(
                    "select issuer_id, exclusion_reason from mart.strategy_decisions where strategy_run_id = %s",
                    (metadata["strategy_run_id"],),
                ).fetchall()
            )
            status = reader.execute(
                "select obligation_count, failed_count, complete from mart.topt_capture_status where run_id = %s",
                (run_id,),
            ).fetchone()
            (report,) = reader.execute(
                "select payload from mart.datahub_quality_report where run_id = %s", (run_id,)
            ).fetchone()
        return Observed(
            payloads={ticker: by_listing[listing] for ticker, listing in listing_of.items()},
            listing_of=listing_of,
            factor_inputs=factor_inputs,
            core=core,
            decisions=decisions,
            status=(status[0], status[1], status[2]),
            report=report,
            cik_of=dict(host.cik_by_ticker),
            requests=after_first,
            submissions_per_cik=host.count(_PATH_OF_SUBMISSIONS),
            archive_per_cik=host.count(_PATH_OF_ARCHIVE),
            requests_after_a_second_reader=Counter(host.requests),
        )


@dataclass(frozen=True)
class Ticks:
    measured: Observed
    before: Observed


@pytest.fixture(scope="module")
def ticks() -> Iterator[Ticks]:
    """Two deployed ticks over one fixture universe: with the cover reader, and without it (the route before this change)."""
    yield Ticks(measured=_observe(reader_on=True), before=_observe(reader_on=False))


def test_the_payload_keys_of_the_reader_are_declared_on_the_payload_model() -> None:
    """The freeze validates every financial-fact payload against a model that forbids extra keys."""
    assert set(PAYLOAD_KEYS) <= set(FinancialFactPayload.model_fields)


# --- criterion (b): the deployed tick carries the ratio, and an unparsed outcome never aborts ----------------


def test_the_pdd_shaped_listing_carries_ratio_four_with_its_title_and_filing(ticks: Ticks) -> None:
    """Red on main: the payload has no `listing_units_per_share` key, because no reader runs in the route."""
    payload = ticks.measured.payloads["NVDA"]

    assert payload["listing_units_per_share"] == "4"
    assert payload["ratio_pattern"] == PATTERN_DEPOSITARY
    assert payload["ratio_title"] == PDD_TITLE_PRINTED
    assert payload["ratio_reason"] is None
    assert payload["ratio_form"] == "20-F"
    assert payload["ratio_filed"] == FILED
    assert payload["ratio_accession"] == _dashed(_accession_of(ticks.measured.cik_of["NVDA"]))


def test_a_plain_title_gives_one_ordinary_share(ticks: Ticks) -> None:
    payload = ticks.measured.payloads["MSFT"]

    assert payload["listing_units_per_share"] == "1"
    assert payload["ratio_pattern"] == PATTERN_PLAIN_SHARE
    assert payload["ratio_title"] == PLAIN_TITLE


@pytest.mark.parametrize(
    ("ticker", "reason", "title"),
    [
        ("AMZN", REASON_UNFAMILIAR_TITLE, UNFAMILIAR_TITLE),
        ("AAPL", "read_failed:HTTPStatusError", None),
        ("GOOGL", REASON_NO_ANNUAL_FILING, None),
        ("META", REASON_TICKER_NOT_ON_COVER, None),
    ],
)
def test_an_unparsed_outcome_names_its_reason_and_keeps_the_numbers_of_the_issuer(
    ticks: Ticks, ticker: str, reason: str, title: str | None
) -> None:
    """The offline company facts carry gross profit, revenue and shares for every issuer. A cover that cannot be read
    must leave them in the payload and the core row."""
    payload = ticks.measured.payloads[ticker]

    assert payload["listing_units_per_share"] is None
    assert payload["ratio_pattern"] == PATTERN_UNPARSED
    assert payload["ratio_reason"] == reason
    assert payload["ratio_title"] == title
    assert (payload["gross_profit"], payload["revenue"], payload["shares_outstanding"]) == (
        "210000000",
        "100000000",
        "10000000",
    )
    assert ticks.measured.core[payload["issuer_id"]][2] == "available"


def test_every_listing_carries_all_seven_keys_and_the_tick_captures_every_cell(ticks: Ticks) -> None:
    measured = ticks.measured

    assert len(measured.payloads) == 21
    for ticker, payload in measured.payloads.items():
        assert all(key in payload for key in PAYLOAD_KEYS), ticker
    assert measured.status == (84, 0, True), "no obligation failed because a cover did not parse"
    # The offline bundle of the bank has no preferred-stock figure, so the strategy excludes it. That is the
    # same with and without the reader. No decision is excluded because of a cover.
    expected = Counter({None: 19, "missing_preferred_stock_value": 1})
    assert Counter(measured.decisions.values()) == expected
    assert Counter(ticks.before.decisions.values()) == expected


def test_the_route_before_this_change_writes_no_ratio_key(ticks: Ticks) -> None:
    """Control of the two ticks: the only difference between them is the reader. The 'not measured' state is a missing key."""
    assert ticks.before.payloads, "the control tick wrote no payload"
    for ticker, payload in ticks.before.payloads.items():
        assert not any(key in payload for key in PAYLOAD_KEYS), ticker


# --- the SEC calls that the reader adds ---------------------------------------------------------------------


def test_the_reader_adds_no_submissions_request_and_one_archive_request_for_each_issuer_with_a_filing(
    ticks: Ticks,
) -> None:
    """The issuer registry reads each CIK's submissions index first. The reader asks for the same key, so the index
    of landed keys serves it. The primary document has one key for each accession."""
    measured, before = ticks.measured, ticks.before
    ciks = set(measured.cik_of.values())

    assert measured.submissions_per_cik == dict.fromkeys(ciks, 1)
    assert before.submissions_per_cik == measured.submissions_per_cik, "the reader added no submissions request"
    with_filing = {cik for ticker, cik in measured.cik_of.items() if ticker != "GOOGL"}
    assert measured.archive_per_cik == dict.fromkeys(with_filing, 1)
    assert not before.archive_per_cik, "the route before this change asks for no filing"
    assert sum(measured.archive_per_cik.values()) == 20, "20 documents on the first day; none on the next days"


def test_a_second_reader_of_the_same_filing_makes_no_request(ticks: Ticks) -> None:
    """The standards lane reads the same key (`filing:{cik}:{accession}:{document}`). A filing that landed is not fetched again."""
    assert ticks.measured.requests_after_a_second_reader == ticks.measured.requests


# --- criterion (c): no served number changes ----------------------------------------------------------------


def test_the_frozen_factor_input_of_every_listing_is_the_same_with_and_without_the_reader(ticks: Ticks) -> None:
    measured, before = ticks.measured.factor_inputs, ticks.before.factor_inputs

    assert len(measured) == len(before) == 20, "one frozen input for each issuer; an empty comparison proves nothing"
    assert _without_identity(measured) == _without_identity(before)
    shares = [
        component["shares_outstanding"]
        for issuer in measured.values()
        for component in issuer["market_value_components"]
    ]
    assert len(shares) == 21 and {item["availability"] for item in shares} == {"available"}


def test_the_core_result_of_every_listing_is_the_same_with_and_without_the_reader(ticks: Ticks) -> None:
    measured, before = ticks.measured.core, ticks.before.core

    assert len(measured) == len(before) == 20
    assert measured == before
    assert sum(row[2] == "available" for row in measured.values()) >= 19, "the comparison covers real values"


def test_the_pdd_shaped_listing_still_multiplies_the_ordinary_count_by_the_close(ticks: Ticks) -> None:
    """Step 1b fixes this. Step 1a must not: the ratio is measured, and no value reads it. Ordinary shares 10,000,000
    times the close 40 is the market value, whatever the cover says."""
    issuer = ticks.measured.payloads["NVDA"]["issuer_id"]
    (component,) = ticks.measured.factor_inputs[issuer]["market_value_components"]

    assert ticks.measured.payloads["NVDA"]["listing_units_per_share"] == "4"
    assert Decimal(component["shares_outstanding"]["value"]) == Decimal("10000000")
    assert Decimal(component["market_price"]["value"]) == Decimal("40")
    assert ticks.measured.core[issuer] == ticks.before.core[issuer]


# --- the dashboard row --------------------------------------------------------------------------------------


def test_the_quality_report_lists_each_listing_with_its_outcome_and_counts_them(ticks: Ticks) -> None:
    """The row is a measurement. Red on main: the report has no `listing_ratio` block."""
    block = ticks.measured.report["listing_ratio"]
    cells = {cell["listing_id"]: cell for cell in block["cells"].values()}

    assert block["counts"] == {"parsed": 1, "ordinary": 16, "unparsed": 4, "not_measured": 0}
    pdd = cells[ticks.measured.listing_of["NVDA"]]
    assert (pdd["outcome"], pdd["listing_units_per_share"], pdd["pattern"], pdd["title"]) == (
        "parsed",
        "4",
        PATTERN_DEPOSITARY,
        PDD_TITLE_PRINTED,
    )
    amzn = cells[ticks.measured.listing_of["AMZN"]]
    assert (amzn["outcome"], amzn["listing_units_per_share"], amzn["reason"], amzn["title"]) == (
        "unparsed",
        None,
        REASON_UNFAMILIAR_TITLE,
        UNFAMILIAR_TITLE,
    )


def test_the_report_of_the_route_before_this_change_says_not_measured_for_every_listing(ticks: Ticks) -> None:
    """A payload without the keys is `not_measured`. It is never counted as `unparsed`."""
    block = ticks.before.report["listing_ratio"]

    assert block["counts"] == {"parsed": 0, "ordinary": 0, "unparsed": 0, "not_measured": 21}
