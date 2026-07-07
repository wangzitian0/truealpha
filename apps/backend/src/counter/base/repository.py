from typing import Protocol
from uuid import UUID
from src.counter.base.types.key import CounterKey

class CounterRepository(Protocol):
    def bump(self, user_id: UUID, key: CounterKey) -> int:
        ...
    def total(self, key: CounterKey) -> int:
        ...
    def for_user(self, user_id: UUID, key: CounterKey) -> int:
        ...
