"""The model as q6's classifier (#772, init.md §7 module 6).

The judgement half of theme purity: which of an issuer's reportable segments belong to a
stated theme. `factors.base.theme_purity` does the arithmetic and never judges; this is
where the judging happens, and it goes through the same gated, replayed, recorded path as
the headcount selector — `invoke` is shared, not copied.

Two properties carry most of the weight here, and neither is about accuracy:

1. **The classifier never sees the revenue.** It gets segment NAMES and the theme
   definition. A model that can see which segment is the big one can be graded on how
   flattering its answer is, and q6's whole claim is that the share comes from a judgement
   made without knowing what it would produce.
2. **A segment the model did not answer for stays unclassified, never `False`.** `None`
   lowers confidence in the share; `False` lowers the share. Collapsing them lets a silent
   model read as a confident "not in the theme".
"""

from __future__ import annotations

import json

import pytest
from data_engine.config import settings
from data_engine.sources import gateway
from data_engine.sources.llm import (
    SEGMENT_THEME_TASK,
    ModelNotConfigured,
    build_chat_request,
    classify_segments,
)
from truealpha_contracts.theme_purity import THEMES

AVGO = ("Semiconductor solutions", "Infrastructure software")
THEME = THEMES["ai-infrastructure"]


class _Conn:
    def __init__(self, replay_row=None):
        self.replay_row = replay_row
        self.inserts: list[tuple] = []

    def execute(self, sql, params=()):
        text = " ".join(sql.split())
        if text.startswith("select invocation_id, decision"):
            return _R([self.replay_row] if self.replay_row else [])
        if text.startswith("insert into staging.model_invocations"):
            self.inserts.append(params)
            return _R([])
        raise AssertionError(text[:60])


class _R:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


def _answer(verdicts, served="glm-served"):
    content = json.dumps({"verdicts": verdicts})
    body = {
        "model": served,
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 300, "completion_tokens": 90, "total_tokens": 390},
    }
    return json.dumps(body).encode()


def _transport(payload):
    def transport(url, headers, body):  # noqa: ARG001
        transport.request = json.loads(body)
        return 200, payload

    return transport


@pytest.fixture
def seated(monkeypatch):
    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_model", "glm-test")
    ledger = gateway.MemoryLedger()
    previous = gateway.set_writer(ledger)
    yield ledger
    gateway.set_writer(previous)


def _classify(conn, transport, segments=AVGO, theme=THEME):
    return classify_segments(
        conn,
        cik=1730168,
        accession="0001730168-25-000121",
        issuer_label="AVGO (issuer:cik:0001730168)",
        theme=theme.theme,
        inclusion=theme.inclusion,
        segments=segments,
        caller="test",
        transport=transport,
    )


def test_no_provider_seated_is_an_explicit_refusal(monkeypatch) -> None:
    monkeypatch.setattr(settings, "llm_api_key", "")
    with pytest.raises(ModelNotConfigured):
        _classify(None, None)


def test_the_classifier_is_never_shown_the_revenue(seated) -> None:
    """The property that makes the share defensible. A judgement made while seeing which
    segment is worth 36,858 and which 27,029 can be tuned to the answer it produces."""
    transport = _transport(_answer([{"index": 0, "in_theme": True, "reason": "accelerators"}]))
    _classify(_Conn(), transport)
    sent = json.dumps(transport.request)
    assert "36858" not in sent and "36,858" not in sent
    assert "27029" not in sent and "27,029" not in sent
    assert "Semiconductor solutions" in sent, "it does see the names"
    assert THEME.inclusion[:40] in sent, "and the theme definition it is judged against"


def test_a_segment_the_model_skipped_is_unclassified_not_a_no(seated) -> None:
    """The failure this exists to prevent: a short answer read positionally, or read as
    'everything else is out', turns silence into a judgement and moves the share."""
    transport = _transport(_answer([{"index": 0, "in_theme": True, "reason": "accelerators"}]))
    result = _classify(_Conn(), transport)
    assert result.verdicts == (True, None), "the unanswered segment is None, not False"


def test_verdicts_are_placed_by_index_not_by_position(seated) -> None:
    """A model that answers out of order, or answers about only the second segment, must
    not have its verdict shifted onto the first."""
    transport = _transport(_answer([{"index": 1, "in_theme": False, "reason": "enterprise software"}]))
    result = _classify(_Conn(), transport)
    assert result.verdicts == (None, False)
    assert result.reasons[1] == "enterprise software" and result.reasons[0] == ""


def test_an_index_outside_the_segment_list_is_dropped_rather_than_wrapped(seated) -> None:
    """`verdicts[5]` for a two-segment issuer is an answer about a segment that does not
    exist. Dropping it is the classification analogue of the headcount rule that a value
    which is not a candidate is a refusal — the model may judge, never invent."""
    transport = _transport(
        _answer(
            [
                {"index": 5, "in_theme": True, "reason": "hallucinated"},
                {"index": -1, "in_theme": True, "reason": "also hallucinated"},
                {"index": 0, "in_theme": True, "reason": "accelerators"},
            ]
        )
    )
    result = _classify(_Conn(), transport)
    assert result.verdicts == (True, None)


def test_a_non_boolean_verdict_is_unclassified(seated) -> None:
    """`"yes"`, `1`, `"true"` are not judgements this pipeline can count. Coercing them
    would make the share depend on a model's formatting."""
    transport = _transport(
        _answer([{"index": 0, "in_theme": "yes", "reason": "r"}, {"index": 1, "in_theme": 1, "reason": "r"}])
    )
    result = _classify(_Conn(), transport)
    assert result.verdicts == (None, None)


def test_an_unparseable_answer_classifies_nothing(seated) -> None:
    body = {"model": "glm-served", "choices": [{"message": {"content": "sorry, I cannot"}}], "usage": {}}
    result = _classify(_Conn(), _transport(json.dumps(body).encode()))
    assert result.verdicts == (None, None)
    assert "unparseable" in result.reasons[0]


def test_a_full_classification_is_recorded_with_the_task_s_own_identity(seated) -> None:
    """The invocation plane is shared with the headcount task, so the row has to say WHICH
    question was asked: `prompt_version` and `prompt_sha256` are the task's, not the
    module's."""
    transport = _transport(
        _answer(
            [
                {"index": 0, "in_theme": True, "reason": "accelerators and networking"},
                {"index": 1, "in_theme": False, "reason": "enterprise software"},
            ]
        )
    )
    conn = _Conn()
    result = _classify(conn, transport)
    assert result.verdicts == (True, False)
    assert len(conn.inserts) == 1
    row = conn.inserts[0]
    assert "segment-theme:v1" in row, "the row names this task's prompt version"
    assert SEGMENT_THEME_TASK.prompt_sha256 in row
    assert SEGMENT_THEME_TASK.schema_sha256 in row
    assert "segment_revenue" in row, "and the standard it was asked for"
    assert result.extractor.startswith("model:glm-served:")
    assert sum(entry.cost for entry in seated) == 390, "the token cost reaches the ledger"


def test_the_theme_is_part_of_the_request_digest_so_replay_is_per_theme(seated) -> None:
    """One task identity covers "judge a segment against a stated theme"; the theme itself
    travels in the user content. If it did not reach the digest, an issuer classified for
    `semiconductors` would replay that answer for `ai-infrastructure`."""
    first = build_chat_request(SEGMENT_THEME_TASK, "Theme: A\nSegments:\n[0] X", model="m")
    second = build_chat_request(SEGMENT_THEME_TASK, "Theme: B\nSegments:\n[0] X", model="m")
    assert first != second
    assert first["messages"][0] == second["messages"][0], "same system prompt, so one prompt_version"
    assert first["temperature"] == 0 and first["thinking"] == {"type": "disabled"}
    assert first["max_tokens"] == SEGMENT_THEME_TASK.max_tokens > 300, "room for one verdict per segment"


def test_an_identical_prior_ask_is_replayed_instead_of_re_asked(seated) -> None:
    """§9: replay never silently calls the model again."""
    stored = (
        "model-invocation:abc",
        {"verdicts": [True, False], "reasons": ["a", "b"]},
        "resp-sha",
        300,
        90,
        "req-sha",
        "zai",
        "glm-served",
    )

    def transport(url, headers, body):  # noqa: ARG001
        raise AssertionError("replay must not reach the provider")

    result = _classify(_Conn(replay_row=stored), transport)
    assert result.verdicts == (True, False)
    assert result.invocation.replayed is True
    assert result.invocation.invocation_id == "model-invocation:abc"
