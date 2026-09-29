"""Расширение TimescaleDB. Таблицы — этап 6 плана (docs/architecture/data.md).

Revision ID: 0001
Revises:
Create Date: 2026-09-26
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")


def downgrade() -> None:
    op.execute("DROP EXTENSION IF EXISTS timescaledb")
