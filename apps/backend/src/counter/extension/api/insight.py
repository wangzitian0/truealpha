from uuid import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from src.counter.base.types.count import Count
from src.counter.base.types.key import CounterKey
from src.counter.extension.sql import SqlCounterRepository

async def read_count(
    db: AsyncSession,
    *,
    key: CounterKey,
    user_id: UUID | None = None,
) -> Count:
    repo = SqlCounterRepository(db)
    if user_id is None:
        return Count(await repo.total(key))
    return Count(await repo.for_user(user_id, key))
