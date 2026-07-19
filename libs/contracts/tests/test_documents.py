from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from truealpha_contracts.documents import (
    DocumentDownloadTicket,
    DocumentListQuery,
    DocumentPage,
    DocumentTombstone,
    NewDocumentRevision,
    ResearchDocument,
    ResearchDocumentRevision,
)

_NOW = datetime(2026, 7, 18, tzinfo=UTC)
_LATER = datetime(2026, 7, 18, 0, 30, tzinfo=UTC)
_SHA256 = "a" * 64


def _document(
    *,
    document_id: str = "document:alice-1",
    tenant_id: str = "tenant:truealpha",
    owner_principal_id: str = "principal:alice",
    created_at: datetime = _NOW,
) -> ResearchDocument:
    return ResearchDocument(
        document_id=document_id,
        tenant_id=tenant_id,
        owner_principal_id=owner_principal_id,
        created_at=created_at,
    )


def _revision(
    *,
    revision_id: str = "revision:1",
    document_id: str = "document:alice-1",
    tenant_id: str = "tenant:truealpha",
    owner_principal_id: str = "principal:alice",
    source_artifact_id: str = "report:" + _SHA256,
    artifact_sha256: str = _SHA256,
    artifact_byte_length: int = 1024,
    artifact_content_type: str = "application/json",
    created_at: datetime = _NOW,
) -> ResearchDocumentRevision:
    return ResearchDocumentRevision(
        revision_id=revision_id,
        document_id=document_id,
        tenant_id=tenant_id,
        owner_principal_id=owner_principal_id,
        source_artifact_id=source_artifact_id,
        artifact_sha256=artifact_sha256,
        artifact_byte_length=artifact_byte_length,
        artifact_content_type=artifact_content_type,
        created_at=created_at,
    )


def test_document_round_trips() -> None:
    document = _document()
    assert document.document_id == "document:alice-1"
    assert document.owner_principal_id == "principal:alice"


def test_document_rejects_naive_datetime() -> None:
    with pytest.raises(ValidationError):
        _document(created_at=datetime(2026, 7, 18))  # noqa: DTZ001 - the point under test


@pytest.mark.parametrize("bad_id", ["", " ", "document with spaces", "!not-stable"])
def test_document_rejects_unstable_id(bad_id: str) -> None:
    with pytest.raises(ValidationError):
        _document(document_id=bad_id)


def test_document_frozen() -> None:
    document = _document()
    with pytest.raises(ValidationError):
        document.document_id = "document:alice-2"  # type: ignore[misc]


def test_revision_round_trips() -> None:
    revision = _revision()
    assert revision.revision_id == "revision:1"
    assert revision.artifact_sha256 == _SHA256
    assert revision.source_artifact_id == "report:" + _SHA256


def test_revision_rejects_non_hex_sha256() -> None:
    with pytest.raises(ValidationError):
        _revision(artifact_sha256="not-a-digest")


def test_revision_rejects_uppercase_sha256() -> None:
    with pytest.raises(ValidationError):
        _revision(artifact_sha256="A" * 64)


def test_revision_rejects_negative_byte_length() -> None:
    with pytest.raises(ValidationError):
        _revision(artifact_byte_length=-1)


def test_revision_has_no_object_key_field() -> None:
    """The private locator must never surface on the consumer DTO — see #373."""
    assert "object_key" not in ResearchDocumentRevision.model_fields
    assert "bucket" not in ResearchDocumentRevision.model_fields


def test_tombstone_round_trips() -> None:
    tombstone = DocumentTombstone(
        tombstone_id="tombstone:1",
        document_id="document:alice-1",
        tenant_id="tenant:truealpha",
        owner_principal_id="principal:alice",
        created_at=_NOW,
    )
    assert tombstone.document_id == "document:alice-1"


def test_download_ticket_requires_expiry_after_creation() -> None:
    with pytest.raises(ValueError, match="expires_at must be after created_at"):
        DocumentDownloadTicket(
            ticket_id="ticket:1",
            document_id="document:alice-1",
            revision_id="revision:1",
            tenant_id="tenant:truealpha",
            owner_principal_id="principal:alice",
            expires_at=_NOW,
            created_at=_NOW,
        )


def test_download_ticket_valid() -> None:
    ticket = DocumentDownloadTicket(
        ticket_id="ticket:1",
        document_id="document:alice-1",
        revision_id="revision:1",
        tenant_id="tenant:truealpha",
        owner_principal_id="principal:alice",
        expires_at=_LATER,
        created_at=_NOW,
    )
    assert ticket.expires_at == _LATER


def test_download_ticket_has_no_redeemed_at_field() -> None:
    """Redemption is an atomic conditional UPDATE at the repository layer,
    not model state — see #396's ClarificationToken, the pattern this
    mirrors."""
    assert "redeemed_at" not in DocumentDownloadTicket.model_fields


def test_document_list_query_defaults() -> None:
    query = DocumentListQuery()
    assert query.limit == 50
    assert query.before is None


def test_document_list_query_rejects_naive_before() -> None:
    with pytest.raises(ValidationError):
        DocumentListQuery(before=datetime(2026, 7, 18))  # noqa: DTZ001 - the point under test


def test_document_page_defaults_empty() -> None:
    page = DocumentPage()
    assert page.documents == ()
    assert page.next_before is None


def test_new_document_revision_rejects_unstable_source_artifact_id() -> None:
    with pytest.raises(ValidationError):
        NewDocumentRevision(
            source_artifact_id="not stable",
            artifact_sha256=_SHA256,
            artifact_byte_length=10,
            artifact_content_type="application/json",
            artifact_bytes=b"{}",
        )


def test_new_document_revision_rejects_non_hex_sha256() -> None:
    with pytest.raises(ValidationError):
        NewDocumentRevision(
            source_artifact_id="report:" + _SHA256,
            artifact_sha256="not-a-digest",
            artifact_byte_length=10,
            artifact_content_type="application/json",
            artifact_bytes=b"{}",
        )
