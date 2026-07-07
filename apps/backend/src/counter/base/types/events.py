from dataclasses import dataclass
from datetime import datetime
from uuid import UUID
from src.counter.base.types.key import CounterKey
from src.platform.events.event import DomainEvent

EVENT_TYPE = "counter.Incremented"

@dataclass(frozen=True)
class Incremented(DomainEvent):
    user_id: UUID
    key: CounterKey
    count: int

    @classmethod
    def create(cls, *, user_id: UUID, key: CounterKey, count: int, at: datetime) -> Incremented:
        return cls(
            event_type=EVENT_TYPE,
            occurred_at=at,
            user_id=user_id,
            key=key,
            count=count,
        )

    def payload(self) -> dict:
        return {
            "aggregate_id": str(self.user_id),
            "user_id": str(self.user_id),
            "key": self.key.value,
            "count": self.count,
            "at": self.occurred_at.isoformat(),
        }
