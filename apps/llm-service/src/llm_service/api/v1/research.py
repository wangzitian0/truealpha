"""Research, mart, XHS card, and RLS document OpenAPI 3.1 endpoints (`/api/v1/research/*`)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from llm_service.api.v1._deps import require_principal
from llm_service.services.auth_service import PrincipalRecord, access_context_for
from llm_service.services.research_service import (
    BacktestDetailResponse,
    BacktestRunsResponse,
    CardExportRequest,
    CardExportResponse,
    CreateDocumentRequest,
    CreateDocumentResponse,
    DeleteDocumentResponse,
    DocumentDetailResponse,
    DocumentListResponse,
    EntityDetailResponse,
    FundValuationsResponse,
    OverviewResponse,
    RankingsResponse,
    StrategyRunPageResponse,
    ThemesResponse,
    TraceResponse,
    create_document,
    delete_document,
    export_or_render_card,
    get_backtest_detail,
    get_document_detail,
    get_document_revision_bytes,
    get_entity_detail,
    get_fund_valuations,
    get_overview,
    get_rankings,
    get_strategy_run_page,
    get_themes_overview,
    get_trace,
    list_backtest_runs,
    list_documents,
)

router = APIRouter(prefix="/api/v1/research", tags=["research"])


@router.get("/overview", response_model=OverviewResponse, operation_id="research_overview")
def overview(principal: PrincipalRecord = Depends(require_principal)) -> OverviewResponse:
    return get_overview(access_context_for(principal))


@router.get("/strategy", response_model=StrategyRunPageResponse, operation_id="research_strategy")
def strategy(
    strategy_id: str = Query(default="large_model_value_v0"),
    universe: str | None = Query(default=None),
    cutoff: str | None = Query(default=None),
    principal: PrincipalRecord = Depends(require_principal),
) -> StrategyRunPageResponse:
    return get_strategy_run_page(
        access_context_for(principal), strategy_id=strategy_id, universe=universe, cutoff=cutoff
    )


@router.get("/rankings", response_model=RankingsResponse, operation_id="research_rankings")
def rankings(
    universe: str | None = Query(default="all"),
    cutoff: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
    principal: PrincipalRecord = Depends(require_principal),
) -> RankingsResponse:
    return get_rankings(access_context_for(principal), universe=universe, cutoff=cutoff, limit=limit)


@router.get("/trace", response_model=TraceResponse, operation_id="research_trace")
def trace(
    issuer_id: str = Query(..., min_length=1),
    cutoff: str | None = Query(default=None),
    principal: PrincipalRecord = Depends(require_principal),
) -> TraceResponse:
    return get_trace(access_context_for(principal), issuer_id=issuer_id, cutoff=cutoff)


@router.get("/entities/{issuer_id:path}", response_model=EntityDetailResponse, operation_id="research_entity_detail")
def entity_detail(
    issuer_id: str,
    principal: PrincipalRecord = Depends(require_principal),
) -> EntityDetailResponse:
    return get_entity_detail(access_context_for(principal), issuer_id=issuer_id)


@router.get("/themes", response_model=ThemesResponse, operation_id="research_themes")
def themes(_principal: PrincipalRecord = Depends(require_principal)) -> ThemesResponse:
    return get_themes_overview()


@router.get("/funds", response_model=FundValuationsResponse, operation_id="research_funds")
def funds(_principal: PrincipalRecord = Depends(require_principal)) -> FundValuationsResponse:
    return get_fund_valuations()


@router.get("/backtests", response_model=BacktestRunsResponse, operation_id="research_backtests_list")
def backtests_list(
    limit: int = Query(default=20, ge=1, le=100),
    _principal: PrincipalRecord = Depends(require_principal),
) -> BacktestRunsResponse:
    return list_backtest_runs(limit=limit)


@router.get("/backtests/{run_id}", response_model=BacktestDetailResponse, operation_id="research_backtest_detail")
def backtest_detail(
    run_id: str,
    _principal: PrincipalRecord = Depends(require_principal),
) -> BacktestDetailResponse:
    detail = get_backtest_detail(run_id)
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="backtest_not_found")
    return detail


@router.post("/cards/export", response_model=CardExportResponse, operation_id="research_cards_export")
def export_card(
    payload: CardExportRequest,
    principal: PrincipalRecord = Depends(require_principal),
) -> CardExportResponse:
    try:
        return export_or_render_card(access_context_for(principal), payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from None


@router.get("/documents", response_model=DocumentListResponse, operation_id="research_documents_list")
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
    "/documents", response_model=CreateDocumentResponse, status_code=201, operation_id="research_documents_create"
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


@router.get("/documents/{document_id}", response_model=DocumentDetailResponse, operation_id="research_documents_detail")
def documents_detail(
    document_id: str,
    principal: PrincipalRecord = Depends(require_principal),
) -> DocumentDetailResponse:
    detail = get_document_detail(access_context_for(principal), document_id=document_id)
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="document_not_found")
    return detail


@router.delete(
    "/documents/{document_id}", response_model=DeleteDocumentResponse, operation_id="research_documents_delete"
)
def documents_delete(
    document_id: str,
    principal: PrincipalRecord = Depends(require_principal),
) -> DeleteDocumentResponse:
    deleted = delete_document(access_context_for(principal), document_id=document_id)
    if deleted is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="document_not_found")
    return deleted


@router.get(
    "/documents/{document_id}/revisions/{revision_id}/content",
    operation_id="research_documents_revision_content",
)
def documents_revision_content(
    document_id: str,
    revision_id: str,
    principal: PrincipalRecord = Depends(require_principal),
) -> Response:
    found = get_document_revision_bytes(access_context_for(principal), document_id=document_id, revision_id=revision_id)
    if found is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="document_revision_not_found")
    raw, content_type = found
    return Response(content=raw, media_type=content_type)
