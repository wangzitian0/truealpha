"""Panel OpenAPI 3.1 endpoints (`/api/v1/panel/*`)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from llm_service.api.v1._deps import require_principal
from llm_service.services.auth_service import PrincipalRecord, access_context_for
from llm_service.services.panel_service import (
    MacroPanelResponse,
    PanelCoverageResponse,
    UniverseOverviewResponse,
    load_macro_curve,
    load_universes_panel,
    load_wide_panel_coverage,
)
from llm_service.services.strategy_service import DASHBOARD_STRATEGY_ID

router = APIRouter(prefix="/api/v1/panel", tags=["panel"])


@router.get("/universes", response_model=UniverseOverviewResponse, operation_id="panel_universes")
def panel_universes(_principal: PrincipalRecord = Depends(require_principal)) -> UniverseOverviewResponse:
    return load_universes_panel()


@router.get("/coverage", response_model=PanelCoverageResponse, operation_id="panel_coverage")
def panel_coverage(
    strategy_id: str = Query(default=DASHBOARD_STRATEGY_ID),
    universe: str | None = Query(default=None),
    cutoff: str | None = Query(default=None),
    node_key: str | None = Query(default=None),
    issuer_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    principal: PrincipalRecord = Depends(require_principal),
) -> PanelCoverageResponse:
    return load_wide_panel_coverage(
        access_context_for(principal),
        strategy_id=strategy_id,
        universe=universe,
        cutoff=cutoff,
        node_key=node_key,
        issuer_id=issuer_id,
        limit=limit,
    )


@router.get("/macro", response_model=MacroPanelResponse, operation_id="panel_macro")
def panel_macro(_principal: PrincipalRecord = Depends(require_principal)) -> MacroPanelResponse:
    return load_macro_curve()
