"""Entities OpenAPI 3.1 endpoints (`/api/v1/entities/*`)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from llm_service.api.v1._deps import require_principal
from llm_service.services.auth_service import PrincipalRecord, access_context_for
from llm_service.services.entities_service import (
    AnalystRatings,
    EntityDetailResponse,
    FundValuationsResponse,
    IssuerModuleUnavailable,
    SupplyChainExposure,
    ThemesResponse,
    TraceResponse,
    get_entity_detail,
    get_fund_valuations,
    get_themes_overview,
    get_trace,
    load_analyst_ratings,
    load_supply_chain_exposure,
)

router = APIRouter(prefix="/api/v1/entities", tags=["entities"])


@router.get("/holdings", response_model=FundValuationsResponse, operation_id="entities_holdings")
def holdings(_principal: PrincipalRecord = Depends(require_principal)) -> FundValuationsResponse:
    return get_fund_valuations()


@router.get("/themes", response_model=ThemesResponse, operation_id="entities_themes")
def themes(_principal: PrincipalRecord = Depends(require_principal)) -> ThemesResponse:
    return get_themes_overview()


@router.get("/trace", response_model=TraceResponse, operation_id="entities_trace")
def trace(
    issuer_id: str = Query(..., min_length=1),
    cutoff: str | None = Query(default=None),
    principal: PrincipalRecord = Depends(require_principal),
) -> TraceResponse:
    return get_trace(access_context_for(principal), issuer_id=issuer_id, cutoff=cutoff)


@router.get(
    "/{issuer_id:path}/supply-chain",
    response_model=SupplyChainExposure | IssuerModuleUnavailable,
    operation_id="entities_supply_chain",
)
def supply_chain(
    issuer_id: str,
    _principal: PrincipalRecord = Depends(require_principal),
) -> SupplyChainExposure | IssuerModuleUnavailable:
    return load_supply_chain_exposure(issuer_id=issuer_id)


@router.get(
    "/{issuer_id:path}/analyst-ratings",
    response_model=AnalystRatings | IssuerModuleUnavailable,
    operation_id="entities_analyst_ratings",
)
def analyst_ratings(
    issuer_id: str,
    _principal: PrincipalRecord = Depends(require_principal),
) -> AnalystRatings | IssuerModuleUnavailable:
    return load_analyst_ratings(issuer_id=issuer_id)


@router.get("/{issuer_id:path}", response_model=EntityDetailResponse, operation_id="entities_detail")
def entity_detail(
    issuer_id: str,
    convention: str | None = Query(default=None),
    principal: PrincipalRecord = Depends(require_principal),
) -> EntityDetailResponse:
    return get_entity_detail(access_context_for(principal), issuer_id=issuer_id, convention=convention)
