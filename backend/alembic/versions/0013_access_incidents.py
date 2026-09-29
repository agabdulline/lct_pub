"""Вскрытие под охраной (этап 14.3, docs/architecture/access.md §4): инциденты подобъектов и их открытия.

- `runs.events_until` — водяной знак принятых событий прогона (максимум `ts`): решения правила, зависящие от
  будущего (волна ±2 с, снятие охраны в течение 5 мин), принимаются по нему, а не по часам эмулятора.
- `access_incidents` — кандидаты, инциденты и «снято после открытия» прогона (`run_id`) и базовой истории
  (`run_id = NULL`, `python -m app.cli access backfill`). Ключ — подобъект и начало.
- `access_incident_openings` — открытия дверей и люков инцидента.

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-27
"""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.add_column("runs", sa.Column("events_until", TS))
    op.create_table(
        "access_incidents",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("runs.id")),
        sa.Column("object_id", sa.Integer(), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("sub_object_id", sa.Integer(), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("guard_channel_id", sa.Integer(), sa.ForeignKey("channels.id"), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("start_ts", TS, nullable=False),
        sa.Column("qualified_ts", TS),
        sa.Column("end_ts", TS),
        sa.Column("end_reason", sa.Text()),
        sa.Column("armed_since", TS),
        sa.Column("disarmed_ts", TS),
        sa.Column("openings", sa.Integer(), server_default="0", nullable=False),
        sa.Column("hatch_openings", sa.Integer(), server_default="0", nullable=False),
        sa.Column("off_hours", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("motion", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("guard_faulty", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("repeated", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("priority", sa.Text()),
        sa.Column("work_order_id", sa.BigInteger(), sa.ForeignKey("work_orders.id", ondelete="SET NULL")),
        sa.Column("resolution", sa.Text()),
        sa.Column("comment", sa.Text()),
        sa.Column("updated_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('candidate', 'open', 'closed', 'dismissed')", name="access_incidents_status_check"
        ),
        sa.CheckConstraint(
            "end_reason IS NULL OR end_reason IN ('disarmed', 'quiet', 'run_end')", name="access_incidents_end_check"
        ),
        sa.CheckConstraint(
            "resolution IS NULL OR resolution IN ('works', 'intrusion', 'false_alarm')",
            name="access_incidents_resolution_check",
        ),
    )
    op.create_index(
        "access_incidents_uq",
        "access_incidents",
        ["run_id", "sub_object_id", "start_ts"],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )
    op.create_index("access_incidents_feed_idx", "access_incidents", ["run_id", "start_ts"])
    op.create_table(
        "access_incident_openings",
        sa.Column(
            "incident_id", sa.BigInteger(), sa.ForeignKey("access_incidents.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("channel_id", sa.Integer(), sa.ForeignKey("channels.id"), primary_key=True),
        sa.Column("ts", TS, primary_key=True),
        sa.Column("is_alarm", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("closed_ts", TS),
    )


def downgrade() -> None:
    op.drop_table("access_incident_openings")
    op.drop_table("access_incidents")
    op.drop_column("runs", "events_until")
