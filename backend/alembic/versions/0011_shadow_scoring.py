"""Теневой скоринг (этап 15.2, docs/architecture/backend.md §2.1): несколько версий событийной модели на одной тревоге.

- `runs.primary_model` — основная версия событийной модели прогона: её решение даёт вердикт тревоги и заявку;
  `runs.shadow_models` — версии, которые оценивают те же тревоги рядом, без вердиктов и заявок. Пусто — как раньше:
  одна активная модель ML-сервиса.
- Уникальный ключ журнала прогнозов — с версией: у одной тревоги по прогнозу на каждую версию.
- `predictions (alarm_id, model_version)` — какие версии уже оценили тревогу (поиск неоценённых при скоринге).

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-27
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("primary_model", sa.Text()))
    op.add_column(
        "runs", sa.Column("shadow_models", ARRAY(sa.Text()), server_default=sa.text("'{}'::text[]"), nullable=False)
    )
    op.drop_index("predictions_uq", "predictions")
    op.create_index(
        "predictions_uq",
        "predictions",
        ["run_id", "model", "model_version", "channel_id", "decision_time"],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )
    op.create_index(
        "predictions_alarm_idx",
        "predictions",
        ["alarm_id", "model_version"],
        postgresql_where=sa.text("alarm_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("predictions_alarm_idx", "predictions")
    # у тревог с несколькими версиями остаётся одна — основная (у неё ссылка из alarms.prediction_id) или первая
    op.execute(
        """
        DELETE FROM predictions p USING predictions q
        WHERE p.run_id IS NOT DISTINCT FROM q.run_id AND p.model = q.model AND p.channel_id = q.channel_id
          AND p.decision_time = q.decision_time AND p.id > q.id
          AND NOT EXISTS (SELECT 1 FROM alarms a WHERE a.prediction_id = p.id)
        """
    )
    op.drop_index("predictions_uq", "predictions")
    op.create_index(
        "predictions_uq",
        "predictions",
        ["run_id", "model", "channel_id", "decision_time"],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )
    op.drop_column("runs", "shadow_models")
    op.drop_column("runs", "primary_model")
