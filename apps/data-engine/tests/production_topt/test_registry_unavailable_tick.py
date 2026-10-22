"""#1323 through the deployed tick (`run_topt_live_tick`) with a fake SEC socket that times out.

Staging, 2026-10-20 22:45 UTC: `topt_live_pipeline` failed after 1.7 minutes with
`could not resolve the operating branch for CIK 0001730168: The read operation timed out`.
One SEC read timeout of one issuer failed the whole tick.

Here the issuer registry (SEC submissions) is the real code over a fake socket. The socket times
out for one CIK. The tick retries with backoff, excludes only that issuer as `registry_unavailable`,
publishes the other nineteen decisions and advances the pointer.
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from dataclasses import replace
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

import dagster as dg
import httpx
import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub.production_topt import composition, issuer_registry, sec_financial_adapter
from data_engine.sources import sec
from truealpha_contracts.strategy import ExclusionReason
from truealpha_runtime.testing import isolated_test_database

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "support"))
from production_topt.test_degraded_capture_forced import (  # noqa: E402
    _arm,
    _bundle,
    _live_topt_tick,
    _offline_routes,
    _quote,
)
from production_topt.test_plausibility_tick import _HEAD_SQL, _LISTING_SQL  # noqa: E402
from macro_seed import store_risk_free_series  # noqa: E402
from split_series_seed import store_topt_series  # noqa: E402

#: The settled session the tick prices on.
SESSION = date(2026, 4, 8)
#: The ticker whose registry read times out. A non-bank, so its numerator is the gross profit.
TIMED_OUT_TICKER = "NVDA"
_BANK_TICKER = "JPM"
#: The deployed route builder, taken before any test replaces it.
_REAL_BUILD_ROUTES = composition.build_routes
#: The financial-fact payload of one issuer in one run.
_PAYLOAD_SQL = """
    select p.normalized_payload
    from raw.capture_obligations ob
    join staging.capture_observation_obligations link on link.capture_obligation_id = ob.obligation_id
    join staging.capture_observation_payloads p on p.observation_id = link.observation_id
    where ob.run_id = %s and ob.capture_requirement_id = 'financial-fact:v1'
      and p.normalized_payload->>'issuer_id' = %s and p.normalized_payload ? 'gross_profit'
"""
#: The reason the owner named. `ExclusionReason.REGISTRY_UNAVAILABLE` must carry exactly this text.
REASON = "registry_unavailable"

_SUBMISSIONS_PATH = re.compile(r"/submissions/CIK(\d{10})\.json$")


@pytest.fixture(scope="module")
def tick_database_url():
    with isolated_test_database("registry_unavailable_tick") as target_url:
        store_topt_series(target_url)
        # The tick reads the risk-free rate of its cutoff from the stored series (#1255).
        store_risk_free_series(target_url)
        yield target_url


class _FakeSecSocket:
    """The SEC host as the registry sees it: the ticker crosswalk and one submissions document per CIK.

    A CIK in `timing_out` raises the httpx read timeout of the staging log.
    """

    def __init__(self, cik_by_ticker: dict[str, int], *, timing_out: set[int]) -> None:
        self.cik_by_ticker = cik_by_ticker
        self.timing_out = timing_out
        self.submissions_reads: list[int] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == sec.TICKERS_URL:
            rows = {
                str(index): {"cik_str": cik, "ticker": ticker.replace(".", "-"), "title": ticker}
                for index, (ticker, cik) in enumerate(sorted(self.cik_by_ticker.items()))
            }
            return httpx.Response(200, json=rows)
        match = _SUBMISSIONS_PATH.search(request.url.path)
        assert match is not None, f"the registry asked the socket for {request.url}"
        cik = int(match.group(1))
        self.submissions_reads.append(cik)
        if cik in self.timing_out:
            raise httpx.ReadTimeout("The read operation timed out", request=request)
        bank = cik == self.cik_by_ticker.get(_BANK_TICKER)
        return httpx.Response(200, json={"sic": "6021" if bank else "3674", "filings": {"recent": {}}})


#: The fiscal period that the fake facts report. A flagged payload must drop it with the numerators.
_PERIOD = date(2025, 12, 31)


def _bundle_with_periods(branch: Any) -> Any:
    """The offline bundle with both operating numerators and the period of the operating metric set."""
    bundle = _bundle(branch)
    return replace(
        bundle,
        pre_provision_profit=bundle.pre_provision_profit or Decimal("5000000"),
        operating_period_end=_PERIOD,
        revenue_period_end=_PERIOD,
    )


def _arm_registry(
    monkeypatch: pytest.MonkeyPatch, *, timing_out_tickers: set[str] | None
) -> tuple[_FakeSecSocket, list[float]]:
    """The deployed route builder with a fake SEC socket. Prices and company facts stay offline.

    `timing_out_tickers` None means the whole SEC host times out."""
    _arm(monkeypatch, quote=lambda: _quote(SESSION, Decimal("40")), price_cutoff=SESSION)
    socket = _FakeSecSocket({}, timing_out=set())
    pauses: list[float] = []

    def build(plan: Any, connection: Any = None) -> dict[str, Any]:
        offline = _offline_routes(plan, connection, quote=lambda: _quote(SESSION, Decimal("40")), price_cutoff=SESSION)
        # The numbering of `_offline_routes`: the headcount facts it records sit under these CIKs.
        for subject_id, (_issuer, _instrument, _listing, ticker) in plan.coordinates.items():
            socket.cik_by_ticker.setdefault(ticker, 100000 + sorted(plan.coordinates).index(subject_id))
        socket.timing_out = {
            cik
            for ticker, cik in socket.cik_by_ticker.items()
            if timing_out_tickers is None or ticker in timing_out_tickers
        }
        routes = _REAL_BUILD_ROUTES(plan, connection)
        for work_item_id, binding in plan.bindings.items():
            if binding.obligation.capture_requirement_id != "financial-fact:v1":
                routes[work_item_id] = offline[work_item_id]
        return routes

    monkeypatch.setattr(composition, "build_routes", build)
    monkeypatch.setattr(sec, "client", lambda: httpx.Client(transport=httpx.MockTransport(socket.handle)))
    # The facts come from the offline bundle of each CIK's branch; the registry code is the real one.
    monkeypatch.setattr(
        sec_financial_adapter,
        "sec_financial_fetcher",
        lambda cik, cutoff, branch, **_kwargs: _bundle_with_periods(branch),
    )
    monkeypatch.setattr(issuer_registry, "_pause", pauses.append, raising=False)
    return socket, pauses


def test_the_payload_contract_names_the_key_the_registry_writes() -> None:
    """The freeze validates every financial-fact payload against `FinancialFactPayload`, which forbids extras."""
    from data_engine.datahub.production_topt.materialization import FinancialFactPayload

    assert issuer_registry.UNAVAILABLE_PAYLOAD_KEY in FinancialFactPayload.model_fields
    assert ExclusionReason.REGISTRY_UNAVAILABLE.value == REASON


def _decisions(reader: psycopg.Connection[Any], strategy_run_id: str) -> dict[str, str | None]:
    return dict(
        reader.execute(
            "select issuer_id, exclusion_reason from mart.strategy_decisions where strategy_run_id = %s",
            (strategy_run_id,),
        ).fetchall()
    )


def _issuer_of(reader: psycopg.Connection[Any], run_id: str, ticker: str) -> str:
    (coordinates,) = reader.execute(_LISTING_SQL, (run_id, ticker)).fetchall()
    return str(coordinates[1])


def test_one_registry_timeout_excludes_one_issuer_and_the_tick_publishes_the_rest(
    tick_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1323: three attempts with a 1 s then 2 s backoff, then only the timed-out issuer is excluded."""
    socket, pauses = _arm_registry(monkeypatch, timing_out_tickers={TIMED_OUT_TICKER})

    metadata = _live_topt_tick(tick_database_url, monkeypatch, executed_at=_at(22, 15), force_fetch=True, accept=True)

    timed_out_cik = socket.cik_by_ticker[TIMED_OUT_TICKER]
    assert socket.submissions_reads.count(timed_out_cik) == 3
    assert pauses == [1.0, 2.0], "the wait doubles between the attempts and ends after the last one"
    others = [cik for cik in socket.submissions_reads if cik != timed_out_cik]
    assert len(others) == len(set(others)) == len(set(socket.cik_by_ticker.values())) - 1, "one read per other CIK"
    with psycopg.connect(tick_database_url) as reader:
        issuer = _issuer_of(reader, metadata["capture_run_id"], TIMED_OUT_TICKER)
        decisions = _decisions(reader, metadata["strategy_run_id"])
        assert len(decisions) == 20
        assert [i for i, why in decisions.items() if why == REASON] == [issuer]
        assert decisions[issuer] == REASON
        # the pointer advanced to this run
        assert reader.execute(_HEAD_SQL).fetchone()[0] == metadata["capture_run_id"]
        # the evidence: both numerators are withheld, the flag is written, the honest facts stay
        (payload,) = [row[0] for row in reader.execute(_PAYLOAD_SQL, (metadata["capture_run_id"], issuer)).fetchall()]
        assert payload["gross_profit"] is None and payload["pre_provision_profit"] is None
        assert payload["registry_unavailable"] is True and payload["operating_period_end"] is None
        assert payload["revenue"] is not None and payload["total_assets"] is not None
        assert payload["revenue_period_end"] == _PERIOD.isoformat(), "the honest facts keep their period"
        # the same bundle, for an issuer the registry classified, keeps its numerators and its period
        (classified,) = [
            row[0]
            for row in reader.execute(
                _PAYLOAD_SQL, (metadata["capture_run_id"], _issuer_of(reader, metadata["capture_run_id"], "AAPL"))
            ).fetchall()
        ]
        assert classified["gross_profit"] is not None and classified["pre_provision_profit"] is not None
        assert classified["operating_period_end"] == _PERIOD.isoformat()
        assert "registry_unavailable" not in classified
        # the row states a gap
        (availability,) = reader.execute(
            "select availability from mart.topt_core_results where run_id = %s and issuer_id = %s",
            (metadata["capture_run_id"], issuer),
        ).fetchone()
        assert availability != "available"
    assert metadata["registry_unavailable_excluded"] == [issuer]


def test_the_next_unforced_tick_asks_the_registry_again(
    tick_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The observation of an unclassified issuer never satisfies another run (the 12 h reuse window of #635).

    Tick A cannot read the registry for NVDA. Tick B, 15 minutes later and not forced, reads it.
    Without the guard in the reuse query, B would reuse A's flagged observation and keep NVDA excluded."""
    _arm_registry(monkeypatch, timing_out_tickers={TIMED_OUT_TICKER})
    first = _live_topt_tick(tick_database_url, monkeypatch, executed_at=_at(22, 30), force_fetch=True, accept=True)
    assert len(first["registry_unavailable_excluded"]) == 1
    _arm_registry(monkeypatch, timing_out_tickers=set())

    second = _live_topt_tick(tick_database_url, monkeypatch, executed_at=_at(22, 45), accept=True)

    assert second["registry_unavailable_excluded"] == []
    with psycopg.connect(tick_database_url) as reader:
        issuer = _issuer_of(reader, second["capture_run_id"], TIMED_OUT_TICKER)
        assert _decisions(reader, second["strategy_run_id"])[issuer] != REASON
        (availability,) = reader.execute(
            "select availability from mart.topt_core_results where run_id = %s and issuer_id = %s",
            (second["capture_run_id"], issuer),
        ).fetchone()
        assert availability == "available"


def test_a_registry_outage_costs_one_attempt_per_issuer_after_three_and_the_empty_eligible_set_refuses_the_run(
    tick_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every read times out. Three issuers spend three attempts each, the 18 others one attempt each, and the run
    is refused: the empty eligible set is the only run refusal."""
    socket, _pauses = _arm_registry(monkeypatch, timing_out_tickers=None)
    monkeypatch.setattr(settings, "database_url", tick_database_url)
    with psycopg.connect(tick_database_url) as reader:
        head_before = reader.execute(_HEAD_SQL).fetchone()

    from data_engine.lanes.capture import ToptLiveTickConfig, run_topt_live_tick

    config = ToptLiveTickConfig(executed_at=_at(23, 0).isoformat(), force_fetch=True)
    with pytest.raises(dg.Failure, match="all 20 decisions are excluded"):
        run_topt_live_tick(dg.build_op_context(), config)

    # `_offline_routes` gives each of the 21 listings its own fake CIK (GOOG and GOOGL are two listings of one
    # issuer, so 21 CIKs stand for 20 issuers). The three lowest CIKs spend three attempts. Each of the 18 others
    # gets one attempt, and none is left unread: 3 x 3 + 18 = 27 reads.
    ciks = sorted(set(socket.cik_by_ticker.values()))
    assert len(ciks) == 21
    assert Counter(socket.submissions_reads) == {cik: 3 if cik in ciks[:3] else 1 for cik in ciks}
    assert len(socket.submissions_reads) == 3 * 3 + 18
    with psycopg.connect(tick_database_url) as reader:
        assert reader.execute(_HEAD_SQL).fetchone() == head_before, "the pointer stays on the previous accepted run"


def _at(hour: int, minute: int) -> datetime:
    """A tick time of the session day: the same day keeps every core row fresh."""
    return datetime.combine(SESSION, time(hour, minute), tzinfo=UTC)
