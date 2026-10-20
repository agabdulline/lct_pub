"""Модели рабочей базы. Описание таблиц и слоёв — docs/architecture/data.md.

`run_id IS NULL` — базовая история (журнал до 30.06.2026), иначе — данные прогона эмулятора (D21).
Уникальные индексы с `run_id` объявлены `NULLS NOT DISTINCT`, чтобы базовая история дедуплицировалась так же,
как прогоны. `events`, `events_raw` и `channel_hourly` — гипертаблицы TimescaleDB: первичного ключа в БД у них
нет, ключ ORM задан через `__mapper_args__`.
"""

import datetime as dt

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

TS = DateTime(timezone=True)

RUN_STATUSES = ("running", "paused", "finished", "stopped")
RUN_KINDS = ("scenario", "dataset")
CHANNEL_STREAMS = ("significant", "raw")
HACKATHON = "hackathon"  # source данных хакатона
ALARM_EPISODE_KINDS = ("ppr", "group", "solitary")
VERDICTS = ("pending", "ppr", "sensor_fault", "reaction", "no_score")
PREDICTION_STATUSES = ("scored", "skipped")
OUTCOMES = ("fault", "no_fault")
OUTCOME_SOURCES = ("live", "history")
EQUIPMENT_KINDS = ("power_outage", "equipment_fault")
WORK_ORDER_STATUSES = ("new", "in_progress", "done", "rejected")
WORK_ORDER_OPEN_STATUSES = ("new", "in_progress")
PRIORITIES = ("high", "medium", "low")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _run_fk() -> ForeignKey:
    return ForeignKey("runs.id")


class Run(Base):
    """Прогон эмулятора: запуск сценария. Данные прогона помечаются его id и не удаляются при новом запуске."""

    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint(_in("status", RUN_STATUSES), name="runs_status_check"),
        CheckConstraint(_in("kind", RUN_KINDS), name="runs_kind_check"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scenario: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="running")
    sim_start: Mapped[dt.datetime] = mapped_column(TS)  # граница: база до неё + данные прогона
    sim_end: Mapped[dt.datetime | None] = mapped_column(TS)  # конец периода сценария
    sim_now: Mapped[dt.datetime | None] = mapped_column(TS)  # последнее симуляционное время прогона
    speed: Mapped[float] = mapped_column(Float, server_default="1")
    model_version: Mapped[str | None] = mapped_column(Text)
    params: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))  # объекты, include_gas и т. п.
    started_at: Mapped[dt.datetime] = mapped_column(TS, server_default=text("now()"))
    finished_at: Mapped[dt.datetime | None] = mapped_column(TS)
    # исходы досчитаны по журналу после конца прогона до этого момента данных (app/domain/outcomes.py)
    outcomes_until: Mapped[dt.datetime | None] = mapped_column(TS)
    # scenario — прогон сценария; dataset — служебный прогон-основа с историей внешнего набора (D28)
    kind: Mapped[str] = mapped_column(Text, server_default="scenario")
    base_run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id"))  # основа сценария на внешнем наборе
    # теневой скоринг (15.2): основная версия событийной модели — вердикты и заявки, теневые — оценки рядом
    # водяной знак принятых событий прогона (максимум ts): правила «по будущему» решаются по нему (access.md §4)
    events_until: Mapped[dt.datetime | None] = mapped_column(TS)
    # принятых событий прогона: events_raw + events значимых каналов — ведёт приём и загрузчик набора; считать по
    # таблицам — секунды: run_id не сегмент сжатия, события прогона лежат в сжатых чанках базы
    events: Mapped[int] = mapped_column(BigInteger, server_default="0")
    # события, которые источник отправил, а шлюз не принял (сверка со счётчиком `EventsSent`, gateway/audit.py),
    # и окна симуляционного времени, где они пропали: [{start_ts, end_ts, events}]
    events_lost: Mapped[int] = mapped_column(Integer, server_default="0")
    loss_windows: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    primary_model: Mapped[str | None] = mapped_column(Text)
    shadow_models: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'::text[]"))


class Object(Base):
    """Иерархия справочника объектов: 1 — район, 2 — объект, 3 — подобъект."""

    __tablename__ = "objects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("objects.id"), index=True)
    level: Mapped[int] = mapped_column(SmallInteger)
    kind: Mapped[str] = mapped_column(Text)  # district / controlHouse / guardObject
    name: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text, server_default=HACKATHON)  # набор данных (этап 14)
    meta: Mapped[dict | None] = mapped_column(JSONB)  # свойства объекта внешнего набора: возраст, модель агрегата


class Channel(Base):
    """Канал из справочника с производными полями (data.md §4). Привязан к подобъекту."""

    __tablename__ = "channels"
    __table_args__ = (CheckConstraint(_in("stream", CHANNEL_STREAMS), name="channels_stream_check"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    object_id: Mapped[int] = mapped_column(ForeignKey("objects.id"), index=True)
    system_type: Mapped[str] = mapped_column(Text)
    sensor_type: Mapped[str] = mapped_column(Text, index=True)
    tag: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    picket_number: Mapped[int | None] = mapped_column(Integer)
    picket_offset: Mapped[float | None] = mapped_column(Float)  # «+X» после номера пикета, единицы как в названии
    tag_group: Mapped[str | None] = mapped_column(Text)  # первые две части тега: «15-11.1»
    source: Mapped[str] = mapped_column(Text, server_default=HACKATHON)  # набор данных (этап 14)
    # маршрут приёма (D31): raw — в events_raw, в events — через зону нечувствительности deadband, если она задана
    stream: Mapped[str] = mapped_column(Text, server_default="significant")
    deadband: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(Text)  # единицы показаний внешнего набора


class _EventColumns:
    run_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("runs.id"))
    channel_id: Mapped[int] = mapped_column(Integer)
    event_id: Mapped[int | None] = mapped_column(BigInteger)  # ид_события журнала; по OPC UA не передаётся
    ts: Mapped[dt.datetime] = mapped_column(TS)
    is_alarm: Mapped[bool] = mapped_column(Boolean)
    value_raw: Mapped[str] = mapped_column(Text)  # значение как в источнике
    value_num: Mapped[float | None] = mapped_column(Float)  # число; коды ошибок температуры — NULL


class Event(_EventColumns, Base):
    """Значимые изменения за все годы; газ — через зону нечувствительности (D25). Гипертаблица, чанк — месяц."""

    __tablename__ = "events"
    __table_args__ = (
        Index(
            "events_uq",
            "channel_id",
            "ts",
            "run_id",
            "event_id",
            "value_raw",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
        Index("events_run_ts_idx", "run_id", "ts", postgresql_where=text("run_id IS NOT NULL")),
    )
    __mapper_args__ = {"primary_key": ["channel_id", "ts", "run_id", "event_id", "value_raw"]}


class EventRaw(_EventColumns, Base):
    """Сырой поток газа и движения, окно 90 дней от последнего события данных. Гипертаблица, чанк — 7 дней."""

    __tablename__ = "events_raw"
    __table_args__ = (
        Index(
            "events_raw_uq",
            "channel_id",
            "ts",
            "run_id",
            "event_id",
            "value_raw",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
        Index("events_raw_run_ts_idx", "run_id", "ts", postgresql_where=text("run_id IS NOT NULL")),
    )
    __mapper_args__ = {"primary_key": ["channel_id", "ts", "run_id", "event_id", "value_raw"]}


class ChannelState(Base):
    """Текущее состояние канала в базе или в прогоне — последнее событие полного потока."""

    __tablename__ = "channel_state"
    __table_args__ = (
        Index("channel_state_uq", "run_id", "channel_id", unique=True, postgresql_nulls_not_distinct=True),
    )
    __mapper_args__ = {"primary_key": ["run_id", "channel_id"]}

    run_id: Mapped[int | None] = mapped_column(_run_fk())
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"))
    ts: Mapped[dt.datetime] = mapped_column(TS)
    value_raw: Mapped[str] = mapped_column(Text)
    value_num: Mapped[float | None] = mapped_column(Float)
    is_alarm: Mapped[bool] = mapped_column(Boolean)
    last_alarm_at: Mapped[dt.datetime | None] = mapped_column(TS)
    fault_since: Mapped[dt.datetime | None] = mapped_column(TS)  # начало открытого эпизода неисправности


class _AggregateColumns:
    run_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("runs.id"))
    channel_id: Mapped[int] = mapped_column(Integer)
    n_events: Mapped[int] = mapped_column(Integer)
    n_alarm: Mapped[int] = mapped_column(Integer)
    n_fault: Mapped[int] = mapped_column(Integer)  # «Неисправен» и «Отключено устройство»
    n_num: Mapped[int] = mapped_column(Integer)  # числовых показаний — для min / max / mean
    min: Mapped[float | None] = mapped_column(Float)
    max: Mapped[float | None] = mapped_column(Float)
    mean: Mapped[float | None] = mapped_column(Float)


class ChannelHourly(_AggregateColumns, Base):
    """Почасовые итоги из полного сырого потока (до зоны нечувствительности), окно 90 дней. Гипертаблица."""

    __tablename__ = "channel_hourly"
    __table_args__ = (
        Index("channel_hourly_uq", "channel_id", "hour", "run_id", unique=True, postgresql_nulls_not_distinct=True),
    )
    __mapper_args__ = {"primary_key": ["channel_id", "hour", "run_id"]}

    hour: Mapped[dt.datetime] = mapped_column(TS)


class ChannelDaily(_AggregateColumns, Base):
    """Суточные итоги из полного сырого потока за все годы. Сутки — по Europe/Moscow."""

    __tablename__ = "channel_daily"
    __table_args__ = (
        Index("channel_daily_uq", "channel_id", "date", "run_id", unique=True, postgresql_nulls_not_distinct=True),
        Index("channel_daily_run_date_idx", "run_id", "date", postgresql_where=text("run_id IS NOT NULL")),
    )
    __mapper_args__ = {"primary_key": ["channel_id", "date", "run_id"]}

    date: Mapped[dt.date] = mapped_column(Date)


class FaultEpisode(Base):
    """Неисправное состояние канала: от первого «Неисправен» / «Отключено устройство» до первого иного значения."""

    __tablename__ = "fault_episodes"
    __table_args__ = (
        Index("fault_episodes_uq", "channel_id", "start_ts", "run_id", unique=True, postgresql_nulls_not_distinct=True),
        Index("fault_episodes_open_idx", "channel_id", postgresql_where=text("end_ts IS NULL")),
        Index("fault_episodes_run_idx", "run_id", "start_ts", postgresql_where=text("run_id IS NOT NULL")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(_run_fk())
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"))
    start_ts: Mapped[dt.datetime] = mapped_column(TS)
    end_ts: Mapped[dt.datetime | None] = mapped_column(TS)  # NULL — неисправность продолжается
    state: Mapped[str] = mapped_column(Text)  # значение, которым эпизод начался


class AlarmEpisode(Base):
    """Эпизод дымовых тревог на объекте (уровень 2): плановая проверка, группа или одиночная (data.md §5)."""

    __tablename__ = "alarm_episodes"
    __table_args__ = (
        CheckConstraint(_in("kind", ALARM_EPISODE_KINDS), name="alarm_episodes_kind_check"),
        Index("alarm_episodes_uq", "object_id", "start_ts", "run_id", unique=True, postgresql_nulls_not_distinct=True),
        Index("alarm_episodes_start_idx", "start_ts"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(_run_fk())
    object_id: Mapped[int] = mapped_column(ForeignKey("objects.id"))
    start_ts: Mapped[dt.datetime] = mapped_column(TS)
    end_ts: Mapped[dt.datetime] = mapped_column(TS)  # последняя тревога эпизода
    n_channels: Mapped[int] = mapped_column(Integer)
    n_alarms: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(Text)
    has_lever: Mapped[bool] = mapped_column(Boolean, server_default="false")  # был «Рычаг сдернут» на объекте
    channel_ids: Mapped[list[int]] = mapped_column(ARRAY(Integer), server_default=text("'{}'"))


class EquipmentEpisode(Base):
    """Эпизод оборудования: на объекте (уровень 2) за окно ушло в тревожное состояние не меньше порога не дымовых
    каналов (backend.md §6.2). Пока порог не набран — кандидат (`qualified = false`), в ленту не идёт."""

    __tablename__ = "equipment_episodes"
    __table_args__ = (
        CheckConstraint(_in("kind", EQUIPMENT_KINDS), name="equipment_episodes_kind_check"),
        Index("equipment_episodes_feed_idx", "run_id", "start_ts", postgresql_where=text("qualified")),
        Index("equipment_episodes_open_idx", "run_id", "object_id", postgresql_where=text("end_ts IS NULL")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(_run_fk())
    object_id: Mapped[int] = mapped_column(ForeignKey("objects.id"))
    kind: Mapped[str] = mapped_column(Text)
    value: Mapped[str] = mapped_column(Text)  # преобладающее состояние каналов
    qualified: Mapped[bool] = mapped_column(Boolean, server_default="false")
    start_ts: Mapped[dt.datetime] = mapped_column(TS)  # первое начало эпизода
    last_onset_ts: Mapped[dt.datetime] = mapped_column(TS)
    end_ts: Mapped[dt.datetime | None] = mapped_column(TS)  # в норму вернулась доля каналов end_share
    n_channels: Mapped[int] = mapped_column(Integer, server_default="0")
    n_returned: Mapped[int] = mapped_column(Integer, server_default="0")
    updated_at: Mapped[dt.datetime] = mapped_column(TS, server_default=text("now()"))


class EquipmentEpisodeChannel(Base):
    """Канал эпизода оборудования: когда и в каком состоянии вошёл, когда вернулся в норму."""

    __tablename__ = "equipment_episode_channels"
    __table_args__ = (Index("equipment_episode_channels_channel_idx", "channel_id"),)

    episode_id: Mapped[int] = mapped_column(ForeignKey("equipment_episodes.id", ondelete="CASCADE"), primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), primary_key=True)
    onset_ts: Mapped[dt.datetime] = mapped_column(TS)
    value: Mapped[str] = mapped_column(Text)
    returned_ts: Mapped[dt.datetime | None] = mapped_column(TS)


class Prediction(Base):
    """Журнал прогнозов: решения моделей, пропуски с причиной и фактические исходы."""

    __tablename__ = "predictions"
    __table_args__ = (
        CheckConstraint(_in("status", PREDICTION_STATUSES), name="predictions_status_check"),
        CheckConstraint(f"outcome IS NULL OR {_in('outcome', OUTCOMES)}", name="predictions_outcome_check"),
        CheckConstraint(
            f"outcome_source IS NULL OR {_in('outcome_source', OUTCOME_SOURCES)}",
            name="predictions_outcome_source_check",
        ),
        Index(
            "predictions_uq",
            "run_id",
            "model",
            "model_version",
            "channel_id",
            "decision_time",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
        Index("predictions_alarm_idx", "alarm_id", "model_version", postgresql_where=text("alarm_id IS NOT NULL")),
        Index("predictions_channel_idx", "channel_id", "decision_time"),
        Index("predictions_decision_idx", "decision_time"),
        Index("predictions_open_idx", "horizon_end", postgresql_where=text("outcome IS NULL AND status = 'scored'")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(_run_fk())
    model: Mapped[str] = mapped_column(Text)  # smoke_alarm / daily
    model_version: Mapped[str] = mapped_column(Text)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"))
    alarm_id: Mapped[int | None] = mapped_column(ForeignKey("alarms.id", use_alter=True, name="predictions_alarm_fk"))
    decision_time: Mapped[dt.datetime] = mapped_column(TS)
    horizon_start: Mapped[dt.datetime] = mapped_column(TS)
    horizon_end: Mapped[dt.datetime] = mapped_column(TS)
    status: Mapped[str] = mapped_column(Text)
    skip_reason: Mapped[str | None] = mapped_column(Text)
    score: Mapped[float | None] = mapped_column(Float)
    threshold: Mapped[float | None] = mapped_column(Float)
    alert: Mapped[bool | None] = mapped_column(Boolean)
    seen_channel: Mapped[bool | None] = mapped_column(Boolean)
    # у объекта и шлейфа канала есть исходы прошлых решений; false — слепая зона модели, null — пропуск
    object_history: Mapped[bool | None] = mapped_column(Boolean)
    context: Mapped[dict | None] = mapped_column(JSONB)
    factors: Mapped[list | None] = mapped_column(JSONB)
    outcome: Mapped[str | None] = mapped_column(Text)  # fault / no_fault, NULL — горизонт не истёк
    outcome_at: Mapped[dt.datetime | None] = mapped_column(TS)  # начало неисправности или конец горизонта
    # live — по часам прогона, history — после конца прогона по базовой истории журнала
    outcome_source: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(TS, server_default=text("now()"))


class Alarm(Base):
    """Дымовая тревога в ленте и её вердикт (триаж, допущение A6)."""

    __tablename__ = "alarms"
    __table_args__ = (
        CheckConstraint(_in("verdict", VERDICTS), name="alarms_verdict_check"),
        Index("alarms_uq", "channel_id", "ts", "run_id", unique=True, postgresql_nulls_not_distinct=True),
        Index("alarms_ts_idx", "ts"),
        Index("alarms_object_ts_idx", "object_id", "ts"),
        Index("alarms_episode_idx", "episode_id"),
        Index("alarms_run_idx", "run_id", postgresql_where=text("run_id IS NOT NULL")),  # счётчики /runs
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(_run_fk())
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"))
    object_id: Mapped[int] = mapped_column(ForeignKey("objects.id"))  # объект уровня 2 — для эпизодов и фильтров
    ts: Mapped[dt.datetime] = mapped_column(TS)
    episode_id: Mapped[int | None] = mapped_column(ForeignKey("alarm_episodes.id"))
    verdict: Mapped[str] = mapped_column(Text, server_default="pending")
    verdict_refined: Mapped[bool] = mapped_column(Boolean, server_default="false")  # «уточнён» после распознания ППР
    prediction_id: Mapped[int | None] = mapped_column(ForeignKey("predictions.id"))  # решение, давшее вердикт
    created_at: Mapped[dt.datetime] = mapped_column(TS, server_default=text("now()"))  # приём в систему, реальное время


class WorkOrder(Base):
    """Заявка на ТО: по правилу или вручную. Одна открытая заявка на канал и правило."""

    __tablename__ = "work_orders"
    __table_args__ = (
        CheckConstraint(_in("status", WORK_ORDER_STATUSES), name="work_orders_status_check"),
        CheckConstraint(_in("priority", PRIORITIES), name="work_orders_priority_check"),
        Index(
            "work_orders_open_uq",
            "run_id",
            "channel_id",
            "rule",
            unique=True,
            postgresql_nulls_not_distinct=True,
            postgresql_where=text(f"{_in('status', WORK_ORDER_OPEN_STATUSES)} AND rule <> 'manual'"),
        ),
        Index("work_orders_run_created_idx", "run_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(_run_fk())
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"))
    prediction_id: Mapped[int | None] = mapped_column(ForeignKey("predictions.id"))
    rule: Mapped[str] = mapped_column(Text)  # alarm_model_alert / daily_model_alert / solitary_repeat / ... / manual
    priority: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="new")
    title: Mapped[str] = mapped_column(Text)
    due_date: Mapped[dt.date | None] = mapped_column(Date)
    comment: Mapped[str | None] = mapped_column(Text)
    assignee: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(TS)  # по системным часам: в прогоне — симуляционное время
    updated_at: Mapped[dt.datetime] = mapped_column(TS)
    closed_at: Mapped[dt.datetime | None] = mapped_column(TS)


class WorkOrderEvent(Base):
    """История заявки: создание и каждая смена статуса, комментария или исполнителя."""

    __tablename__ = "work_order_events"
    __table_args__ = (Index("work_order_events_order_idx", "work_order_id", "ts"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    work_order_id: Mapped[int] = mapped_column(ForeignKey("work_orders.id", ondelete="CASCADE"))
    ts: Mapped[dt.datetime] = mapped_column(TS)  # по системным часам: в прогоне — симуляционное время
    status: Mapped[str] = mapped_column(Text)
    comment: Mapped[str | None] = mapped_column(Text)
    assignee: Mapped[str | None] = mapped_column(Text)


ACCESS_STATUSES = ("candidate", "open", "closed", "dismissed")
ACCESS_END_REASONS = ("disarmed", "quiet", "run_end")
ACCESS_RESOLUTIONS = ("works", "intrusion", "false_alarm")
ACCESS_VERDICTS = ("reaction", "own")  # модель доступа: нужна реакция / вероятно свой
ACCESS_DECIDERS = ("model", "rule")


class AccessIncident(Base):
    """Вскрытие под охраной (access.md §3–4): эпизод подобъекта от первого открытия под охраной. Кандидат — пока не
    прошло 5 мин без снятия охраны; «снято после открытия» — `dismissed`."""

    __tablename__ = "access_incidents"
    __table_args__ = (
        CheckConstraint(_in("status", ACCESS_STATUSES), name="access_incidents_status_check"),
        CheckConstraint(
            f"end_reason IS NULL OR {_in('end_reason', ACCESS_END_REASONS)}", name="access_incidents_end_check"
        ),
        CheckConstraint(
            f"resolution IS NULL OR {_in('resolution', ACCESS_RESOLUTIONS)}", name="access_incidents_resolution_check"
        ),
        CheckConstraint(f"verdict IS NULL OR {_in('verdict', ACCESS_VERDICTS)}", name="access_incidents_verdict_check"),
        CheckConstraint(
            f"decided_by IS NULL OR {_in('decided_by', ACCESS_DECIDERS)}", name="access_incidents_decided_check"
        ),
        Index(
            "access_incidents_uq",
            "run_id",
            "sub_object_id",
            "start_ts",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
        Index("access_incidents_feed_idx", "run_id", "start_ts"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(_run_fk())
    object_id: Mapped[int] = mapped_column(ForeignKey("objects.id"))  # объект, уровень 2
    sub_object_id: Mapped[int] = mapped_column(ForeignKey("objects.id"))
    guard_channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"))
    status: Mapped[str] = mapped_column(Text)
    start_ts: Mapped[dt.datetime] = mapped_column(TS)
    qualified_ts: Mapped[dt.datetime | None] = mapped_column(TS)  # start + 5 мин — по логике правила
    end_ts: Mapped[dt.datetime | None] = mapped_column(TS)
    end_reason: Mapped[str | None] = mapped_column(Text)
    armed_since: Mapped[dt.datetime | None] = mapped_column(TS)
    disarmed_ts: Mapped[dt.datetime | None] = mapped_column(TS)
    openings: Mapped[int] = mapped_column(Integer, server_default="0")
    hatch_openings: Mapped[int] = mapped_column(Integer, server_default="0")
    off_hours: Mapped[bool] = mapped_column(Boolean, server_default="false")
    motion: Mapped[bool] = mapped_column(Boolean, server_default="false")
    guard_faulty: Mapped[bool] = mapped_column(Boolean, server_default="false")
    repeated: Mapped[bool] = mapped_column(Boolean, server_default="false")
    priority: Mapped[str | None] = mapped_column(Text)
    work_order_id: Mapped[int | None] = mapped_column(ForeignKey("work_orders.id", ondelete="SET NULL"))
    resolution: Mapped[str | None] = mapped_column(Text)  # разбор диспетчером
    # модель доступа (lct-8f): «нужна реакция» / «вероятно свой» при первом открытии, оценка, кто решил, почему не она
    verdict: Mapped[str | None] = mapped_column(Text)
    score: Mapped[float | None] = mapped_column(Float)
    threshold: Mapped[float | None] = mapped_column(Float)
    model_version: Mapped[str | None] = mapped_column(Text)
    decided_by: Mapped[str | None] = mapped_column(Text)
    model_note: Mapped[str | None] = mapped_column(Text)
    comment: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[dt.datetime] = mapped_column(TS, server_default=text("now()"))


class AccessIncidentOpening(Base):
    """Открытие двери или люка в инциденте: канал, время, флаг тревоги, когда закрыли."""

    __tablename__ = "access_incident_openings"

    incident_id: Mapped[int] = mapped_column(ForeignKey("access_incidents.id", ondelete="CASCADE"), primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), primary_key=True)
    ts: Mapped[dt.datetime] = mapped_column(TS, primary_key=True)
    is_alarm: Mapped[bool] = mapped_column(Boolean, server_default="false")
    closed_ts: Mapped[dt.datetime | None] = mapped_column(TS)


class RunAudit(Base):
    """Журнал действий с прогоном (15.2): смена основной версии событийной модели, пересчёт прошлых вердиктов."""

    __tablename__ = "run_audit"
    __table_args__ = (Index("run_audit_run_idx", "run_id", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    ts: Mapped[dt.datetime] = mapped_column(TS, server_default=text("now()"))  # реальное время действия
    sim_ts: Mapped[dt.datetime | None] = mapped_column(TS)  # часы прогона в момент действия
    action: Mapped[str] = mapped_column(Text)  # primary_model, reverdict
    details: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))


class ModelVersion(Base):
    """Реестр загруженных моделей: порог, офлайн-метрики, sha256 артефакта."""

    __tablename__ = "model_versions"

    model: Mapped[str] = mapped_column(Text, primary_key=True)
    version: Mapped[str] = mapped_column(Text, primary_key=True)
    threshold: Mapped[float | None] = mapped_column(Float)
    metrics: Mapped[dict | None] = mapped_column(JSONB)
    artifact_sha256: Mapped[str | None] = mapped_column(Text)
    trained_on: Mapped[str | None] = mapped_column(Text)
    config: Mapped[dict | None] = mapped_column(JSONB)
    loaded_at: Mapped[dt.datetime] = mapped_column(TS, server_default=text("now()"))


class ExternalLabel(Base):
    """Разметка внешнего набора: пожар, отказ агрегата, проезд без допуска — исходы внешних моделей (этап 14)."""

    __tablename__ = "external_labels"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    dataset: Mapped[str] = mapped_column(Text)
    object_id: Mapped[int] = mapped_column(ForeignKey("objects.id"))
    channel_id: Mapped[int | None] = mapped_column(ForeignKey("channels.id"))
    start_ts: Mapped[dt.datetime] = mapped_column(TS)
    end_ts: Mapped[dt.datetime | None] = mapped_column(TS)
    label: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict | None] = mapped_column(JSONB)


class ExternalCredential(Base):
    """Пропуск внешнего набора: машина, карта сотрудника."""

    __tablename__ = "external_credentials"

    dataset: Mapped[str] = mapped_column(Text, primary_key=True)
    credential_id: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict | None] = mapped_column(JSONB)


class ExternalPermit(Base):
    """Допуск: пропуск может проходить на объект. У объекта есть допуски — пускают только перечисленных."""

    __tablename__ = "external_permits"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    dataset: Mapped[str] = mapped_column(Text)
    credential_id: Mapped[str] = mapped_column(Text)
    object_id: Mapped[int] = mapped_column(ForeignKey("objects.id"))
    valid_from: Mapped[dt.datetime | None] = mapped_column(TS)
    valid_to: Mapped[dt.datetime | None] = mapped_column(TS)
