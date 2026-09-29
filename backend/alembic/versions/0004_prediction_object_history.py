"""predictions.object_history — у объекта и шлейфа канала есть исходы прошлых решений (ml-service, docs/api.md §2).

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-26
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("predictions", sa.Column("object_history", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("predictions", "object_history")
