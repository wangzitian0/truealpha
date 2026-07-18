from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from truealpha_contracts.conversations import (
    ClarificationToken,
    Conversation,
    ConversationMessage,
    ConversationOutcome,
    MessageRole,
    ResearchGapRequest,
)

_NOW = datetime(2026, 7, 18, tzinfo=UTC)
_LATER = datetime(2026, 7, 18, 0, 30, tzinfo=UTC)


def _conversation(
    *,
    conversation_id: str = "conversation:alice-1",
    tenant_id: str = "tenant:truealpha",
    owner_principal_id: str = "principal:alice",
    created_at: datetime = _NOW,
) -> Conversation:
    return Conversation(
        conversation_id=conversation_id,
        tenant_id=tenant_id,
        owner_principal_id=owner_principal_id,
        created_at=created_at,
    )


def test_conversation_round_trips() -> None:
    conversation = _conversation()
    assert conversation.conversation_id == "conversation:alice-1"
    assert conversation.owner_principal_id == "principal:alice"


def test_conversation_rejects_naive_datetime() -> None:
    with pytest.raises(ValidationError):
        _conversation(created_at=datetime(2026, 7, 18))  # noqa: DTZ001 - the point under test


@pytest.mark.parametrize("bad_id", ["", " ", "conversation with spaces", "!not-stable"])
def test_conversation_rejects_unstable_id(bad_id: str) -> None:
    with pytest.raises(ValidationError):
        _conversation(conversation_id=bad_id)


def test_conversation_message_carries_a_typed_outcome() -> None:
    message = ConversationMessage(
        message_id="message:1",
        conversation_id="conversation:alice-1",
        tenant_id="tenant:truealpha",
        owner_principal_id="principal:alice",
        role=MessageRole.USER,
        content="what's ADM's PEG trend?",
        outcome=ConversationOutcome.RESULT,
        created_at=_NOW,
    )
    assert message.outcome is ConversationOutcome.RESULT
    assert message.role is MessageRole.USER


def test_conversation_message_outcome_defaults_to_none_for_a_user_prompt() -> None:
    message = ConversationMessage(
        message_id="message:1",
        conversation_id="conversation:alice-1",
        tenant_id="tenant:truealpha",
        owner_principal_id="principal:alice",
        role=MessageRole.USER,
        content="what's ADM's PEG trend?",
        created_at=_NOW,
    )
    assert message.outcome is None


def test_conversation_message_frozen() -> None:
    message = ConversationMessage(
        message_id="message:1",
        conversation_id="conversation:alice-1",
        tenant_id="tenant:truealpha",
        owner_principal_id="principal:alice",
        role=MessageRole.ASSISTANT,
        content="here is the trend",
        outcome=ConversationOutcome.RESULT,
        created_at=_NOW,
    )
    with pytest.raises(ValidationError):
        message.content = "edited"  # type: ignore[misc]


def test_clarification_token_requires_expiry_after_creation() -> None:
    with pytest.raises(ValueError, match="expires_at must be after created_at"):
        ClarificationToken(
            token_id="token:1",
            conversation_id="conversation:alice-1",
            tenant_id="tenant:truealpha",
            owner_principal_id="principal:alice",
            originating_message_id="message:1",
            requested_fields=("cutoff",),
            expires_at=_NOW,
            created_at=_LATER,
        )


def test_clarification_token_valid() -> None:
    token = ClarificationToken(
        token_id="token:1",
        conversation_id="conversation:alice-1",
        tenant_id="tenant:truealpha",
        owner_principal_id="principal:alice",
        originating_message_id="message:1",
        requested_fields=("cutoff", "convention"),
        candidate_choices=("historical_cagr", "analyst_consensus"),
        expires_at=_LATER,
        created_at=_NOW,
    )
    assert token.requested_fields == ("cutoff", "convention")


def test_clarification_token_requires_at_least_one_requested_field() -> None:
    with pytest.raises(ValidationError):
        ClarificationToken(
            token_id="token:1",
            conversation_id="conversation:alice-1",
            tenant_id="tenant:truealpha",
            owner_principal_id="principal:alice",
            originating_message_id="message:1",
            requested_fields=(),
            expires_at=_LATER,
            created_at=_NOW,
        )


def test_research_gap_request_conversation_id_is_optional() -> None:
    gap = ResearchGapRequest(
        gap_request_id="gap:1",
        tenant_id="tenant:truealpha",
        owner_principal_id="principal:alice",
        conversation_id=None,
        prompt_text="cover XYZ's supply chain",
        created_at=_NOW,
    )
    assert gap.conversation_id is None


def test_research_gap_request_rejects_empty_prompt() -> None:
    with pytest.raises(ValidationError):
        ResearchGapRequest(
            gap_request_id="gap:1",
            tenant_id="tenant:truealpha",
            owner_principal_id="principal:alice",
            conversation_id=None,
            prompt_text="",
            created_at=_NOW,
        )
