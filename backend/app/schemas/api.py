"""Схемы ответов API — по docs/api.md v0.2 и web/src/lib/api/types.ts, имена полей совпадают. Из этих схем
генерируются типы интерфейса (`make api-types`)."""

import datetime as dt
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Status = Literal["ok", "risk", "fault", "alarm", "ppr", "unknown"]
# Закрытые наборы справочника хакатона (docs/data-format.md §5–7) — в OpenAPI перечислениями, как
# в web/src/lib/api/types.ts. У внешних наборов (этап 14) типы, подсистемы и виды объектов свои — поэтому `| str`
HackathonSensorType = Literal[
    "Датчик дыма",
    "Тепловой датчик",
    "Ручной извещатель",
    "Состояние УИР-Р",
    "КД Дверь",
    "Датчик движения",
    "КД АВ",
    "КД Люк",
    "Стекло",
    "9-секционный люк",
    "Состояние фазы",
    "Переключатель",
    "Состояние вентилятора",
    "Состояние насоса",
    "Состояние охраны",
    "Датчик затопления",
    "Датчик температуры",
    "Газовый датчик",
    "ИБП",
]
SensorType = HackathonSensorType | str
HackathonSubsystem = Literal[
    "Пожарная охрана",
    "Охранная подсистема",
    "Диспетчерский контроль",
    "Температурная подсистема",
    "Газовая охрана",
    "Диагностическая подсистема",
]
Subsystem = HackathonSubsystem | str
ObjectKind = Literal["district", "controlHouse", "guardObject"] | str
Verdict = Literal["ppr", "sensor_fault", "reaction", "no_score", "pending"]
AlarmKind = Literal["ppr", "group", "solitary"]
ObjectLevel = Literal["district", "object", "sub_object"]
# Модели хакатона; у внешних наборов — имя задачи (`magdeburg_fire`, этап 14)
ModelName = Literal["smoke_alarm", "daily"] | str
PredictionStatus = Literal["scored", "skipped", "pending"]
Outcome = Literal["fault", "no_fault"]
WorkOrderRule = Literal[
    "alarm_model_alert",
    "daily_model_alert",
    "solitary_repeat",
    "fault_persisting",
    "manual",
    "ext_fire_alert",
    "ext_access_violation",
    "ext_pdm_failure",
    "access_open_armed",
]
Priority = Literal["high", "medium", "low"]
WorkOrderStatus = Literal["new", "in_progress", "done", "rejected"]


class Page(BaseModel):
    total: int
    page: int
    size: int


# --- объекты и каналы -----------------------------------------------------------------------------------------


class Aggregates(BaseModel):
    channels: int
    active_alarms: int
    at_risk: int
    in_fault: int
    max_risk: float | None
    status: Status


class ObjectNode(Aggregates):
    id: int
    parent_id: int | None
    level: ObjectLevel
    kind: ObjectKind
    name: str
    source: str  # набор данных: hackathon или ключ внешнего набора (этап 14)
    children: list["ObjectNode"] = []


class SensorTypeSummary(BaseModel):
    sensor_type: SensorType
    channels: int
    at_risk: int
    in_fault: int
    max_risk: float | None


class PathItem(BaseModel):
    id: int
    name: str
    level: ObjectLevel


class ObjectSummary(Aggregates):
    id: int
    parent_id: int | None
    level: ObjectLevel
    kind: ObjectKind
    name: str
    source: str


class ObjectDetail(ObjectSummary):
    path: list[PathItem]
    sub_objects: list[ObjectSummary]
    by_sensor_type: list[SensorTypeSummary]


class Channel(BaseModel):
    id: int
    name: str
    tag: str
    tag_group: str | None
    subsystem: Subsystem
    sensor_type: SensorType
    object_id: int
    object_name: str
    sub_object_id: int
    sub_object_name: str
    picket_number: int | None
    picket_offset: float | None
    status: Status
    value: str | None
    value_ts: dt.datetime | None
    is_alarm: bool
    risk: float | None
    risk_threshold: float | None
    seen_channel: bool | None
    # из последнего оценённого решения событийной модели; null — решений нет или канал не дымовой
    object_history: bool | None
    source: str


class ChannelPage(Page):
    items: list[Channel]


class ChannelCounters(BaseModel):
    alarms_30d: int
    solitary_alarms_30d: int
    faults_365d: int
    fault_hours_30d: float
    last_fault_at: dt.datetime | None


class Factor(BaseModel):
    feature: str
    value: bool | float | str | None  # bool — признак-флаг внешней модели (`no_permit` у vast_gate)
    contribution: float | None  # null — у модели-правила вклада нет, фактор только объясняет решение


class ModelScore(BaseModel):
    """Оценка тревоги одной версией событийной модели (теневой скоринг, 15.2)."""

    model_config = ConfigDict(protected_namespaces=())

    model_version: str
    prediction_id: int
    status: PredictionStatus
    score: float | None
    threshold: float | None
    alert: bool | None
    primary: bool  # основная версия прогона: её решение даёт вердикт
    skip_reason: str | None = None  # у пропуска — причина (`coverage_gap`, `state_not_normal`, `ml_unavailable`, …)
    # пропуск «одно решение на канал в сутки», а решение версии по каналу за сутки до этого действует: прогноз,
    # статус, оценка, порог и тревога — из того решения, как у `Alarm.prediction`
    inherited: bool = False


class Prediction(BaseModel):
    id: int
    run_id: int | None
    model: ModelName
    model_version: str
    channel_id: int
    channel_name: str
    sensor_type: SensorType
    object_id: int
    object_name: str
    alarm_id: int | None
    decision_time: dt.datetime
    horizon_start: dt.datetime
    horizon_end: dt.datetime
    status: PredictionStatus
    skip_reason: str | None
    score: float | None
    threshold: float | None
    alert: bool | None
    seen_channel: bool | None
    object_history: bool | None  # у объекта и шлейфа есть исходы прошлых решений; false — слепая зона модели
    context: dict[str, Any] | None
    factors: list[Factor] | None
    outcome: Outcome | None
    outcome_at: dt.datetime | None
    # live — по часам прогона; history — досчитан по журналу после конца прогона
    outcome_source: Literal["live", "history"] | None
    paired: list[ModelScore] = []  # оценки той же тревоги другими версиями модели (15.2)
    # на прогноз ссылается вердикт его тревоги: решение (у связки — решившей версии) или пропуск, если не решила ни
    # одна; false — решала другая версия или решение унаследовано; null — у модели нет тревог (ежедневная, внешние)
    decided: bool | None = None


class PredictionPage(Page):
    items: list[Prediction]


class WorkOrder(BaseModel):
    id: int
    run_id: int | None
    channel_id: int
    channel_name: str
    sensor_type: SensorType
    object_id: int
    object_name: str
    # подобъект канала: одинаковые имена каналов на разных подобъектах объекта различимы («ДД ПК27» ×3)
    sub_object_id: int
    sub_object_name: str
    prediction_id: int | None
    rule: WorkOrderRule
    priority: Priority
    status: WorkOrderStatus
    title: str
    due_date: dt.date
    created_at: dt.datetime
    updated_at: dt.datetime
    closed_at: dt.datetime | None
    assignee: str | None
    comment: str | None


class Neighbor(BaseModel):
    id: int
    name: str
    sensor_type: SensorType
    status: Status
    picket_number: int | None


class DataGap(BaseModel):
    """Разрыв данных перед прогоном: от последнего события базы до старта прогона (A18)."""

    start_ts: dt.datetime
    end_ts: dt.datetime


class ChannelDetail(Channel):
    counters: ChannelCounters
    last_alarm_prediction: Prediction | None
    last_daily_prediction: Prediction | None
    open_work_orders: list[WorkOrder]
    neighbors: list[Neighbor]
    # начало открытой неисправности канала; null — канал не в неисправности
    fault_since: dt.datetime | None = None
    # разрыв данных перед прогоном, если он дольше `data_gap_hours` правила `fault_persisting`: неисправность, начатая
    # до него, длится «на начало журнала», а длительность для заявки считается с его конца
    data_gap: DataGap | None = None


class TimelineEvent(BaseModel):
    ts: dt.datetime
    value: str
    value_num: float | None
    is_alarm: bool
    status: Status


class StateEpisode(BaseModel):
    kind: Literal["fault", "alarm", "off"]
    start_ts: dt.datetime
    end_ts: dt.datetime | None


class DailyTotal(BaseModel):
    date: dt.date
    n_events: int
    n_alarm: int
    n_fault: int
    min: float | None
    max: float | None
    mean: float | None


class ChannelTimeline(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: dt.datetime = Field(alias="from")
    to: dt.datetime
    events: list[TimelineEvent]
    episodes: list[StateEpisode]
    daily: list[DailyTotal]


# --- граф ---------------------------------------------------------------------------------------------------------


class GraphNode(BaseModel):
    id: str
    kind: Literal["district", "object", "sub_object", "tag_group", "channel"]
    label: str
    parent: str | None
    subsystem: Subsystem | None
    sensor_type: SensorType | None
    status: Status
    risk: float | None
    active_alarms: int
    picket_number: int | None
    channels: int | None
    at_risk: int | None
    in_fault: int | None
    source: str


class GraphEdge(BaseModel):
    source: str
    target: str


class Graph(BaseModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]


# --- тревоги ------------------------------------------------------------------------------------------------------


class AlarmEpisodeRef(BaseModel):
    id: int
    object_id: int
    kind: AlarmKind
    start_ts: dt.datetime
    end_ts: dt.datetime | None
    n_channels: int


class AlarmPrediction(BaseModel):
    id: int
    status: PredictionStatus
    score: float | None
    threshold: float | None
    alert: bool | None
    skip_reason: str | None
    inherited: bool
    decision_time: dt.datetime | None
    object_history: bool | None
    # версия, чьё решение дало вердикт (у связки — v3 или baseline-v0); null — решения нет
    decided_by: str | None = None
    # связка: чем пропустила версия с более высоким приоритетом, если решала следующая («v3 пропустила: coverage_gap»)
    fallback_reason: str | None = None


class Alarm(BaseModel):
    id: int
    run_id: int | None
    ts: dt.datetime
    channel_id: int
    channel_name: str
    sensor_type: SensorType
    object_id: int
    object_name: str
    sub_object_id: int
    sub_object_name: str
    picket_number: int | None
    value: str
    episode: AlarmEpisodeRef | None
    verdict: Verdict
    verdict_refined: bool
    prediction: AlarmPrediction | None
    neighbors_alarmed_1h: int | None
    work_order_id: int | None
    scores: list[ModelScore] = []  # оценки всех версий событийной модели, основная — primary (15.2)
    disagree: bool | None = None  # версии разошлись в тревоге модели; null — сравнить нечего


class AlarmCounts(BaseModel):
    active: int
    ppr: int
    ppr_alert: int  # тревоги плановой проверки, по которым модель выше порога: датчик, вероятно, не пройдёт проверку
    sensor_fault: int
    reaction: int
    no_score: int
    disagree: int = 0  # версии модели разошлись (15.2)


class AlarmPage(Page):
    items: list[Alarm]
    counts: AlarmCounts


class AlarmNeighbor(BaseModel):
    channel_id: int
    channel_name: str
    picket_number: int | None
    ts: dt.datetime | None
    status: Status


class AlarmDetail(Alarm):
    neighbors: list[AlarmNeighbor]
    prediction_detail: Prediction | None


class AlarmEpisode(BaseModel):
    id: int
    run_id: int | None
    object_id: int
    object_name: str
    kind: AlarmKind
    start_ts: dt.datetime
    end_ts: dt.datetime | None
    n_channels: int
    n_alarms: int
    has_lever: bool
    channel_ids: list[int]


class Clock(BaseModel):
    mode: Literal["real", "stream"]
    now: dt.datetime


# --- метрики и модели ---------------------------------------------------------------------------------------------


class LeadTime(BaseModel):
    min_s: float
    p10_s: float
    p50_s: float
    p90_s: float
    max_s: float


class MetricsBlock(BaseModel):
    decisions: int
    positives: int
    alerts: int
    tp: int
    fp: int
    fn: int
    tn: int
    precision: float | None
    recall: float | None
    f1: float | None
    lead_time: LeadTime | None


class Metrics(BaseModel):
    model_config = ConfigDict(populate_by_name=True, protected_namespaces=())

    model: ModelName
    model_version: str | None
    from_: dt.datetime | None = Field(alias="from")
    to: dt.datetime | None
    open: int
    open_fault: int  # открытые, у которых неисправность уже началась: попадут в метрики после конца горизонта
    horizon_hours: list[float] | None  # горизонт модели от решения: [начало, конец], ч
    note: str | None  # пояснение для интерфейса, когда прогнозы ещё открыты
    history: int  # из закрытых решений — с исходом, досчитанным по журналу после конца прогона
    outcomes_until: dt.datetime | None  # исходы прогона досчитаны по журналу до этого момента данных
    all: MetricsBlock
    seen: MetricsBlock
    unseen: MetricsBlock
    # ежедневная (задача C): закрытые решения по моделям таблицы маршрутизации и по типам датчиков; у прочих — null
    by_version: dict[str, MetricsBlock] | None = None
    by_sensor_type: dict[str, MetricsBlock] | None = None


class OfflineMetrics(BaseModel):
    period: str
    precision: float
    recall: float
    f1: float
    ap: float | None
    tp: int
    fp: int
    decisions: int
    positives: int


class LeadTimeQuartiles(LeadTime):
    p25_s: float
    p75_s: float


class OfflineBlock(BaseModel):
    decisions: int
    positives: int
    alerts: int
    tp: int
    fp: int
    fn: int
    tn: int
    precision: float | None
    recall: float | None
    f1: float | None
    ap: float | None
    lead_time: LeadTimeQuartiles | None


class OfflineQuarter(OfflineBlock):
    quarter: str
    role: Literal["early_stop", "threshold", "test"]


class OfflineByType(OfflineBlock):
    sensor_type: SensorType
    base_rate: float


class OfflineByEpisodeKind(OfflineBlock):
    kind: AlarmKind


class OfflineRule(OfflineBlock):
    name: str


class OfflineByClass(OfflineBlock):
    model_config = ConfigDict(populate_by_name=True)

    class_: str = Field(alias="class")


class PrPoint(BaseModel):
    threshold: float
    precision: float
    recall: float


class OfflinePeriod(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: str = Field(alias="from")
    to: str
    label: str


class OfflineReport(BaseModel):
    """Офлайн-проверка базовой или внешней модели (ml/models/<модель>/offline.json, docs/api.md §2) — без
    преобразований. У внешних моделей нет `seen` / `unseen` и кварталов, зато есть `by_class` и `by_experiment`."""

    model_config = ConfigDict(protected_namespaces=())

    computed_at: dt.datetime
    model: ModelName
    version: str
    period: OfflinePeriod
    threshold: float
    test: OfflineBlock
    seen: OfflineBlock | None
    unseen: OfflineBlock | None
    seen_rule: str | None
    lead_time: LeadTimeQuartiles | None
    pr_curve: list[PrPoint] | None = None  # нет у моделей без оценки (правило, нормы — vast_gate)
    by_quarter: list[OfflineQuarter] | None
    by_type: list[OfflineByType] | None
    by_episode_kind: list[OfflineByEpisodeKind] | None = None  # только событийная: ППР / группа / одиночная
    rule: OfflineRule | None
    eval_scheme: str | None = None  # внешние модели: схема проверки (`leave-one-experiment-out`)
    by_class: list[OfflineByClass] | None = None
    fire_vs_nuisance: OfflineBlock | None = None  # magdeburg_fire: пожар против помехи на решениях этих двух классов
    by_experiment: list[dict[str, Any]] | None = None  # по опытам набора, поля — у набора (docs/api.md §2)


class ModelInfo(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    model: ModelName
    version: str
    # each_channel — отдельные модели типов (#243), chain — связка событийных версий
    family: Literal["v3", "baseline", "external", "each_channel", "chain"]
    title: str
    loaded: bool
    active: bool  # скорит задачу по умолчанию; у событийной — основная версия новых прогонов (15.2)
    scoring: bool = True  # готова оценивать: версию можно сделать основной или теневой
    threshold: float | None
    features: int
    trained_on: str
    horizon: str
    sensor_types: list[str]
    artifact_sha256: str | None
    offline_metrics: OfflineMetrics | None
    offline: OfflineReport | None = None  # полный офлайн-отчёт базовых моделей; у v3 — null
    # чья модель: команда (baseline), ML-разработчик (v3, each_channel); у связки — оба; у внешних — null
    author: str | None = None
    # ежедневные (задача C): типы датчиков, которые модель ведёт по таблице маршрутизации; [] — не ведёт ни одного
    routes: list[str] | None = None
    # внешние модели (этап 14): набор, синтетика ли он, ритм вызова, схема проверки; у моделей хакатона — null
    dataset: str | None = None
    synthetic: bool | None = None
    trigger: str | None = None
    eval_scheme: str | None = None
    # связка версий (`family: chain`): её версии по порядку — решает первая, у которой решение есть
    chain: list[str] | None = None
    # внешние модели: как закрывается исход (`label_in_horizon`…) и какие метки разметки — положительные
    outcome: str | None = None
    positive_labels: list[str] | None = None


class RoutingOffline(BaseModel):
    """Офлайн-итог маршрута или смеси ежедневных моделей на общих строках (`ml/reports/daily-routing`)."""

    decisions: int
    positives: int
    alerts: int
    tp: int
    fp: int
    precision: float | None
    recall: float | None
    lead_days: dict[str, float] | None = None  # упреждение TP, сут: p10, p50, p90
    episodes: int | None = None  # эпизодов «Неисправен» на строках
    caught_episodes: int | None = None  # из них с тревогой модели до начала


class DailyRoute(BaseModel):
    sensor_type: str
    model: str  # версия ежедневной модели, как `ModelInfo.version` и `predictions.model_version`
    why: str | None = None
    offline: RoutingOffline | None = None


class DailyUnrouted(BaseModel):
    sensor_type: str
    why: str | None = None


class DailyRouting(BaseModel):
    """Таблица маршрутизации ежедневного прогноза (задача C): какая модель ведёт какой тип датчика — ответ
    ml-service (`ml/models/daily_routing.json`)."""

    version: str
    sha256: str | None = None
    routes: list[DailyRoute]
    unrouted: list[DailyUnrouted] = []  # типы без ежедневной модели и почему
    rule: str | None = None  # правило выбора модели для типа
    report: str | None = None  # отчёт сравнения в репозитории
    test: OfflinePeriod | None = None  # период офлайн-сравнения
    offline: dict[str, RoutingOffline] | None = None  # routing — смесь по таблице, v0_only — одна v0 на тех же строках


class ModelsResponse(BaseModel):
    items: list[ModelInfo]
    daily_routing: DailyRouting | None = None  # null — ml-service маршрутизацию не отдаёт


class CompareFault(BaseModel):
    fault: int  # тревог только у версии, неисправность была
    no_fault: int  # тревог только у версии, неисправности не было


class CompareDisagree(BaseModel):
    decisions: int  # решения, где версии разошлись: тревога у одной
    alert_only: dict[str, int]  # версия → тревог только у неё
    right: dict[str, int]  # версия → из расхождений права она (по исходу)
    alert_only_fault: dict[str, int] | None = None  # версия → из тревог только у неё с неисправностью
    by_kind: dict[str, dict[str, CompareFault]] | None = None  # вид эпизода → версия → тревоги только у неё


class CompareDelta(BaseModel):
    value: float
    ci90: list[float]  # 90% интервал парного кластерного бутстрапа


class CompareDiff(BaseModel):
    """Разность метрик a − b на одних решениях (ml/reports/compare-v0-v3)."""

    a: str
    b: str
    precision: CompareDelta
    recall: CompareDelta
    ap: CompareDelta


class CompareClusters(BaseModel):
    object_days: int  # объекто-сутки выборки — кластеры бутстрапа
    with_faults: int  # из них с неисправностями


class CompareKindBlock(BaseModel):
    decisions: int
    positives: int
    alerts: int
    tp: int
    fp: int
    fn: int
    precision: float | None
    recall: float | None


class CompareOther(BaseModel):
    alerts: int
    tp: int
    fp: int
    fn: int
    precision: float | None
    recall: float | None


class CompareCoverage(BaseModel):
    """Правило версии «N суток без пропуска журнала»: решения, которых у неё нет из-за пропуска, и как на них
    сработали другие версии."""

    model_config = ConfigDict(populate_by_name=True)

    version: str
    silent_days: int
    gap_days: list[str]
    from_: str = Field(alias="from")
    to: str
    decisions: int
    positives: int
    others: dict[str, CompareOther]


class ModelCompare(BaseModel):
    """Офлайн-сравнение версий событийной модели на одних решениях (15.2, считает ml-service): пересечение
    допустимых для всех версий решений, каждая версия — на своём пороге. Поля от `ci` и ниже — итоги отчёта
    ml/reports/compare-v0-v3, необязательные."""

    period: OfflinePeriod
    decisions: int
    positives: int
    versions: dict[str, OfflineBlock]
    disagree: CompareDisagree
    ci: dict[str, dict[str, list[float]]] | None = None  # версия → precision|recall|ap → [lo, hi], 90%
    diff: CompareDiff | None = None
    clusters: CompareClusters | None = None
    by_kind: dict[str, dict[str, CompareKindBlock]] | None = None  # вид эпизода → версия → блок
    coverage: CompareCoverage | None = None
    # офлайн-связка «v3, где оценила, иначе v0» (ml.v3_bridge.fallback) — ответ ml-service как есть
    fallback: dict | None = None


# --- эпизоды оборудования -------------------------------------------------------------------------------------------

EquipmentKind = Literal["power_outage", "equipment_fault"]


class EquipmentTypeCount(BaseModel):
    sensor_type: SensorType
    count: int


class EquipmentSubObject(BaseModel):
    id: int
    name: str
    channels: int


class EquipmentEpisode(BaseModel):
    """Эпизод оборудования в ленте (backend.md §6.2): информационная строка, вердикта модели нет."""

    id: int
    run_id: int | None
    kind: EquipmentKind
    value: str  # преобладающее состояние каналов: «Обесточен», «Неисправен»…
    object_id: int
    object_name: str
    sub_object_id: int | None  # подобъект с наибольшим числом каналов эпизода — для перехода на карту
    sub_object_name: str | None
    sub_objects: list[EquipmentSubObject]  # все затронутые подобъекты, по убыванию каналов
    start_ts: dt.datetime
    end_ts: dt.datetime | None  # null — открыт: в норму вернулось меньше половины каналов
    channels: int
    returned_channels: int  # вернулись в норму
    active_channels: int  # сейчас ещё в тревожном состоянии
    by_type: list[EquipmentTypeCount]


class EquipmentMember(BaseModel):
    channel_id: int
    channel_name: str
    sensor_type: SensorType
    sub_object_id: int
    sub_object_name: str
    picket_number: int | None
    value: str  # состояние, в котором канал вошёл в эпизод
    start_ts: dt.datetime
    end_ts: dt.datetime | None  # вернулся в норму
    status: Status  # статус канала сейчас


class EquipmentEpisodeDetail(EquipmentEpisode):
    members: list[EquipmentMember]


class EquipmentCounts(BaseModel):
    active: int
    power_outage: int
    equipment_fault: int


class EquipmentEpisodePage(Page):
    items: list[EquipmentEpisode]
    counts: EquipmentCounts


# --- вскрытие под охраной ------------------------------------------------------------------------------------------

AccessStatus = Literal["candidate", "open", "closed", "dismissed"]
AccessResolution = Literal["works", "intrusion", "false_alarm"]


class AccessChannel(BaseModel):
    id: int
    name: str
    sensor_type: str


class AccessFlags(BaseModel):
    off_hours: bool  # ночь 22–6 или выходной, по Москве по началу
    motion: bool  # «Обнаружено движение» на подобъекте после начала до последнего открытия + 5 мин
    guard_faulty: bool  # «Много неисправных устройств» на момент начала: возможна ложная тревога
    repeated: bool  # 3 и больше инцидентов подобъекта за 7 сут


class AccessIncident(BaseModel):
    """Вскрытие под охраной (docs/architecture/access.md): метка правил охраны, не подтверждённое проникновение."""

    id: int
    run_id: int | None
    status: AccessStatus
    priority: Literal["high", "medium"] | None  # у «снято после открытия» — null
    object_id: int
    object_name: str
    sub_object_id: int
    sub_object_name: str
    guard_channel_id: int
    armed_since: dt.datetime | None
    start_ts: dt.datetime
    qualified_ts: dt.datetime | None  # start + 5 мин — по логике правила
    end_ts: dt.datetime | None  # null — идёт
    end_reason: Literal["disarmed", "quiet", "run_end"] | None
    disarmed_after_min: int | None  # снято через N мин от начала
    openings: int
    hatch_openings: int
    channels: list[AccessChannel]
    flags: AccessFlags
    work_order_id: int | None
    resolution: AccessResolution | None
    comment: str | None
    updated_at: dt.datetime
    source_note: str


class AccessOpening(BaseModel):
    channel_id: int
    channel_name: str
    sensor_type: str
    picket_number: int | None
    ts: dt.datetime
    is_alarm: bool
    closed_ts: dt.datetime | None


class GuardEvent(BaseModel):
    ts: dt.datetime
    value: str  # «На охране», «Снято с охраны», «Много неисправных устройств», «Устройства на объекте исправны»


class AccessIncidentDetail(AccessIncident):
    opening_events: list[AccessOpening]  # открытия инцидента по времени
    guard_events: list[GuardEvent]  # переходы охраны подобъекта от начала охраны до конца инцидента


class AccessCounts(BaseModel):
    open: int
    closed: int
    dismissed: int


class AccessIncidentPage(Page):
    items: list[AccessIncident]
    counts: AccessCounts


class AccessIncidentPatch(BaseModel):
    resolution: AccessResolution | None = None
    comment: str | None = None


# --- сценарий из своего журнала ------------------------------------------------------------------------------------

UploadField = Literal["event_id", "channel_id", "datetime", "date", "time", "is_alarm", "value"]


class UploadFormat(BaseModel):
    kind: Literal["csv", "xlsx"]
    encoding: str | None  # utf-8, utf-8-sig, cp1251; у XLSX — null
    delimiter: str | None  # «,», «;», табуляция; у XLSX — null
    sheet: str | None  # лист XLSX
    header: bool  # есть строка заголовка


class UploadColumn(BaseModel):
    index: int
    name: str  # из заголовка; без него — «Колонка N»
    samples: list[str]  # до 5 значений


class UploadFieldMatch(BaseModel):
    field: UploadField
    label: str
    required: bool  # время — datetime или date + time
    column: int | None
    match: Literal["exact", "synonym", "guess", "manual"] | None  # как нашли колонку; manual — назначена вручную


class UploadWarning(BaseModel):
    code: str  # no_header, field_missing, unknown_channels, bad_dates, bad_alarm, invalid_rows, duplicates, period
    message: str  # по-русски, для человека
    count: int | None = None
    field: UploadField | None = None
    examples: list[str] | None = None


class UploadObject(BaseModel):
    id: int
    name: str
    channels: int


class UploadStats(BaseModel):
    events: int  # после удаления дублей и строк без канала, времени или значения
    channels: int
    unknown_channels: int  # каналов нет в справочнике — эмулятор их не передаёт
    duplicates: int
    invalid_rows: int
    objects: list[UploadObject]


class UploadPeriod(BaseModel):
    start: dt.datetime
    end: dt.datetime  # не включается


class ScenarioUpload(BaseModel):
    """Разбор загруженного журнала под текущее сопоставление колонок."""

    id: str
    file_name: str
    size_bytes: int
    format: UploadFormat
    sheets: list[str] | None
    columns: list[UploadColumn]
    fields: list[UploadFieldMatch]
    required_missing: list[UploadField]
    ready: bool
    rows: int  # строк данных в файле
    period: UploadPeriod | None
    stats: UploadStats
    warnings: list[UploadWarning]
    preview: list[list[str]]  # первые 20 строк — ячейки по колонкам файла
    name_suggestion: str


class ScenarioUploadUpdate(BaseModel):
    mapping: dict[UploadField, int | None] | None = None
    sheet: str | None = None
    title: str | None = None
    dry_run: bool = False  # true — только пересчитать разбор, сценарий не создавать


class ScenarioCreated(BaseModel):
    scenario_id: str
    title: str
    period: UploadPeriod | None
    events: int
    channels: int
    objects: list[UploadObject]
    warnings: list[UploadWarning]


# --- дашборд ------------------------------------------------------------------------------------------------------


class LivePR(BaseModel):
    precision: float | None
    recall: float | None
    decisions: int
    positives: int
    history: int  # из решений — с исходом по журналу после конца прогона
    alerts: int  # все тревоги модели (alert), без учёта конца горизонта
    alerts_confirmed: int  # из них с исходом fault
    open: int  # оценённые решения без исхода


class LiveByModel(BaseModel):
    smoke_alarm: LivePR
    daily: LivePR


class AlarmsHour(BaseModel):
    hour: dt.datetime
    ppr: int
    sensor_fault: int
    reaction: int
    no_score: int


class DashboardSummary(BaseModel):
    now: dt.datetime
    active_alarms: int
    alarms_24h: int
    ppr_share_24h: float | None
    alert_predictions_24h: int
    open_work_orders: int
    overdue_work_orders: int
    channels_total: int
    channels_in_fault: int
    channels_at_risk: int
    live: LiveByModel
    alarms_by_hour: list[AlarmsHour]


class ObjectRisk(BaseModel):
    object_id: int
    object_name: str
    channels: int
    at_risk: int
    in_fault: int
    active_alarms: int
    max_risk: float | None
    by_sensor_type: list[SensorTypeSummary]


class DashboardRisk(BaseModel):
    top: list[Channel]
    by_object: list[ObjectRisk]


# --- заявки -------------------------------------------------------------------------------------------------------


class WorkOrderPage(Page):
    items: list[WorkOrder]


class WorkOrderHistoryItem(BaseModel):
    ts: dt.datetime
    status: WorkOrderStatus
    comment: str | None
    assignee: str | None


class WorkOrderDetail(WorkOrder):
    prediction: Prediction | None
    rule_description: str
    history: list[WorkOrderHistoryItem]


class WorkOrderCreate(BaseModel):
    channel_id: int
    priority: Priority
    title: str = Field(min_length=1, max_length=500)
    due_date: dt.date
    comment: str | None = None
    assignee: str | None = None


class WorkOrderPatch(BaseModel):
    status: WorkOrderStatus | None = None
    comment: str | None = None
    assignee: str | None = None
