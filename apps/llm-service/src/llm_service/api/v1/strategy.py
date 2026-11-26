"""Strategy OpenAPI 3.1 endpoints (`/api/v1/strategy/*`)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from llm_service.api.v1._deps import require_principal
from llm_service.services.auth_service import PrincipalRecord, access_context_for
from llm_service.services.strategy_service import (
    DASHBOARD_STRATEGY_ID,
    BacktestDetailResponse,
    BacktestRunsResponse,
    RankingsResponse,
    TriggerBacktestRequest,
    TriggerBacktestResponse,
    get_backtest_detail,
    get_rankings,
    list_backtest_runs,
    trigger_backtest_run,
)

router = APIRouter(prefix="/api/v1/strategy", tags=["strategy"])


@router.get("/rankings", response_model=RankingsResponse, operation_id="strategy_rankings")
def rankings(
    strategy_id: str = Query(default=DASHBOARD_STRATEGY_ID),
    universe: str | None = Query(default="all"),
    cutoff: str | None = Query(default=None),
    convention: str | None = Query(default=None),
    limit: int = Query(default=1000, ge=1, le=1000),
    principal: PrincipalRecord = Depends(require_principal),
) -> RankingsResponse:
    return get_rankings(
        access_context_for(principal),
        strategy_id=strategy_id,
        universe=universe,
        cutoff=cutoff,
        convention=convention,
        limit=limit,
    )


@router.get("/backtests", response_model=BacktestRunsResponse, operation_id="strategy_backtests")
def backtests_list(
    limit: int = Query(default=20, ge=1, le=100),
    strategy_key: str | None = Query(default=None),
    _principal: PrincipalRecord = Depends(require_principal),
) -> BacktestRunsResponse:
    return list_backtest_runs(limit=limit, strategy_key=strategy_key)


@router.post(
    "/backtests/trigger",
    response_model=TriggerBacktestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="strategy_backtest_trigger",
)
def backtest_trigger(
    payload: TriggerBacktestRequest | None = None,
    principal: PrincipalRecord = Depends(require_principal),
) -> TriggerBacktestResponse:
    try:
        return trigger_backtest_run(principal.principal_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None


@router.get("/backtests/{run_id}", response_model=BacktestDetailResponse, operation_id="strategy_backtest_detail")
def backtest_detail(
    run_id: str,
    _principal: PrincipalRecord = Depends(require_principal),
) -> BacktestDetailResponse:
    detail = get_backtest_detail(run_id)
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="backtest_not_found")
    return detail
