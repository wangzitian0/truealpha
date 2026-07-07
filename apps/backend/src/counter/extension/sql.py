from uuid import UUID
from sqlalchemy import String, func, select
from sqlalchemy.dialects.postgresql import UUID as PGUUID, insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column
from src.counter.base.types.key import CounterKey
from src.database import Base

class CounterTally(Base):
    __tablename__ = "counter_tally"

    user_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, nullable=False)
    key: Mapped[str] = mapped_column(String(255), primary_key=True, nullable=False, index=True)
    count: Mapped[int] = mapped_column(nullable=False, server_default="0")

class SqlCounterRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def bump(self, user_id: UUID, key: CounterKey) -> int:
        stmt = (
            postgresql_insert(CounterTally)
            .values(user_id=user_id, key=key.value, count=1)
            .on_conflict_do_update(
                index_elements=[CounterTally.user_id, CounterTally.key],
                set_={"count": CounterTally.count + 1},
            )
            .returning(CounterTally.count)
        )
        result = await self._db.execute(stmt)
        return int(result.scalar_one())

    async def total(self, key: CounterKey) -> int:
        result = await self._db.execute(
            select(func.coalesce(func.sum(CounterTally.count), 0)).where(CounterTally.key == key.value)
        )
        return int(result.scalar_one())

    async def for_user(self, user_id: UUID, key: CounterKey) -> int:
        result = await self._db.execute(
            select(CounterTally.count).where(
                CounterTally.user_id == user_id,
                CounterTally.key == key.value,
            )
        )
        row = result.scalar_one_or_none()
        return int(row) if row is not None else 0
