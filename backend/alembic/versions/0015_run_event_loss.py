"""Потери событий прогона: отправлено источником, а шлюзом не принято (сверка со счётчиком `EventsSent`).

Шлюз под нагрузкой терял уведомления OPC UA, и прогон закрывался `finished` без предупреждения (прогон 60 стенда —
3 600 событий). Число и окна симуляционного времени пишет шлюз (`app/gateway/audit.py`), их видно в `/runs` и в
потоке `run.updated`.

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-28
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("events_lost", sa.Integer, server_default="0", nullable=False))
    op.add_column("runs", sa.Column("loss_windows", JSONB, server_default=sa.text("'[]'::jsonb"), nullable=False))


def downgrade() -> None:
    op.drop_column("runs", "loss_windows")
    op.drop_column("runs", "events_lost")
