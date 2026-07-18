"""Owner-scoped conversation persistence + clarification tokens — see #396.

Slice of #225's completed discovery: a deterministic typed state machine
over conversational research questions. This module supplies the DTOs
`authorize_access` and the eventual #46 orchestration layer speak in; it
does not implement intent extraction or SQL query selection — those are
#46's scope. Conversation/message storage lives in the additive `app`
schema (migration 0030), RLS-isolated per owner exactly like #229's
`app.private_research_objects`.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum

from pydantic import Field, field_validator

from truealpha_contracts.access import StrictFrozenModel
from truealpha_contracts.models import _require_aware

_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$")


def _stable_id(value: str, field_name: str) -> str:
    if _ID_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a stable identifier")
    return value


class ConversationOutcome(StrEnum):
    """Exactly #225's typed outcomes — nothing free-form. A message's
    `outcome` records which of these it resolved to; a model may only ever
    produce a `RESULT` reply by executing a bounded `ResearchQueryService`
    read (#46's job), never by inventing a value."""

    RESULT = "result"
    CLARIFICATION_REQUIRED = "clarification_required"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED = "unsupported"
    DENIED = "denied"
    RATE_LIMITED = "rate_limited"
    INVALID = "invalid"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class Conversation(StrictFrozenModel):
    """A conversation root. No mutable "current state" field — the owner-
    facing view is a projection over its append-only messages."""

    conversation_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    owner_principal_id: str = Field(min_length=1)
    created_at: datetime

    @field_validator("conversation_id", "tenant_id", "owner_principal_id")
    @classmethod
    def _validate_ids(cls, value: str, info) -> str:
        return _stable_id(value, info.field_name)

    @field_validator("created_at")
    @classmethod
    def _validate_created_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "created_at")


class ConversationMessage(StrictFrozenModel):
    """One append-only turn. `content` is free text (the user's prompt, or
    the assistant's grounded reply). `outcome` is one of #225's seven typed
    states, never inferred from content — but it is *assistant-side*
    semantics: a user's own prompt has no outcome until it is processed, so
    `outcome` is `None` for a `role="user"` row and set for a `role=
    "assistant"` row once #46's orchestration resolves one."""

    message_id: str = Field(min_length=1)
    conversation_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    owner_principal_id: str = Field(min_length=1)
    role: MessageRole
    content: str = Field(min_length=1)
    outcome: ConversationOutcome | None = None
    created_at: datetime

    @field_validator("message_id", "conversation_id", "tenant_id", "owner_principal_id")
    @classmethod
    def _validate_ids(cls, value: str, info) -> str:
        return _stable_id(value, info.field_name)

    @field_validator("created_at")
    @classmethod
    def _validate_created_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "created_at")


class ClarificationToken(StrictFrozenModel):
    """Short-lived, single-redemption token binding the originating message,
    candidate choices, and the fields whose ambiguity must be resolved
    (#225: "entity vs listing, cutoff, convention, ..."). `redeemed_at` is
    `None` until redemption; redemption is an atomic conditional UPDATE at
    the repository layer (`WHERE redeemed_at IS NULL AND expires_at > now()`),
    not represented as model state here — a stale or replayed token simply
    fails that WHERE clause and is treated as invalid, without revealing
    whether it existed, already redeemed, or expired."""

    token_id: str = Field(min_length=1)
    conversation_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    owner_principal_id: str = Field(min_length=1)
    originating_message_id: str = Field(min_length=1)
    requested_fields: tuple[str, ...] = Field(min_length=1)
    candidate_choices: tuple[str, ...] = ()
    expires_at: datetime
    created_at: datetime

    @field_validator("token_id", "conversation_id", "tenant_id", "owner_principal_id", "originating_message_id")
    @classmethod
    def _validate_ids(cls, value: str, info) -> str:
        return _stable_id(value, info.field_name)

    @field_validator("expires_at", "created_at")
    @classmethod
    def _validate_aware(cls, value: datetime, info) -> datetime:
        return _require_aware(value, info.field_name)

    def model_post_init(self, _context: object) -> None:
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")


class ResearchGapRequest(StrictFrozenModel):
    """Consent-gated demand-intake record. Per #225: this row must never be
    created without the user's explicit consent — the repository method
    that inserts it is the only path, and callers simply do not invoke it
    when consent was declined. There is no "declined" row and no retained
    prompt text for a declined request; absence of a row *is* the decline."""

    gap_request_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    owner_principal_id: str = Field(min_length=1)
    conversation_id: str | None = None
    prompt_text: str = Field(min_length=1)
    created_at: datetime

    @field_validator("gap_request_id", "tenant_id", "owner_principal_id")
    @classmethod
    def _validate_ids(cls, value: str, info) -> str:
        return _stable_id(value, info.field_name)

    @field_validator("conversation_id")
    @classmethod
    def _validate_optional_conversation_id(cls, value: str | None) -> str | None:
        return None if value is None else _stable_id(value, "conversation_id")

    @field_validator("created_at")
    @classmethod
    def _validate_created_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "created_at")
