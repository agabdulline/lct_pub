"""Время приёма тревоги в систему (реальные часы) — для замера задержки «событие → тревога → оценка → лента».

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-26
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # у уже загруженных тревог — момент миграции: время приёма базовой истории не известно
    op.add_column(
        "alarms", sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False)
    )


def downgrade() -> None:
    op.drop_column("alarms", "created_at")
