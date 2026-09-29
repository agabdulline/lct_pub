"""Схема рабочей базы: справочники, прогоны, слои событий, итоги, эпизоды, тревоги, прогнозы, заявки.

Описание — docs/architecture/data.md §1–3. Гипертаблицы TimescaleDB: events (чанк — месяц), events_raw
и channel_hourly (чанк — 7 дней). Функции run_* — представление прогона: база до sim_start + данные прогона.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-26
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)
NOW = sa.text("now()")


def _run_id() -> sa.Column:
    return sa.Column("run_id", sa.Integer(), sa.ForeignKey("runs.id"), nullable=True)


def _event_table(name: str) -> None:
    op.create_table(
        name,
        _run_id(),
        sa.Column("channel_id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.BigInteger(), nullable=True),
        sa.Column("ts", TS, nullable=False),
        sa.Column("is_alarm", sa.Boolean(), nullable=False),
        sa.Column("value_raw", sa.Text(), nullable=False),
        sa.Column("value_num", sa.Float(), nullable=True),
    )
    op.create_index(
        f"{name}_uq",
        name,
        ["channel_id", "ts", "run_id", "event_id", "value_raw"],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )
    op.create_index(f"{name}_run_ts_idx", name, ["run_id", "ts"], postgresql_where=sa.text("run_id IS NOT NULL"))


def _aggregate_table(name: str, period: str, period_type: sa.types.TypeEngine) -> None:
    op.create_table(
        name,
        _run_id(),
        sa.Column("channel_id", sa.Integer(), nullable=False),
        sa.Column(period, period_type, nullable=False),
        sa.Column("n_events", sa.Integer(), nullable=False),
        sa.Column("n_alarm", sa.Integer(), nullable=False),
        sa.Column("n_fault", sa.Integer(), nullable=False),
        sa.Column("n_num", sa.Integer(), nullable=False),
        sa.Column("min", sa.Float(), nullable=True),
        sa.Column("max", sa.Float(), nullable=True),
        sa.Column("mean", sa.Float(), nullable=True),
    )
    op.create_index(
        f"{name}_uq", name, ["channel_id", period, "run_id"], unique=True, postgresql_nulls_not_distinct=True
    )


def _hypertable(name: str, column: str, interval: str, orderby: str) -> None:
    # Индекс по времени по умолчанию не нужен: запросы идут по каналу (уникальный индекс) или по прогону
    op.execute(
        f"SELECT create_hypertable('{name}', by_range('{column}', INTERVAL '{interval}'), "
        "create_default_indexes => false)"
    )
    op.execute(
        f"ALTER TABLE {name} SET (timescaledb.compress, timescaledb.compress_segmentby = 'channel_id', "
        f"timescaledb.compress_orderby = '{orderby}')"
    )


# Представление прогона (docs/architecture/data.md §3). p_run IS NULL — только базовая история.
# Функции простые SQL и STABLE — планировщик их встраивает, фильтры по каналу и времени доходят до индексов.
RUN_VIEW_FUNCTIONS = """
CREATE FUNCTION run_sim_start(p_run integer) RETURNS timestamptz
LANGUAGE sql STABLE AS $$
  SELECT coalesce((SELECT sim_start FROM runs WHERE id = p_run), 'infinity'::timestamptz)
$$;

CREATE FUNCTION run_events(p_run integer) RETURNS SETOF events
LANGUAGE sql STABLE AS $$
  SELECT * FROM events WHERE run_id IS NULL AND ts < run_sim_start(p_run)
  UNION ALL
  SELECT * FROM events WHERE run_id = p_run
$$;

CREATE FUNCTION run_events_raw(p_run integer) RETURNS SETOF events_raw
LANGUAGE sql STABLE AS $$
  SELECT * FROM events_raw WHERE run_id IS NULL AND ts < run_sim_start(p_run)
  UNION ALL
  SELECT * FROM events_raw WHERE run_id = p_run
$$;

CREATE FUNCTION run_channel_hourly(p_run integer) RETURNS SETOF channel_hourly
LANGUAGE sql STABLE AS $$
  SELECT * FROM channel_hourly WHERE run_id IS NULL AND hour < date_trunc('hour', run_sim_start(p_run))
  UNION ALL
  SELECT * FROM channel_hourly WHERE run_id = p_run
$$;

CREATE FUNCTION run_channel_daily(p_run integer) RETURNS SETOF channel_daily
LANGUAGE sql STABLE AS $$
  SELECT * FROM channel_daily
  WHERE run_id IS NULL AND (p_run IS NULL OR date < (run_sim_start(p_run) AT TIME ZONE 'Europe/Moscow')::date)
  UNION ALL
  SELECT * FROM channel_daily WHERE run_id = p_run
$$;

CREATE FUNCTION run_fault_episodes(p_run integer) RETURNS SETOF fault_episodes
LANGUAGE sql STABLE AS $$
  SELECT * FROM fault_episodes WHERE run_id IS NULL AND (p_run IS NULL OR end_ts < run_sim_start(p_run))
  UNION ALL
  SELECT * FROM fault_episodes WHERE run_id = p_run
$$;

CREATE FUNCTION run_alarm_episodes(p_run integer) RETURNS SETOF alarm_episodes
LANGUAGE sql STABLE AS $$
  SELECT * FROM alarm_episodes WHERE run_id IS NULL AND (p_run IS NULL OR end_ts < run_sim_start(p_run))
  UNION ALL
  SELECT * FROM alarm_episodes WHERE run_id = p_run
$$;

CREATE FUNCTION run_alarms(p_run integer) RETURNS SETOF alarms
LANGUAGE sql STABLE AS $$
  SELECT * FROM alarms WHERE run_id IS NULL AND ts < run_sim_start(p_run)
  UNION ALL
  SELECT * FROM alarms WHERE run_id = p_run
$$;

CREATE FUNCTION run_channel_state(p_run integer) RETURNS SETOF channel_state
LANGUAGE sql STABLE AS $$
  SELECT * FROM channel_state WHERE run_id IS NULL AND p_run IS NULL
  UNION ALL
  SELECT * FROM channel_state WHERE run_id = p_run
$$;
"""

RUN_VIEW_NAMES = [
    "run_channel_state",
    "run_alarms",
    "run_alarm_episodes",
    "run_fault_episodes",
    "run_channel_daily",
    "run_channel_hourly",
    "run_events_raw",
    "run_events",
    "run_sim_start",
]


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("scenario", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="running", nullable=False),
        sa.Column("sim_start", TS, nullable=False),
        sa.Column("sim_end", TS, nullable=True),
        sa.Column("sim_now", TS, nullable=True),
        sa.Column("speed", sa.Float(), server_default="1", nullable=False),
        sa.Column("model_version", sa.Text(), nullable=True),
        sa.Column("params", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("started_at", TS, server_default=NOW, nullable=False),
        sa.Column("finished_at", TS, nullable=True),
        sa.CheckConstraint("status IN ('running', 'paused', 'finished', 'stopped')", name="runs_status_check"),
    )
    op.create_table(
        "objects",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("parent_id", sa.Integer(), sa.ForeignKey("objects.id"), nullable=True),
        sa.Column("level", sa.SmallInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
    )
    op.create_index("ix_objects_parent_id", "objects", ["parent_id"])
    op.create_table(
        "channels",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("object_id", sa.Integer(), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("system_type", sa.Text(), nullable=False),
        sa.Column("sensor_type", sa.Text(), nullable=False),
        sa.Column("tag", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("picket_number", sa.Integer(), nullable=True),
        sa.Column("picket_offset", sa.Float(), nullable=True),
        sa.Column("tag_group", sa.Text(), nullable=True),
    )
    op.create_index("ix_channels_object_id", "channels", ["object_id"])
    op.create_index("ix_channels_sensor_type", "channels", ["sensor_type"])
    op.create_table(
        "model_versions",
        sa.Column("model", sa.Text(), primary_key=True),
        sa.Column("version", sa.Text(), primary_key=True),
        sa.Column("threshold", sa.Float(), nullable=True),
        sa.Column("metrics", postgresql.JSONB(), nullable=True),
        sa.Column("artifact_sha256", sa.Text(), nullable=True),
        sa.Column("trained_on", sa.Text(), nullable=True),
        sa.Column("config", postgresql.JSONB(), nullable=True),
        sa.Column("loaded_at", TS, server_default=NOW, nullable=False),
    )

    _event_table("events")
    _hypertable("events", "ts", "1 month", "ts, run_id, event_id, value_raw")
    _event_table("events_raw")
    _hypertable("events_raw", "ts", "7 days", "ts, run_id, event_id, value_raw")

    _aggregate_table("channel_hourly", "hour", TS)
    _hypertable("channel_hourly", "hour", "7 days", "hour, run_id")
    _aggregate_table("channel_daily", "date", sa.Date())
    op.create_index(
        "channel_daily_run_date_idx",
        "channel_daily",
        ["run_id", "date"],
        postgresql_where=sa.text("run_id IS NOT NULL"),
    )

    op.create_table(
        "channel_state",
        _run_id(),
        sa.Column("channel_id", sa.Integer(), sa.ForeignKey("channels.id"), nullable=False),
        sa.Column("ts", TS, nullable=False),
        sa.Column("value_raw", sa.Text(), nullable=False),
        sa.Column("value_num", sa.Float(), nullable=True),
        sa.Column("is_alarm", sa.Boolean(), nullable=False),
        sa.Column("last_alarm_at", TS, nullable=True),
        sa.Column("fault_since", TS, nullable=True),
    )
    op.create_index(
        "channel_state_uq", "channel_state", ["run_id", "channel_id"], unique=True, postgresql_nulls_not_distinct=True
    )

    op.create_table(
        "fault_episodes",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        _run_id(),
        sa.Column("channel_id", sa.Integer(), sa.ForeignKey("channels.id"), nullable=False),
        sa.Column("start_ts", TS, nullable=False),
        sa.Column("end_ts", TS, nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
    )
    op.create_index(
        "fault_episodes_uq",
        "fault_episodes",
        ["channel_id", "start_ts", "run_id"],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )
    op.create_index(
        "fault_episodes_open_idx", "fault_episodes", ["channel_id"], postgresql_where=sa.text("end_ts IS NULL")
    )
    op.create_index(
        "fault_episodes_run_idx",
        "fault_episodes",
        ["run_id", "start_ts"],
        postgresql_where=sa.text("run_id IS NOT NULL"),
    )

    op.create_table(
        "alarm_episodes",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        _run_id(),
        sa.Column("object_id", sa.Integer(), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("start_ts", TS, nullable=False),
        sa.Column("end_ts", TS, nullable=False),
        sa.Column("n_channels", sa.Integer(), nullable=False),
        sa.Column("n_alarms", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("has_lever", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("channel_ids", postgresql.ARRAY(sa.Integer()), server_default=sa.text("'{}'"), nullable=False),
        sa.CheckConstraint("kind IN ('ppr', 'group', 'solitary')", name="alarm_episodes_kind_check"),
    )
    op.create_index(
        "alarm_episodes_uq",
        "alarm_episodes",
        ["object_id", "start_ts", "run_id"],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )
    op.create_index("alarm_episodes_start_idx", "alarm_episodes", ["start_ts"])

    op.create_table(
        "predictions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        _run_id(),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=False),
        sa.Column("channel_id", sa.Integer(), sa.ForeignKey("channels.id"), nullable=False),
        sa.Column("alarm_id", sa.BigInteger(), nullable=True),  # FK на alarms — ниже, после создания alarms
        sa.Column("decision_time", TS, nullable=False),
        sa.Column("horizon_start", TS, nullable=False),
        sa.Column("horizon_end", TS, nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("skip_reason", sa.Text(), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("threshold", sa.Float(), nullable=True),
        sa.Column("alert", sa.Boolean(), nullable=True),
        sa.Column("seen_channel", sa.Boolean(), nullable=True),
        sa.Column("context", postgresql.JSONB(), nullable=True),
        sa.Column("factors", postgresql.JSONB(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("outcome_at", TS, nullable=True),
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.CheckConstraint("status IN ('scored', 'skipped')", name="predictions_status_check"),
        sa.CheckConstraint("outcome IS NULL OR outcome IN ('fault', 'no_fault')", name="predictions_outcome_check"),
    )
    op.create_index(
        "predictions_uq",
        "predictions",
        ["run_id", "model", "channel_id", "decision_time"],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )
    op.create_index("predictions_channel_idx", "predictions", ["channel_id", "decision_time"])
    op.create_index("predictions_decision_idx", "predictions", ["decision_time"])
    op.create_index(
        "predictions_open_idx",
        "predictions",
        ["horizon_end"],
        postgresql_where=sa.text("outcome IS NULL AND status = 'scored'"),
    )

    op.create_table(
        "alarms",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        _run_id(),
        sa.Column("channel_id", sa.Integer(), sa.ForeignKey("channels.id"), nullable=False),
        sa.Column("object_id", sa.Integer(), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("ts", TS, nullable=False),
        sa.Column("episode_id", sa.BigInteger(), sa.ForeignKey("alarm_episodes.id"), nullable=True),
        sa.Column("verdict", sa.Text(), server_default="pending", nullable=False),
        sa.Column("verdict_refined", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("prediction_id", sa.BigInteger(), sa.ForeignKey("predictions.id"), nullable=True),
        sa.CheckConstraint(
            "verdict IN ('pending', 'ppr', 'sensor_fault', 'reaction', 'no_score')", name="alarms_verdict_check"
        ),
    )
    op.create_index(
        "alarms_uq", "alarms", ["channel_id", "ts", "run_id"], unique=True, postgresql_nulls_not_distinct=True
    )
    op.create_index("alarms_ts_idx", "alarms", ["ts"])
    op.create_index("alarms_object_ts_idx", "alarms", ["object_id", "ts"])
    op.create_index("alarms_episode_idx", "alarms", ["episode_id"])
    op.create_foreign_key("predictions_alarm_fk", "predictions", "alarms", ["alarm_id"], ["id"])

    op.create_table(
        "work_orders",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        _run_id(),
        sa.Column("channel_id", sa.Integer(), sa.ForeignKey("channels.id"), nullable=False),
        sa.Column("prediction_id", sa.BigInteger(), sa.ForeignKey("predictions.id"), nullable=True),
        sa.Column("rule", sa.Text(), nullable=False),
        sa.Column("priority", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="new", nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("assignee", sa.Text(), nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("closed_at", TS, nullable=True),
        sa.CheckConstraint("status IN ('new', 'in_progress', 'done', 'rejected')", name="work_orders_status_check"),
        sa.CheckConstraint("priority IN ('high', 'medium', 'low')", name="work_orders_priority_check"),
    )
    op.create_index(
        "work_orders_open_uq",
        "work_orders",
        ["run_id", "channel_id", "rule"],
        unique=True,
        postgresql_nulls_not_distinct=True,
        postgresql_where=sa.text("status IN ('new', 'in_progress') AND rule <> 'manual'"),
    )
    op.create_index("work_orders_run_created_idx", "work_orders", ["run_id", "created_at"])

    op.execute(RUN_VIEW_FUNCTIONS)


def downgrade() -> None:
    for name in RUN_VIEW_NAMES:
        op.execute(f"DROP FUNCTION IF EXISTS {name}(integer)")
    op.drop_constraint("predictions_alarm_fk", "predictions", type_="foreignkey")
    for table in [
        "work_orders",
        "alarms",
        "predictions",
        "alarm_episodes",
        "fault_episodes",
        "channel_state",
        "channel_daily",
        "channel_hourly",
        "events_raw",
        "events",
        "model_versions",
        "channels",
        "objects",
        "runs",
    ]:
        op.drop_table(table)
