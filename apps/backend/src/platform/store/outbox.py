from datetime import datetime
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column
from src.database import Base

STATUS_PENDING = "pending"
STATUS_PUBLISHED = "published"

class Outbox(Base):
    __tablename__ = "outbox"
    __table_args__ = (sa.Index("ix_outbox_status_id", "status", "id"),)

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    event_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    source_pkg: Mapped[str] = mapped_column(sa.Text, nullable=False)
    aggregate_id: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default=STATUS_PENDING)
    published_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)

class OutboxRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def enqueue(
        self,
        *,
        occurred_at: datetime,
        event_type: str,
        source_pkg: str,
        payload: dict,
        aggregate_id: str | None = None,
    ) -> Outbox:
        row = Outbox(
            occurred_at=occurred_at,
            event_type=event_type,
            source_pkg=source_pkg,
            aggregate_id=aggregate_id,
            payload=payload,
            status=STATUS_PENDING,
        )
        self._session.add(row)
        return row

    async def fetch_pending(self, *, limit: int) -> list[Outbox]:
        result = await self._session.execute(
            sa.select(Outbox).where(Outbox.status == STATUS_PENDING).order_by(Outbox.id).limit(limit)
        )
        return list(result.scalars().all())

    async def mark_published(self, row: Outbox, *, published_at: datetime) -> None:
        row.status = STATUS_PUBLISHED
        row.published_at = published_at
        await self._session.flush()
