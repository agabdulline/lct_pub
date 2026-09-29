"""Журнал действий с прогоном (этап 15.2, docs/architecture/backend.md §2.1): смена основной версии событийной модели
и пересчёт прошлых вердиктов по ней — кто что поменял и какие вердикты изменились.

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-27
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "run_audit",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("run_id", sa.Integer, sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("sim_ts", sa.DateTime(timezone=True)),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("details", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
    )
    op.create_index("run_audit_run_idx", "run_audit", ["run_id", "id"])


def downgrade() -> None:
    op.drop_index("run_audit_run_idx", "run_audit")
    op.drop_table("run_audit")
