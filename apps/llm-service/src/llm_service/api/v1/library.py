"""Library OpenAPI 3.1 endpoints (`/api/v1/library/*`)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from truealpha_runtime.research_report import ReportSectionKind, ResearchReportKind

from llm_service.api.v1._deps import require_principal
from llm_service.services.auth_service import PrincipalRecord, access_context_for
from llm_service.services.library_service import (
    CardExportRequest,
    CardExportResponse,
    CreateDocumentRequest,
    CreateDocumentResponse,
    DeleteDocumentResponse,
    DocumentDetailResponse,
    DocumentListResponse,
    LibraryReportResponse,
    build_library_report,
    create_document,
    delete_document,
    export_or_render_card,
    get_document_detail,
    list_documents,
)

router = APIRouter(prefix="/api/v1/library", tags=["library"])


@router.get("/reports", response_model=LibraryReportResponse, operation_id="library_report")
def reports(
    report_kind: ResearchReportKind = Query(default=ResearchReportKind.THEME_RANKING),
    target_entity_ids: list[str] = Query(default=["theme:large_model_value"]),
    cutoff_at: datetime | None = Query(default=None),
    section_kinds: list[ReportSectionKind] = Query(default=[]),
    strategy_id: str | None = Query(default=None),
    title: str | None = Query(default=None),
    format: Literal["json", "markdown"] = Query(default="json"),
    principal: PrincipalRecord = Depends(require_principal),
) -> LibraryReportResponse:
    try:
        return build_library_report(
            access_context_for(principal),
            report_kind=report_kind,
            target_entity_ids=tuple(target_entity_ids),
            cutoff_at=cutoff_at,
            section_kinds=tuple(section_kinds),
            strategy_id=strategy_id,
            title=title,
            output_format=format,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from None


@router.post("/cards/export", response_model=CardExportResponse, operation_id="library_card_export")
def export_card(
    payload: CardExportRequest,
    principal: PrincipalRecord = Depends(require_principal),
) -> CardExportResponse:
    try:
        return export_or_render_card(access_context_for(principal), payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from None


@router.get("/documents", response_model=DocumentListResponse, operation_id="library_documents_list")
def documents_list(
    limit: int | None = Query(default=None, ge=1, le=200),
    before_created_at: str | None = Query(default=None),
    before_document_id: str | None = Query(default=None),
    principal: PrincipalRecord = Depends(require_principal),
) -> DocumentListResponse:
    try:
        return list_documents(
            access_context_for(principal),
            limit=limit,
            before_created_at=before_created_at,
            before_document_id=before_document_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None


@router.post(
    "/documents", response_model=CreateDocumentResponse, status_code=201, operation_id="library_documents_create"
)
def documents_create(
    payload: CreateDocumentRequest,
    principal: PrincipalRecord = Depends(require_principal),
) -> CreateDocumentResponse:
    try:
        return create_document(
            access_context_for(principal),
            source_artifact_id=payload.source_artifact_id,
            content_bytes=payload.content.encode("utf-8"),
            content_type=payload.content_type,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None


@router.get("/documents/{document_id}", response_model=DocumentDetailResponse, operation_id="library_document_detail")
def documents_detail(
    document_id: str,
    principal: PrincipalRecord = Depends(require_principal),
) -> DocumentDetailResponse:
    detail = get_document_detail(access_context_for(principal), document_id=document_id)
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="document_not_found")
    return detail


@router.delete(
    "/documents/{document_id}", response_model=DeleteDocumentResponse, operation_id="library_document_delete"
)
def documents_delete(
    document_id: str,
    principal: PrincipalRecord = Depends(require_principal),
) -> DeleteDocumentResponse:
    deleted = delete_document(access_context_for(principal), document_id=document_id)
    if deleted is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="document_not_found")
    return deleted
