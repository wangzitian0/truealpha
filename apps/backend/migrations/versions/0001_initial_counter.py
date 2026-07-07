"""Initial migration for counter tables.

Revision ID: 0001
Revises: None
Create Date: 2026-06-27

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

def upgrade() -> None:
    # 1. Create counter_tally table
    op.create_table(
        "counter_tally",
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("count", sa.Integer(), server_default="0", nullable=False),
        sa.PrimaryKeyConstraint("user_id", "key")
    )
    op.create_index("ix_counter_tally_key", "counter_tally", ["key"], unique=False)

    # 2. Create outbox table
    op.create_table(
        "outbox",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("source_pkg", sa.Text(), nullable=False),
        sa.Column("aggregate_id", sa.Text(), nullable=True),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id")
    )
    op.create_index("ix_outbox_status_id", "outbox", ["status", "id"], unique=False)

def downgrade() -> None:
    op.drop_index("ix_outbox_status_id", table_name="outbox")
    op.drop_table("outbox")
    op.drop_index("ix_counter_tally_key", table_name="counter_tally")
    op.drop_table("counter_tally")
