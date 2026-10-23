"""#1444: the decoding version of the model request.

Z.ai documents that GLM-5.3 and GLM-5.3-FLASH accept only `thinking: enabled`. The code asked with
`thinking: disabled` (v1) and 481 staging answers were stored that way. A decoding version is a
named request form. The environment contract names the version of each environment; the default is
v1. These tests hold three facts: v1 is byte-identical to what stored answers were asked with, an
environment that names v2 sends the documented body, and an answer stored under one version replays
under the other, so that no change of version asks a stored question again.

No test reaches the live provider. The replay tests need Postgres and run in CI.
"""

from __future__ import annotations

import dataclasses
import json
import os
import uuid
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from data_engine.config import settings
from data_engine.quality import model_key_health
from data_engine.sources import gateway, llm, model_decoding, model_policy
from truealpha_contracts.environment_contract import load_environment_contract

SEAT = load_environment_contract().model_seat
KEY = "sk-test-not-a-real-key-0123456789"

#: The digests of the v1 request, computed from the request builder of origin/main on 2026-10-22
#: (before the versions existed), not from this repository's current code. Every stored answer of
#: a v1 ask was keyed by such a digest.
HEADCOUNT_USER = "Issuer: AEP, form 10-K.\nCandidates:\n[0] 17,581: x\n[1] 6,994: y"
THEME_USER = "Theme: A\nSegments:\n[0] X"
SUPPLY_USER = "Issuer: X\nFiling text:\nabc"
V1_DIGESTS = {
    "headcount": (
        llm.HEADCOUNT_TASK,
        HEADCOUNT_USER,
        "8e2836b0625276a7d623aa13b0ae1b974146853258fa175e91b12984175221f1",
    ),
    "theme": (llm.SEGMENT_THEME_TASK, THEME_USER, "ae83640253cbb45a0a4d42c179e898f6a71c2f7af146333f642acca22e0bac45"),
    "supply-chain": (
        llm.SUPPLY_CHAIN_TASK,
        SUPPLY_USER,
        "be4c1436a47d608dca2dc2ac05695948ea253b417d2f8f5ed2e8fee1ebc45ef4",
    ),
}

#: A task that needs a whole answer; the key probe asks only for proof that the provider answered.
TASK = dataclasses.replace(model_key_health.PROBE_TASK, answer_required=True)

OK = json.dumps(
    {
        "model": "glm-served",
        "choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 9, "total_tokens": 29},
    }
).encode()


OK_WITHOUT_FINISH_REASON = json.dumps(
    {
        "model": "glm-served",
        "choices": [{"message": {"content": '{"ok": true}'}}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 9, "total_tokens": 29},
    }
).encode()


@pytest.fixture(autouse=True)
def seated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "llm_api_key", KEY)
    monkeypatch.setattr(settings, "llm_model", "glm-test")
    monkeypatch.setattr(model_policy, "pacer_for", lambda environment: None)  # pacing has its own tests


def in_environment(monkeypatch: pytest.MonkeyPatch, name: str, *, decoding: str | None = None) -> None:
    """Run as ``name``; when ``decoding`` is given, the contract names that version for it."""
    monkeypatch.setattr(gateway, "capacity_environment", lambda: name)
    if decoding is None:
        return
    limits = SEAT.limits_for(name)
    assert limits is not None, f"{name} is not listed in the contract"
    named = SEAT.model_copy(
        update={"environments": {**SEAT.environments, name: limits.model_copy(update={"decoding_version": decoding})}}
    )
    monkeypatch.setattr(model_policy, "seat_contract", lambda: named)


def ask(*, transport: Any) -> Any:
    """One ask that records nothing but its ledger row, as the key probe does."""
    return llm.invoke(
        None,
        task=model_key_health.PROBE_TASK,
        cik=0,
        accession="",
        user_content="ping",
        caller="test:decoding",
        standard="test",
        parse=lambda text: {"answered": bool(text)},
        refusal=lambda detail: {"answered": False, "reason": detail},
        persist=False,
        transport=transport,
    )


class Capture:
    def __init__(self, answer: bytes = OK) -> None:
        self.bodies: list[dict[str, Any]] = []
        self._answer = answer

    def __call__(self, url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
        self.bodies.append(json.loads(body))
        return 200, self._answer


# --- v1 is what stored answers were asked with -------------------------------------------------


@pytest.mark.parametrize("name", sorted(V1_DIGESTS))
def test_the_v1_request_has_the_digest_that_stored_answers_were_keyed_by(name: str) -> None:
    task, user, expected = V1_DIGESTS[name]
    request = llm.build_chat_request(task, user, model="glm-5.3-flash", decoding=model_decoding.V1)
    assert len(expected) == 64 and llm._request_sha256(request) == expected
    assert request["thinking"] == {"type": "disabled"} and "reasoning_effort" not in request


def test_every_version_of_the_contract_is_known_and_production_stays_on_v1() -> None:
    known = {version.name for version in model_decoding.KNOWN}
    assert {limits.decoding_version for limits in SEAT.environments.values()} <= known
    assert SEAT.decoding_version_for("production") == "v1", "production changes version only with an approved release"
    assert SEAT.decoding_version_for("dev") == "v1"


def test_an_unknown_version_in_the_contract_is_refused_before_any_call(monkeypatch) -> None:
    in_environment(monkeypatch, "staging", decoding="v9")
    transport = Capture()
    with pytest.raises(ValueError, match="unknown decoding version 'v9'"):
        ask(transport=transport)
    assert transport.bodies == []


# --- the body each version sends -----------------------------------------------------------------


def test_an_environment_on_v1_sends_thinking_disabled_and_the_task_max_tokens(monkeypatch) -> None:
    in_environment(monkeypatch, "production")
    transport = Capture()
    ask(transport=transport)
    (body,) = transport.bodies
    assert body["thinking"] == {"type": "disabled"} and "reasoning_effort" not in body
    assert body["max_tokens"] == model_key_health.PROBE_TASK.max_tokens


def test_an_environment_on_v2_sends_the_documented_body_and_the_same_max_tokens(monkeypatch, call_ledger) -> None:
    in_environment(monkeypatch, "staging", decoding="v2")
    transport = Capture()
    ask(transport=transport)
    (body,) = transport.bodies
    assert body["thinking"] == {"type": "enabled"} and body["reasoning_effort"] == "low"
    assert "disabled" not in json.dumps(body)
    assert body["max_tokens"] == model_key_health.PROBE_TASK.max_tokens, "no more room without evidence"
    (row,) = call_ledger
    assert row.request_params is not None
    assert (row.request_params["decoding_version"], row.request_params["thinking"]) == ("v2", "enabled")
    assert row.request_params["reasoning_effort"] == "low"


def test_the_ledger_row_names_the_version_that_was_sent(monkeypatch, call_ledger) -> None:
    in_environment(monkeypatch, "production")
    ask(transport=Capture())
    (row,) = call_ledger
    assert row.request_params is not None
    assert (row.request_params["decoding_version"], row.request_params["thinking"]) == ("v1", "disabled")
    assert row.request_params["reasoning_effort"] is None


# --- what the response says it cost -----------------------------------------------------------------


def test_the_ledger_row_carries_the_output_tokens_and_the_reasoning_tokens_when_reported(
    monkeypatch, call_ledger
) -> None:
    in_environment(monkeypatch, "staging", decoding="v2")
    reported = json.dumps(
        {
            "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 30,
                "completion_tokens": 120,
                "total_tokens": 150,
                "completion_tokens_details": {"reasoning_tokens": 80},
            },
        }
    ).encode()
    ask(transport=Capture(reported))
    ask(transport=Capture(OK))
    assert [(row.completion_tokens, row.reasoning_tokens) for row in call_ledger] == [(120, 80), (9, None)]


def test_the_token_columns_are_a_group_of_their_own_written_only_by_a_record_that_has_tokens() -> None:
    """The migration of the token columns (20261021T1100) may run after that of the attempt columns
    (20261021T0900): a failed attempt has no tokens and needs only the first."""
    called_at = datetime.now(UTC)
    failed = gateway.CallRecord(source=llm.SOURCE, endpoint="e", caller="t", called_at=called_at, attempt=1)
    answered = gateway.CallRecord(
        source=llm.SOURCE, endpoint="e", caller="t", called_at=called_at, attempt=2, completion_tokens=9
    )
    failed_sql, _ = gateway._insert_statement(failed)
    answered_sql, answered_params = gateway._insert_statement(answered)
    assert failed_sql == gateway._INSERT_ATTEMPT_SQL
    assert "completion_tokens" in answered_sql and "reasoning_tokens" in answered_sql
    assert answered_params[-2:] == (9, None)


# --- an answer that was cut off is not an answer -----------------------------------------------------


def cut_off(completion_tokens: int = 16) -> bytes:
    return json.dumps(
        {
            "model": "glm-served",
            "choices": [{"message": {"content": ""}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 20, "completion_tokens": completion_tokens, "total_tokens": 36},
        }
    ).encode()


def test_an_answer_cut_off_at_max_tokens_is_an_error_and_a_failed_ledger_row(monkeypatch, call_ledger) -> None:
    in_environment(monkeypatch, "staging", decoding="v2")
    with pytest.raises(llm.ModelOutputTruncated) as raised:
        llm.invoke(
            None,
            task=TASK,
            cik=0,
            accession="",
            user_content="ping",
            caller="t",
            standard="t",
            parse=lambda text: {"answered": bool(text)},
            refusal=lambda detail: {"answered": False, "reason": detail},
            persist=False,
            transport=Capture(cut_off()),
        )
    assert isinstance(raised.value, llm.ModelHTTPError), "callers that defer a cell on a model error defer this one"
    (row,) = call_ledger
    assert row.ok is False and row.status_code == 200 and "finish_reason=length" in (row.error or "")
    assert row.completion_tokens == 16


def test_the_key_probe_counts_an_answer_cut_off_at_its_tiny_limit_as_proof_of_the_key(monkeypatch, call_ledger) -> None:
    in_environment(monkeypatch, "staging", decoding="v2")
    ask(transport=Capture(cut_off()))
    (row,) = call_ledger
    assert row.ok is True, "no failure for the adoption rule: the probe asks for proof, not for an answer"


def test_the_probe_verdict_is_green_when_the_answer_is_cut_off(monkeypatch) -> None:
    in_environment(monkeypatch, "staging", decoding="v2")
    # `probe` builds its own transport through the gateway; replace the module seam it calls through.
    monkeypatch.setattr(llm, "_gateway_transport", lambda url, headers, body: (200, cut_off()))
    health = model_key_health.probe()
    assert (health.ok, health.outcome) == (True, "answered")


# --- replay across versions (Postgres; CI) --------------------------------------------------------------


def connect() -> psycopg.Connection[Any]:
    try:
        return psycopg.connect(settings.database_url, connect_timeout=3)
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")


@pytest.fixture
def warehouse():
    connection = connect()
    try:
        yield connection
    finally:
        connection.rollback()  # staging.model_invocations is append-only: a test never commits
        connection.close()


def fresh() -> tuple[int, str]:
    """A subject and filing that no earlier run asked about."""
    return 999_100_000 + uuid.uuid4().int % 99_999, f"decoding-{uuid.uuid4().hex}"


def never(*_: Any) -> tuple[int, bytes]:
    raise AssertionError("a stored answer must be replayed, not asked again")


def stored(connection: Any, cik: int, accession: str) -> list[tuple[str, str | None]]:
    rows = connection.execute(
        "select request #>> '{thinking,type}', request ->> 'reasoning_effort' "
        "from staging.model_invocations where subject_cik = %s and accession = %s order by id",
        (cik, accession),
    ).fetchall()
    return [tuple(row) for row in rows]


def invoke_at(connection: Any, cik: int, accession: str, transport: Any, content: str = "ping") -> Any:
    return llm.invoke(
        connection,
        task=TASK,
        cik=cik,
        accession=accession,
        user_content=content,
        caller="test:decoding",
        standard="test",
        parse=lambda text: {"answered": bool(text)},
        refusal=lambda detail: {"answered": False, "reason": detail},
        persist=True,
        transport=transport,
    )


def test_an_answer_stored_under_v1_replays_when_the_environment_moves_to_v2(monkeypatch, warehouse) -> None:
    cik, accession = fresh()
    in_environment(monkeypatch, "production")
    # An old row may hold a response without a finish_reason: it must still replay.
    first = invoke_at(warehouse, cik, accession, Capture(OK_WITHOUT_FINISH_REASON))
    assert first.replayed is False and stored(warehouse, cik, accession) == [("disabled", None)]
    in_environment(monkeypatch, "staging", decoding="v2")
    replayed = invoke_at(warehouse, cik, accession, never)
    assert replayed.replayed is True and replayed.decision == first.decision
    assert replayed.invocation_id == first.invocation_id
    assert stored(warehouse, cik, accession) == [("disabled", None)], "nothing was asked and nothing was written"


def test_an_answer_stored_under_v2_replays_when_the_environment_returns_to_v1(monkeypatch, warehouse) -> None:
    cik, accession = fresh()
    in_environment(monkeypatch, "staging", decoding="v2")
    first = invoke_at(warehouse, cik, accession, Capture())
    assert stored(warehouse, cik, accession) == [("enabled", "low")]
    in_environment(monkeypatch, "production")
    replayed = invoke_at(warehouse, cik, accession, never)
    assert replayed.replayed is True and replayed.invocation_id == first.invocation_id


def test_a_question_nobody_answered_is_asked_in_the_version_of_its_environment(monkeypatch, warehouse) -> None:
    cik, accession = fresh()
    in_environment(monkeypatch, "staging", decoding="v2")
    transport = Capture()
    invoke_at(warehouse, cik, accession, transport, content="first question")
    invoke_at(warehouse, cik, accession, transport, content="second question")
    assert len(transport.bodies) == 2
    assert stored(warehouse, cik, accession) == [("enabled", "low"), ("enabled", "low")]


def test_a_cut_off_answer_is_stored_but_never_replayed(monkeypatch, warehouse) -> None:
    cik, accession = fresh()
    in_environment(monkeypatch, "staging", decoding="v2")
    with pytest.raises(llm.ModelOutputTruncated):
        invoke_at(warehouse, cik, accession, Capture(cut_off()))
    retry = Capture()
    again = invoke_at(warehouse, cik, accession, retry)
    assert len(retry.bodies) == 1, "the cut-off answer is not replayed: the question is asked again"
    assert again.replayed is False and again.decision == {"answered": True}
    third = invoke_at(warehouse, cik, accession, never)
    assert third.replayed is True and third.invocation_id == again.invocation_id
    assert len(stored(warehouse, cik, accession)) == 2


def test_the_replay_looks_for_the_request_of_every_known_version(monkeypatch) -> None:
    """The statement a connection receives: one digest and one question for each known version."""
    in_environment(monkeypatch, "staging", decoding="v2")
    executed: list[tuple[str, tuple]] = []

    class Connection:
        autocommit = False

        def execute(self, sql: str, params: tuple = ()) -> Any:
            executed.append((" ".join(sql.split()), params))

            class Rows:
                def fetchone(self) -> None:
                    return None

            return Rows()

    llm.invoke(
        Connection(),
        task=model_key_health.PROBE_TASK,
        cik=1,
        accession="a",
        user_content="ping",
        caller="t",
        standard="t",
        parse=lambda text: {},
        refusal=lambda detail: {},
        transport=Capture(),
    )
    ((sql, params),) = [entry for entry in executed if entry[0].startswith("select invocation_id")]
    versions = len(model_decoding.KNOWN)
    assert sql.count("request_sha256 = %s") == versions
    digests = [
        llm._request_sha256(llm.build_chat_request(model_key_health.PROBE_TASK, "ping", model="glm-test", decoding=v))
        for v in model_decoding.KNOWN
    ]
    assert [p for p in params if isinstance(p, str) and len(p) == 64] == digests
    assert "finish_reason" in sql and "<> 'length'" in sql


def test_the_lock_on_a_question_is_the_same_in_every_version(monkeypatch) -> None:
    """A process that still asks in v1 and one that asks in v2 must wait for each other."""
    locks: list[str] = []

    class Connection:
        autocommit = False

        def execute(self, sql: str, params: tuple = ()) -> Any:
            if sql.startswith("select pg_advisory_xact_lock("):
                locks.append(params[0])

            class Rows:
                def fetchone(self) -> None:
                    return None

            return Rows()

    for environment, decoding in (("production", None), ("staging", "v2")):
        in_environment(monkeypatch, environment, decoding=decoding)
        llm.invoke(
            Connection(),
            task=TASK,
            cik=1,
            accession="a",
            user_content="ping",
            caller="t",
            standard="t",
            parse=lambda text: {},
            refusal=lambda detail: {},
            transport=Capture(),
        )
    assert len(locks) == 2 and locks[0] == locks[1]
