"""Внешние наборы данных (этап 14, docs/architecture/external-data.md): справочник, прогон-основа, разметка, допуски.

- `objects.source`, `channels.source` — набор (у данных хакатона — `hackathon`); `objects.meta` — свойства объекта
  набора (возраст и модель агрегата Azure PdM), `channels.unit` — единицы показаний.
- `channels.stream` и `channels.deadband` — маршрут приёма события задаёт канал, а не список типов в коде (D31):
  у хакатона газ — `raw` с зоной 0.02, движение — `raw`, остальное — `significant`.
- `runs.kind` — `scenario` или `dataset`: история внешнего набора — служебный прогон-основа (D28), а не строки
  с `run_id IS NULL`, которые у хакатона читают целиком (загрузчик, окно сырого потока, «сейчас» базы).
  Сценарный прогон на внешних данных ссылается на основу `runs.base_run_id`. Представления `run_*(p)` берут базовую
  часть либо из хакатона (`run_id IS NULL`, как раньше), либо из основы — `run_base(p)`; у самого прогона-основы
  базовой части нет. Ветки выключаются однократным фильтром, план запросов хакатона не меняется.
- `external_labels` — разметка набора для исходов внешних моделей; `external_credentials`, `external_permits` —
  пропуска и допуски (кому куда можно).

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-27
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)

# Базовая часть представления: у прогонов хакатона — строки базы, у сценарных прогонов внешнего набора — строки его
# прогона-основы; условие по времени (что было до старта) — своё у каждой таблицы
_VIEWS = {
    "events": "ts < run_sim_start(p_run)",
    "events_raw": "ts < run_sim_start(p_run)",
    "channel_hourly": "hour < date_trunc('hour', run_sim_start(p_run))",
    "channel_daily": "(p_run IS NULL OR date < (run_sim_start(p_run) AT TIME ZONE 'Europe/Moscow')::date)",
    "fault_episodes": "(p_run IS NULL OR end_ts < run_sim_start(p_run))",
    "alarm_episodes": "(p_run IS NULL OR end_ts < run_sim_start(p_run))",
    "alarms": "ts < run_sim_start(p_run)",
}
# Как было до миграции — для отката: только база хакатона
_OLD = {table: f"run_id IS NULL AND {cond}" for table, cond in _VIEWS.items()}


def _view(table: str, body: str) -> str:
    return f"""
    CREATE OR REPLACE FUNCTION run_{table}(p_run integer) RETURNS SETOF {table} LANGUAGE sql STABLE AS $$
      {body}
    $$
    """


def upgrade() -> None:
    op.add_column("objects", sa.Column("source", sa.Text(), server_default="hackathon", nullable=False))
    op.add_column("objects", sa.Column("meta", JSONB()))
    op.create_index("objects_source_idx", "objects", ["source"])
    op.add_column("channels", sa.Column("source", sa.Text(), server_default="hackathon", nullable=False))
    op.add_column("channels", sa.Column("stream", sa.Text(), server_default="significant", nullable=False))
    op.add_column("channels", sa.Column("deadband", sa.Float()))
    op.add_column("channels", sa.Column("unit", sa.Text()))
    op.create_check_constraint("channels_stream_check", "channels", "stream IN ('significant', 'raw')")
    op.create_index("channels_source_idx", "channels", ["source"])
    op.execute("UPDATE channels SET stream = 'raw' WHERE sensor_type IN ('Газовый датчик', 'Датчик движения')")
    op.execute("UPDATE channels SET deadband = 0.02 WHERE sensor_type = 'Газовый датчик'")

    op.add_column("runs", sa.Column("kind", sa.Text(), server_default="scenario", nullable=False))
    op.add_column("runs", sa.Column("base_run_id", sa.Integer(), sa.ForeignKey("runs.id")))
    op.create_check_constraint("runs_kind_check", "runs", "kind IN ('scenario', 'dataset')")
    op.execute(
        """
        CREATE FUNCTION run_base(p_run integer) RETURNS integer LANGUAGE sql STABLE AS $$
          SELECT CASE WHEN kind = 'dataset' THEN -1 ELSE base_run_id END FROM runs WHERE id = p_run
        $$
        """
    )
    for table, before_start in _VIEWS.items():
        op.execute(
            _view(
                table,
                f"""SELECT * FROM {table} WHERE run_id IS NULL AND run_base(p_run) IS NULL AND {before_start}
      UNION ALL
      SELECT * FROM {table} WHERE run_base(p_run) IS NOT NULL AND run_id = run_base(p_run) AND {before_start}
      UNION ALL
      SELECT * FROM {table} WHERE run_id = p_run""",
            )
        )

    op.create_table(
        "external_labels",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("dataset", sa.Text(), nullable=False),
        sa.Column("object_id", sa.Integer(), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("channel_id", sa.Integer(), sa.ForeignKey("channels.id")),
        sa.Column("start_ts", TS, nullable=False),
        sa.Column("end_ts", TS),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("meta", JSONB()),
    )
    op.create_index("external_labels_object_idx", "external_labels", ["dataset", "object_id", "start_ts"])
    op.create_index(
        "external_labels_channel_idx",
        "external_labels",
        ["channel_id", "start_ts"],
        postgresql_where=sa.text("channel_id IS NOT NULL"),
    )
    op.create_table(
        "external_credentials",
        sa.Column("dataset", sa.Text(), primary_key=True),
        sa.Column("credential_id", sa.Text(), primary_key=True),
        sa.Column("kind", sa.Text()),
        sa.Column("meta", JSONB()),
    )
    op.create_table(
        "external_permits",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("dataset", sa.Text(), nullable=False),
        sa.Column("credential_id", sa.Text(), nullable=False),
        sa.Column("object_id", sa.Integer(), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("valid_from", TS),
        sa.Column("valid_to", TS),
    )
    op.create_index("external_permits_object_idx", "external_permits", ["dataset", "object_id"])
    op.create_index("external_permits_credential_idx", "external_permits", ["dataset", "credential_id"])


def downgrade() -> None:
    op.drop_table("external_permits")
    op.drop_table("external_credentials")
    op.drop_table("external_labels")
    for table, base in _OLD.items():
        body = f"SELECT * FROM {table} WHERE {base}\n      UNION ALL\n      SELECT * FROM {table} WHERE run_id = p_run"
        op.execute(_view(table, body))
    op.execute("DROP FUNCTION run_base(integer)")
    op.drop_constraint("runs_kind_check", "runs")
    op.drop_column("runs", "base_run_id")
    op.drop_column("runs", "kind")
    op.drop_index("channels_source_idx", "channels")
    op.drop_constraint("channels_stream_check", "channels")
    for column in ("unit", "deadband", "stream", "source"):
        op.drop_column("channels", column)
    op.drop_index("objects_source_idx", "objects")
    op.drop_column("objects", "meta")
    op.drop_column("objects", "source")
