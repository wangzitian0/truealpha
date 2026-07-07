from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from src.database import get_db
from src.counter import (
    CounterKey,
    InvalidCounterKeyError,
    NegativeCountError,
    record_increment,
    read_count,
)

router = APIRouter(prefix="/counter", tags=["Counter"])

class IncrementRequest(BaseModel):
    user_id: UUID = Field(..., description="UUID of the user triggering the increment")
    key: str = Field(..., description="Dotted key string e.g., 'button.click'")

class CountResponse(BaseModel):
    key: str
    count: int
    user_id: str | None = None

@router.post("/increment", response_model=CountResponse)
async def increment_counter(
    payload: IncrementRequest,
    db: AsyncSession = Depends(get_db),
):
    try:
        key_obj = CounterKey(payload.key)
    except InvalidCounterKeyError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        new_count = await record_increment(db, user_id=payload.user_id, key=key_obj)
        await db.commit()
        return CountResponse(
            key=payload.key,
            count=int(new_count),
            user_id=str(payload.user_id),
        )
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/count", response_model=CountResponse)
async def get_counter_value(
    key: str = Query(..., description="Dotted key e.g., 'button.click'"),
    user_id: UUID | None = Query(None, description="Optional UUID to filter by user"),
    db: AsyncSession = Depends(get_db),
):
    try:
        key_obj = CounterKey(key)
    except InvalidCounterKeyError as e:
        raise HTTPException(status_code=400, detail=str(e))

    count_val = await read_count(db, key=key_obj, user_id=user_id)
    return CountResponse(
        key=key,
        count=int(count_val),
        user_id=str(user_id) if user_id else None,
    )
