"""Источник исхода прогноза и досчёт исходов завершённого прогона по журналу.

`predictions.outcome_source`: `live` — исход закрыт по часам прогона, `history` — после конца прогона по базовой
истории (тот же журнал 2026 года, который проигрывал сценарий). У закрытых до миграции — `live`.
`runs.outcomes_until`: до какого момента данных исходы прогона досчитаны по журналу; метрики прогона считают
закрытыми прогнозы с концом горизонта до этого момента.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-26
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("predictions", sa.Column("outcome_source", sa.Text()))
    op.create_check_constraint(
        "predictions_outcome_source_check",
        "predictions",
        "outcome_source IS NULL OR outcome_source IN ('live', 'history')",
    )
    op.execute("UPDATE predictions SET outcome_source = 'live' WHERE outcome IS NOT NULL")
    op.add_column("runs", sa.Column("outcomes_until", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("runs", "outcomes_until")
    op.drop_constraint("predictions_outcome_source_check", "predictions")
    op.drop_column("predictions", "outcome_source")
