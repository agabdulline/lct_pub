"""Эпизоды оборудования в ленте: массовый уход не дымовых каналов объекта в тревожное состояние (backend.md §6.2).

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-26
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "equipment_episodes",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("runs.id")),
        sa.Column("object_id", sa.Integer(), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("qualified", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("start_ts", TS, nullable=False),
        sa.Column("last_onset_ts", TS, nullable=False),
        sa.Column("end_ts", TS),
        sa.Column("n_channels", sa.Integer(), server_default="0", nullable=False),
        sa.Column("n_returned", sa.Integer(), server_default="0", nullable=False),
        sa.Column("updated_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("kind IN ('power_outage', 'equipment_fault')", name="equipment_episodes_kind_check"),
    )
    op.create_index(
        "equipment_episodes_feed_idx",
        "equipment_episodes",
        ["run_id", "start_ts"],
        postgresql_where=sa.text("qualified"),
    )
    op.create_index(
        "equipment_episodes_open_idx",
        "equipment_episodes",
        ["run_id", "object_id"],
        postgresql_where=sa.text("end_ts IS NULL"),
    )
    op.create_table(
        "equipment_episode_channels",
        sa.Column(
            "episode_id", sa.BigInteger(), sa.ForeignKey("equipment_episodes.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("channel_id", sa.Integer(), sa.ForeignKey("channels.id"), primary_key=True),
        sa.Column("onset_ts", TS, nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("returned_ts", TS),
    )
    op.create_index("equipment_episode_channels_channel_idx", "equipment_episode_channels", ["channel_id"])


def downgrade() -> None:
    op.drop_table("equipment_episode_channels")
    op.drop_table("equipment_episodes")
