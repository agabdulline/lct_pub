// Russian labels for API enums: one place for all screens.

import type {
  AccessEndReason,
  AccessFlags,
  AccessResolution,
  AlarmKind,
  AnyModelName,
  DatasetDirection,
  EquipmentKind,
  ModelName,
  RunStatus,
  SensorType,
  SkipReason,
  Status,
  Verdict,
  WorkOrderPriority,
  WorkOrderRule,
  WorkOrderStatus,
} from '@/lib/api/types'

export const STATUS_LABEL: Record<Status, string> = {
  ok: 'Норма',
  risk: 'Риск',
  fault: 'Неисправен',
  alarm: 'Тревога',
  ppr: 'Плановая проверка',
  unknown: 'Нет данных',
}

export const VERDICT_LABEL: Record<Verdict, string> = {
  ppr: 'Плановая проверка',
  sensor_fault: 'Вероятна неисправность датчика',
  reaction: 'Требует реакции',
  no_score: 'Нет оценки',
  pending: 'Оценивается',
}

export const VERDICT_SHORT: Record<Verdict, string> = {
  ppr: 'ППР',
  sensor_fault: 'Неисправность',
  reaction: 'Реакция',
  no_score: 'Нет оценки',
  pending: 'Оценивается',
}

export const VERDICT_ACTION: Record<Verdict, string> = {
  ppr: 'Информационно',
  sensor_fault: 'Автозаявка на проверку датчика',
  reaction: 'Штатная реакция',
  no_score: 'Штатная реакция',
  pending: 'Модель оценивает — до 10 с; при ускорении это минуты по часам прогона',
}

/** Verdict → status token (docs/architecture/frontend.md §2). */
export const VERDICT_STATUS: Record<Verdict, Status> = {
  ppr: 'ppr',
  sensor_fault: 'risk',
  reaction: 'alarm',
  no_score: 'unknown',
  pending: 'unknown',
}

export const ALARM_KIND_LABEL: Record<AlarmKind, string> = {
  ppr: 'ППР',
  group: 'Группа',
  solitary: 'Одиночная',
}

export const SKIP_REASON_LABEL: Record<SkipReason, string> = {
  fault_recent_24h: 'Канал был неисправен в последние 24 ч',
  fault_same_second: 'Неисправность пришла в ту же секунду',
  history_lt_7d: 'Меньше 7 дней истории канала',
  decided_within_24h: 'По каналу уже было решение за последние 24 ч',
  in_fault: 'Канал уже неисправен',
  no_coverage: 'Нет данных за период',
  unsupported_type: 'Тип канала не покрыт моделью',
  alarm_not_found: 'Тревога не найдена в данных прогона',
  unknown_channel: 'Канала нет в справочнике',
  not_smoke_channel: 'Модель оценивает только дымовые датчики',
  not_eligible: 'Тревога не проходит правила допустимости',
  ml_unavailable: 'ML-сервис недоступен',
  model_missing: 'Модель не загружена',
  daily_model_missing: 'Ежедневная модель не загружена',
  coverage_gap: 'За 8 суток до тревоги был день без данных журнала — v3 молчит 8 суток после пропуска',
  state_not_normal: 'Перед тревогой канал был не в «Норме» — v3 такие не оценивает',
}

/** Short skip reasons for a line of the feed or the journal: why the chain's first version did not decide. */
const SKIP_REASON_SHORT: Partial<Record<SkipReason, string>> = {
  coverage_gap: 'день без данных журнала за 8 суток',
  state_not_normal: 'канал перед тревогой не в «Норме»',
  decided_within_24h: 'решение по каналу за сутки',
  fault_recent_24h: 'канал был неисправен за сутки',
  history_lt_7d: 'мало истории канала',
  in_fault: 'канал неисправен',
  ml_unavailable: 'ML-сервис недоступен',
}

/** Skip reasons are an open set filled by the ML service: unknown codes are shown as they are. */
export const skipReasonLabel = (reason: string | null | undefined): string =>
  reason ? (SKIP_REASON_LABEL[reason as SkipReason] ?? reason) : '—'

export const MODEL_LABEL: Record<ModelName, string> = {
  smoke_alarm: 'Событийная',
  daily: 'Ежедневная',
}

export const MODEL_DESCRIPTION: Record<ModelName, string> = {
  smoke_alarm: 'После дымовой тревоги: неисправность датчика в ближайшие 24 ч',
  daily: 'Каждые сутки: отказ канала через 1–30 суток',
}

// ---------- event-model versions (stage 15): v0 of the team and v3 of the ML developer score every alarm

// Daily models of the routing table (task C): the team's v0 and the ML developer's type models.
export const VERSION_LABEL: Record<string, string> = {
  'baseline-v0': 'v0',
  v3: 'v3',
  ups_ensemble: 'ансамбль ИБП',
  manual_alarm: 'модель извещателя',
}
export const VERSION_AUTHOR: Record<string, string> = {
  'baseline-v0': 'базовая модель команды',
  v3: 'модель ML-разработчика',
  ups_ensemble: 'модель ML-разработчика',
  manual_alarm: 'модель ML-разработчика',
}

/**
 * A chain of event-model versions (owner's decision: the ML developer's model where it closes the case, else ours):
 * `v3>baseline-v0` — the first version that decided makes the verdict. `null` — a single version.
 */
export const chainOf = (v: string | null | undefined): string[] | null => (v && v.includes('>') ? v.split('>') : null)
/** Short name of a version: `baseline-v0` → «v0», a chain → «v3 → v0»; an unknown one as it is. */
export const versionLabel = (v: string | null | undefined): string => {
  if (!v) return '—'
  const chain = chainOf(v)
  return chain ? chain.map(versionLabel).join(' → ') : (VERSION_LABEL[v] ?? v)
}
/** Who a version is: the team's, the ML developer's, or a chain of them in its order. */
export const versionAuthor = (v: string): string | undefined => {
  const chain = chainOf(v)
  if (!chain) return VERSION_AUTHOR[v]
  const [first, ...rest] = chain.map(versionLabel)
  return `связка: ${first}, где оценила, иначе ${rest.join(', иначе ')}`
}
/** A stable order of versions everywhere: the team's v0 first, then v3, then chains, then the rest by name. */
export const byVersion = (a: string, b: string) => {
  const rank = (v: string) => (v === 'baseline-v0' ? 0 : v === 'v3' ? 1 : chainOf(v) ? 2 : 3)
  return rank(a) - rank(b) || a.localeCompare(b)
}

/** Why a version made no decision: its skip reason or, without one, the general case. */
export const versionSkipText = (reason: string | null | undefined) =>
  reason ? skipReasonLabel(reason) : 'тревога не прошла правила допустимости версии'

/**
 * Who made the verdict under a chain and why not the first version: «решение v0 — v3 пропустила: <причина>».
 * `decidedBy` — the version whose decision it is; `fallbackReason` — the first version's skip reason when it did not;
 * `short` — the brief reason for a line of the feed or the journal.
 */
export function decisionText(chain: string[], decidedBy: string | null, fallbackReason: string | null | undefined, short = false): string {
  const why = (short && fallbackReason && SKIP_REASON_SHORT[fallbackReason as SkipReason]) || versionSkipText(fallbackReason)
  const reason = `${why.charAt(0).toLowerCase()}${why.slice(1)}`
  // No part decided: the verdict points at the last part's skip — «нет оценки».
  if (!decidedBy) return `${versionLabel(chain[0])} пропустила: ${reason}, ${chain.slice(1).map(versionLabel).join(', ')} тоже без решения`
  const by = versionLabel(decidedBy)
  const i = chain.indexOf(decidedBy)
  if (i <= 0) return `решение ${by}`
  return `решение ${by} — ${chain.slice(0, i).map(versionLabel).join(', ')} пропустила: ${reason}`
}

/** Alarm episode kinds in words, for the comparison of versions by kind. */
export const ALARM_KIND_TITLE: Record<AlarmKind, string> = {
  ppr: 'Плановая проверка',
  group: 'Группа тревог',
  solitary: 'Одиночная тревога',
}

export const isHackathonModel = (m: AnyModelName): m is ModelName => m === 'smoke_alarm' || m === 'daily'

/** Short label of any model: the hackathon ones by name, an external one by its task (its title is in /models). */
export const modelLabel = (m: AnyModelName) => (isHackathonModel(m) ? MODEL_LABEL[m] : m)

export const modelDescription = (m: AnyModelName) => (isHackathonModel(m) ? MODEL_DESCRIPTION[m] : 'Модель внешнего набора')

export const RULE_LABEL: Record<WorkOrderRule, string> = {
  alarm_model_alert: 'Событийная модель',
  daily_model_alert: 'Ежедневная модель',
  solitary_repeat: 'Повторные одиночные тревоги',
  fault_persisting: 'Неисправен > 72 ч',
  manual: 'Вручную',
  ext_fire_alert: 'Внешние данные: пожар',
  ext_access_violation: 'Внешние данные: проезд',
  ext_pdm_failure: 'Внешние данные: отказ агрегата',
  access_open_armed: 'Вскрытие под охраной',
}

export const PRIORITY_LABEL: Record<WorkOrderPriority, string> = { high: 'Высокий', medium: 'Средний', low: 'Низкий' }

export const PRIORITY_STATUS: Record<WorkOrderPriority, Status> = { high: 'alarm', medium: 'risk', low: 'unknown' }

export const WO_STATUS_LABEL: Record<WorkOrderStatus, string> = {
  new: 'Новая',
  in_progress: 'В работе',
  done: 'Выполнена',
  rejected: 'Отклонена',
}

export const WO_STATUS_STATUS: Record<WorkOrderStatus, Status> = {
  new: 'risk',
  in_progress: 'ppr',
  done: 'ok',
  rejected: 'unknown',
}

export const RUN_STATUS_LABEL: Record<RunStatus, string> = {
  running: 'идёт',
  paused: 'пауза',
  finished: 'завершён',
  stopped: 'остановлен',
}

/** Known model features. Unknown names are shown as is — the list comes from ml/src. */
export const FEATURE_LABEL: Record<string, string> = {
  history_loop_rate_365d: 'Частота отказов в группе тега за год',
  loop_past_rate: 'Доля прошлых решений шлейфа, закончившихся отказом',
  peers_alarm_1h: 'Тревоги соседей за час',
  own_solitary_alarms_30d: 'Одиночные тревоги канала за 30 сут',
  own_fault_count_365d: 'Отказы канала за год',
  picket_neighbors_alarmed_1h: 'Соседи по пикету с тревогой за час',
  hours_since_last_fault: 'Часов с последнего отказа',
  object_alarm_rate_7d: 'Тревоги на объекте за 7 сут',
  own_state_switches_24h: 'Переключения состояния за 24 ч',
  fault_days_30d: 'Дней с отказом за 30 сут',
  state_switches_7d: 'Переключения состояния за 7 сут',
  hours_in_fault_30d: 'Часов в отказе за 30 сут',
  days_since_last_fault: 'Дней с последнего отказа',
  events_per_day_30d: 'Событий в сутки за 30 сут',
  value_std_7d: 'Разброс значения за 7 сут',
  group_fault_share_30d: 'Доля отказов в группе за 30 сут',

  // Event baseline-v0 (ml/baseline/features.py): channel, object, picket neighbours, tag group, loop, time.
  ch_alarms_1d: 'Тревоги канала за сутки',
  ch_alarms_7d: 'Тревоги канала за 7 сут',
  ch_alarms_30d: 'Тревоги канала за 30 сут',
  ch_solitary_30d: 'Одиночные тревоги канала за 30 сут',
  ch_faults_30d: 'Отказы канала за 30 сут',
  ch_faults_365d: 'Отказы канала за год',
  ch_hours_since_fault: 'Часов с последнего отказа канала',
  ch_undefined_30d: 'Состояния «Неопределен» за 30 сут',
  ch_disabled_365d: 'Отключения устройства за год',
  ch_events_1d: 'События канала за сутки',
  ch_days_observed: 'Дней наблюдения канала',
  ch_past_n: 'Прошлые решения по каналу за год',
  ch_past_rate: 'Доля прошлых решений канала, закончившихся отказом',
  obj_active_channels_90d: 'Активные каналы объекта за 90 сут',
  obj_alarm_channels_5m: 'Каналы объекта с тревогой за 5 мин',
  obj_alarm_channels_1h: 'Каналы объекта с тревогой за час',
  obj_alarm_channels_24h: 'Каналы объекта с тревогой за сутки',
  solitary_1h: 'Одиночная: соседи по объекту молчат час',
  obj_fault_channels_1h: 'Каналы объекта с отказом за час',
  obj_fault_channels_24h: 'Каналы объекта с отказом за сутки',
  obj_past_n: 'Прошлые решения по объекту за год',
  obj_past_rate: 'Доля прошлых решений объекта, закончившихся отказом',
  near_active_channels_90d: 'Активные соседи по пикету за 90 сут',
  near_alarm_channels_1h: 'Соседи по пикету с тревогой за час',
  near_solitary_1h: 'Соседи по пикету молчат час',
  near_fault_channels_24h: 'Соседи по пикету с отказом за сутки',
  grp_alarm_channels_1h: 'Каналы группы тега с тревогой за час',
  grp_fault_channels_24h: 'Каналы группы тега с отказом за сутки',
  grp_past_n: 'Прошлые решения по группе тега за год',
  grp_past_rate: 'Доля прошлых решений группы тега, закончившихся отказом',
  loop_alarm_channels_1h: 'Каналы шлейфа с тревогой за час',
  loop_past_n: 'Прошлые решения по шлейфу за год',
  hour: 'Час суток',
  weekday: 'День недели',
  month_sin: 'Сезон (синус месяца)',
  month_cos: 'Сезон (косинус месяца)',
  ppr_like: 'Похоже на плановую проверку: будни, 8–12 ч, ≥ 9 тревог за час',

  // Daily baseline-v0-daily (ml/baseline/daily.py).
  sensor_type: 'Тип датчика',
  events_1d: 'События за сутки',
  events_7d: 'События за 7 сут',
  events_30d: 'События за 30 сут',
  events_90d: 'События за 90 сут',
  days_active_30d: 'Дней с событиями за 30 сут',
  days_active_90d: 'Дней с событиями за 90 сут',
  alarms_7d: 'Тревоги за 7 сут',
  alarms_30d: 'Тревоги за 30 сут',
  alarms_90d: 'Тревоги за 90 сут',
  faults_msg_7d: 'Сообщения «Неисправен» за 7 сут',
  faults_msg_30d: 'Сообщения «Неисправен» за 30 сут',
  faults_msg_90d: 'Сообщения «Неисправен» за 90 сут',
  num_mean_7d: 'Среднее значение за 7 сут',
  num_mean_30d: 'Среднее значение за 30 сут',
  num_max_7d: 'Максимум значения за 7 сут',
  onsets_7d: 'Начала отказов за 7 сут',
  onsets_30d: 'Начала отказов за 30 сут',
  onsets_90d: 'Начала отказов за 90 сут',
  onsets_365d: 'Начала отказов за год',
  days_since_onset: 'Дней с начала последнего отказа',
  days_since_recovery: 'Дней с последнего восстановления',
  fault_hours_30d: 'Часов в отказе за 30 сут',
  mean_episode_hours_365d: 'Средняя длительность отказа за год, ч',
  obj_in_fault: 'Каналов объекта в отказе сейчас',
  obj_onsets_7d: 'Начала отказов на объекте за 7 сут',
  // External models (stage 14): VAST 2017 — reasons of an unusual pass.
  no_permit: 'Нет допуска к закрытому объекту',
  route: 'Необычный маршрут',
  segment_time_fast: 'Участок пройден слишком быстро',
  segment_time_slow: 'Участок пройден слишком медленно',
}

/** The event model's blind spot, `object_history === false` (ml-service, PR #71). */
export const OBJECT_HISTORY_NOTE =
  'У объекта и шлейфа канала нет исходов прошлых решений — событийная модель здесь слепа: на 531 таком решении 2025–2026 ' +
  'она не дала ни одной тревоги. Оценка «ниже порога» здесь ничего не говорит о датчике.'

/**
 * Closed decisions whose outcome was computed from the journal after the run ended (backend: outcome_source =
 * history): a short run's horizon reaches past its clock, the journal has what happened next.
 */
export const historyNote = (history: number, decisions: number) =>
  history > 0 ? `досчитано по журналу после конца прогона (${history.toLocaleString('ru-RU')} из ${decisions.toLocaleString('ru-RU')})` : null

export const EQUIPMENT_KIND_LABEL: Record<EquipmentKind, string> = {
  power_outage: 'Обесточивание',
  equipment_fault: 'Отказ оборудования',
}

export const EQUIPMENT_ACTION: Record<EquipmentKind, string> = {
  power_outage: 'Проверить электропитание объекта',
  equipment_fault: 'Проверить оборудование объекта',
}

// ---------- Access incidents (docs/architecture/access.md)

export const ACCESS_TITLE = 'Вскрытие под охраной'
/** Shown wherever an incident is: the rule of the guard system, not a confirmed intrusion. */
export const ACCESS_NOTE = 'Метка правил охраны, не подтверждённое проникновение'
export const ACCESS_RULE_TEXT =
  'Дверь или люк открыли, пока подобъект на охране, и охрану не сняли за 5 минут. Кто открыл — свои без снятия охраны, чужие или ложный контакт, — решает диспетчер.'

export const ACCESS_RESOLUTION_LABEL: Record<AccessResolution, string> = {
  works: 'Свои работы без снятия охраны',
  intrusion: 'Проникновение',
  false_alarm: 'Ложное',
}

export const ACCESS_END_LABEL: Record<AccessEndReason, string> = {
  disarmed: 'сняли охрану',
  quiet: '10 мин без открытий, всё закрыто',
  run_end: 'конец прогона',
}

/** Marks of an incident: a short badge and what it means. The hatch is not a flag of the API but a count. */
export const ACCESS_MARK: Record<'hatch' | keyof AccessFlags, { label: string; hint: string }> = {
  hatch: { label: 'люк', hint: 'Открывали люк — высокий приоритет' },
  off_hours: { label: 'ночь', hint: 'Ночь или выходной по Москве: 22:00–06:00, суббота, воскресенье — высокий приоритет' },
  motion: { label: 'движение', hint: 'Датчик движения подобъекта сработал от начала до 5 мин после последнего открытия' },
  guard_faulty: { label: 'охрана неисправна', hint: '«Много неисправных устройств» на момент начала — возможна ложная тревога' },
  repeated: { label: 'повторяется', hint: 'Три и больше инцидентов подобъекта за 7 суток — возможно, работы без снятия охраны' },
}

/** Noun forms of a channel type for counts: «71 фаза, 21 насос, 30 вентиляторов». */
export const SENSOR_COUNT_FORMS: Record<SensorType, [string, string, string]> = {
  'Датчик дыма': ['датчик дыма', 'датчика дыма', 'датчиков дыма'],
  'Тепловой датчик': ['тепловой датчик', 'тепловых датчика', 'тепловых датчиков'],
  'Ручной извещатель': ['ручной извещатель', 'ручных извещателя', 'ручных извещателей'],
  'Состояние УИР-Р': ['УИР-Р', 'УИР-Р', 'УИР-Р'],
  'КД Дверь': ['дверь', 'двери', 'дверей'],
  'Датчик движения': ['датчик движения', 'датчика движения', 'датчиков движения'],
  'КД АВ': ['АВ', 'АВ', 'АВ'],
  'КД Люк': ['люк', 'люка', 'люков'],
  Стекло: ['стекло', 'стекла', 'стёкол'],
  '9-секционный люк': ['9-секционный люк', '9-секционных люка', '9-секционных люков'],
  'Состояние фазы': ['фаза', 'фазы', 'фаз'],
  Переключатель: ['переключатель', 'переключателя', 'переключателей'],
  'Состояние вентилятора': ['вентилятор', 'вентилятора', 'вентиляторов'],
  'Состояние насоса': ['насос', 'насоса', 'насосов'],
  'Состояние охраны': ['состояние охраны', 'состояния охраны', 'состояний охраны'],
  'Датчик затопления': ['датчик затопления', 'датчика затопления', 'датчиков затопления'],
  'Датчик температуры': ['датчик температуры', 'датчика температуры', 'датчиков температуры'],
  'Газовый датчик': ['газовый датчик', 'газовых датчика', 'газовых датчиков'],
  ИБП: ['ИБП', 'ИБП', 'ИБП'],
}

/** Directions of the task, word for word — how the jury knows them (external-data.md §7). */
export const DATASET_DIRECTION_LABEL: Record<DatasetDirection, string> = {
  fire_risk: 'Пожарный риск',
  unauthorized_access: 'Несанкционированный доступ',
  wear: 'Износ инфраструктуры',
}

// ---------- external models (stage 14): labels of the data set's own terms, raw value when unknown

/** Classes of external models: Magdeburg — fire against nuisance, VAST — pass without permit and unusual pass. */
export const EXTERNAL_CLASS_LABEL: Record<string, string> = {
  fire: 'Пожар',
  nuisance: 'Помеха',
  background: 'Фон',
  violation: 'Проезд без допуска',
  suspicious: 'Необычный проезд',
  failure: 'Отказ в 24 ч',
}

/** Experiment kinds of the Magdeburg data set: 4 kinds of smouldering and fire, 8 nuisances. */
export const EXPERIMENT_KIND_LABEL: Record<string, string> = {
  Wood: 'дерево',
  Candles: 'свечи',
  Cable: 'кабель',
  Lunts: 'хлопок',
  Smoke: 'дым-машина',
  Deodorant: 'дезодорант',
  Ethanol: 'этанол',
  CO: 'выброс CO',
  Exhaust: 'выхлоп',
  Green_Waste: 'садовый мусор',
  Welding: 'сварка',
  Abrasive_Cutting: 'абразивная резка',
}

export const externalClassLabel = (c: string) => EXTERNAL_CLASS_LABEL[c] ?? c

export const EVAL_SCHEME_LABEL: Record<string, string> = {
  'leave-one-experiment-out': 'каждый опыт — моделью, обученной без него',
  'out-of-time': 'на периоде после обучения',
}

/** Call rhythm of an external model: `every_60s` → «каждые 60 с». */
export function triggerLabel(trigger: string | null | undefined): string {
  if (!trigger) return '—'
  if (trigger === 'on_event') return 'на каждое событие'
  if (trigger === 'hourly') return 'раз в час'
  if (trigger === 'daily') return 'раз в сутки'
  const every = /^every_(\d+)s$/.exec(trigger)
  return every ? `каждые ${every[1]} с` : trigger
}

/** Horizon of a model: `now` — the external model judges the moment of decision. */
export const horizonLabel = (horizon: string) => (horizon === 'now' ? 'в момент решения' : horizon)

/**
 * An external model's horizon, hours: 0 — it detects at the moment of decision (Magdeburg, VAST); more — it forecasts,
 * the outcome is a label starting within [T, T+h) (Azure PdM: 24). From the live metrics when there are any, else from
 * the model's `horizon` («[T, T+24h)», «24h», «now», «класс в момент T»).
 */
export function externalHorizonHours(model: { horizon?: string | null } | undefined, metrics?: { horizon_hours: [number, number] | null } | null): number {
  const live = metrics?.horizon_hours
  if (live) return Math.max(0, live[1])
  const h = model?.horizon ? /(\d+(?:[.,]\d+)?)\s*(?:h|ч)/.exec(model.horizon) : null
  return h ? Number(h[1].replace(',', '.')) : 0
}

/**
 * Where a work order is: the channel's sub-object — channel names repeat across the sub-objects of an object
 * («ДД ПК27» ×3 on «объект Тау»). The object is prefixed unless the sub-object's name already has it («ПС объект Тау»);
 * a backend without the field (before this contract) — the object alone.
 */
export const placeLabel = (w: { object_name: string; sub_object_name?: string | null }) =>
  !w.sub_object_name ? w.object_name : w.sub_object_name.includes(w.object_name) ? w.sub_object_name : `${w.object_name} · ${w.sub_object_name}`
