"""Administrator operational OpenAPI 3.1 endpoints (`/api/v1/admin/*`)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from llm_service.api.v1._deps import require_admin
from llm_service.services.admin_service import (
    DatahubStatsResponse,
    OpsOverviewResponse,
    TriggerPipelineRequest,
    TriggerPipelineResponse,
    get_datahub_stats,
    get_ops_overview,
    trigger_pipeline,
)
from llm_service.services.auth_service import PrincipalRecord

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


@router.get("/ops", response_model=OpsOverviewResponse, operation_id="admin_ops_overview")
def ops_overview(_admin: PrincipalRecord = Depends(require_admin)) -> OpsOverviewResponse:
    return get_ops_overview()


@router.post(
    "/triggers",
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


@router.get("/datahub", response_model=DatahubStatsResponse, operation_id="admin_datahub_stats")
def datahub_stats(_admin: PrincipalRecord = Depends(require_admin)) -> DatahubStatsResponse:
    return get_datahub_stats()
