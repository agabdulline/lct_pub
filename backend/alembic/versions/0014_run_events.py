"""Счётчик событий прогона и индекс тревог по прогону — для `GET /runs` (docs/architecture/backend.md §2.1).

Подсчёт событий прогона по таблицам занимал 3.8–4 с на 58 прогонах: `run_id` не сегмент сжатия, события прогона лежат
в сжатых чанках базы и распаковываются целиком. Теперь число ведёт приём и загрузчик набора, здесь — один раз
досчитываем уже записанные прогоны той же формулой: `events_raw` + `events` значимых каналов.

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-28
"""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("events", sa.BigInteger, server_default="0", nullable=False))
    op.execute(
        """
        UPDATE runs r SET events = coalesce(er.n, 0) + coalesce(ev.n, 0)
        FROM runs r0
        LEFT JOIN (SELECT run_id, count(*) AS n FROM events_raw WHERE run_id IS NOT NULL GROUP BY run_id) er
            ON er.run_id = r0.id
        LEFT JOIN (
            SELECT e.run_id, count(*) AS n FROM events e JOIN channels c ON c.id = e.channel_id
            WHERE e.run_id IS NOT NULL AND c.stream = 'significant' GROUP BY e.run_id
        ) ev ON ev.run_id = r0.id
        WHERE r.id = r0.id
        """
    )
    op.create_index("alarms_run_idx", "alarms", ["run_id"], postgresql_where=sa.text("run_id IS NOT NULL"))


def downgrade() -> None:
    op.drop_index("alarms_run_idx", table_name="alarms")
    op.drop_column("runs", "events")
