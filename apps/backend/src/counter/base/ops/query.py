from uuid import UUID
from src.counter.base.repository import CounterRepository
from src.counter.base.types.count import Count
from src.counter.base.types.key import CounterKey

def get_count(
    repo: CounterRepository,
    *,
    key: CounterKey,
    user_id: UUID | None = None,
) -> Count:
    if user_id is None:
        return Count(repo.total(key))
    return Count(repo.for_user(user_id, key))
