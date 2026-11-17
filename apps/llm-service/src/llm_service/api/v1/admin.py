"""Administrator operational OpenAPI 3.1 endpoints (`/api/v1/admin/*`)."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, status

from llm_service.api.v1._deps import require_admin
from llm_service.services.admin_service import (
    DatahubStatsResponse,
    MacroPanelResponse,
    OpsOverviewResponse,
    PipelineOverviewResponse,
    QualityOverviewResponse,
    TriggerPipelineRequest,
    TriggerPipelineResponse,
    UniverseOverviewResponse,
    VerdictHistoryResponse,
    get_datahub_stats,
    get_macro_panel,
    get_ops_overview,
    get_pipeline_overview,
    get_quality_overview,
    get_universes_overview,
    get_verdict_history,
    trigger_pipeline,
)
from llm_service.services.auth_service import PrincipalRecord, access_context_for

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])
_CHECK_NAME_RE = re.compile(r"^[a-z0-9_]+(@[a-z0-9_.:-]+)?$")


@router.get("/ops", response_model=OpsOverviewResponse, operation_id="admin_ops_overview")
def ops_overview(_admin: PrincipalRecord = Depends(require_admin)) -> OpsOverviewResponse:
    return get_ops_overview()


@router.get("/pipeline", response_model=PipelineOverviewResponse, operation_id="admin_pipeline_overview")
def pipeline_overview(_admin: PrincipalRecord = Depends(require_admin)) -> PipelineOverviewResponse:
    return get_pipeline_overview()


@router.get("/quality", response_model=QualityOverviewResponse, operation_id="admin_quality_overview")
def quality_overview(admin: PrincipalRecord = Depends(require_admin)) -> QualityOverviewResponse:
    return get_quality_overview(access_context_for(admin))


@router.get("/macro", response_model=MacroPanelResponse, operation_id="admin_macro_panel")
def macro_panel(_admin: PrincipalRecord = Depends(require_admin)) -> MacroPanelResponse:
    return get_macro_panel()


@router.get("/universes", response_model=UniverseOverviewResponse, operation_id="admin_universes_overview")
def universes_overview(_admin: PrincipalRecord = Depends(require_admin)) -> UniverseOverviewResponse:
    return get_universes_overview()


@router.get("/datahub", response_model=DatahubStatsResponse, operation_id="admin_datahub_stats")
def datahub_stats(_admin: PrincipalRecord = Depends(require_admin)) -> DatahubStatsResponse:
    return get_datahub_stats()


@router.get("/verdicts/{check_name}", response_model=VerdictHistoryResponse, operation_id="admin_verdict_history")
def verdict_history(check_name: str, _admin: PrincipalRecord = Depends(require_admin)) -> VerdictHistoryResponse:
    if not _CHECK_NAME_RE.match(check_name):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid check_name")
    return get_verdict_history(check_name)


@router.post(
    "/pipeline/trigger",
    response_model=TriggerPipelineResponse,
    status_code=202,
    operation_id="admin_pipeline_trigger",
)
def pipeline_trigger(
    payload: TriggerPipelineRequest,
    admin: PrincipalRecord = Depends(require_admin),
) -> TriggerPipelineResponse:
    try:
        return trigger_pipeline(admin.principal_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
