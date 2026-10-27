"""Every external call of a standards job is one ledger row that names its fetch key (#1521, step 2).

The jobs run through their deployed entry points (`run_analyst_ratings`, `extract_headcount` on a
`SourceGateway`, `select_headcount` and the model key probe) over fake vendors. Each row goes through
the one ledger writer into a real `staging.api_call_ledger`, and the test reads the rows back.
The assertions fail on a call that carries no identity, whichever site makes it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import dagster as dg
import psycopg
import pytest
from data_engine.config import settings
from data_engine.lanes import standards
from data_engine.sources import gateway
from data_engine.sources import moomoo as mm

TESTS_DIR = Path(__file__).resolve().parent


def _rows(source: str) -> list[tuple[str, str | None]]:
    """(endpoint, fetch_key) of every ledger row of ``source``, oldest first."""
    return gateway._pg_execute(
        "select endpoint, fetch_key from staging.api_call_ledger where source = %s order by id", (source,)
    ).fetchall()


def _count(source: str) -> int:
    return len(_rows(source))


class _OpenD:
    """A quote context answering the three research endpoints the analyst job asks for."""

    def __init__(self) -> None:
        self.calls = 0

    def get_research_analyst_consensus(self, code: str):
        self.calls += 1
        return mm.moomoo.RET_OK, {"rating": 4, "total": 10, "buy": 60.0, "hold": 30.0, "sell": 10.0}

    def get_research_rating_summary(self, code: str, **_kwargs: Any):
        self.calls += 1
        return mm.moomoo.RET_OK, {"analyst_rating_summary_list": []}

    def get_valuation_detail(self, code: str, **_kwargs: Any):
        self.calls += 1
        return mm.moomoo.RET_OK, {}


@pytest.fixture
def analyst_world(moomoo_ledger_db: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[_OpenD]:
    """A captured TOPT head on the ledger database, the lane ops on that database, and a fake OpenD.

    The ledger connection opens before `psycopg.connect` is replaced, so the ledger keeps its own
    autocommit connection while the ops share the test's transaction."""
    monkeypatch.syspath_prepend(str(TESTS_DIR))
    import test_canonical_issuer_resolution as world

    gateway._pg()
    connection = psycopg.connect(settings.database_url, connect_timeout=3, autocommit=False)
    lane = world._LaneConnection(connection)
    monkeypatch.setattr(psycopg, "connect", lambda *_a, **_k: lane)
    opend = _OpenD()

    @contextmanager
    def connect() -> Iterator[_OpenD]:
        yield opend

    monkeypatch.setattr(mm, "connect", connect)
    monkeypatch.setattr(mm, "throttle", lambda: None)
    world._capture_head(connection, monkeypatch)
    try:
        yield opend
    finally:
        connection.rollback()
        connection.close()


def _run_analyst_ratings() -> dict[str, Any]:
    import test_canonical_issuer_resolution as world

    config = standards.StandardBackfillConfig(executed_at=world.EXECUTED_AT, universe=world.UNIVERSE)
    return json.loads(standards.run_analyst_ratings(dg.build_op_context(), config, "{}"))


def test_every_analyst_ratings_call_names_its_fetch_key(analyst_world: _OpenD) -> None:
    summary = _run_analyst_ratings()

    rows = _rows("moomoo")
    assert summary["rows"] > 0 and analyst_world.calls > 0, "the job reached the vendor"
    assert len(rows) == analyst_world.calls, "one ledger row per OpenD request"
    assert {endpoint for endpoint, _key in rows} == {
        "get_research_analyst_consensus",
        "get_research_rating_summary",
        "get_valuation_detail",
    }
    assert [endpoint for endpoint, key in rows if key is None] == [], "a moomoo row without a fetch key"


def test_a_second_analyst_ratings_run_on_one_utc_day_asks_moomoo_nothing(analyst_world: _OpenD) -> None:
    _run_analyst_ratings()
    first = _count("moomoo")
    assert first > 0

    _run_analyst_ratings()

    assert _count("moomoo") == first, "the second run reads the landed bytes of each key"


def test_every_sec_call_of_a_filing_reader_names_its_fetch_key(moomoo_ledger_db: str, monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(TESTS_DIR))
    import test_standard_backfill as backfill

    connection = backfill.FakeConnection()
    outcome = backfill.extract_headcount(
        1730168,
        connection=connection,
        http=backfill._fixture_edgar(),
        gateway=backfill._gateway(connection),
        standard=backfill.STANDARD,
        cutoff=backfill.CUTOFF,
        write=False,
        store=backfill.FakeStore(),
    )

    rows = _rows("sec")
    assert outcome.status == "resolved" and len(rows) == 2, "the submissions index and the filing"
    assert [endpoint for endpoint, key in rows if key is None] == []


def test_every_model_call_names_its_fetch_key(moomoo_ledger_db: str, monkeypatch) -> None:
    from data_engine.quality import model_key_health
    from data_engine.sources import llm

    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_model", "glm-test")
    answer = json.dumps(
        {
            "model": "glm-served",
            "choices": [
                {"message": {"content": json.dumps({"value": 17581, "candidate_index": 0, "reason": "total"})}}
            ],
            "usage": {"prompt_tokens": 244, "completion_tokens": 61, "total_tokens": 305},
        }
    ).encode()

    def transport(_url: str, _headers: dict[str, str], _body: bytes) -> tuple[int, bytes]:
        return 200, answer

    llm.select_headcount(
        None,
        cik=4904,
        accession="000000490426000013",
        form="10-K",
        issuer_label="AEP",
        candidates=[llm.Candidate(17581, "As of December 31, 2025, AEP had a total of 17,581 employees.")],
        caller="identity-test",
        persist=False,
        transport=transport,
        now=lambda: datetime(2026, 9, 7, 12, tzinfo=UTC),
    )
    model_key_health.probe(transport=transport)

    rows = _rows(llm.SOURCE)
    assert len(rows) == 2, "the headcount selection and the key health probe"
    assert [endpoint for endpoint, key in rows if key is None] == [], "a model row without a fetch key"


# --- #1521 step 3: the gateway derives the repeat reason ------------------------------------


class _HttpResponse:
    """An httpx response: bytes in `content`, a status, and the checks that the callers use."""

    status_code = 200

    def __init__(self, body: bytes) -> None:
        assert isinstance(body, bytes), "an httpx response holds bytes"
        self.content = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return json.loads(self.content)


class _Http:
    """An httpx-shaped client that answers every GET with one body."""

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.urls: list[str] = []

    def __enter__(self) -> _Http:
        return self

    def __exit__(self, *_exc: object) -> None: ...

    def get(self, url: str, **_kwargs: Any) -> _HttpResponse:
        self.urls.append(url)
        return _HttpResponse(self.body)


def _reasons(source: str) -> list[str | None]:
    """The repeat reason of every ledger row of ``source``, oldest first."""
    rows = gateway._pg_execute(
        "select repeat_reason from staging.api_call_ledger where source = %s order by id", (source,)
    ).fetchall()
    return [reason for (reason,) in rows]


_SUBMISSIONS = json.dumps({"filings": {"recent": {"form": [], "filingDate": [], "accessionNumber": []}}}).encode()
_HTML = b"<html><body>Request Rate Threshold Exceeded</body></html>"


def test_a_forced_fetch_writes_the_key_and_the_reason_probe(moomoo_ledger_db: str, landed_keys) -> None:
    from data_engine.sources import sec

    with gateway.probe_scope(True):
        sec.fetch_company_submissions(320193, http=_Http(_SUBMISSIONS))

    ((endpoint, key),) = _rows("sec")
    assert key == sec.submissions_key(320193).ledger_key, "a probe row names its key"
    assert _reasons("sec") == ["probe"]
    assert landed_keys.bodies == {}, "a probe lands nothing"


def test_a_window_that_is_not_final_writes_the_key_and_the_reason_window_not_final(
    moomoo_ledger_db: str, landed_keys, monkeypatch: pytest.MonkeyPatch
) -> None:
    from data_engine.sources import yahoo

    http = _Http(b'{"chart": {"result": []}}')
    monkeypatch.setattr(yahoo.httpx, "Client", lambda **_kwargs: http)

    yahoo.fetch_daily_chart("AAPL")  # no `end`: the window ends today and may still change

    ((endpoint, key),) = _rows("yahoo")
    assert key is not None and key.startswith("yahoo|chart"), "an unfinished window names its key"
    assert _reasons("yahoo") == ["window_not_final"]
    assert landed_keys.bodies == {}


def test_an_html_answer_for_the_submissions_key_is_not_landed_and_writes_answer_refused(
    moomoo_ledger_db: str, landed_keys
) -> None:
    from datetime import UTC, datetime

    from data_engine.datahub.standards.filing_extraction import latest_periodic_filing
    from data_engine.sources import sec

    http = _Http(_HTML)
    with pytest.raises(ValueError):
        latest_periodic_filing(320193, http=http, cutoff=datetime(2026, 10, 27, tzinfo=UTC))

    assert landed_keys.bodies == {}, "the filing reader landed the HTML under the submissions key (#1502)"
    ((endpoint, key),) = _rows("sec")
    assert key == sec.submissions_key(320193).ledger_key, "a refused answer keeps its key"
    assert _reasons("sec") == ["answer_refused"]


def test_a_model_attempt_writes_the_reason_model_call(moomoo_ledger_db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from data_engine.sources import llm

    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_model", "glm-test")
    answer = json.dumps(
        {
            "model": "glm-served",
            "choices": [
                {"message": {"content": json.dumps({"value": 17581, "candidate_index": 0, "reason": "total"})}}
            ],
            "usage": {"prompt_tokens": 244, "completion_tokens": 61, "total_tokens": 305},
        }
    ).encode()

    llm.select_headcount(
        None,
        cik=4904,
        accession="000000490426000013",
        form="10-K",
        issuer_label="AEP",
        candidates=[llm.Candidate(17581, "As of December 31, 2025, AEP had a total of 17,581 employees.")],
        caller="identity-test",
        persist=False,
        transport=lambda _url, _headers, _body: (200, answer),
        now=lambda: datetime(2026, 9, 7, 12, tzinfo=UTC),
    )

    assert [key is not None for _endpoint, key in _rows(llm.SOURCE)] == [True]
    assert _reasons(llm.SOURCE) == ["model_call"]
