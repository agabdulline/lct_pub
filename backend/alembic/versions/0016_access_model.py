"""Модель доступа «свой или нужна реакция» в инцидентах вскрытия под охраной (access.md §4, lct-8f).

Модель оценивает кандидата правила сразу при первом открытии: «нужна реакция» — инцидент и заявка сразу,
«вероятно свой» — кандидат в ленте без заявки; через 5 мин без снятия охраны инцидент заводит правило. Здесь — её
оценка и кто решил: `decided_by` — `model` или `rule`; `model_note` — почему модель не решала (сервис недоступен,
кандидат уже решён правилом).

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-20
"""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("access_incidents", sa.Column("verdict", sa.Text))
    op.add_column("access_incidents", sa.Column("score", sa.Float))
    op.add_column("access_incidents", sa.Column("threshold", sa.Float))
    op.add_column("access_incidents", sa.Column("model_version", sa.Text))
    op.add_column("access_incidents", sa.Column("decided_by", sa.Text))
    op.add_column("access_incidents", sa.Column("model_note", sa.Text))
    op.create_check_constraint(
        "access_incidents_verdict_check", "access_incidents", "verdict IS NULL OR verdict IN ('reaction', 'own')"
    )
    op.create_check_constraint(
        "access_incidents_decided_check", "access_incidents", "decided_by IS NULL OR decided_by IN ('model', 'rule')"
    )


def downgrade() -> None:
    op.drop_constraint("access_incidents_decided_check", "access_incidents")
    op.drop_constraint("access_incidents_verdict_check", "access_incidents")
    for column in ("model_note", "decided_by", "model_version", "threshold", "score", "verdict"):
        op.drop_column("access_incidents", column)
