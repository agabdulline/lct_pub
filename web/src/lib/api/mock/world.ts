// Mock world: a small simulation of a run that produces the same resources and SSE events as the backend
// (docs/api.md). Enabled with VITE_API_MOCK=1, never bundled otherwise. Triage follows assumption A6.

import type {
  AccessIncident,
  AccessIncidentPatch,
  AccessOpening,
  Alarm,
  AlarmEpisode,
  EmulatorState,
  EquipmentEpisode,
  EquipmentMember,
  Factor,
  GuardEvent,
  Iso,
  ModelName,
  ModelScore,
  Prediction,
  ReverdictSummary,
  Run,
  RunAuditItem,
  RunStatus,
  Scenario,
  SkipReason,
  Status,
  StreamEventMap,
  StreamEventType,
  Verdict,
  WorkOrderDetail,
  WorkOrderPriority,
  WorkOrderRule,
} from '@/lib/api/types'

import { createRng, expIntervalMs, lognormal, pick, randInt, score, shuffle, type Rng } from './random'
import {
  ALARM_THRESHOLD,
  ALARM_VALUE,
  DAILY_ROUTES,
  DAILY_THRESHOLD,
  DAILY_TYPES,
  buildTopology,
  normalValue,
  type MockChannel,
  type Topology,
} from './topology'

const MIN = 60_000
const HOUR = 60 * MIN
const DAY = 24 * HOUR
const MSK = 3 * HOUR

export const iso = (ms: number): Iso => new Date(ms + MSK).toISOString().slice(0, 19) + '+03:00'
export const parseIso = (value: string): number => new Date(value).getTime()
const mskHour = (ms: number) => new Date(ms + MSK).getUTCHours()
const mskDate = (ms: number) => new Date(ms + MSK).toISOString().slice(0, 10)
const mskMidnight = (ms: number) => Math.floor((ms + MSK) / DAY) * DAY - MSK

/**
 * Stage 15: every admissible alarm is scored by both event-model versions; the run's primary one makes verdicts and
 * work orders. Thresholds are on the mock's score scale.
 */
export const V0 = 'baseline-v0'
export const V3 = 'v3'
export const EVENT_VERSIONS = [V0, V3]
/** The owner's chain (lct-95): v3 where it decided, else v0 — the default primary of new runs when both are ready. */
export const CHAIN = `${V3}>${V0}`
/** Versions a primary can be: each scoring version and the chain. */
export const PRIMARY_CHOICES = [...EVENT_VERSIONS, CHAIN]
const chainParts = (v: string) => (v.includes('>') ? v.split('>') : null)
/** Versions scoring in the shadow of a primary: the rest of the scoring versions, a chain's parts are not shadows. */
export const shadowsOf = (primary: string) => EVENT_VERSIONS.filter((v) => !(chainParts(primary) ?? [primary]).includes(v))
const VERSION_THRESHOLD: Record<string, number> = { [V0]: ALARM_THRESHOLD, [V3]: ALARM_THRESHOLD + 0.03 }

const toScore = (p: Prediction, primary: boolean): ModelScore => ({
  model_version: p.model_version,
  prediction_id: p.id,
  status: p.status,
  score: p.score,
  threshold: p.threshold,
  alert: p.alert,
  primary,
  inherited: false,
  skip_reason: p.skip_reason,
})

/** The event model's blind spots: objects without outcome history (`object_history = false`), it never alerts there. */
const NO_HISTORY_OBJECTS = new Set([105, 109])
/** Backend sends at most this many `prediction.updated` per tick; the rest is caught up by refetch. */
const OUTCOME_EVENTS_PER_TICK = 500

export type ChannelOverride = { status: Status; value: string; value_ts: number; is_alarm: boolean; risk?: number | null }

type FaultEpisode = { channel_id: number; start: number; end: number | null }

type Scheduled = { at: number; run: () => void }

export type RunState = {
  run: Run
  simNow: number
  rng: Rng
  live: boolean
  channel: Map<number, ChannelOverride>
  alarms: Alarm[]
  alarmById: Map<number, Alarm>
  episodes: AlarmEpisode[]
  openEpisode: Map<number, { episode: AlarmEpisode; lastTs: number; alarmIds: number[] }>
  predictions: Prediction[]
  predictionById: Map<number, Prediction>
  workOrders: WorkOrderDetail[]
  lastDecision: Map<number, { prediction: Prediction; ts: number }>
  /** Channel → `object_history` of its last scored event-model decision (Channel.object_history). */
  objectHistory: Map<number, boolean>
  faults: FaultEpisode[]
  openFault: Map<number, FaultEpisode>
  solitaryHistory: Map<number, number[]>
  queue: Scheduled[]
  next: { solitary: number; group: number; ppr: number; flip: number; daily: number; close: number; outage: number; access: number }
  /** Stage 15: primary switches and re-verdicts of the run, newest first (`/runs/{id}/audit`). */
  audit: RunAuditItem[]
  burstUntil: number
  equipment: MockEquipment[]
  access: MockAccess[]
}

/** An access incident of the mock (opened under guard): the incident, its openings and the guard transitions. */
export type MockAccess = { inc: AccessIncident; openings: AccessOpening[]; guard: GuardEvent[] }

/** An equipment episode of the mock with its channels (GET /equipment-episodes/{id} → members). */
export type MockEquipment = { ep: EquipmentEpisode; members: EquipmentMember[]; created: boolean; lastEmit: number }

/** Channel types that go «Обесточен» together when an object loses power. */
const POWER_TYPES = new Set(['Состояние фазы', 'Состояние насоса', 'Состояние вентилятора'])
/** The episode rule of the backend (triage.yaml): at least 5 channels of one object within 5 minutes. */
const EQUIPMENT_MIN_CHANNELS = 5

type Listener = <T extends StreamEventType>(type: T, data: StreamEventMap[T]) => void

export const SCENARIOS: Scenario[] = [
  {
    id: 'ppr_check',
    title: 'Плановая проверка сигнализации',
    description:
      'Утренняя проверка АПС на объекте: десятки дымовых датчиков срабатывают по очереди, операторы дёргают рычаги. ' +
      'Эпизод распознаётся как плановая проверка — вердикт тревог меняется на «плановая проверка».',
    period_start: '2026-03-17T08:30:00+03:00',
    period_end: '2026-03-17T12:30:00+03:00',
    objects: [{ id: 104, name: 'Объект Эпсилон' }],
    default_speed: 10,
    max_speed: 60,
    include_gas: false,
    timeline: [
      { at: '2026-03-17T08:50:00+03:00', text: 'Первые дымовые тревоги на объекте' },
      { at: '2026-03-17T09:00:00+03:00', text: 'Десятый датчик — эпизод распознан как плановая проверка, вердикты уточняются' },
      { at: '2026-03-17T09:30:00+03:00', text: 'Проверка идёт по пикетам — волна на карте объектов' },
    ],
    ready: true,
  },
  {
    id: 'noisy_detector',
    title: 'Одиночная тревога шумящего датчика',
    description:
      'Одиночная дымовая тревога, соседи молчат. Модель ставит высокую оценку — через несколько минут у датчика ' +
      '«Неисправен». Показывает вердикт «вероятна неисправность» и автозаявку.',
    period_start: '2026-04-08T13:00:00+03:00',
    period_end: '2026-04-09T13:00:00+03:00',
    objects: [{ id: 102, name: 'Объект Гамма' }],
    default_speed: 30,
    max_speed: 60,
    include_gas: false,
    timeline: [
      { at: '2026-04-08T14:12:00+03:00', text: 'Одиночная дымовая тревога, соседи молчат' },
      { at: '2026-04-08T14:12:05+03:00', text: 'Модель выше порога — вердикт «вероятна неисправность», автозаявка' },
      { at: '2026-04-08T14:15:00+03:00', text: 'У датчика «Неисправен» — прогноз подтвердился' },
    ],
    ready: true,
  },
  {
    id: 'power_loss',
    title: 'Обесточивание',
    description: 'Насосы, вентиляторы и фазы объекта уходят в «Обесточен» группой — реальная авария, а не шум датчиков.',
    period_start: '2026-02-07T06:00:00+03:00',
    period_end: '2026-02-07T10:00:00+03:00',
    objects: [{ id: 107, name: 'Объект Тета' }],
    default_speed: 10,
    max_speed: 30,
    include_gas: true,
    timeline: [{ at: '2026-02-07T07:02:00+03:00', text: 'Фазы, насосы и вентиляторы уходят в «Обесточен» группой' }],
    ready: true,
  },
  {
    id: 'magdeburg_mock',
    title: 'Цех: опыты с огнём и помехами (мок)',
    description: 'Мок сценария внешнего набора: синтетические сигналы мок-топологии под видом набора Magdeburg — для проверки раздела «Внешние данные» и плашки прогона.',
    period_start: '2026-02-10T09:00:00+03:00',
    period_end: '2026-02-10T12:00:00+03:00',
    objects: [{ id: 108, name: 'Объект Йота' }],
    default_speed: 60,
    max_speed: 60,
    include_gas: false,
    timeline: [],
    ready: true,
    source: 'builtin',
    dataset: {
      key: 'magdeburg',
      title: 'Indoor Fire Dataset with Distributed Multi-Sensor Nodes (Industrial Hall)',
      short: 'Magdeburg · цех',
      url: 'https://data.mendeley.com/datasets/yghykzm4km/1',
      license: 'CC BY 4.0',
      synthetic: false,
    },
  },
  {
    id: 'ordinary_day',
    title: 'Обычный день объекта',
    description: 'Сутки жизни всех объектов в ускорении: фоновые тревоги, неисправности, ежедневный прогноз в 00:10.',
    period_start: '2026-05-10T00:00:00+03:00',
    period_end: '2026-05-17T00:00:00+03:00',
    objects: [],
    default_speed: 60,
    max_speed: 60,
    include_gas: false,
    timeline: [{ at: '2026-05-11T00:10:00+03:00', text: 'Ежедневный прогноз — заявки на профилактику' }],
    ready: true,
  },
]

const ALARM_FACTORS = [
  'history_loop_rate_365d',
  'peers_alarm_1h',
  'own_solitary_alarms_30d',
  'own_fault_count_365d',
  'picket_neighbors_alarmed_1h',
  'hours_since_last_fault',
  'object_alarm_rate_7d',
  'own_state_switches_24h',
]
const DAILY_FACTORS = [
  'fault_days_30d',
  'state_switches_7d',
  'hours_in_fault_30d',
  'days_since_last_fault',
  'events_per_day_30d',
  'value_std_7d',
  'group_fault_share_30d',
]

export class MockWorld {
  readonly topo: Topology
  readonly runs = new Map<number, RunState>()
  private listeners = new Map<number, Set<Listener>>()
  private ids = { alarm: 1, episode: 1, prediction: 1, workOrder: 1, run: 1, equipment: 1, access: 1 }
  private smoke: MockChannel[]
  private smokeWeights: number[]
  private ticker: ReturnType<typeof setInterval> | null = null
  private lastTickReal = 0
  private lastClockEmit = 0
  targetSpeed = 60

  constructor() {
    this.topo = buildTopology()
    this.smoke = this.topo.channels.filter((c) => c.sensor_type === 'Датчик дыма')
    let acc = 0
    this.smokeWeights = this.smoke.map((c) => (acc += 0.02 + c.noise))

    this.createRun('ppr_check', 10, 'finished', { history: 4 * HOUR })
    const noisy = this.createRun('noisy_detector', 30, 'stopped', { history: 20 * HOUR })
    // A run that lost events at the gateway (#229): the banner and the runs table say which windows are incomplete.
    const t0 = parseIso(noisy.run.sim_start)
    noisy.run.loss_windows = [
      { start_ts: iso(t0 + 3 * HOUR), end_ts: iso(t0 + 3 * HOUR + 6 * MIN), events: 1_240 },
      { start_ts: iso(t0 + 11 * HOUR + 20 * MIN), end_ts: iso(t0 + 11 * HOUR + 21 * MIN), events: 86 },
    ]
    noisy.run.events_lost = 1_326
    this.createRun('ordinary_day', 60, 'running', { history: 58 * HOUR })
    // The whole history of an external dataset is a service run: never started, chosen or «latest».
    const base = this.createRun('magdeburg_mock', 1, 'finished', { history: 3 * HOUR })
    Object.assign(base.run, {
      kind: 'dataset',
      scenario: 'dataset:magdeburg',
      scenario_title: 'Indoor Fire Dataset with Distributed Multi-Sensor Nodes (Industrial Hall)',
      model_version: null,
    })
  }

  /** The service run with the whole history of an external dataset. */
  datasetBaseRun(key: string): RunState | null {
    for (const rs of this.runs.values()) if (rs.run.kind === 'dataset' && rs.run.dataset === key) return rs
    return null
  }

  // ---------- runs and clock

  latestRunId(): number | null {
    let max: number | null = null
    for (const [id, rs] of this.runs) if (rs.run.kind !== 'dataset' && (max === null || id > max)) max = id
    return max
  }

  runState(runId: number | null | undefined): RunState | null {
    const id = runId ?? this.latestRunId()
    return id === null ? null : (this.runs.get(id) ?? null)
  }

  activeRun(): RunState | null {
    for (const rs of this.runs.values()) if (rs.run.status === 'running' || rs.run.status === 'paused') return rs
    return null
  }

  createRun(scenarioId: string, speed: number, status: RunStatus, opts: { history?: number; primary?: string } = {}): RunState {
    const scenario = SCENARIOS.find((s) => s.id === scenarioId) ?? SCENARIOS[3]
    const id = this.ids.run++
    const simStart = parseIso(scenario.period_start)
    const simEnd = parseIso(scenario.period_end)
    const run: Run = {
      id,
      scenario: scenario.id,
      scenario_title: scenario.title,
      status: 'running',
      sim_start: iso(simStart),
      sim_end: iso(simEnd),
      sim_now: iso(simStart),
      speed,
      model_version: 'v3',
      started_at: iso(Date.now() - Math.max(0, 3 - this.runs.size) * 2 * HOUR),
      finished_at: null,
      events: 0,
      events_lost: 0,
      loss_windows: [],
      alarms: 0,
      predictions: 0,
      work_orders: 0,
      live: null,
      kind: 'scenario',
      primary_model: opts.primary ?? CHAIN,
      shadow_models: shadowsOf(opts.primary ?? CHAIN),
      base_run_id: scenario.dataset ? (this.datasetBaseRun(scenario.dataset.key)?.run.id ?? null) : null,
      dataset: scenario.dataset?.key ?? null,
    }
    const rng = createRng(1000 + id * 7919)
    const rs: RunState = {
      run,
      simNow: simStart,
      rng,
      live: false,
      channel: new Map(),
      alarms: [],
      audit: [],
      alarmById: new Map(),
      episodes: [],
      openEpisode: new Map(),
      predictions: [],
      predictionById: new Map(),
      workOrders: [],
      lastDecision: new Map(),
      objectHistory: new Map(),
      faults: [],
      openFault: new Map(),
      solitaryHistory: new Map(),
      queue: [],
      next: {
        solitary: simStart + expIntervalMs(rng, 3),
        group: simStart + expIntervalMs(rng, 0.4),
        ppr: simStart + (scenario.id === 'ppr_check' ? 20 * MIN : expIntervalMs(rng, 0.2)),
        flip: simStart + expIntervalMs(rng, 20),
        daily: mskMidnight(simStart) + 10 * MIN + (mskMidnight(simStart) + 10 * MIN <= simStart ? DAY : 0),
        close: simStart + HOUR,
        // The power loss scenario has its outage at 07:02; elsewhere a rare one, so the feed has equipment rows.
        outage: simStart + (scenario.id === 'power_loss' ? 62 * MIN : expIntervalMs(rng, 0.08)),
        // Opened under guard: less than one incident a day on the whole network; the mock shows one early.
        access: simStart + 25 * MIN,
      },
      burstUntil: 0,
      equipment: [],
      access: [],
    }
    this.runs.set(id, rs)
    // Seed the run with its past, without events.
    const target = Math.min(simEnd, simStart + (opts.history ?? 0))
    this.advance(rs, target)
    this.seedWorkOrderProgress(rs)
    rs.run.status = status
    if (status !== 'running' && status !== 'paused') rs.run.finished_at = iso(parseIso(rs.run.started_at) + HOUR)
    rs.live = status === 'running'
    return rs
  }

  /**
   * Switch the run's primary version: alarms scored from now on follow it; past verdicts stay. `applyToPast` — the
   * mock only sums up what a re-verdict would do (alarms with a decision of the version, alert changes); it does not
   * rewrite the verdicts.
   */
  setPrimary(runId: number, version: string, applyToPast = false): { run: Run; reverdict: ReverdictSummary | null } | null {
    const rs = this.runs.get(runId)
    if (!rs) return null
    const from = rs.run.primary_model ?? CHAIN
    const at = iso(Date.now())
    const simTs = iso(rs.simNow)
    let id = rs.audit[0]?.id ?? 0
    rs.audit.unshift({ id: ++id, ts: at, sim_ts: simTs, action: 'primary_model', details: { from, to: version, shadow_models: shadowsOf(version), apply_to_past: applyToPast } })
    rs.run.primary_model = version
    rs.run.shadow_models = shadowsOf(version)
    let reverdict: ReverdictSummary | null = null
    if (applyToPast) {
      const members = chainParts(version) ?? [version]
      const scoredBy = (v: string) =>
        new Map(rs.predictions.filter((p) => p.model === 'smoke_alarm' && p.model_version === v && p.status === 'scored' && p.alarm_id !== null).map((p) => [p.alarm_id!, p]))
      // A chain re-verdicts by its first version that decided the alarm.
      const own = new Map<number, Prediction>()
      for (const v of [...members].reverse()) for (const [a, p] of scoredBy(v)) own.set(a, p)
      const verdicts: Record<string, number> = {}
      let changed = 0
      for (const a of rs.alarms) {
        const p = own.get(a.id)
        if (!p || a.verdict === 'ppr') continue
        changed++
        const to = p.alert ? 'reaction' : 'sensor_fault'
        if (to !== a.verdict) verdicts[`${a.verdict}→${to}`] = (verdicts[`${a.verdict}→${to}`] ?? 0) + 1
      }
      reverdict = { version, alarms: rs.alarms.length, changed, verdicts, no_decision: rs.alarms.length - own.size, orders_created: 0 }
      rs.audit.unshift({ id: ++id, ts: at, sim_ts: simTs, action: 'reverdict', details: { ...reverdict, changes: [] } })
    }
    this.emit(rs, 'run.updated', { id: runId, primary_model: version })
    return { run: rs.run, reverdict }
  }

  startScenario(scenarioId: string, speed: number, primary?: string): Run {
    const active = this.activeRun()
    if (active) {
      active.run.status = 'stopped'
      active.run.finished_at = iso(Date.now())
      active.live = false
    }
    this.targetSpeed = speed
    const rs = this.createRun(scenarioId, speed, 'running', { primary })
    return rs.run
  }

  setState(state: 'pause' | 'resume' | 'stop') {
    const rs = this.activeRun()
    if (!rs) return
    if (state === 'pause') rs.run.status = 'paused'
    if (state === 'resume') rs.run.status = 'running'
    if (state === 'stop') {
      rs.run.status = 'stopped'
      rs.run.finished_at = iso(Date.now())
      rs.live = false
    }
  }

  emulatorState(): EmulatorState {
    const rs = this.activeRun() ?? this.runState(null)
    const running = rs?.run.status === 'running'
    return {
      state: rs ? (rs.run.status === 'running' || rs.run.status === 'paused' ? rs.run.status : 'idle') : 'idle',
      scenario: rs?.run.scenario ?? null,
      run_id: rs?.run.id ?? null,
      target_speed: rs?.run.speed ?? this.targetSpeed,
      effective_speed: running && rs ? this.effectiveSpeed(rs) : 0,
      sim_time: rs ? iso(rs.simNow) : null,
      sim_start: rs?.run.sim_start ?? null,
      sim_end: rs?.run.sim_end ?? null,
      events_sent: rs?.run.events ?? 0,
      events_total: rs ? Math.round((rs.run.events / Math.max(0.01, this.progress(rs))) || 0) || null : null,
      max_speed: SCENARIOS.find((s) => s.id === rs?.run.scenario)?.max_speed ?? 60,
    }
  }

  private progress(rs: RunState): number {
    const start = parseIso(rs.run.sim_start)
    const end = rs.run.sim_end ? parseIso(rs.run.sim_end) : NaN
    return end > start ? Math.min(1, (rs.simNow - start) / (end - start)) : 1
  }

  private effectiveSpeed(rs: RunState) {
    // D18: during a burst the emulator slows simulated time instead of dropping events.
    return rs.simNow < rs.burstUntil ? Math.max(1, Math.round(rs.run.speed * 0.55)) : rs.run.speed
  }

  // ---------- live ticker and SSE

  subscribe(runId: number | null, listener: Listener): () => void {
    const id = runId ?? this.latestRunId() ?? 0
    const set = this.listeners.get(id) ?? new Set()
    set.add(listener)
    this.listeners.set(id, set)
    this.ensureTicker()
    return () => set.delete(listener)
  }

  private emit<T extends StreamEventType>(rs: RunState, type: T, data: StreamEventMap[T]) {
    if (!rs.live) return
    for (const l of this.listeners.get(rs.run.id) ?? []) l(type, data)
  }

  ensureTicker() {
    if (this.ticker) return
    this.lastTickReal = performance.now()
    this.ticker = setInterval(() => this.tick(), 250)
  }

  private tick() {
    const now = performance.now()
    const dt = now - this.lastTickReal
    this.lastTickReal = now
    const rs = this.activeRun()
    if (!rs || rs.run.status !== 'running') return
    const end = rs.run.sim_end ? parseIso(rs.run.sim_end) : Infinity
    const target = Math.min(end, rs.simNow + dt * this.effectiveSpeed(rs))
    this.advance(rs, target)
    if (rs.simNow >= end) {
      rs.run.status = 'finished'
      rs.run.finished_at = iso(Date.now())
    }
    if (now - this.lastClockEmit > 1000) {
      this.lastClockEmit = now
      this.emit(rs, 'clock.tick', {
        mode: 'stream',
        now: iso(rs.simNow),
        run_id: rs.run.id,
        effective_speed: this.effectiveSpeed(rs),
        state: rs.run.status === 'running' ? 'running' : 'paused',
      })
    }
  }

  // ---------- simulation

  advance(rs: RunState, target: number) {
    for (;;) {
      const q = rs.queue[0]?.at ?? Infinity
      const n = rs.next
      const t = Math.min(q, n.solitary, n.group, n.ppr, n.flip, n.daily, n.close, n.outage, n.access)
      if (t > target) break
      rs.simNow = Math.max(rs.simNow, t)
      if (t === q) rs.queue.shift()!.run()
      else if (t === n.solitary) {
        n.solitary = t + expIntervalMs(rs.rng, 3)
        this.solitaryAlarm(rs, t)
      } else if (t === n.group) {
        n.group = t + expIntervalMs(rs.rng, 0.4)
        this.groupEpisode(rs, t)
      } else if (t === n.ppr) {
        n.ppr = t + (rs.run.scenario === 'ppr_check' ? 3 * DAY : 3 * HOUR + expIntervalMs(rs.rng, 0.4))
        const h = mskHour(t)
        if (h >= 8 && h <= 17) this.pprEpisode(rs, t)
      } else if (t === n.flip) {
        n.flip = t + expIntervalMs(rs.rng, 20)
        this.randomFlip(rs, t)
      } else if (t === n.daily) {
        n.daily = t + DAY
        this.dailyForecast(rs, t)
      } else if (t === n.outage) {
        n.outage = rs.run.scenario === 'power_loss' ? Infinity : t + expIntervalMs(rs.rng, 0.08)
        this.outage(rs, t)
      } else if (t === n.access) {
        n.access = t + expIntervalMs(rs.rng, 0.25)
        this.accessOpened(rs, t)
      } else {
        n.close = t + HOUR
        this.closeOutcomes(rs, t)
      }
    }
    rs.simNow = Math.max(rs.simNow, target)
    rs.run.sim_now = iso(rs.simNow)
  }

  private schedule(rs: RunState, at: number, run: () => void) {
    const item = { at, run }
    const i = rs.queue.findIndex((s) => s.at > at)
    if (i === -1) rs.queue.push(item)
    else rs.queue.splice(i, 0, item)
  }

  effective(rs: RunState, c: MockChannel): MockChannel & { value_ts: number } {
    const o = rs.channel.get(c.id)
    if (!o) return { ...c, value_ts: parseIso(rs.run.sim_start) - (c.id % 97) * HOUR }
    return { ...c, status: o.status, value: o.value, is_alarm: o.is_alarm, value_ts: o.value_ts, risk: o.risk ?? c.risk }
  }

  private setChannel(rs: RunState, c: MockChannel, t: number, patch: Partial<ChannelOverride>) {
    const prev = this.effective(rs, c)
    const next: ChannelOverride = {
      status: patch.status ?? prev.status,
      value: patch.value ?? prev.value,
      is_alarm: patch.is_alarm ?? prev.is_alarm,
      value_ts: t,
      risk: patch.risk !== undefined ? patch.risk : prev.risk,
    }
    rs.channel.set(c.id, next)
    rs.run.events++
    if (next.status !== prev.status || patch.risk !== undefined) {
      this.emit(rs, 'channel.status', {
        run_id: rs.run.id,
        channel_id: c.id,
        sub_object_id: c.sub_object_id,
        object_id: c.object_id,
        status: next.status,
        prev_status: prev.status,
        value: next.value,
        risk: next.risk ?? null,
        ts: iso(t),
      })
    }
  }

  /** Status of a channel without alarm or fault: the daily model decides between risk and ok. */
  private calmStatus(risk: number | null): Status {
    return risk !== null && risk >= DAILY_THRESHOLD ? 'risk' : 'ok'
  }

  private pickSmoke(rs: RunState): MockChannel {
    const total = this.smokeWeights[this.smokeWeights.length - 1]
    const r = rs.rng() * total
    let lo = 0
    let hi = this.smokeWeights.length - 1
    while (lo < hi) {
      const mid = (lo + hi) >> 1
      if (this.smokeWeights[mid] < r) lo = mid + 1
      else hi = mid
    }
    return this.smoke[lo]
  }

  private solitaryAlarm(rs: RunState, t: number) {
    for (let i = 0; i < 5; i++) {
      const c = this.pickSmoke(rs)
      const st = this.effective(rs, c).status
      if (st === 'ok' || st === 'risk' || st === 'unknown') return this.raiseAlarm(rs, c, t)
    }
  }

  private groupEpisode(rs: RunState, t: number) {
    const seed = this.pickSmoke(rs)
    const peers = (this.topo.channelsBySub.get(seed.sub_object_id) ?? []).filter(
      (c) =>
        c.sensor_type === 'Датчик дыма' &&
        c.picket_number !== null &&
        seed.picket_number !== null &&
        Math.abs(c.picket_number - seed.picket_number) <= 6,
    )
    const n = Math.min(peers.length, randInt(rs.rng, 3, 6))
    shuffle(rs.rng, peers)
      .slice(0, n)
      .forEach((c, i) => this.schedule(rs, t + i * randInt(rs.rng, 15, 150) * 1000, () => this.raiseAlarm(rs, c, rs.simNow)))
  }

  private pprEpisode(rs: RunState, t: number) {
    const obj = rs.run.scenario === 'ppr_check' ? this.topo.objects[4] : pick(rs.rng, this.topo.objects)
    const fire = (this.topo.channelsByObject.get(obj.id) ?? [])
      .filter((c) => c.sensor_type === 'Датчик дыма' || c.sensor_type === 'Тепловой датчик')
      .sort((a, b) => a.sub_object_id - b.sub_object_id || (a.picket_number ?? 0) - (b.picket_number ?? 0))
    const n = Math.min(fire.length, randInt(rs.rng, 14, 40))
    const start = randInt(rs.rng, 0, Math.max(0, fire.length - n))
    let at = t
    for (const c of fire.slice(start, start + n)) {
      at += randInt(rs.rng, 12, 70) * 1000
      this.schedule(rs, at, () => this.raiseAlarm(rs, c, rs.simNow))
    }
    rs.burstUntil = at
  }

  private randomFlip(rs: RunState, t: number) {
    const faulty = [...rs.openFault.values()]
    if (faulty.length > 40 || (faulty.length > 0 && rs.rng() < 0.45)) {
      const f = pick(rs.rng, faulty)
      const c = this.topo.channelById.get(f.channel_id)
      if (c) this.recover(rs, c, t)
      return
    }
    for (let i = 0; i < 4; i++) {
      const c = pick(rs.rng, this.topo.channels)
      if (rs.rng() > 0.15 + c.noise) continue
      const st = this.effective(rs, c).status
      if (st === 'ok' || st === 'risk') return this.startFault(rs, c, t)
    }
  }

  // ---------- access incidents (docs/architecture/access.md): the rule of the guard system, not a model

  /**
   * A door or hatch of an armed sub-object opens. A quarter of the time the guard is taken off within 5 minutes —
   * «снято после открытия», a journal mark only. Otherwise the candidate becomes an incident at start + 5 min with an
   * `access_open_armed` order, grows by a few more openings and ends by disarming or by 10 quiet minutes.
   */
  private accessOpened(rs: RunState, t: number) {
    const subs = this.topo.subObjects.filter((s) => (this.topo.channelsBySub.get(s.id) ?? []).some((c) => c.sensor_type === 'КД Дверь' || c.sensor_type === 'КД Люк'))
    if (!subs.length) return
    const sub = pick(rs.rng, subs)
    const chans = this.topo.channelsBySub.get(sub.id) ?? []
    const doors = chans.filter((c) => c.sensor_type === 'КД Дверь' || c.sensor_type === 'КД Люк')
    const guard = chans.find((c) => c.sensor_type === 'Состояние охраны') ?? doors[0]
    const object = this.topo.byId.get(sub.parent_id ?? sub.id) ?? sub
    const armedSince = t - Math.round((2 + rs.rng() * 18) * HOUR)
    const first = pick(rs.rng, doors)
    const msk = new Date(t + MSK)
    const offHours = msk.getUTCHours() >= 22 || msk.getUTCHours() < 6 || msk.getUTCDay() === 0 || msk.getUTCDay() === 6
    const a: MockAccess = {
      inc: {
        id: this.ids.access++,
        run_id: rs.run.id,
        status: 'candidate',
        priority: null,
        object_id: object.id,
        object_name: object.name,
        sub_object_id: sub.id,
        sub_object_name: sub.name,
        guard_channel_id: guard.id,
        armed_since: iso(armedSince),
        start_ts: iso(t),
        qualified_ts: null,
        end_ts: null,
        end_reason: null,
        disarmed_after_min: null,
        openings: 0,
        hatch_openings: 0,
        channels: [],
        flags: { off_hours: offHours, motion: rs.rng() < 0.4, guard_faulty: rs.rng() < 0.1, repeated: false },
        work_order_id: null,
        resolution: null,
        comment: null,
        updated_at: iso(t),
        source_note: 'Метка правил охраны, не подтверждённое проникновение',
      },
      openings: [],
      guard: [{ ts: iso(armedSince), value: 'На охране' }],
    }
    rs.access.push(a)
    this.accessOpening(rs, a, first, t)
    const own = rs.rng() < 0.25
    // The access model at the first opening (lct-8f): mostly right, sometimes it alarms on staff or misses a stranger.
    const reaction = own ? rs.rng() < 0.12 : rs.rng() < 0.8
    const score = Math.round((reaction ? 0.65 + rs.rng() * 0.33 : 0.05 + rs.rng() * 0.5) * 1000) / 1000
    Object.assign(a.inc, { verdict: reaction ? 'reaction' : 'own', score, threshold: 0.608, model_version: 'access-open-v0', decided_by: null, model_note: null })
    // «нужна реакция» — an incident at once (after the 2 s burst window); «вероятно свой» — a feed row, the rule waits 5 min
    if (reaction) this.schedule(rs, t + 2000, () => this.accessQualified(rs, a, t + 2000, 'model'))
    else this.emit(rs, 'access_incident.updated', { ...a.inc })
    if (own) {
      // The guard is taken off 30 s – 4 min after the opening: one's own came late to disarm.
      const off = t + Math.round(30_000 + rs.rng() * 210_000)
      this.schedule(rs, off, () => {
        a.guard.push({ ts: iso(off), value: 'Снято с охраны' })
        // a model alarm on staff is closed by the disarming, the rule's candidate becomes «снято после открытия»
        const status = a.inc.status === 'open' ? 'closed' : 'dismissed'
        Object.assign(a.inc, { status, end_ts: iso(off), end_reason: 'disarmed', disarmed_after_min: Math.round((off - t) / MIN), updated_at: iso(off) })
        this.emit(rs, 'access_incident.updated', { ...a.inc })
      })
      return
    }
    if (!reaction) this.schedule(rs, t + 5 * MIN, () => this.accessQualified(rs, a, t + 5 * MIN, 'rule'))
    let at = t
    const more = randInt(rs.rng, 1, 5)
    for (let i = 0; i < more; i++) {
      at += Math.round((1 + rs.rng() * 4) * MIN)
      const c = rs.rng() < 0.6 ? first : pick(rs.rng, doors)
      const when = at
      this.schedule(rs, when, () => this.accessOpening(rs, a, c, when, true))
    }
    const disarm = rs.rng() < 0.4
    const end = disarm ? at + Math.round((2 + rs.rng() * 15) * MIN) : at + 10 * MIN
    this.schedule(rs, end, () => {
      if (disarm) a.guard.push({ ts: iso(end), value: 'Снято с охраны' })
      Object.assign(a.inc, {
        status: 'closed',
        end_ts: iso(end),
        end_reason: disarm ? 'disarmed' : 'quiet',
        disarmed_after_min: disarm ? Math.round((end - t) / MIN) : null,
        updated_at: iso(end),
      })
      this.emit(rs, 'access_incident.updated', { ...a.inc })
    })
  }

  private accessOpening(rs: RunState, a: MockAccess, c: MockChannel, t: number, announce = false) {
    if (a.inc.status === 'closed' || a.inc.status === 'dismissed') return
    const closed = t + Math.round((10 + rs.rng() * 90) * 1000)
    a.openings.push({ channel_id: c.id, channel_name: c.name, sensor_type: c.sensor_type, picket_number: c.picket_number, ts: iso(t), is_alarm: true, closed_ts: iso(closed) })
    a.inc.openings = a.openings.length
    a.inc.hatch_openings = a.openings.filter((o) => o.sensor_type === 'КД Люк').length
    if (!a.inc.channels.some((x) => x.id === c.id)) a.inc.channels.push({ id: c.id, name: c.name, sensor_type: c.sensor_type })
    a.inc.priority = a.inc.status === 'candidate' ? null : this.accessPriority(a)
    a.inc.updated_at = iso(t)
    if (announce && a.inc.status === 'open') this.emit(rs, 'access_incident.updated', { ...a.inc })
  }

  /** The dispatcher's review (PATCH /access-incidents/{id}): stored and announced; the work order is not touched. */
  reviewAccess(rs: RunState, id: number, patch: AccessIncidentPatch): AccessIncident | null {
    const a = rs.access.find((x) => x.inc.id === id && (x.inc.status !== 'candidate' || x.inc.verdict === 'own'))
    if (!a) return null
    if (patch.resolution !== undefined) a.inc.resolution = patch.resolution
    if (patch.comment !== undefined) a.inc.comment = patch.comment
    a.inc.updated_at = iso(rs.simNow)
    this.emit(rs, 'access_incident.updated', { ...a.inc })
    return { ...a.inc }
  }

  private accessPriority = (a: MockAccess) => (a.inc.hatch_openings > 0 || a.inc.flags.off_hours ? 'high' : 'medium') as 'high' | 'medium'

  private accessQualified(rs: RunState, a: MockAccess, t: number, by: 'model' | 'rule') {
    if (a.inc.status !== 'candidate') return
    const recent = rs.access.filter((x) => x.inc.sub_object_id === a.inc.sub_object_id && x.inc.status !== 'candidate' && x.inc.status !== 'dismissed')
    Object.assign(a.inc, { status: 'open', qualified_ts: iso(t), priority: this.accessPriority(a), updated_at: iso(t), decided_by: by })
    if (by === 'model') a.inc.source_note = 'Модель «снимут ли охрану»: прокси «свой или чужой», не обнаружение проникновения'
    a.inc.flags.repeated = recent.filter((x) => t - parseIso(x.inc.start_ts) <= 7 * DAY).length >= 3
    const guard = this.topo.channelById.get(a.inc.guard_channel_id)
    if (guard) {
      // One open order per sub-object: its guard channel (backend: work_orders_open_uq).
      const wo = this.createWorkOrder(rs, guard, t, 'access_open_armed', a.inc.priority ?? 'medium', null)
      const open = rs.workOrders.find((w) => w.channel_id === guard.id && w.rule === 'access_open_armed' && (w.status === 'new' || w.status === 'in_progress'))
      a.inc.work_order_id = (wo ?? open)?.id ?? null
    }
    this.emit(rs, 'access_incident.created', { ...a.inc })
  }

  // ---------- equipment episodes

  /** An object loses power: its phases, pumps and fans go «Обесточен» within a minute, most come back later. */
  private outage(rs: RunState, t: number) {
    const candidates = this.topo.objects.filter((o) => (this.topo.channelsByObject.get(o.id) ?? []).filter((c) => POWER_TYPES.has(c.sensor_type)).length >= 12)
    const object = rs.run.scenario === 'power_loss' ? this.topo.byId.get(107) : candidates.length ? pick(rs.rng, candidates) : undefined
    if (!object) return
    const chosen = shuffle(
      rs.rng,
      (this.topo.channelsByObject.get(object.id) ?? []).filter((c) => POWER_TYPES.has(c.sensor_type) && this.effective(rs, c).status !== 'fault' && rs.rng() < 0.8),
    ).slice(0, 140)
    if (chosen.length < EQUIPMENT_MIN_CHANNELS) return
    const eq: MockEquipment = {
      ep: {
        id: this.ids.equipment++,
        run_id: rs.run.id,
        kind: 'power_outage',
        object_id: object.id,
        object_name: object.name,
        sub_object_id: chosen[0].sub_object_id,
        sub_object_name: this.topo.byId.get(chosen[0].sub_object_id)!.name,
        sub_objects: [],
        start_ts: iso(t),
        end_ts: null,
        channels: 0,
        active_channels: 0,
        returned_channels: 0,
        value: 'Обесточен',
        by_type: [],
      },
      members: [],
      created: false,
      lastEmit: 0,
    }
    rs.equipment.push(eq)
    chosen.forEach((c, i) => this.schedule(rs, t + Math.round((i / chosen.length) * 60_000), () => this.equipmentDown(rs, eq, c, rs.simNow)))
  }

  private equipmentDown(rs: RunState, eq: MockEquipment, c: MockChannel, t: number) {
    this.setChannel(rs, c, t, { status: 'fault', value: 'Обесточен', is_alarm: false })
    const sub = this.topo.byId.get(c.sub_object_id)!
    eq.members.push({
      channel_id: c.id,
      channel_name: c.name,
      sensor_type: c.sensor_type,
      sub_object_id: sub.id,
      sub_object_name: sub.name,
      picket_number: c.picket_number,
      value: 'Обесточен',
      start_ts: iso(t),
      end_ts: null,
      status: 'fault',
    })
    this.refreshEquipment(eq)
    // A fifth of the channels stay without power long after the run (like 27 phases of the real outage).
    const back = rs.rng() < 0.2 ? 3 * DAY : Math.min(3 * HOUR, 12 * MIN + lognormal(rs.rng, 8 * MIN, 0.6))
    this.schedule(rs, t + back, () => this.equipmentUp(rs, eq, c, rs.simNow))
    if (!eq.created && eq.members.length >= EQUIPMENT_MIN_CHANNELS) {
      eq.created = true
      eq.lastEmit = t
      this.emit(rs, 'equipment_episode.created', { ...eq.ep })
    } else if (eq.created && t - eq.lastEmit >= 20_000) {
      eq.lastEmit = t
      this.emit(rs, 'equipment_episode.updated', { ...eq.ep })
    }
  }

  private equipmentUp(rs: RunState, eq: MockEquipment, c: MockChannel, t: number) {
    const m = eq.members.find((x) => x.channel_id === c.id && x.end_ts === null)
    if (!m) return
    const cur = this.effective(rs, c)
    this.setChannel(rs, c, t, { status: this.calmStatus(cur.risk), value: normalValue(c.sensor_type, rs.rng), is_alarm: false })
    m.end_ts = iso(t)
    m.status = this.calmStatus(cur.risk)
    this.refreshEquipment(eq)
    // Closed once half of the channels are back; the close is announced at once, growth at most every 20 s.
    const closes = eq.ep.end_ts === null && eq.ep.returned_channels * 2 >= eq.ep.channels
    if (closes) eq.ep.end_ts = iso(t)
    if (eq.created && (closes || t - eq.lastEmit >= 20_000)) {
      eq.lastEmit = t
      this.emit(rs, 'equipment_episode.updated', { ...eq.ep })
    }
  }

  private refreshEquipment(eq: MockEquipment) {
    const bySub = new Map<number, { id: number; name: string; channels: number }>()
    const byType = new Map<string, number>()
    for (const m of eq.members) {
      const s = bySub.get(m.sub_object_id) ?? { id: m.sub_object_id, name: m.sub_object_name, channels: 0 }
      s.channels++
      bySub.set(m.sub_object_id, s)
      byType.set(m.sensor_type, (byType.get(m.sensor_type) ?? 0) + 1)
    }
    const subs = [...bySub.values()].sort((a, b) => b.channels - a.channels)
    const returned = eq.members.filter((m) => m.end_ts !== null).length
    Object.assign(eq.ep, {
      sub_objects: subs,
      sub_object_id: subs[0]?.id ?? eq.ep.sub_object_id,
      sub_object_name: subs[0]?.name ?? eq.ep.sub_object_name,
      channels: eq.members.length,
      returned_channels: returned,
      active_channels: eq.members.length - returned,
      by_type: [...byType.entries()]
        .sort((a, b) => b[1] - a[1])
        .map(([sensor_type, count]) => ({ sensor_type: sensor_type as EquipmentMember['sensor_type'], count })),
    })
  }

  private startFault(rs: RunState, c: MockChannel, t: number) {
    if (rs.openFault.has(c.id)) return
    const ep: FaultEpisode = { channel_id: c.id, start: t, end: null }
    rs.faults.push(ep)
    rs.openFault.set(c.id, ep)
    this.setChannel(rs, c, t, { status: 'fault', value: 'Неисправен', is_alarm: c.sensor_type === 'Датчик дыма' })
    // Close positive outcomes early: the fault is inside their horizon.
    for (let i = rs.predictions.length - 1; i >= 0 && i > rs.predictions.length - 4000; i--) {
      const p = rs.predictions[i]
      if (p.channel_id !== c.id || p.outcome !== null || p.status !== 'scored') continue
      if (parseIso(p.horizon_start) <= t && t < parseIso(p.horizon_end)) {
        p.outcome = 'fault'
        p.outcome_at = iso(t)
        p.outcome_source = 'live'
        this.emitOutcome(rs, p)
      }
    }
    this.schedule(rs, t + Math.min(30 * HOUR, lognormal(rs.rng, 50 * MIN, 1.2)), () => this.recover(rs, c, rs.simNow))
  }

  private recover(rs: RunState, c: MockChannel, t: number) {
    const ep = rs.openFault.get(c.id)
    if (!ep) return
    ep.end = t
    rs.openFault.delete(c.id)
    this.setChannel(rs, c, t, {
      status: this.calmStatus(this.effective(rs, c).risk),
      value: normalValue(c.sensor_type, rs.rng),
      is_alarm: false,
    })
  }

  private raiseAlarm(rs: RunState, c: MockChannel, t: number) {
    const prevStatus = this.effective(rs, c).status
    if (prevStatus === 'alarm' || prevStatus === 'ppr' || prevStatus === 'fault') return
    // Episode on the object: alarms within 30 minutes of the previous one join it.
    let open = rs.openEpisode.get(c.object_id)
    if (!open || t - open.lastTs > 30 * MIN) {
      const episode: AlarmEpisode = {
        id: this.ids.episode++,
        run_id: rs.run.id,
        object_id: c.object_id,
        object_name: this.topo.byId.get(c.object_id)!.name,
        kind: 'solitary',
        start_ts: iso(t),
        end_ts: null,
        n_channels: 0,
        n_alarms: 0,
        has_lever: false,
        channel_ids: [],
      }
      if (open) open.episode.end_ts = iso(open.lastTs)
      rs.episodes.push(episode)
      open = { episode, lastTs: t, alarmIds: [] }
      rs.openEpisode.set(c.object_id, open)
    }
    const ep = open.episode
    open.lastTs = t
    if (!ep.channel_ids.includes(c.id)) ep.channel_ids.push(c.id)
    ep.n_channels = ep.channel_ids.length
    ep.n_alarms++
    const wasPpr = ep.kind === 'ppr'
    ep.kind = ep.n_channels >= 10 ? 'ppr' : ep.n_channels >= 2 ? 'group' : 'solitary'
    ep.has_lever ||= ep.kind === 'ppr'

    const sub = this.topo.byId.get(c.sub_object_id)!
    const neighbors = rs.alarms.filter(
      (a) => a.sub_object_id === c.sub_object_id && a.channel_id !== c.id && t - parseIso(a.ts) < HOUR,
    ).length
    const alarm: Alarm = {
      id: this.ids.alarm++,
      run_id: rs.run.id,
      ts: iso(t),
      channel_id: c.id,
      channel_name: c.name,
      sensor_type: c.sensor_type,
      object_id: c.object_id,
      object_name: this.topo.byId.get(c.object_id)!.name,
      sub_object_id: c.sub_object_id,
      sub_object_name: sub.name,
      picket_number: c.picket_number,
      value: ALARM_VALUE[c.sensor_type] ?? 'Тревога',
      episode: { id: ep.id, object_id: ep.object_id, kind: ep.kind, start_ts: ep.start_ts, end_ts: null, n_channels: ep.n_channels },
      verdict: 'pending',
      verdict_refined: false,
      prediction: null,
      neighbors_alarmed_1h: neighbors,
      work_order_id: null,
      scores: [],
      disagree: null,
    }
    rs.alarms.push(alarm)
    rs.alarmById.set(alarm.id, alarm)
    open.alarmIds.push(alarm.id)
    rs.run.alarms++
    this.setChannel(rs, c, t, { status: ep.kind === 'ppr' ? 'ppr' : 'alarm', value: alarm.value, is_alarm: true })
    this.emit(rs, 'alarm.created', alarm)

    // Episode kind changes are visible on all its alarms.
    for (const id of open.alarmIds) {
      const a = rs.alarmById.get(id)!
      a.episode = { ...alarm.episode! }
      if (ep.kind === 'ppr' && !wasPpr && a.id !== alarm.id) {
        const ch = this.topo.channelById.get(a.channel_id)!
        if (this.effective(rs, ch).status === 'alarm') this.setChannel(rs, ch, t, { status: 'ppr' })
        // Pending alarms get «ppr» when scored; scored ones are refined now (A6).
        if (a.verdict === 'pending' || a.verdict === 'ppr') continue
        a.verdict_refined = true
        a.verdict = 'ppr'
        this.emit(rs, 'alarm.scored', a)
      }
    }
    this.emit(rs, 'alarm_episode.updated', { ...ep, channel_ids: [...ep.channel_ids] })

    if (ep.kind === 'solitary') {
      const hist = (rs.solitaryHistory.get(c.id) ?? []).filter((x) => t - x < 30 * DAY)
      hist.push(t)
      rs.solitaryHistory.set(c.id, hist)
      if (hist.length >= 3) this.createWorkOrder(rs, c, t, 'solitary_repeat', 'medium', null)
    }

    // Scoring: the worker debounces by object for 5 s, the model answers in a few seconds.
    if (rs.live) setTimeout(() => this.scoreAlarm(rs, alarm, t), 1500 + rs.rng() * 2500)
    else this.scoreAlarm(rs, alarm, t)

    // The alarm state clears after a few minutes.
    this.schedule(rs, t + Math.min(2 * HOUR, lognormal(rs.rng, 4 * MIN, 0.9)), () => {
      const cur = this.effective(rs, c)
      if (cur.status === 'alarm' || cur.status === 'ppr')
        this.setChannel(rs, c, rs.simNow, { status: this.calmStatus(cur.risk), value: normalValue(c.sensor_type, rs.rng), is_alarm: false })
    })
  }

  private scoreAlarm(rs: RunState, alarm: Alarm, t: number) {
    const c = this.topo.channelById.get(alarm.channel_id)!
    const decision = t + 1000
    const last = rs.lastDecision.get(c.id)
    const recentFault = rs.faults.some((f) => f.channel_id === c.id && (f.end ?? Infinity) > t - DAY && f.start < t)
    let skip: SkipReason | null = null
    if (last && decision - last.ts < DAY) skip = 'decided_within_24h'
    else if (recentFault) skip = 'fault_recent_24h'
    else if (!c.seen_channel && rs.rng() < 0.3) skip = 'history_lt_7d'

    const solitary = alarm.episode?.kind === 'solitary'
    const primaryVersion = rs.run.primary_model ?? CHAIN
    const chain = chainParts(primaryVersion)
    // The versions in the order they decide: a chain's parts, else the primary and then its shadows.
    const order = chain ?? [primaryVersion, ...(rs.run.shadow_models ?? [])]
    // v3 stays silent for 8 days after a day without journal data (`coverage_gap`): now and then in the mock.
    const own = (v: string): SkipReason | null => skip ?? (v === V3 && rs.rng() < 0.08 ? 'coverage_gap' : null)
    let preds: Prediction[]
    if (skip === null) {
      // A12: inside planned checks noisy detectors score high — they fail the check.
      const ppr = alarm.episode?.kind === 'ppr'
      const mean = solitary ? Math.min(0.92, 0.08 + c.noise * 1.3) : ppr ? Math.min(0.9, 0.05 + c.noise * 1.6) : 0.03 + c.noise * 0.2
      const raw = score(rs.rng, c.seen_channel ? mean : mean * 0.4, 7)
      // Without outcome history the model stays below the threshold (531 such decisions, no alert).
      const s0 = NO_HISTORY_OBJECTS.has(c.object_id) ? Math.min(raw * 0.4, ALARM_THRESHOLD * 0.8) : raw
      // v3 sees the same alarm with other features: close to v0, sometimes on the other side of its threshold.
      const s3 = Math.min(0.99, Math.max(0.001, s0 + (rs.rng() - 0.45) * 0.22))
      const scoreOf = (v: string) => (v === V3 ? s3 : s0)
      const context = { alarm_kind: alarm.episode?.kind ?? 'solitary', neighbors_alarmed_1h: alarm.neighbors_alarmed_1h, alarm_id: alarm.id }
      preds = order.map((v) => {
        const why = own(v)
        return why
          ? this.createPrediction(rs, c, 'smoke_alarm', decision, decision, decision + DAY, 0, VERSION_THRESHOLD[v], { alarm_id: alarm.id }, why, v)
          : this.createPrediction(rs, c, 'smoke_alarm', decision, decision, decision + DAY, scoreOf(v), VERSION_THRESHOLD[v], context, null, v)
      })
      // Ground truth of the mock does not depend on which version is primary: both alert — a fault ~85% of the
      // time (v3 precision); one of them — a coin; none — rarely.
      const alerts = [s0 >= VERSION_THRESHOLD[V0], s3 >= VERSION_THRESHOLD[V3]].filter(Boolean).length
      const pFault = alerts === 2 ? 0.85 : alerts === 1 ? 0.45 : solitary ? 0.12 : 0.015
      if (rs.rng() < pFault) {
        const delay = Math.min(20 * HOUR, lognormal(rs.rng, 2.3 * MIN, 1.3))
        this.schedule(rs, decision + delay, () => this.startFault(rs, c, rs.simNow))
      }
    } else {
      preds = order.map((v) => this.createPrediction(rs, c, 'smoke_alarm', decision, decision, decision + DAY, 0, VERSION_THRESHOLD[v], { alarm_id: alarm.id }, skip, v))
    }
    // A chain decides by its first version that scored; a single primary — by itself.
    const prediction = (chain ? preds.find((p) => p.status === 'scored') : null) ?? preds[0]
    const shadows = preds.filter((p) => p !== prediction)
    if (prediction.status === 'scored') rs.lastDecision.set(c.id, { prediction, ts: decision })
    // Each version sees the others' decision on the same alarm; the alarm carries all of them.
    const all = [prediction, ...shadows]
    for (const p of all) p.paired = all.filter((o) => o !== p).map((o) => toScore(o, o === prediction))
    alarm.scores = all.map((p) => toScore(p, p === prediction))
    const scored = all.filter((p) => p.status === 'scored')
    alarm.disagree = scored.length === all.length && all.length > 1 ? new Set(scored.map((p) => p.alert)).size > 1 : null

    const inherited = skip === 'decided_within_24h' && last ? last.prediction : null
    const basis = prediction.status === 'scored' ? prediction : inherited
    let verdict: Verdict = 'no_score'
    if (alarm.episode?.kind === 'ppr') verdict = 'ppr'
    else if (basis) verdict = basis.alert ? 'sensor_fault' : 'reaction'
    alarm.verdict = verdict
    // The verdict points at the decision it came from; none decided under a chain — at the last part's skip (lct-95).
    const pointed = inherited ? null : (basis ?? (chain ? preds[preds.length - 1] : prediction))
    for (const p of all) p.decided = p === pointed
    alarm.prediction = {
      id: (basis ?? pointed ?? prediction).id,
      status: basis ? 'scored' : prediction.status,
      score: basis?.score ?? null,
      threshold: basis?.threshold ?? ALARM_THRESHOLD,
      alert: basis?.alert ?? null,
      skip_reason: basis ? null : prediction.skip_reason,
      inherited: inherited !== null,
      decision_time: (basis ?? prediction).decision_time,
      object_history: (basis ?? prediction).object_history,
      // Who decided under the chain and, when not its first version, why that one did not.
      decided_by: basis?.model_version ?? null,
      fallback_reason: chain && basis?.model_version !== chain[0] ? (preds.find((p) => p.model_version === chain[0])?.skip_reason ?? null) : null,
    }
    if (verdict === 'sensor_fault' || (verdict === 'ppr' && basis?.alert)) {
      // One open order per channel and rule: a repeated alert points to the existing one.
      const wo =
        (inherited ? null : this.createWorkOrder(rs, c, decision + 9000, 'alarm_model_alert', 'high', prediction.id)) ??
        rs.workOrders.find((w) => w.channel_id === c.id && w.rule === 'alarm_model_alert' && (w.status === 'new' || w.status === 'in_progress'))
      if (wo) alarm.work_order_id = wo.id
    }
    this.emit(rs, 'alarm.scored', alarm)
  }

  private factors(rs: RunState, names: string[], s: number): Factor[] {
    return shuffle(rs.rng, [...names])
      .slice(0, 5)
      .map((feature, i) => ({
        feature,
        value: Math.round(rs.rng() * 1000) / 100,
        contribution: Math.round((s - 0.2) * (1.2 - i * 0.2) * (rs.rng() < 0.8 ? 1 : -1) * 1000) / 1000,
      }))
      .sort((a, b) => Math.abs(b.contribution) - Math.abs(a.contribution))
  }

  private createPrediction(
    rs: RunState,
    c: MockChannel,
    model: ModelName,
    decision: number,
    hStart: number,
    hEnd: number,
    s: number,
    threshold: number,
    context: Record<string, unknown> | null,
    skip: SkipReason | null = null,
    version?: string,
  ): Prediction {
    const p: Prediction = {
      id: this.ids.prediction++,
      run_id: rs.run.id,
      model,
      model_version: version ?? (model === 'smoke_alarm' ? (rs.run.primary_model ?? V0) : (DAILY_ROUTES[c.sensor_type] ?? V0)),
      channel_id: c.id,
      channel_name: c.name,
      sensor_type: c.sensor_type,
      object_id: c.object_id,
      object_name: this.topo.byId.get(c.object_id)!.name,
      alarm_id: typeof context?.alarm_id === 'number' ? context.alarm_id : null,
      decision_time: iso(decision),
      horizon_start: iso(hStart),
      horizon_end: iso(hEnd),
      status: skip ? 'skipped' : 'scored',
      skip_reason: skip,
      score: skip ? null : Math.round(s * 1000) / 1000,
      threshold,
      alert: skip ? null : s >= threshold,
      seen_channel: c.seen_channel,
      object_history: skip || model !== 'smoke_alarm' ? null : !NO_HISTORY_OBJECTS.has(c.object_id),
      context,
      factors: skip ? null : this.factors(rs, model === 'smoke_alarm' ? ALARM_FACTORS : DAILY_FACTORS, s),
      outcome: null,
      outcome_at: null,
      outcome_source: null,
      paired: [],
    }
    rs.predictions.push(p)
    rs.predictionById.set(p.id, p)
    if (p.object_history !== null) rs.objectHistory.set(c.id, p.object_history)
    rs.run.predictions++
    // Daily forecast is announced once per batch (daily_forecast.completed), not per channel.
    if (model === 'smoke_alarm') this.emit(rs, 'prediction.created', p)
    return p
  }

  private dailyForecast(rs: RunState, t: number) {
    const hStart = t + DAY
    const hEnd = t + 30 * DAY
    const alerts: { c: MockChannel; p: Prediction }[] = []
    for (const c of this.topo.channels) {
      if (!DAILY_TYPES.includes(c.sensor_type) || c.risk === null) continue
      const cur = this.effective(rs, c)
      const risk = Math.min(0.99, Math.max(0.01, (cur.risk ?? c.risk) + (rs.rng() - 0.5) * 0.06))
      if (cur.status === 'fault') continue
      const status: Status = cur.status === 'ok' && risk >= DAILY_THRESHOLD ? 'risk' : cur.status === 'risk' && risk < DAILY_THRESHOLD ? 'ok' : cur.status
      rs.channel.set(c.id, { status, value: cur.value, is_alarm: cur.is_alarm, value_ts: cur.value_ts, risk })
      // The journal stores every decision; the mock keeps the informative ones to stay light.
      if (risk < 0.12 && rs.rng() > 0.04) continue
      // Task C: each channel is scored by the model its type is routed to.
      const p = this.createPrediction(rs, c, 'daily', t, hStart, hEnd, risk, DAILY_THRESHOLD, { date: mskDate(t) }, null, DAILY_ROUTES[c.sensor_type])
      if (p.alert) alerts.push({ c, p })
      if (rs.rng() < (p.alert ? 0.74 : 0.04)) {
        const at = hStart + rs.rng() * 29 * DAY
        this.schedule(rs, at, () => this.startFault(rs, c, rs.simNow))
      }
    }
    // 20 orders a day, ranked by the share of the score within its own model: the models' scales differ.
    const byModel = new Map<string, { c: MockChannel; p: Prediction }[]>()
    for (const a of alerts) byModel.set(a.p.model_version, [...(byModel.get(a.p.model_version) ?? []), a])
    ;[...byModel.values()]
      .flatMap((list) => list.sort((a, b) => (b.p.score ?? 0) - (a.p.score ?? 0)).map((a, i) => ({ ...a, share: (i + 1) / list.length })))
      .sort((a, b) => a.share - b.share)
      .slice(0, 20)
      .forEach(({ c, p }) => this.createWorkOrder(rs, c, t + 20_000, 'daily_model_alert', (p.score ?? 0) >= 0.7 ? 'medium' : 'low', p.id))
    this.emit(rs, 'daily_forecast.completed', { run_id: rs.run.id, date: mskDate(t), decision_time: iso(t), predictions: this.topo.channels.filter((c) => DAILY_TYPES.includes(c.sensor_type)).length, alerts: alerts.length })
  }

  private closeOutcomes(rs: RunState, t: number) {
    let sent = 0
    for (const p of rs.predictions) {
      if (p.outcome === null && p.status === 'scored' && parseIso(p.horizon_end) <= t) {
        p.outcome = 'no_fault'
        p.outcome_at = p.horizon_end
        p.outcome_source = 'live'
        if (sent++ < OUTCOME_EVENTS_PER_TICK) this.emitOutcome(rs, p)
      }
    }
  }

  private emitOutcome(rs: RunState, p: Prediction) {
    this.emit(rs, 'prediction.updated', {
      run_id: rs.run.id,
      id: p.id,
      channel_id: p.channel_id,
      model: p.model,
      outcome: p.outcome!,
      outcome_at: p.outcome_at,
      outcome_source: p.outcome_source,
    })
  }

  private createWorkOrder(
    rs: RunState,
    c: MockChannel,
    t: number,
    rule: WorkOrderRule,
    priority: WorkOrderPriority,
    predictionId: number | null,
  ): WorkOrderDetail | null {
    const open = rs.workOrders.some(
      (w) => w.channel_id === c.id && w.rule === rule && (w.status === 'new' || w.status === 'in_progress'),
    )
    if (open) return null
    const due = t + (priority === 'high' ? DAY : 7 * DAY)
    const titles: Record<WorkOrderRule, string> = {
      alarm_model_alert: `Проверить ${c.sensor_type.toLowerCase()} ${c.name}: высокая вероятность неисправности`,
      daily_model_alert: `Профилактика ${c.name}: риск отказа в ближайшие 30 суток`,
      solitary_repeat: `Проверить ${c.name}: повторные одиночные тревоги`,
      fault_persisting: `Устранить неисправность ${c.name}: канал неисправен дольше 72 ч`,
      manual: `Проверить ${c.name}`,
      ext_fire_alert: `Проверить ${c.name}: признаки пожара`,
      ext_access_violation: `Разобрать проезд через ${c.name}`,
      ext_pdm_failure: `Превентивная замена: ${c.name}`,
      access_open_armed: `Проверить вскрытие под охраной: ${c.name}`,
    }
    const wo: WorkOrderDetail = {
      id: this.ids.workOrder++,
      run_id: rs.run.id,
      channel_id: c.id,
      channel_name: c.name,
      sensor_type: c.sensor_type,
      object_id: c.object_id,
      object_name: this.topo.byId.get(c.object_id)!.name,
      sub_object_id: c.sub_object_id,
      sub_object_name: this.topo.byId.get(c.sub_object_id)!.name,
      prediction_id: predictionId,
      rule,
      priority,
      status: 'new',
      title: titles[rule],
      due_date: mskDate(due),
      created_at: iso(t),
      updated_at: iso(t),
      closed_at: null,
      assignee: null,
      comment: null,
      prediction: predictionId ? (rs.predictionById.get(predictionId) ?? null) : null,
      rule_description: RULE_DESCRIPTIONS[rule],
      history: [{ ts: iso(t), status: 'new', comment: 'Создана автоматически', assignee: null }],
    }
    rs.workOrders.push(wo)
    rs.run.work_orders++
    const { prediction: _p, rule_description: _r, history: _h, ...plain } = wo
    this.emit(rs, 'work_order.created', plain)
    return wo
  }

  private seedWorkOrderProgress(rs: RunState) {
    const people = ['Иванов А.', 'Петрова М.', 'Сидоров К.', 'Смена 2']
    for (const wo of rs.workOrders) {
      const age = rs.simNow - parseIso(wo.created_at)
      if (age < 3 * HOUR) continue
      const r = rs.rng()
      const t = parseIso(wo.created_at) + age * 0.5
      if (r < 0.35) {
        wo.status = 'in_progress'
        wo.assignee = pick(rs.rng, people)
      } else if (r < 0.6) {
        wo.status = 'done'
        wo.assignee = pick(rs.rng, people)
        wo.comment = 'Датчик заменён'
      } else if (r < 0.68) {
        wo.status = 'rejected'
        wo.comment = 'Ложное срабатывание, датчик исправен'
      } else continue
      wo.updated_at = iso(t)
      if (wo.status === 'done' || wo.status === 'rejected') wo.closed_at = wo.updated_at
      wo.history.push({ ts: iso(t), status: wo.status, comment: wo.comment, assignee: wo.assignee })
    }
  }

  // ---------- manual work orders

  addManualWorkOrder(rs: RunState, c: MockChannel, input: { priority: WorkOrderPriority; title: string; due_date: string; comment?: string | null; assignee?: string | null }) {
    const wo = this.createWorkOrder(rs, c, rs.simNow, 'manual', input.priority, null)
    if (!wo) return null
    Object.assign(wo, { title: input.title, due_date: input.due_date, comment: input.comment ?? null, assignee: input.assignee ?? null })
    return wo
  }

  faultEpisodes(rs: RunState, channelId: number) {
    return rs.faults.filter((f) => f.channel_id === channelId)
  }
}

export const RULE_DESCRIPTIONS: Record<WorkOrderRule, string> = {
  alarm_model_alert: 'Событийная модель выше порога: после дымовой тревоги вероятна неисправность датчика в ближайшие 24 ч.',
  daily_model_alert: 'Ежедневная модель выше порога: вероятен отказ канала через 1–30 суток.',
  solitary_repeat: 'Не меньше трёх одиночных тревог канала за 30 суток — правило без ML для каналов без истории.',
  fault_persisting: 'Канал в состоянии «Неисправен» дольше 72 ч — правило без ML.',
  manual: 'Заявка создана вручную диспетчером.',
  ext_fire_alert: 'Внешняя модель Magdeburg — признаки пожара на узле цеха (внешние данные).',
  ext_access_violation: 'Внешняя модель VAST — проезд без допуска или необычный маршрут (синтетика).',
  ext_pdm_failure: 'Внешняя модель Azure PdM — вероятен отказ компонента агрегата в ближайшие 24 ч (синтетика).',
  access_open_armed: 'Дверь или люк открыли, пока подобъект на охране, и охрану не сняли за 5 минут — правило охраны, не подтверждённое проникновение.',
}

let world: MockWorld | null = null
export const getWorld = () => (world ??= new MockWorld())
