"""The depositary ratio through the deployed tick and the history projector (#1519, step 1a and step 1b).

These tests drive the deployed Dagster op `run_topt_live_tick` over the deployed route builder
(`composition.build_routes` and `sec_financial_adapter.build_route`). The SEC host is a fake socket under
the deployed `sec` client, the deployed gateway and the deployed index of landed keys. The capture, the
freeze, the core engine, the strategy seed and the quality report are real. Only the company-facts bytes
and the vendors of prices come from fixed offline answers.

The fixture universe is the 21-listing TOPT corpus. Each listing has its own CIK. The fake SEC host answers
one cover per issuer. NVDA is PDD-shaped: its cover title says "each representing four Class A ordinary
shares". Five issuers show the failure shapes: an unfamiliar title, a refused document, no annual filing, a
cover of another ticker and a plain title. Every other issuer has a plain title.

The tests below judge four things.

1. The payload of each issuer carries the outcome, and an `unparsed` outcome never aborts the capture of the
   issuer's raw financial-fact numbers.
2. The cover read adds no `submissions` request and one archive request for each issuer (the SEC calls that
   the issue estimates).
3. `on_close_basis` divides the ordinary share count by `listing_units_per_share` before splits, so the
   PDD-shaped listing values `current_pgp = (ordinary / 4 * close) / gross_profit`. An unparsed cover or a
   missing ratio field makes `shares_outstanding` and the core result `unavailable` with
   `missing_listing_unit_ratio`, and drops `shares_outstanding` from the seeded strategy inputs.
4. The history projector (`strategy_history.project_issuer_rows`) converts shares by the cover ratio and
   drops `shares_outstanding` when the cover does not parse.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time
from decimal import Context, Decimal, localcontext
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
    seeded_shares: dict[str, Decimal]  # issuer id -> seeded shares_outstanding in staging.strategy_backtest_inputs
    refused_rules: tuple[str, ...]  # plausibility run-refusal rules recorded by judge_strategy
    status: tuple[int, int, bool]  # obligations, failed, complete
    report: dict[str, Any]  # the persisted quality report
    cik_of: dict[str, int]
    requests: Counter[str]
    submissions_per_cik: Counter[int]
    archive_per_cik: Counter[int]
    requests_after_a_second_reader: Counter[str]


_CORE_VALUE_COLUMNS = (
    "operating_branch, operating_metric, availability, operating_efficiency, capital_adjusted_gross_profit, gppe, "
    "tier, target_ps_lower, target_ps_upper, target_ps_midpoint, current_ps, valuation_gap, confidence, freshness, "
    "reason_codes, operating_period_end, revenue_period_end, shares_period_end, availability_status, "
    "source_evidence_status, factor_validation_status, current_pgp, target_pgp_lower, target_pgp_upper, "
    "target_pgp_midpoint"
)


def _bundle_without_preferred_stock(branch: Any) -> Any:
    """The offline bundle of `test_degraded_capture_forced`, with no preferred-stock figure for the bank.

    The shared bundle carries all four banking leaves (#1518). This module needs a bank that the strategy
    excludes with its own reason, `missing_preferred_stock_value`: no zero stand-in for a missing preferred
    stock (owner decision D2). So the bank of this module has equity, goodwill and intangibles and no
    preferred stock, which is the capture of a bank whose balance sheet names no preferred line."""
    return replace(_bundle(branch), preferred_stock_value=None)


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
        sec_financial_adapter,
        "sec_financial_fetcher",
        lambda cik, cutoff, branch, **_kwargs: _bundle_without_preferred_stock(branch),
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
    from data_engine.lanes import capture

    with isolated_test_database("ratio_on" if reader_on else "ratio_off") as url, pytest.MonkeyPatch.context() as mp:
        store_topt_series(url)
        store_risk_free_series(url)
        host = FakeSecHost()
        _arm_deployed_route(mp, host, reader_on=reader_on)
        real_judge_strategy = capture.judge_strategy
        refused_rules: list[str] = []

        def record_judge_strategy(*args: Any, **kwargs: Any) -> Any:
            verdict = real_judge_strategy(*args, **kwargs)
            refused_rules.extend(violation.rule for violation in verdict.run_refusals)
            # Without the reader, every issuer lacks `listing_units_per_share`, so `judge_strategy` refuses the
            # empty eligible set. Commit the control tick's rows in the isolated DB so tests can inspect them.
            return verdict if reader_on else replace(verdict, run_refusals=())

        mp.setattr(capture, "judge_strategy", record_judge_strategy)
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
            seeded_shares = {
                issuer_id: Decimal(str(value))
                for issuer_id, value in reader.execute(
                    "select issuer_id, value from staging.strategy_backtest_inputs where input_key = 'shares_outstanding'"
                ).fetchall()
            }
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
            seeded_shares=seeded_shares,
            refused_rules=tuple(refused_rules),
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
def test_an_unparsed_outcome_names_its_reason_and_marks_the_issuer_unavailable_with_missing_listing_unit_ratio(
    ticks: Ticks, ticker: str, reason: str, title: str | None
) -> None:
    """The offline company facts carry gross profit, revenue and shares for every issuer. A cover that cannot be read
    keeps the raw figures in the payload, refuses the share count on close basis with `missing_listing_unit_ratio`,
    marks the core row unavailable with `missing_listing_unit_ratio`, and excludes the strategy decision."""
    payload = ticks.measured.payloads[ticker]
    issuer_id = payload["issuer_id"]
    listing_id = ticks.measured.listing_of[ticker]
    component = next(
        item
        for item in ticks.measured.factor_inputs[issuer_id]["market_value_components"]
        if item["listing_id"] == listing_id
    )

    assert payload["listing_units_per_share"] is None
    assert payload["ratio_pattern"] == PATTERN_UNPARSED
    assert payload["ratio_reason"] == reason
    assert payload["ratio_title"] == title
    assert (payload["gross_profit"], payload["revenue"], payload["shares_outstanding"]) == (
        "210000000",
        "100000000",
        "10000000",
    )
    assert component["shares_outstanding"]["availability"] == "unavailable"
    assert component["shares_outstanding"]["value"] is None
    assert component["shares_outstanding"]["unavailable_reason"] == "missing_listing_unit_ratio"
    assert ticks.measured.core[issuer_id][2] == "unavailable"
    assert ticks.measured.core[issuer_id][14] == ["missing_listing_unit_ratio"]
    assert issuer_id not in ticks.measured.seeded_shares
    assert ticks.measured.decisions[issuer_id] == "missing_market_value_input"


def test_every_listing_carries_all_seven_keys_and_the_tick_captures_every_cell(ticks: Ticks) -> None:
    measured = ticks.measured

    assert len(measured.payloads) == 21
    for ticker, payload in measured.payloads.items():
        assert all(key in payload for key in PAYLOAD_KEYS), ticker
    assert measured.status == (84, 0, True), "no obligation failed because a cover did not parse"
    # Four issuers have unparsed covers (`AMZN`, `AAPL`, `GOOGL`, `META`) and are excluded with
    # `missing_market_value_input`. The bank (`JPM`) has no preferred-stock figure and is excluded with
    # `missing_preferred_stock_value`. The remaining 15 issuers (including `NVDA` with ratio 4) are evaluated.
    assert Counter(measured.decisions.values()) == Counter(
        {None: 15, "missing_market_value_input": 4, "missing_preferred_stock_value": 1}
    )
    # Without the reader, no payload carries `listing_units_per_share`, so all 19 non-bank issuers lack a
    # share basis and are excluded with `missing_market_value_input`.
    assert Counter(ticks.before.decisions.values()) == Counter(
        {"missing_market_value_input": 19, "missing_preferred_stock_value": 1}
    )


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


# --- Step 1b: share conversion on close basis and refusal on missing ratio ----------------------------------


def test_the_pdd_shaped_listing_divides_ordinary_shares_by_four_before_multiplying_by_close(ticks: Ticks) -> None:
    """Step 1b converts the ordinary share count to listing units (`10,000,000 / 4 = 2,500,000`) before
    multiplying by the close (`40`), so `current_pgp = (ordinary / 4 * close) / gross_profit`."""
    nvda_issuer = ticks.measured.payloads["NVDA"]["issuer_id"]
    msft_issuer = ticks.measured.payloads["MSFT"]["issuer_id"]
    (nvda_component,) = ticks.measured.factor_inputs[nvda_issuer]["market_value_components"]
    (msft_component,) = ticks.measured.factor_inputs[msft_issuer]["market_value_components"]

    assert ticks.measured.payloads["NVDA"]["listing_units_per_share"] == "4"
    assert Decimal(nvda_component["shares_outstanding"]["value"]) == Decimal("2500000")
    assert Decimal(nvda_component["market_price"]["value"]) == Decimal("40")
    assert Decimal(msft_component["shares_outstanding"]["value"]) == Decimal("10000000")
    assert ticks.measured.seeded_shares[nvda_issuer] == Decimal("2500000")
    assert ticks.measured.seeded_shares[msft_issuer] == Decimal("10000000")

    nvda_core = ticks.measured.core[nvda_issuer]
    msft_core = ticks.measured.core[msft_issuer]
    with localcontext(Context(prec=34)):
        expected_market_cap = (Decimal("10000000") / Decimal(4)) * Decimal("40")
        expected_pgp = expected_market_cap / Decimal("210000000")
        expected_ps = expected_market_cap / Decimal("100000000")
        expected_msft_pgp = (Decimal("10000000") * Decimal("40")) / Decimal("210000000")

    assert nvda_core[2] == "available"
    assert nvda_core[10] == expected_ps == Decimal("1")
    assert nvda_core[21] == expected_pgp
    assert msft_core[10] == Decimal("4") == 4 * nvda_core[10]
    assert msft_core[21] == expected_msft_pgp


def test_missing_listing_unit_ratio_in_payload_marks_every_share_count_and_core_result_unavailable(
    ticks: Ticks,
) -> None:
    """When the payload has no `listing_units_per_share` field (`ticks.before`), every listing's share count
    is unavailable with `missing_listing_unit_ratio`, no share count is seeded, and every non-bank core row
    is unavailable with `missing_listing_unit_ratio`."""
    before = ticks.before
    shares = [
        component["shares_outstanding"]
        for issuer in before.factor_inputs.values()
        for component in issuer["market_value_components"]
    ]
    assert len(shares) == 21
    assert {item["availability"] for item in shares} == {"unavailable"}
    assert {item["unavailable_reason"] for item in shares} == {"missing_listing_unit_ratio"}
    assert before.seeded_shares == {}
    assert ticks.measured.refused_rules == ()
    assert before.refused_rules == ("empty-eligible-set", "empty-eligible-set")

    bank_issuer = before.payloads[_BANK_TICKER]["issuer_id"]
    for issuer_id, row in before.core.items():
        assert row[2] == "unavailable"
        if issuer_id == bank_issuer:
            assert row[14] == ["missing_preferred_stock_value"]
        else:
            assert row[14] == ["missing_listing_unit_ratio"]


def test_the_history_projector_converts_shares_by_the_cover_ratio_and_drops_unparsed_counts() -> None:
    """`strategy_history.project_issuer_rows` reads the cover at `as_of`, divides `shares_outstanding` by
    `listing_units_per_share`, and drops `shares_outstanding` when the cover does not parse."""
    from data_engine.datahub.production_topt.concept_mapping import DEFAULT_RULESET
    from data_engine.datahub.standards.filing_extraction import FilingDocument
    from data_engine.datahub.strategy_history import HistoryIssuer, LastClose, project_issuer_rows
    from factors.production_topt import OperatingBranch

    cutoff = datetime(2026, 4, 1, tzinfo=UTC)
    close = LastClose(Decimal("40"), Decimal("0.90"), datetime(2026, 3, 31, 20, 0, tzinfo=UTC))
    document = {
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            {
                                "end": PERIOD,
                                "start": "2025-01-01",
                                "val": 100_000_000,
                                "filed": FILED,
                                "form": "20-F",
                                "fy": 2025,
                                "fp": "FY",
                                "accn": "0001-2026-02-20",
                            }
                        ]
                    }
                },
                "GrossProfit": {
                    "units": {
                        "USD": [
                            {
                                "end": PERIOD,
                                "start": "2025-01-01",
                                "val": 210_000_000,
                                "filed": FILED,
                                "form": "20-F",
                                "fy": 2025,
                                "fp": "FY",
                                "accn": "0001-2026-02-20",
                            }
                        ]
                    }
                },
                "Assets": {
                    "units": {
                        "USD": [
                            {
                                "end": PERIOD,
                                "val": 500_000_000,
                                "filed": FILED,
                                "form": "20-F",
                                "fy": 2025,
                                "fp": "FY",
                                "accn": "0001-2026-02-20",
                            }
                        ]
                    }
                },
                "CommonStockSharesOutstanding": {
                    "units": {
                        "shares": [
                            {
                                "end": PERIOD,
                                "val": 10_000_000,
                                "filed": FILED,
                                "form": "20-F",
                                "fy": 2025,
                                "fp": "FY",
                                "accn": "0001-2026-02-20",
                            }
                        ]
                    }
                },
            }
        }
    }
    raw_bytes = json.dumps(document).encode()

    def project(ticker: str, cover: bytes | None) -> dict[str, Decimal]:
        issuer = HistoryIssuer(
            issuer_id=f"issuer:{ticker.lower()}",
            instrument_id=f"security:{ticker.lower()}",
            listing_id=f"listing:xnas:{ticker.lower()}",
            ticker=ticker,
            cik=900001,
            operating_branch=OperatingBranch.NON_FINANCIAL,
        )
        reader = CoverRatioReader(
            lambda cik, _as_of: (
                None
                if cover is None
                else FilingDocument(
                    cik=cik,
                    accession="000090000126000001",
                    form="20-F",
                    filing_date=date(2026, 2, 20),
                    primary_document="cover.htm",
                    url="https://www.sec.gov/Archives/edgar/data/900001/000090000126000001/cover.htm",
                    body=cover,
                )
            )
        )
        projection = project_issuer_rows(
            issuer,
            [cutoff],
            load_facts=lambda _cik: (raw_bytes, document),
            headcount_extractor=lambda _cik, _as_of: None,
            last_close=lambda _symbol, _cutoff: close,
            split_events=lambda _symbol: (),
            ruleset=DEFAULT_RULESET,
            listing_ratio_reader=reader,
        )
        return {row.input_key: row.value for row in projection.rows if row.fiscal_period is None}

    pdd_rows = project("PDD", cover_html(("PDD", PDD_TITLE)))
    assert pdd_rows["shares_outstanding"] == Decimal("2500000")
    assert pdd_rows["last_close"] == Decimal("40")
    assert pdd_rows["revenue"] == Decimal("100000000")

    plain_rows = project("MSFT", cover_html(("MSFT", PLAIN_TITLE)))
    assert plain_rows["shares_outstanding"] == Decimal("10000000")

    unfamiliar_rows = project("AMZN", cover_html(("AMZN", UNFAMILIAR_TITLE)))
    assert "shares_outstanding" not in unfamiliar_rows
    assert unfamiliar_rows["revenue"] == Decimal("100000000")
    assert unfamiliar_rows["last_close"] == Decimal("40")

    missing_filing_rows = project("GOOGL", None)
    assert "shares_outstanding" not in missing_filing_rows


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
