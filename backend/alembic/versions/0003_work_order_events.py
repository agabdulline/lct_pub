"""История заявки: создание и каждая смена статуса, комментария или исполнителя (docs/api.md, «Заявка» → history).

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-26
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "work_order_events",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "work_order_id", sa.BigInteger(), sa.ForeignKey("work_orders.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("assignee", sa.Text(), nullable=True),
    )
    op.create_index("work_order_events_order_idx", "work_order_events", ["work_order_id", "ts"])


def downgrade() -> None:
    op.drop_table("work_order_events")
