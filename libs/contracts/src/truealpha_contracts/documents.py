"""Owner-scoped research document lifecycle — see #373 (slice of #235).

This module supplies the DTOs and repository port a TypeScript adapter
(migration 0031's `app` schema tables) implements. It performs no rendering,
no object-storage I/O, and no authorization decision — it persists an
already-rendered #369/#372 artifact's bytes as an opaque, content-addressed
revision, exactly the boundary #396's conversations module holds for
conversation storage (init.md Section 1, rule 2).

`StrictFrozenModel` is defined locally in `truealpha_contracts.access`, not
imported from some nonexistent public base — every DTO here builds on that
one frozen/extra-forbid base directly.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import Field, field_validator

from truealpha_contracts.access import AccessContext, StrictFrozenModel
from truealpha_contracts.models import _require_aware

_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _stable_id(value: str, field_name: str) -> str:
    if _ID_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a stable identifier")
    return value


class ResearchDocument(StrictFrozenModel):
    """A document root. No mutable "current revision" field — the owner-
    facing view is a projection over its append-only revisions."""

    document_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    owner_principal_id: str = Field(min_length=1)
    created_at: datetime

    @field_validator("document_id", "tenant_id", "owner_principal_id")
    @classmethod
    def _validate_ids(cls, value: str, info) -> str:
        return _stable_id(value, info.field_name)

    @field_validator("created_at")
    @classmethod
    def _validate_created_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "created_at")


class ResearchDocumentRevision(StrictFrozenModel):
    """One immutable revision. `source_artifact_id` is the content-addressed
    id of the #369/#372 artifact this revision serializes (e.g.
    `"report:<sha256>"`), kept as informational lineage only — neither
    `ResearchReport` nor `ResearchCard` has a persisted table to foreign-key
    against yet. The private object-storage locator (bucket/key) is
    deliberately absent here: it is server-only and never reaches a
    consumer DTO, browser, or MCP response."""

    revision_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    owner_principal_id: str = Field(min_length=1)
    source_artifact_id: str = Field(min_length=1)
    artifact_sha256: str
    artifact_byte_length: int = Field(ge=0)
    artifact_content_type: str = Field(min_length=1)
    created_at: datetime

    @field_validator("revision_id", "document_id", "tenant_id", "owner_principal_id", "source_artifact_id")
    @classmethod
    def _validate_ids(cls, value: str, info) -> str:
        return _stable_id(value, info.field_name)

    @field_validator("artifact_sha256")
    @classmethod
    def _validate_sha256(cls, value: str) -> str:
        if _SHA256_PATTERN.fullmatch(value) is None:
            raise ValueError("artifact_sha256 must be a lowercase hex sha256 digest")
        return value

    @field_validator("created_at")
    @classmethod
    def _validate_created_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "created_at")


class DocumentTombstone(StrictFrozenModel):
    """Soft-delete marker. Presence of this row for a `document_id` is the
    only signal: get/list must treat a tombstoned document identically to a
    nonexistent one (non-enumerating), never a distinct "deleted" state
    exposed to the caller."""

    tombstone_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    owner_principal_id: str = Field(min_length=1)
    created_at: datetime

    @field_validator("tombstone_id", "document_id", "tenant_id", "owner_principal_id")
    @classmethod
    def _validate_ids(cls, value: str, info) -> str:
        return _stable_id(value, info.field_name)

    @field_validator("created_at")
    @classmethod
    def _validate_created_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "created_at")


class DocumentDownloadTicket(StrictFrozenModel):
    """Short-lived, single-redemption ticket — structurally identical to
    #396's `ClarificationToken`. `redeemed_at` is intentionally not a field
    here: redemption is an atomic conditional UPDATE at the repository layer
    (`WHERE redeemed_at IS NULL AND expires_at > now()`), and a stale or
    replayed ticket simply fails that WHERE clause, indistinguishable from
    one that never existed, already redeemed, or expired, or whose document
    was tombstoned in the meantime."""

    ticket_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    revision_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    owner_principal_id: str = Field(min_length=1)
    expires_at: datetime
    created_at: datetime

    @field_validator("ticket_id", "document_id", "revision_id", "tenant_id", "owner_principal_id")
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


class DocumentCursor(StrictFrozenModel):
    """Keyset pagination cursor. `created_at` alone is not unique — two
    documents can share a timestamp — so `document_id` breaks the tie;
    without it, a page boundary landing on a shared `created_at` could skip
    or repeat rows. Mirrors the TypeScript adapter's cursor shape exactly."""

    created_at: datetime
    document_id: str

    @field_validator("created_at")
    @classmethod
    def _validate_created_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "created_at")

    @field_validator("document_id")
    @classmethod
    def _validate_document_id(cls, value: str) -> str:
        return _stable_id(value, "document_id")


class DocumentListQuery(StrictFrozenModel):
    """Cursor pagination over an owner's documents, newest first. `before` is
    exclusive: pass the previous page's last row's cursor to continue."""

    limit: int = Field(default=50, ge=1, le=200)
    before: DocumentCursor | None = None


class DocumentPage(StrictFrozenModel):
    documents: tuple[ResearchDocument, ...] = ()
    next_before: DocumentCursor | None = None


class NewDocumentRevision(StrictFrozenModel):
    """Caller-supplied input to create a document or append a revision — no
    identity fields: the repository assigns `revision_id`/`document_id` and
    stamps `tenant_id`/`owner_principal_id` from the verified `AccessContext`,
    never from caller input."""

    source_artifact_id: str = Field(min_length=1)
    artifact_sha256: str
    artifact_byte_length: int = Field(ge=0)
    artifact_content_type: str = Field(min_length=1)
    artifact_bytes: bytes

    @field_validator("source_artifact_id")
    @classmethod
    def _validate_source_artifact_id(cls, value: str) -> str:
        return _stable_id(value, "source_artifact_id")

    @field_validator("artifact_sha256")
    @classmethod
    def _validate_sha256(cls, value: str) -> str:
        if _SHA256_PATTERN.fullmatch(value) is None:
            raise ValueError("artifact_sha256 must be a lowercase hex sha256 digest")
        return value

    def model_post_init(self, _context: object) -> None:
        if self.artifact_byte_length != len(self.artifact_bytes):
            raise ValueError("artifact_byte_length must equal len(artifact_bytes)")
        if self.artifact_sha256 != hashlib.sha256(self.artifact_bytes).hexdigest():
            raise ValueError("artifact_sha256 must equal sha256(artifact_bytes)")


class CreatedDocument(StrictFrozenModel):
    """`create_document`'s return shape: the new root plus its first
    revision — mirrors the TypeScript adapter's `{ document, revision }`
    exactly, rather than dropping the revision the caller just created."""

    document: ResearchDocument
    revision: ResearchDocumentRevision


@runtime_checkable
class OwnedDocumentService(Protocol):
    """Cross-language repository port. `apps/app-web/src/server/documents.ts`
    is today's only implementation (a Postgres + S3-compatible object-store
    adapter); this Protocol is the typed contract that implementation must
    satisfy, mirrored field-for-field."""

    def list_documents(self, context: AccessContext, query: DocumentListQuery) -> DocumentPage: ...

    def get_document(self, context: AccessContext, document_id: str) -> ResearchDocument | None: ...

    def list_revisions(self, context: AccessContext, document_id: str) -> tuple[ResearchDocumentRevision, ...]: ...

    def create_document(self, context: AccessContext, revision: NewDocumentRevision) -> CreatedDocument: ...

    def append_revision(
        self, context: AccessContext, document_id: str, revision: NewDocumentRevision
    ) -> ResearchDocumentRevision: ...

    def tombstone_document(self, context: AccessContext, document_id: str) -> DocumentTombstone: ...

    def issue_download_ticket(
        self, context: AccessContext, document_id: str, revision_id: str, expires_in_minutes: int
    ) -> DocumentDownloadTicket: ...

    def redeem_download_ticket(self, context: AccessContext, ticket_id: str) -> bytes | None: ...
