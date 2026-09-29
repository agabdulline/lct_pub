import datetime as dt
from typing import Literal

from pydantic import BaseModel


class LiveMetrics(BaseModel):
    precision: float | None
    recall: float | None
    decisions: int
    positives: int
    history: int  # из решений — с исходом по журналу после конца прогона
    alerts: int  # все тревоги модели (alert), без учёта конца горизонта
    alerts_confirmed: int  # из них с исходом fault
    open: int  # оценённые решения без исхода


class RunLive(BaseModel):
    smoke_alarm: LiveMetrics
    daily: LiveMetrics
    # внешние модели прогона набора (этап 14) по имени задачи: magdeburg_fire, vast_gate
    external: dict[str, LiveMetrics] = {}
    # версии событийной модели (15.2) на решениях, оценённых всеми версиями прогона; без теневых версий — {}
    versions: dict[str, LiveMetrics] = {}


class LossWindow(BaseModel):
    """Окно симуляционного времени, где шлюз не принял события, которые источник отправил."""

    start_ts: dt.datetime | None
    end_ts: dt.datetime | None
    events: int


class RunOut(BaseModel):
    id: int
    scenario: str
    scenario_title: str
    status: Literal["running", "paused", "finished", "stopped"]
    sim_start: dt.datetime
    sim_end: dt.datetime | None
    sim_now: dt.datetime | None
    speed: float
    model_version: str | None
    started_at: dt.datetime
    finished_at: dt.datetime | None
    events: int
    # отправлено источником, а не принято шлюзом (сверка со счётчиком эмулятора); окна — где пропали
    events_lost: int = 0
    loss_windows: list[LossWindow] = []
    alarms: int
    predictions: int
    work_orders: int
    live: RunLive | None
    kind: Literal["scenario", "dataset"]  # dataset — служебный прогон-основа с историей внешнего набора
    base_run_id: int | None  # основа сценария на внешнем наборе
    dataset: str | None  # ключ внешнего набора; null — данные хакатона
    # теневой скоринг (15.2): основная версия событийной модели — вердикты и заявки, теневые — оценки рядом;
    # null и [] — одна активная модель ML-сервиса
    primary_model: str | None
    shadow_models: list[str]


class RunPatch(BaseModel):
    primary_model: str  # версия событийной модели из /models: baseline-v0, v3 или связка v3>baseline-v0
    # пересчитать вердикты прошлых тревог прогона по новой основной версии (журнал — /runs/{id}/audit)
    apply_to_past: bool = False


class ReverdictSummary(BaseModel):
    version: str | None  # основная версия, по которой пересчитано
    alarms: int  # тревог прогона
    changed: int  # тревог с новым вердиктом или решением
    verdicts: dict[str, int] = {}  # переходы вердиктов: «reaction→sensor_fault»: n
    no_decision: int = 0  # тревог без решения этой версии — не изменены
    orders_created: int = 0  # заявок по тревогам этой версии, которых не было


class RunPatchOut(RunOut):
    reverdict: ReverdictSummary | None = None  # итог пересчёта при apply_to_past


class RunAuditItem(BaseModel):
    id: int
    ts: dt.datetime  # реальное время действия
    sim_ts: dt.datetime | None  # часы прогона
    action: Literal["primary_model", "reverdict"]
    details: dict


class RunAuditList(BaseModel):
    items: list[RunAuditItem]


class RunList(BaseModel):
    items: list[RunOut]
