from datetime import UTC, datetime
from uuid import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from src.counter.base.types.count import Count
from src.counter.base.types.events import Incremented
from src.counter.base.types.key import CounterKey
from src.counter.extension.sql import SqlCounterRepository
from src.platform.events.bus import OutboxEventBus

SOURCE_PKG = "counter"

async def record_increment(
    db: AsyncSession,
    *,
    user_id: UUID,
    key: CounterKey,
) -> Count:
    repo = SqlCounterRepository(db)
    new_value = await repo.bump(user_id, key)
    bus = OutboxEventBus(db, source_pkg=SOURCE_PKG)
    bus.publish(Incremented.create(user_id=user_id, key=key, count=new_value, at=datetime.now(UTC)))
    return Count(new_value)
