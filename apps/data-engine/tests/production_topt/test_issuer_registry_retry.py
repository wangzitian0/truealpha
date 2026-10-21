"""The issuer registry reads SEC submissions with a bounded retry, and never names an issuer it did not read (#1323).

Staging, 2026-10-20 22:45 UTC: one read timeout for one issuer failed the tick. The registry code here is the
deployed code. The SEC host is a fake httpx transport, so each test says which request fails and how.
No database: the call ledger of the gateway keeps its records in memory.
"""

from __future__ import annotations

import re
from collections.abc import Callable

import httpx
import pytest
from data_engine.datahub.production_topt import issuer_registry
from data_engine.sources import sec
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

    def handle(self, request: httpx.Request) -> httpx.Response:
        match = _CIK.search(request.url.path)
        assert match is not None, f"the registry asked the socket for {request.url}"
        cik = int(match.group(1))
        self.reads.append(cik)
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


def test_a_block_counts_toward_an_outage(monkeypatch, pauses) -> None:
    """EDGAR answers 403 to a blocked client for every issuer. 403 is not tried again and still trips the breaker."""
    ciks = list(range(1, 21))
    socket, reading = read(monkeypatch, lambda cik, attempt: httpx.Response(403), ciks)

    assert sorted(reading.unavailable) == ciks
    assert socket.reads == ciks, "one read per issuer"
    assert pauses == []


def test_a_missing_issuer_neither_resets_nor_counts_toward_an_outage(monkeypatch, pauses) -> None:
    """Odd CIKs time out, even CIKs are unknown to EDGAR (404). The third timeout trips the breaker even
    though a 404 sits between the timeouts, so the later odd CIKs get one attempt each."""
    ciks = list(range(1, 13))
    socket, reading = read(monkeypatch, lambda cik, attempt: timeout() if cik % 2 else httpx.Response(404), ciks)

    assert sorted(reading.unavailable) == ciks
    # CIKs 1, 3 and 5 time out three times. After CIK 5 the breaker is tripped: CIKs 6..12 get one attempt each.
    assert len(socket.reads) == 3 + 1 + 3 + 1 + 3 + 7
