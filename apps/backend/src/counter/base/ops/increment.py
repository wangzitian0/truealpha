from datetime import UTC, datetime
from uuid import UUID
from src.counter.base.repository import CounterRepository
from src.counter.base.types.count import Count
from src.counter.base.types.events import Incremented
from src.counter.base.types.key import CounterKey
from src.platform.events.bus import EventBus

def increment(
    repo: CounterRepository,
    *,
    user_id: UUID,
    key: CounterKey,
    bus: EventBus | None = None,
    now: datetime | None = None,
) -> Count:
    new_value = repo.bump(user_id, key)
    count = Count(new_value)
    if bus is not None:
        bus.publish(
            Incremented.create(
                user_id=user_id,
                key=key,
                count=new_value,
                at=now or datetime.now(UTC),
            )
        )
    return count
