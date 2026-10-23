"""The issuer registry reads SEC submissions with a bounded retry, and never names an issuer it did not read (#1323).

Staging, 2026-10-20 22:45 UTC: one read timeout for one issuer failed the tick. The registry code here is the
deployed code. The SEC host is a fake httpx transport, so each test says which request fails and how.
No database: the call ledger of the gateway keeps its records in memory.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable

import httpx
import pytest
from data_engine.datahub.production_topt import issuer_registry
from data_engine.sources import gateway, sec
from data_engine.sources.gateway import BudgetExhausted
from factors.production_topt import OperatingBranch

#: A request outcome: an HTTP status with a body, or an exception that the socket raises.
Outcome = httpx.Response | Exception

_CIK = re.compile(r"/submissions/CIK(\d{10})\.json$")


def ok(sic: str = "3674") -> httpx.Response:
    return httpx.Response(200, json={"sic": sic, "filings": {"recent": {}}})


def timeout() -> Exception:
    return httpx.ReadTimeout("The read operation timed out")


class FakeSec:
    """The SEC host. `script(cik, attempt)` gives the outcome of the `attempt`-th request (from 1) for `cik`."""

    def __init__(self, script: Callable[[int, int], Outcome]) -> None:
        self.script = script
        self.reads: list[int] = []
        #: The timeouts that the request carried to the transport: what a real transport would wait.
        self.timeouts: list[dict[str, float | None]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        match = _CIK.search(request.url.path)
        assert match is not None, f"the registry asked the socket for {request.url}"
        cik = int(match.group(1))
        self.reads.append(cik)
        self.timeouts.append(request.extensions["timeout"])
        outcome = self.script(cik, self.reads.count(cik))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def pauses(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []
    monkeypatch.setattr(issuer_registry, "_pause", recorded.append)
    return recorded


def read(
    monkeypatch: pytest.MonkeyPatch, script: Callable[[int, int], Outcome], ciks: list[int]
) -> tuple[FakeSec, issuer_registry.RegistryReading]:
    socket = FakeSec(script)
    monkeypatch.setattr(sec, "client", lambda: httpx.Client(transport=httpx.MockTransport(socket.handle)))
    reading = issuer_registry.resolve_issuer_classifications({f"T{cik}": cik for cik in ciks})
    return socket, reading


def test_a_service_error_that_clears_is_tried_again_with_a_doubling_wait(monkeypatch, pauses) -> None:
    def script(cik: int, attempt: int) -> Outcome:
        return httpx.Response(503) if attempt < 3 else ok("6021")

    socket, reading = read(monkeypatch, script, [1])

    assert socket.reads == [1, 1, 1]
    assert pauses == [1.0, 2.0]
    assert reading.unavailable == {}
    assert reading.classifications[1].operating_branch is OperatingBranch.FINANCIAL


def test_a_rate_limit_is_tried_again(monkeypatch, pauses) -> None:
    socket, reading = read(monkeypatch, lambda cik, attempt: httpx.Response(429) if attempt == 1 else ok(), [1])

    assert socket.reads == [1, 1]
    assert pauses == [1.0]
    assert 1 in reading.classifications


def test_a_missing_issuer_and_a_block_are_not_tried_again(monkeypatch, pauses) -> None:
    for status in (404, 403):
        socket, reading = read(monkeypatch, lambda cik, attempt, status=status: httpx.Response(status), [1])

        assert socket.reads == [1], status
        assert list(reading.unavailable) == [1]
        assert "HTTPStatusError" in reading.unavailable[1]
    assert pauses == []


def test_submissions_that_are_not_a_json_object_name_the_issuer_and_do_not_raise(monkeypatch, pauses) -> None:
    socket, reading = read(monkeypatch, lambda cik, attempt: httpx.Response(200, json=["not", "an", "object"]), [1])

    assert socket.reads == [1]
    assert "not a JSON object" in reading.unavailable[1]


def test_a_200_answer_that_is_not_a_json_object_does_not_land_and_the_next_caller_asks_again(
    monkeypatch, pauses, call_ledger, landed_keys
) -> None:
    """The index of landed keys serves a landed key for the whole UTC day (#1411). An answer that is not the
    submissions of the issuer must stay out of it, or the issuer stays unavailable until the day ends."""
    monkeypatch.setattr(gateway, "utc_day", lambda: "2026-10-22")  # one key for the whole test, at any hour
    key = sec.submissions_key(1)
    script = lambda cik, attempt: httpx.Response(200, json=["not", "an", "object"]) if attempt == 1 else ok("6021")  # noqa: E731
    socket, first = read(monkeypatch, script, [1])

    assert "not a JSON object" in first.unavailable[1]
    assert landed_keys.bodies == {}, "the refused answer is not in the index"
    assert [(row.ok, row.fetch_key) for row in call_ledger] == [(True, None)], "the refused row carries no key"

    second = issuer_registry.resolve_issuer_classifications({"T1": 1})

    assert socket.reads == [1, 1], "the second caller asked SEC again"
    assert second.unavailable == {}
    assert set(landed_keys.bodies) == {key}, "the accepted answer landed"
    assert [(row.ok, row.fetch_key) for row in call_ledger] == [(True, None), (True, key.ledger_key)]


def test_a_spent_budget_names_the_issuer_and_does_not_raise(monkeypatch, pauses) -> None:
    def spent(cik: int, http: object = None) -> dict:
        raise BudgetExhausted("sec", environment="staging", budget=5000, spent=5000)

    monkeypatch.setattr(sec, "client", lambda: httpx.Client(transport=httpx.MockTransport(lambda request: ok())))
    monkeypatch.setattr(sec, "fetch_company_submissions", spent)

    reading = issuer_registry.resolve_issuer_classifications({"T1": 1})

    assert "BudgetExhausted" in reading.unavailable[1]
    assert pauses == []


def test_a_short_burst_of_errors_excludes_the_issuers_it_hit_and_reads_every_other_issuer(monkeypatch, pauses) -> None:
    """Three issuers time out, then the host answers. The 17 others are read, not excluded unread."""
    ciks = list(range(1, 21))
    socket, reading = read(monkeypatch, lambda cik, attempt: timeout() if cik <= 3 else ok(), ciks)

    assert sorted(reading.unavailable) == [1, 2, 3]
    assert sorted(reading.classifications) == list(range(4, 21))
    assert len(socket.reads) == 3 * 3 + 17, "three issuers spend three attempts each, the other 17 are read once"


def test_a_long_outage_costs_one_attempt_per_issuer_after_the_third_failed_issuer(monkeypatch, pauses) -> None:
    ciks = list(range(1, 21))
    socket, reading = read(monkeypatch, lambda cik, attempt: timeout(), ciks)

    assert sorted(reading.unavailable) == ciks
    assert len(socket.reads) == 3 * 3 + 17
    assert len(pauses) == 3 * 2, "the issuers after the trip wait for nothing"
    assert all("ReadTimeout" in reason for reason in reading.unavailable.values()), "each issuer was read and failed"


def _burst_then_timeouts(kind: str, monkeypatch) -> tuple[FakeSec, issuer_registry.RegistryReading]:
    """The three lowest CIKs fail in `kind`; the 17 others time out. A tripped breaker reads each of the 17 once."""
    ciks = list(range(1, 21))
    first = {1, 2, 3}

    def script(cik: int, attempt: int) -> Outcome:
        if cik not in first:
            return timeout()
        return {"403": httpx.Response(403), "not-an-object": httpx.Response(200, json=[1])}[kind]

    if kind != "budget":
        return read(monkeypatch, script, ciks)
    socket = FakeSec(lambda cik, attempt: timeout())
    monkeypatch.setattr(sec, "client", lambda: httpx.Client(transport=httpx.MockTransport(socket.handle)))
    real = sec.fetch_company_submissions

    def fetch(cik: int, http: httpx.Client | None = None) -> dict:
        if cik in first:
            raise BudgetExhausted("sec", environment="staging", budget=5000, spent=5000)
        return real(cik, http)

    monkeypatch.setattr(sec, "fetch_company_submissions", fetch)
    return socket, issuer_registry.resolve_issuer_classifications({f"T{cik}": cik for cik in ciks})


@pytest.mark.parametrize("kind", ["403", "not-an-object", "budget"])
def test_a_failure_that_is_not_retried_still_counts_toward_an_outage(monkeypatch, pauses, kind) -> None:
    """Without the count, 17 timeouts would spend 3 attempts each until three of them trip the breaker."""
    socket, reading = _burst_then_timeouts(kind, monkeypatch)

    assert sorted(reading.unavailable) == list(range(1, 21))
    reads = Counter(socket.reads)
    assert all(reads[cik] == 1 for cik in range(4, 21)), f"{kind}: each of the 17 others gets one attempt"
    assert pauses == [], f"{kind}: nothing was retried, so nothing waited"


def _gzip_that_is_not_gzip() -> httpx.Response:
    return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=httpx.ByteStream(b"not a gzip body"))


@pytest.mark.parametrize(
    "broken",
    [
        pytest.param(lambda: httpx.Response(200, content=b'{"sic": "36'), id="truncated-json"),
        pytest.param(_gzip_that_is_not_gzip, id="decoding-error"),
        pytest.param(lambda: httpx.Response(200, content=b'{"sic": "\xff'), id="cut-utf8"),
    ],
)
def test_a_body_cut_short_is_tried_again(monkeypatch, pauses, broken) -> None:
    """Each attempt gets a fresh response: a response body is read once."""
    socket, reading = read(monkeypatch, lambda cik, attempt: broken() if attempt < 3 else ok(), [1])

    assert socket.reads == [1, 1, 1]
    assert pauses == [1.0, 2.0]
    assert 1 in reading.classifications


def test_every_request_of_the_pass_carries_a_timeout_of_ten_seconds(monkeypatch, pauses) -> None:
    """Measured on staging: the longest successful submissions read took 3.6 s, and the one failure waited the
    30 s of `sec.client()`. The client of the pass is built with 30 s. The request that reaches the transport must
    carry 10 s for connect, read, write and pool: a timeout passed to the fetch function would override it."""
    socket = FakeSec(lambda cik, attempt: timeout() if cik == 2 else ok())
    monkeypatch.setattr(sec, "client", lambda: httpx.Client(transport=httpx.MockTransport(socket.handle), timeout=30.0))

    issuer_registry.resolve_issuer_classifications({"T1": 1, "T2": 2, "T3": 3})

    wanted = {"connect": 10.0, "read": 10.0, "write": 10.0, "pool": 10.0}
    assert len(socket.timeouts) == 5, "one read each for two issuers, three attempts for the one that times out"
    assert all(carried == wanted for carried in socket.timeouts)


def test_a_missing_issuer_neither_resets_nor_counts_toward_an_outage(monkeypatch, pauses) -> None:
    """Odd CIKs time out, even CIKs are unknown to EDGAR (404). The third timeout trips the breaker even
    though a 404 sits between the timeouts, so the later odd CIKs get one attempt each."""
    ciks = list(range(1, 13))
    socket, reading = read(monkeypatch, lambda cik, attempt: timeout() if cik % 2 else httpx.Response(404), ciks)

    assert sorted(reading.unavailable) == ciks
    # CIKs 1, 3 and 5 time out three times. After CIK 5 the breaker is tripped: CIKs 6..12 get one attempt each.
    assert len(socket.reads) == 3 + 1 + 3 + 1 + 3 + 7
