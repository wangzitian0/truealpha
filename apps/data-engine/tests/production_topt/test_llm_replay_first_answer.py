"""One question, one answer, against a real database (§9: replay never silently calls the model again).

In prod, 5 of 269 answered request digests had disagreeing stored answers. AAPL/Semiconductors
and MU/AI-infrastructure purity changed between the TOPT and QQQ heads. There were two causes:
- the theme-purity ops of two heads asked the same question in parallel, and neither could
  see the other's uncommitted invocation row, so both reached the provider;
- replay read `order by id desc`, so the answer stored LAST won.

These tests run on Postgres because both causes live there: transaction visibility, and
the order of the replay read. The model is intercepted at the HTTP boundary
(`llm._gateway_transport`), so the lock, the replay read and the invocation insert run as
in production. The parallel test drives the deployed producer, `materialize_theme_purity`,
which holds one transaction per op and commits at the end, as `run_theme_purity` does.

The parallel test must commit, because a second connection sees nothing uncommitted. The
table is append-only, so every row a test writes uses a fresh accession, and a later run
never replays an earlier run's rows.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub.production_topt import theme_purity
from data_engine.datahub.production_topt.theme_purity import IssuerPartition, materialize_theme_purity
from data_engine.sources import gateway, llm
from truealpha_contracts.theme_purity import THEMES

AI = THEMES["ai-infrastructure"]
SEGMENTS = (("Semiconductor solutions", Decimal("36858000000")), ("Infrastructure software", Decimal("27029000000")))
TOTAL = Decimal("63887000000")
CUTOFF = datetime(2026, 9, 1, tzinfo=UTC)
#: How long a test waits for the other thread before it gives up and fails.
WAIT_SECONDS = 20


def _connect(*, autocommit: bool = False) -> psycopg.Connection[Any]:
    try:
        return psycopg.connect(settings.database_url, connect_timeout=3, autocommit=autocommit)
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")


@pytest.fixture
def connection():
    active = _connect()
    try:
        yield active
    finally:
        active.rollback()
        active.close()


@pytest.fixture
def seated(monkeypatch):
    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_model", "glm-test")
    ledger = gateway.MemoryLedger()
    previous = gateway.set_writer(ledger)
    yield ledger
    gateway.set_writer(previous)


def _fresh_filing() -> tuple[int, str]:
    """A subject and filing no earlier run of this file has asked about."""
    return 999_100_000 + uuid.uuid4().int % 99_999, f"test-{uuid.uuid4().hex}"


def _reply(verdicts: list[bool | None], *, served: str | None = "glm-served") -> bytes:
    items = [{"index": i, "in_theme": v, "reason": f"verdict {i}"} for i, v in enumerate(verdicts)]
    body: dict[str, Any] = {
        "choices": [{"message": {"content": json.dumps({"verdicts": items})}}],
        "usage": {"prompt_tokens": 200, "completion_tokens": 60, "total_tokens": 260},
    }
    if served is not None:
        body["model"] = served
    return json.dumps(body).encode()


def _classify(connection, cik: int, accession: str, transport) -> llm.ThemeClassification:
    return llm.classify_segments(
        connection,
        cik=cik,
        accession=accession,
        issuer_label="AVGO (test)",
        theme=AI.theme,
        inclusion=AI.inclusion,
        segments=[name for name, _ in SEGMENTS],
        caller="test",
        transport=transport,
    )


def _never(*_: Any) -> tuple[int, bytes]:
    raise AssertionError("an answered question must be replayed, not asked again")


def _store_racing_answer(connection, invocation_id: str, verdicts: list[bool | None]) -> None:
    """A second answered row for the question `invocation_id` answered, stored after it
    with a different verdict. Before the lock, two heads racing on one question left
    exactly this."""
    decision = {"verdicts": verdicts, "reasons": ["racing head"] * len(verdicts)}
    connection.execute(
        """
        insert into staging.model_invocations
            (invocation_id, provider, model, base_url_host, standard, subject_cik, accession, prompt_version,
             prompt_sha256, schema_sha256, request_sha256, response_sha256, status_code, prompt_tokens,
             completion_tokens, cost, decision, request, response, started_at, completed_at, served_model)
        select %s, provider, model, base_url_host, standard, subject_cik, accession, prompt_version,
               prompt_sha256, schema_sha256, request_sha256, response_sha256, status_code, prompt_tokens,
               completion_tokens, cost, %s::jsonb, request, response, started_at, completed_at, served_model
        from staging.model_invocations
        where invocation_id = %s
        """,
        ("model-invocation:" + uuid.uuid4().hex * 2, json.dumps(decision), invocation_id),
    )


def test_replay_returns_the_first_answer_not_the_newest(connection, seated) -> None:
    """A question answered twice (by two heads racing before the lock existed) replays the
    answer given first, on every later run. Replaying the newest one let whichever head
    asked last decide the answer for every later head."""
    cik, accession = _fresh_filing()
    first = _classify(connection, cik, accession, lambda *_: (200, _reply([True, False])))
    assert first.verdicts == (True, False) and not first.invocation.replayed
    _store_racing_answer(connection, first.invocation.invocation_id, [False, True])

    replayed = _classify(connection, cik, accession, _never)

    assert replayed.invocation.replayed
    assert replayed.verdicts == (True, False), "the first answer is the answer"
    assert replayed.invocation.invocation_id == first.invocation.invocation_id


def test_an_answer_served_by_the_configured_model_replays_under_a_pinned_name(connection, seated, monkeypatch) -> None:
    """The provider serves the `glm-4.7` alias with `glm-5.3-flash`. Pinning `LLM_MODEL` to
    the served name changes the request digest but not the model that answers, so the stored
    answer replays. A row whose served model was never reported replays only under the
    name it was asked with, which is the key every existing row was stored under."""
    monkeypatch.setattr(settings, "llm_model", "glm-alias")
    cik, accession = _fresh_filing()
    served = _classify(connection, cik, accession, lambda *_: (200, _reply([True, None], served="glm-revision")))
    legacy_cik, legacy_accession = _fresh_filing()
    unreported = _classify(
        connection, legacy_cik, legacy_accession, lambda *_: (200, _reply([False, False], served=None))
    )
    assert served.invocation.served_model == "glm-revision" and unreported.invocation.served_model is None

    monkeypatch.setattr(settings, "llm_model", "glm-revision")
    pinned = _classify(connection, cik, accession, _never)
    assert pinned.invocation.replayed and pinned.invocation.invocation_id == served.invocation.invocation_id
    assert pinned.verdicts == (True, None)
    assert pinned.extractor.startswith("model:glm-revision:")

    asked: list[str] = []

    def transport(url, headers, body):  # noqa: ARG001
        asked.append(json.loads(body)["model"])
        return 200, _reply([True, True], served="glm-revision")

    reasked = _classify(connection, legacy_cik, legacy_accession, transport)
    assert asked == ["glm-revision"] and not reasked.invocation.replayed

    monkeypatch.setattr(settings, "llm_model", "glm-alias")
    legacy = _classify(connection, legacy_cik, legacy_accession, _never)
    assert legacy.invocation.replayed and legacy.verdicts == (False, False)


class _Provider:
    """The provider at the HTTP boundary. The first ask is held open until the test
    releases it, the way a slow model call keeps a head's op mid-transaction. Every later
    ask is answered at once, with a DIFFERENT verdict, so a second call shows in the answers
    as well as in the call count."""

    def __init__(self) -> None:
        self.calls = 0
        self.first_ask_open = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()

    def __call__(self, url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:  # noqa: ARG002
        with self._lock:
            self.calls += 1
            call = self.calls
        if call == 1:
            self.first_ask_open.set()
            assert self.release.wait(WAIT_SECONDS), "the test never released the first ask"
            return 200, _reply([True, False])
        return 200, _reply([False, True])


def _waiting_on_advisory_lock(observer, pid: int) -> bool:
    row = observer.execute("select wait_event_type, wait_event from pg_stat_activity where pid = %s", (pid,)).fetchone()
    return row is not None and row[0] == "Lock" and row[1] == "advisory"


def _head(run_id: str, connection, results: dict[str, Any], errors: list[BaseException]) -> None:
    """One head's theme-purity op, as `run_theme_purity` runs it: one transaction, commit at the end."""
    try:
        results[run_id] = materialize_theme_purity(connection, run_id=run_id, cutoff=CUTOFF, themes=(AI,))
        if not connection.autocommit:
            connection.commit()
    except BaseException as error:  # noqa: BLE001 - re-raised on the test thread
        errors.append(error)
        if not connection.autocommit:
            connection.rollback()


@pytest.mark.parametrize("autocommit", [False, True], ids=["op-transaction", "autocommit"])
def test_two_heads_asking_one_question_in_parallel_reach_the_provider_once(seated, monkeypatch, autocommit) -> None:
    """The TOPT and QQQ heads share issuers (AVGO is in both), and their theme-purity ops run
    at the same time. The second head waits on the question until the first head's op
    commits, then replays that answer. Both heads publish the same share, and the provider
    is asked once."""
    cik, accession = _fresh_filing()
    partition = IssuerPartition(
        cik=cik,
        partition_id="segment-partition:" + uuid.uuid4().hex * 2,
        period_end=date(2025, 11, 2),
        consolidated_revenue=TOTAL,
        partition_residual=Decimal(0),
        parts=SEGMENTS,
        descriptions=tuple(name for name, _ in SEGMENTS),
        accession=accession,
        extraction_confidence=Decimal("0.85"),
        extractor="rule:exhaustive-partition:v1",
    )
    issuer = f"issuer:cik:{cik:010d}"
    monkeypatch.setattr(theme_purity, "load_partitions", lambda _c, *, cutoff, ciks=None: (partition,))
    monkeypatch.setattr(theme_purity, "governed_members", lambda _c, *, run_id: {cik: issuer})
    provider = _Provider()
    monkeypatch.setattr(llm, "_gateway_transport", provider)
    suffix = uuid.uuid4().hex[:12]
    first_head, second_head = f"test-run:topt-{suffix}", f"test-run:qqq-{suffix}"

    first_connection = _connect(autocommit=autocommit)
    second_connection = _connect(autocommit=autocommit)
    observer = _connect(autocommit=True)
    results: dict[str, Any] = {}
    errors: list[BaseException] = []
    first = threading.Thread(target=_head, args=(first_head, first_connection, results, errors))
    second = threading.Thread(target=_head, args=(second_head, second_connection, results, errors))
    try:
        first.start()
        assert provider.first_ask_open.wait(WAIT_SECONDS), "the first head never asked"
        second.start()
        # Release the first ask only once the second head has either reached the provider
        # (the defect) or is waiting on the question (the fix). Either way both have had
        # their chance to ask before the first answer can be seen.
        deadline = time.monotonic() + WAIT_SECONDS
        while provider.calls < 2 and not _waiting_on_advisory_lock(observer, second_connection.info.backend_pid):
            assert time.monotonic() < deadline, "the second head neither asked nor waited"
            assert second.is_alive(), f"the second head ended early: {errors}"
            time.sleep(0.05)
        provider.release.set()
        first.join(WAIT_SECONDS)
        second.join(WAIT_SECONDS)
        assert not first.is_alive() and not second.is_alive()
        assert errors == []

        assert provider.calls == 1, "two heads asking one question reached the provider twice"
        (topt,) = results[first_head]
        (qqq,) = results[second_head]
        assert topt.result.value == qqq.result.value == SEGMENTS[0][1] / TOTAL
        rows = observer.execute(
            "select run_id, theme_share, extractor from mart.issuer_theme_purity where run_id = any(%s) order by run_id",
            ([first_head, second_head],),
        ).fetchall()
        assert len(rows) == 2 and rows[0][1:] == rows[1][1:]
    finally:
        provider.release.set()
        first.join(WAIT_SECONDS)
        second.join(WAIT_SECONDS)
        observer.execute("delete from mart.issuer_theme_purity where run_id = any(%s)", ([first_head, second_head],))
        for active in (first_connection, second_connection, observer):
            active.close()
