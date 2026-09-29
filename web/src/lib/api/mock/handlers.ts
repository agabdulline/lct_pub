// Mock routes: same paths, query parameters and shapes as docs/api.md.

import type {
  AccessIncidentDetail,
  AccessIncidentPage,
  AccessIncidentPatch,
  Alarm,
  AlarmDetail,
  AlarmPage,
  Channel,
  ChannelDetail,
  ChannelTimeline,
  DashboardRisk,
  DashboardSummary,
  Dataset,
  EquipmentEpisodeDetail,
  EquipmentPage,
  ScenarioUploadPut,
  Graph,
  GraphNode,
  Health,
  LeadTime,
  Metrics,
  MetricsBlock,
  ModelCompare,
  ModelInfo,
  ModelName,
  ModelsResponse,
  ObjectDetail,
  ObjectNode,
  OfflineBlock,
  OfflineReport,
  Page,
  Prediction,
  Run,
  SensorType,
  SensorTypeSummary,
  Status,
  TimelineEvent,
  WorkOrder,
  WorkOrderCreate,
  WorkOrderDetail,
  WorkOrderPatch,
} from '@/lib/api/types'

import { createRng, lognormal, randInt } from './random'
import { DAILY_ROUTING } from './daily-routing'
import { ALARM_THRESHOLD, DAILY_ROUTES, DAILY_THRESHOLD, DAILY_TYPES, type MockChannel, type MockObject } from './topology'
import { MockHttpError } from './http-error'
import { scenarioEvents, scenarioSummary } from './scenario-data'
import { getUpload, putUpload } from './upload'
import { CHAIN, EVENT_VERSIONS, PRIMARY_CHOICES, SCENARIOS, V0, V3, getWorld, iso, parseIso, type MockWorld, type RunState } from './world'

export { MockHttpError }

type DatasetMeta = Omit<Dataset, 'base_run_id' | 'period_start' | 'period_end' | 'objects' | 'channels' | 'events'>

/** Registry of open datasets — public metadata as in backend/config/external_datasets.yaml, no data. */
const DATASETS: DatasetMeta[] = [
  {
    key: 'magdeburg',
    title: 'Indoor Fire Dataset with Distributed Multi-Sensor Nodes (Industrial Hall)',
    short: 'Magdeburg · цех',
    direction: 'fire_risk',
    description: 'Опыты в промышленном цехе: тление и горение, помехи, фон между опытами; 9 мультисенсорных узлов.',
    url: 'https://data.mendeley.com/datasets/yghykzm4km/1',
    doi: '10.17632/yghykzm4km.1',
    license: 'CC BY 4.0',
    license_url: 'https://creativecommons.org/licenses/by/4.0/',
    synthetic: false,
  },
  {
    key: 'vast2017',
    title: 'VAST Challenge 2017, Mini-Challenge 1 — Lekagul Preserve',
    short: 'VAST 2017 · заповедник',
    direction: 'unauthorized_access',
    description: 'Синтетический журнал датчиков проезда заповедника.',
    url: 'https://www.cs.umd.edu/hcil/varepository/VAST%20Challenge%202017/challenges/Mini-Challenge%201/',
    doi: null,
    license: 'Бенчмарк VAST Challenge (IEEE VIS), лицензия не указана — для исследований и обучения',
    license_url: null,
    synthetic: true,
  },
]

/** What of a dataset is loaded: the mock loads Magdeburg only (its history — the mock topology), VAST is not loaded. */
function datasetOut(world: MockWorld, d: DatasetMeta): Dataset {
  const base = world.datasetBaseRun(d.key)
  if (!base) return { ...d, base_run_id: null, period_start: null, period_end: null, objects: 0, channels: 0, events: null }
  return {
    ...d,
    base_run_id: base.run.id,
    period_start: base.run.sim_start,
    period_end: base.run.sim_end,
    objects: world.topo.objects.length,
    channels: world.topo.channels.length,
    events: base.run.events,
  }
}

type Query = URLSearchParams
type Ctx = { world: MockWorld; rs: RunState | null; q: Query; params: string[]; body: unknown }
type Handler = (ctx: Ctx) => unknown

const HOUR = 3_600_000
const DAY = 24 * HOUR

const notFound = (what: string) => new MockHttpError(404, 'not_found', `${what} не найден`)

function requireRun(ctx: Ctx): RunState {
  if (!ctx.rs) throw new MockHttpError(404, 'run_not_found', 'Прогон не найден')
  return ctx.rs
}

function paginate<T>(items: T[], q: Query): Page<T> {
  const page = Math.max(1, Number(q.get('page') ?? 1))
  const size = Math.min(500, Math.max(1, Number(q.get('size') ?? 50)))
  return { items: items.slice((page - 1) * size, page * size), total: items.length, page, size }
}

const num = (q: Query, key: string) => (q.has(key) && q.get(key) !== '' ? Number(q.get(key)) : null)
const list = (q: Query, key: string) => q.getAll(key).flatMap((v) => v.split(',')).filter(Boolean)

// ---------- channels and objects

function toChannel(world: MockWorld, rs: RunState | null, c: MockChannel): Channel {
  const e = rs ? world.effective(rs, c) : { ...c, value_ts: 0 }
  return {
    id: c.id,
    name: c.name,
    tag: c.tag,
    tag_group: c.tag_group,
    subsystem: c.subsystem,
    sensor_type: c.sensor_type,
    object_id: c.object_id,
    object_name: world.topo.byId.get(c.object_id)!.name,
    sub_object_id: c.sub_object_id,
    sub_object_name: world.topo.byId.get(c.sub_object_id)!.name,
    picket_number: c.picket_number,
    picket_offset: c.picket_offset,
    status: e.status,
    value: e.value,
    value_ts: e.value_ts ? iso(e.value_ts) : null,
    is_alarm: e.is_alarm,
    risk: e.risk,
    risk_threshold: e.risk === null ? null : DAILY_THRESHOLD,
    seen_channel: c.seen_channel,
    object_history: rs?.objectHistory.get(c.id) ?? null,
    source: 'hackathon',
  }
}

function channelsUnder(world: MockWorld, id: number): MockChannel[] {
  const o = world.topo.byId.get(id)
  if (!o) return []
  if (o.level === 'district') return world.topo.channels
  if (o.level === 'object') return world.topo.channelsByObject.get(id) ?? []
  return world.topo.channelsBySub.get(id) ?? []
}

type Agg = { channels: number; active_alarms: number; at_risk: number; in_fault: number; max_risk: number | null; ppr: number; status: Status }

/** Aggregate status: alarm → ppr → fault (≥10% of channels) → risk (≥5%) → ok. Proposed in docs/api.md. */
function aggregate(world: MockWorld, rs: RunState | null, channels: MockChannel[]): Agg {
  let alarms = 0
  let ppr = 0
  let risk = 0
  let fault = 0
  let max: number | null = null
  for (const c of channels) {
    const e = rs ? world.effective(rs, c) : c
    if (e.status === 'alarm') alarms++
    else if (e.status === 'ppr') ppr++
    else if (e.status === 'fault') fault++
    else if (e.status === 'risk') risk++
    if (e.risk !== null && (max === null || e.risk > max)) max = e.risk
  }
  const n = channels.length
  const status: Status = alarms ? 'alarm' : ppr ? 'ppr' : fault >= n * 0.1 ? 'fault' : risk >= n * 0.05 ? 'risk' : 'ok'
  return { channels: n, active_alarms: alarms + ppr, at_risk: risk, in_fault: fault, max_risk: max, ppr, status }
}

function objectNode(world: MockWorld, rs: RunState | null, o: MockObject): ObjectNode {
  const { ppr: _ppr, ...agg } = aggregate(world, rs, channelsUnder(world, o.id))
  const children =
    o.level === 'district'
      ? world.topo.objects
      : o.level === 'object'
        ? world.topo.subObjects.filter((s) => s.parent_id === o.id)
        : []
  return { ...o, ...agg, source: 'hackathon', children: children.map((c) => objectNode(world, rs, c)) }
}

function bySensorType(world: MockWorld, rs: RunState | null, channels: MockChannel[]): SensorTypeSummary[] {
  const groups = new Map<SensorType, MockChannel[]>()
  for (const c of channels) groups.set(c.sensor_type, [...(groups.get(c.sensor_type) ?? []), c])
  return [...groups.entries()]
    .map(([sensor_type, cs]) => {
      const a = aggregate(world, rs, cs)
      return { sensor_type, channels: a.channels, at_risk: a.at_risk, in_fault: a.in_fault, max_risk: a.max_risk }
    })
    .sort((a, b) => b.channels - a.channels)
}

// ---------- graph

function graphNode(world: MockWorld, rs: RunState | null, o: MockObject): GraphNode {
  const a = aggregate(world, rs, channelsUnder(world, o.id))
  return {
    id: `${o.level}:${o.id}`,
    kind: o.level,
    label: o.name,
    parent: o.parent_id ? `${o.level === 'object' ? 'district' : 'object'}:${o.parent_id}` : null,
    subsystem: null,
    sensor_type: null,
    status: a.status,
    risk: a.max_risk,
    active_alarms: a.active_alarms,
    picket_number: null,
    channels: a.channels,
    at_risk: a.at_risk,
    in_fault: a.in_fault,
    source: 'hackathon',
  }
}

function overviewGraph(world: MockWorld, rs: RunState | null): Graph {
  const { district, objects, subObjects } = world.topo
  const nodes = [district, ...objects, ...subObjects].map((o) => graphNode(world, rs, o))
  const edges = nodes.filter((n) => n.parent).map((n) => ({ source: n.parent!, target: n.id }))
  return { nodes, edges }
}

function subObjectGraph(world: MockWorld, rs: RunState | null, subId: number): Graph {
  const sub = world.topo.byId.get(subId)
  if (!sub || sub.level !== 'sub_object') throw notFound('Подобъект')
  const channels = world.topo.channelsBySub.get(subId) ?? []
  const groups = new Map<string, MockChannel[]>()
  for (const c of channels) {
    const key = c.tag_group ?? 'без группы'
    groups.set(key, [...(groups.get(key) ?? []), c])
  }
  const nodes: GraphNode[] = []
  const edges: Graph['edges'] = []
  for (const [key, cs] of groups) {
    const a = aggregate(world, rs, cs)
    const gid = `tag_group:${subId}:${key}`
    const pickets = cs.map((c) => c.picket_number).filter((p): p is number => p !== null)
    nodes.push({
      id: gid,
      kind: 'tag_group',
      label: key,
      parent: `sub_object:${subId}`,
      subsystem: null,
      sensor_type: null,
      status: a.status,
      risk: a.max_risk,
      active_alarms: a.active_alarms,
      picket_number: pickets.length ? Math.round(pickets.reduce((s, p) => s + p, 0) / pickets.length) : null,
      channels: a.channels,
      at_risk: a.at_risk,
      in_fault: a.in_fault,
      source: 'hackathon',
    })
    edges.push({ source: `sub_object:${subId}`, target: gid })
    for (const c of cs) {
      const e = rs ? world.effective(rs, c) : c
      nodes.push({
        id: `channel:${c.id}`,
        kind: 'channel',
        label: c.name,
        parent: gid,
        subsystem: c.subsystem,
        sensor_type: c.sensor_type,
        status: e.status,
        risk: e.risk,
        active_alarms: e.status === 'alarm' || e.status === 'ppr' ? 1 : 0,
        picket_number: c.picket_number,
        channels: null,
        at_risk: null,
        in_fault: null,
        source: 'hackathon',
      })
      edges.push({ source: gid, target: `channel:${c.id}` })
    }
  }
  return { nodes, edges }
}

// ---------- alarms

function alarmMatches(a: Alarm, q: Query, world: MockWorld): boolean {
  const from = q.get('from')
  const to = q.get('to')
  if (from && parseIso(a.ts) < parseIso(from)) return false
  if (to && parseIso(a.ts) >= parseIso(to)) return false
  const obj = num(q, 'object_id')
  if (obj !== null) {
    const o = world.topo.byId.get(obj)
    if (o?.level === 'object' && a.object_id !== obj) return false
    if (o?.level === 'sub_object' && a.sub_object_id !== obj) return false
  }
  const ch = num(q, 'channel_id')
  if (ch !== null && a.channel_id !== ch) return false
  const kinds = list(q, 'kind')
  if (kinds.length && !kinds.includes(a.episode?.kind ?? 'solitary')) return false
  const verdicts = list(q, 'verdict')
  if (verdicts.length && !verdicts.includes(a.verdict)) return false
  const alert = q.get('alert')
  if (alert === 'true' && a.prediction?.alert !== true) return false
  if (alert === 'false' && a.prediction?.alert === true) return false
  return true
}

function alarms(ctx: Ctx): AlarmPage {
  const rs = ctx.rs
  const base = rs ? [...rs.alarms].reverse() : []
  const withoutVerdict = new URLSearchParams(ctx.q)
  withoutVerdict.delete('verdict')
  // Stage 15: `disagree=true` — the versions disagree on the alarm; the counter counts it in the same scope.
  const disagree = ctx.q.get('disagree') === 'true'
  withoutVerdict.delete('disagree')
  const scope = base.filter((a) => alarmMatches(a, withoutVerdict, ctx.world))
  const items = base.filter((a) => alarmMatches(a, ctx.q, ctx.world) && (!disagree || a.disagree === true))
  const active = rs
    ? scope.filter((a) => {
        const c = ctx.world.topo.channelById.get(a.channel_id)!
        const st = ctx.world.effective(rs, c).status
        return st === 'alarm' || st === 'ppr'
      }).length
    : 0
  const count = (v: string) => scope.filter((a) => a.verdict === v).length
  return {
    ...paginate(items, ctx.q),
    counts: {
      active,
      ppr: count('ppr'),
      ppr_alert: scope.filter((a) => a.verdict === 'ppr' && a.prediction?.alert === true).length,
      sensor_fault: count('sensor_fault'),
      reaction: count('reaction'),
      no_score: count('no_score'),
      disagree: scope.filter((a) => a.disagree === true).length,
    },
  }
}

function alarmDetail(ctx: Ctx): AlarmDetail {
  const rs = requireRun(ctx)
  const a = rs.alarmById.get(Number(ctx.params[0]))
  if (!a) throw notFound('Тревога')
  const c = ctx.world.topo.channelById.get(a.channel_id)!
  const t = parseIso(a.ts)
  const peers = (ctx.world.topo.channelsBySub.get(c.sub_object_id) ?? [])
    .filter((p) => p.id !== c.id && p.sensor_type === c.sensor_type && p.picket_number !== null && c.picket_number !== null)
    .sort((x, y) => Math.abs(x.picket_number! - c.picket_number!) - Math.abs(y.picket_number! - c.picket_number!))
    .slice(0, 8)
  return {
    ...a,
    neighbors: peers.map((p) => {
      const last = [...rs.alarms].reverse().find((x) => x.channel_id === p.id && parseIso(x.ts) <= t && t - parseIso(x.ts) < HOUR)
      return {
        channel_id: p.id,
        channel_name: p.name,
        picket_number: p.picket_number,
        ts: last?.ts ?? null,
        status: ctx.world.effective(rs, p).status,
      }
    }),
    prediction_detail: a.prediction ? (rs.predictionById.get(a.prediction.id) ?? null) : null,
  }
}

// ---------- predictions and metrics

/** The ML developer's daily type models (`ml/each_channel`): id, title, ready to score, threshold. */
const EACH_CHANNEL: [string, string, boolean, number][] = [
  ['ups_ensemble', 'ИБП: ансамбль 75/25', true, 0.302],
  ['manual_alarm', 'Ручной извещатель: исследовательский режим готовности', true, 0.013],
  ['fan', 'Состояние вентилятора: отдельная модель', false, 0.671],
  ['pump', 'Состояние насоса: отдельная модель', false, 0.719],
  ['ups_context', 'ИБП: модель контекста объектов', false, 0.269],
  ['ups_old', 'ИБП: прежняя отдельная модель', false, 0.244],
]

function predictionMatches(p: Prediction, q: Query, world: MockWorld): boolean {
  const model = q.get('model')
  if (model && p.model !== model) return false
  const version = q.get('model_version')
  // A chain (lct-95): the decisions it made — one per alarm, each of the version that decided.
  if (version && (version.includes('>') ? p.decided !== true : p.model_version !== version)) return false
  // Stage 15: this version and another one scored the alarm and exactly one is above its threshold.
  if (q.get('disagree') === 'true' && !(p.status === 'scored' && p.paired?.some((o) => o.status === 'scored' && o.alert !== p.alert))) return false
  // Decisions scored by every version of the run — the same decisions for a fair comparison.
  if (q.get('paired') === 'true' && !(p.status === 'scored' && p.paired?.length && p.paired.every((o) => o.status === 'scored'))) return false
  const from = q.get('from')
  const to = q.get('to')
  if (from && parseIso(p.decision_time) < parseIso(from)) return false
  if (to && parseIso(p.decision_time) >= parseIso(to)) return false
  const alert = q.get('alert')
  if (alert && String(p.alert) !== alert) return false
  const outcome = q.get('outcome')
  if (outcome === 'open' && p.outcome !== null) return false
  if (outcome && outcome !== 'open' && p.outcome !== outcome) return false
  const seen = q.get('seen_channel')
  if (seen && String(p.seen_channel) !== seen) return false
  const history = q.get('object_history')
  if (history && String(p.object_history) !== history) return false
  const status = q.get('status')
  if (status && p.status !== status) return false
  const type = q.get('sensor_type')
  if (type && p.sensor_type !== type) return false
  const ch = num(q, 'channel_id')
  if (ch !== null && p.channel_id !== ch) return false
  const obj = num(q, 'object_id')
  if (obj !== null) {
    const o = world.topo.byId.get(obj)
    const c = world.topo.channelById.get(p.channel_id)!
    if (o?.level === 'object' && p.object_id !== obj) return false
    if (o?.level === 'sub_object' && c.sub_object_id !== obj) return false
  }
  return true
}

function quantile(sorted: number[], q: number) {
  if (!sorted.length) return 0
  const i = Math.min(sorted.length - 1, Math.max(0, Math.round(q * (sorted.length - 1))))
  return sorted[i]
}

function metricsBlock(ps: Prediction[]): MetricsBlock {
  let tp = 0
  let fp = 0
  let fn = 0
  let tn = 0
  const leads: number[] = []
  for (const p of ps) {
    const pos = p.outcome === 'fault'
    if (p.alert && pos) {
      tp++
      if (p.outcome_at) leads.push((parseIso(p.outcome_at) - parseIso(p.decision_time)) / 1000)
    } else if (p.alert) fp++
    else if (pos) fn++
    else tn++
  }
  leads.sort((a, b) => a - b)
  const precision = tp + fp ? tp / (tp + fp) : null
  const recall = tp + fn ? tp / (tp + fn) : null
  const lead_time: LeadTime | null = leads.length
    ? { min_s: leads[0], p10_s: quantile(leads, 0.1), p50_s: quantile(leads, 0.5), p90_s: quantile(leads, 0.9), max_s: leads[leads.length - 1] }
    : null
  return {
    decisions: ps.length,
    positives: tp + fn,
    alerts: tp + fp,
    tp,
    fp,
    fn,
    tn,
    precision,
    recall,
    f1: precision !== null && recall !== null && precision + recall > 0 ? (2 * precision * recall) / (precision + recall) : null,
    lead_time,
  }
}

/** Metrics count only predictions whose horizon has ended: an early `fault` outcome waits for the window. */
function metrics(ctx: Ctx): Metrics {
  const model = (ctx.q.get('model') ?? 'smoke_alarm') as ModelName
  const rs = ctx.rs
  const now = rs?.simNow ?? Date.now()
  const q = new URLSearchParams(ctx.q)
  q.set('model', model)
  q.set('status', 'scored')
  // Metrics never mix versions: without `model_version` — the run's primary one (stage 15).
  const version = q.get('model_version') ?? (model === 'smoke_alarm' ? (rs?.run.primary_model ?? CHAIN) : null)
  if (version) q.set('model_version', version)
  const scored = (rs?.predictions ?? []).filter((p) => predictionMatches(p, q, ctx.world))
  const closed = scored.filter((p) => parseIso(p.horizon_end) <= now && p.outcome !== null)
  // The daily forecast is a mix of models by the routing table (task C): one model in the sample — its id, else null.
  const mixed = [...new Set(scored.map((p) => p.model_version))]
  const by = (key: (p: Prediction) => string) => {
    const groups = new Map<string, Prediction[]>()
    for (const p of closed) groups.set(key(p), [...(groups.get(key(p)) ?? []), p])
    return Object.fromEntries([...groups.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([k, ps]) => [k, metricsBlock(ps)]))
  }
  return {
    model,
    // The mock scores the hackathon models only: an external model has no forecasts here.
    model_version: !scored.length ? null : model === 'daily' && !version ? (mixed.length === 1 ? mixed[0] : null) : (version ?? 'v3'),
    from: ctx.q.get('from'),
    to: ctx.q.get('to'),
    open: scored.length - closed.length,
    open_fault: scored.filter((p) => p.outcome === 'fault' && parseIso(p.horizon_end) > now).length,
    horizon_hours: model === 'daily' ? [24, 720] : [0, 24],
    note: (() => {
      const open = scored.length - closed.length
      const openFault = scored.filter((p) => p.outcome === 'fault' && parseIso(p.horizon_end) > now).length
      if (!open) return null
      return `Открыто ${open.toLocaleString('ru-RU')}${openFault ? `, у ${openFault} неисправность уже началась` : ''} — войдут в метрики после конца горизонта (${model === 'daily' ? '30 сут' : '24 ч'}).`
    })(),
    history: 0,
    outcomes_until: null,
    all: metricsBlock(closed),
    seen: metricsBlock(closed.filter((p) => p.seen_channel)),
    unseen: metricsBlock(closed.filter((p) => !p.seen_channel)),
    by_version: model === 'daily' ? by((p) => p.model_version) : null,
    by_sensor_type: model === 'daily' ? by((p) => p.sensor_type) : null,
  }
}

function livePR(ctx: Ctx, model: ModelName, version?: string) {
  const q = new URLSearchParams()
  q.set('model', model)
  // A version alone — on the decisions scored by every version (stage 15, `live.versions`).
  if (version) {
    q.set('model_version', version)
    q.set('paired', 'true')
  }
  const m = metrics({ ...ctx, q }).all
  const primary = model === 'smoke_alarm' ? (ctx.rs?.run.primary_model ?? CHAIN) : null
  const scored = (ctx.rs?.predictions ?? []).filter(
    (p) =>
      p.model === model &&
      p.status === 'scored' &&
      (version
        ? p.model_version === version && predictionMatches(p, q, ctx.world)
        : primary === null || (primary.includes('>') ? p.decided === true : p.model_version === primary)),
  )
  const alerts = scored.filter((p) => p.alert)
  return {
    precision: m.precision,
    recall: m.recall,
    decisions: m.decisions,
    positives: m.positives,
    history: 0,
    alerts: alerts.length,
    alerts_confirmed: alerts.filter((p) => p.outcome === 'fault').length,
    open: scored.filter((p) => p.outcome === null).length,
  }
}

// ---------- channel timeline

function timeline(ctx: Ctx): ChannelTimeline {
  const rs = requireRun(ctx)
  const c = ctx.world.topo.channelById.get(Number(ctx.params[0]))
  if (!c) throw notFound('Канал')
  const to = ctx.q.get('to') ? parseIso(ctx.q.get('to')!) : rs.simNow
  const from = ctx.q.get('from') ? parseIso(ctx.q.get('from')!) : to - 30 * DAY
  const runStart = parseIso(rs.run.sim_start)
  const rng = createRng(c.id)
  const events: TimelineEvent[] = []
  const episodes: ChannelTimeline['episodes'] = []
  const numeric = c.sensor_type === 'Газовый датчик' || c.sensor_type === 'Датчик температуры'
  const alarmValue = c.sensor_type === 'Датчик дыма' ? 'Обнаружен дым' : 'Не замкнут'
  // Base history before the run: synthetic, deterministic per channel.
  for (let day = from - (from % DAY); day < Math.min(to, runStart); day += DAY) {
    if (rng() < 0.05 + c.noise * 0.4) {
      const s = day + rng() * DAY
      const e = s + Math.min(3 * DAY, lognormal(rng, 2 * HOUR, 1.3))
      if (s >= from && s < Math.min(to, runStart)) {
        episodes.push({ kind: 'fault', start_ts: iso(s), end_ts: e < runStart ? iso(e) : null })
        events.push({ ts: iso(s), value: 'Неисправен', value_num: null, is_alarm: false, status: 'fault' })
        if (e < Math.min(to, runStart)) events.push({ ts: iso(e), value: 'Норма', value_num: null, is_alarm: false, status: 'ok' })
      }
    }
    if (!numeric && rng() < 0.02 + c.noise * 0.3) {
      const s = day + rng() * DAY
      events.push({ ts: iso(s), value: alarmValue, value_num: null, is_alarm: true, status: 'alarm' })
      events.push({ ts: iso(s + randInt(rng, 30, 900) * 1000), value: 'Дыма нет', value_num: null, is_alarm: false, status: 'ok' })
      episodes.push({ kind: 'alarm', start_ts: iso(s), end_ts: iso(s + 5 * 60_000) })
    }
  }
  // Run history: alarms and faults the simulation produced.
  for (const a of rs.alarms) {
    const t = parseIso(a.ts)
    if (a.channel_id !== c.id || t < from || t >= to) continue
    events.push({ ts: a.ts, value: a.value, value_num: null, is_alarm: true, status: a.verdict === 'ppr' ? 'ppr' : 'alarm' })
    episodes.push({ kind: 'alarm', start_ts: a.ts, end_ts: iso(t + 4 * 60_000) })
  }
  for (const f of ctx.world.faultEpisodes(rs, c.id)) {
    if (f.start >= to || (f.end ?? Infinity) < from) continue
    episodes.push({ kind: 'fault', start_ts: iso(f.start), end_ts: f.end ? iso(f.end) : null })
    events.push({ ts: iso(f.start), value: 'Неисправен', value_num: null, is_alarm: false, status: 'fault' })
    if (f.end && f.end < to) events.push({ ts: iso(f.end), value: 'Норма', value_num: null, is_alarm: false, status: 'ok' })
  }
  const daily: ChannelTimeline['daily'] = []
  for (let day = from - (from % DAY); day < to; day += DAY) {
    const date = iso(day).slice(0, 10)
    const dayEvents = events.filter((e) => e.ts.slice(0, 10) === date)
    const base = c.sensor_type === 'Датчик температуры' ? 17 + 3 * Math.sin(day / DAY / 5) : 0.02
    const spread = c.sensor_type === 'Датчик температуры' ? 2 + rng() * 2 : 0.01 + rng() * 0.03
    daily.push({
      date,
      n_events: dayEvents.length + (numeric ? randInt(rng, 20, 400) : randInt(rng, 0, 3)),
      n_alarm: dayEvents.filter((e) => e.is_alarm).length,
      n_fault: dayEvents.filter((e) => e.status === 'fault').length,
      min: numeric ? Math.round((base - spread) * 100) / 100 : null,
      max: numeric ? Math.round((base + spread * 1.5) * 100) / 100 : null,
      mean: numeric ? Math.round(base * 100) / 100 : null,
    })
  }
  events.sort((a, b) => parseIso(a.ts) - parseIso(b.ts))
  episodes.sort((a, b) => parseIso(a.start_ts) - parseIso(b.start_ts))
  return { from: iso(from), to: iso(to), events, episodes, daily }
}

// ---------- work orders

function woMatches(w: WorkOrder, q: Query, world: MockWorld) {
  const statuses = list(q, 'status')
  if (statuses.length && !statuses.includes(w.status)) return false
  const pr = list(q, 'priority')
  if (pr.length && !pr.includes(w.priority)) return false
  const rules = list(q, 'rule')
  if (rules.length && !rules.includes(w.rule)) return false
  const ch = num(q, 'channel_id')
  if (ch !== null && w.channel_id !== ch) return false
  const obj = num(q, 'object_id')
  if (obj !== null) {
    const o = world.topo.byId.get(obj)
    const c = world.topo.channelById.get(w.channel_id)!
    if (o?.level === 'object' && w.object_id !== obj) return false
    if (o?.level === 'sub_object' && c.sub_object_id !== obj) return false
  }
  return true
}

const plainWo = ({ prediction: _p, rule_description: _r, history: _h, ...w }: WorkOrderDetail): WorkOrder => w

function woList(ctx: Ctx) {
  const items = (ctx.rs?.workOrders ?? []).filter((w) => woMatches(w, ctx.q, ctx.world)).map(plainWo).reverse()
  return paginate(items, ctx.q)
}

const CSV_COLUMNS: (keyof WorkOrder)[] = [
  'id', 'created_at', 'status', 'priority', 'rule', 'object_name', 'channel_id', 'channel_name', 'sensor_type',
  'title', 'due_date', 'assignee', 'comment', 'prediction_id',
]

function woCsv(ctx: Ctx): string {
  const rows = (ctx.rs?.workOrders ?? []).filter((w) => woMatches(w, ctx.q, ctx.world)).map(plainWo).reverse()
  const esc = (v: unknown) => {
    const s = v === null || v === undefined ? '' : String(v)
    return /[",\n;]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
  }
  return [CSV_COLUMNS.join(','), ...rows.map((r) => CSV_COLUMNS.map((k) => esc(r[k])).join(','))].join('\n')
}

// ---------- offline report (synthetic, shape of ml/models/<model>/offline.json)

function block(decisions: number, positives: number, p: number, r: number, lead: number | null): OfflineBlock {
  const tp = Math.round(positives * r)
  const fp = Math.round(tp / Math.max(p, 0.01) - tp)
  const fn = positives - tp
  const f1 = p + r > 0 ? (2 * p * r) / (p + r) : null
  const lt = lead === null ? null : { min_s: lead * 0.1, p10_s: lead * 0.3, p25_s: lead * 0.6, p50_s: lead, p75_s: lead * 1.8, p90_s: lead * 4, max_s: lead * 40 }
  return { decisions, positives, alerts: tp + fp, tp, fp, fn, tn: decisions - tp - fp - fn, precision: p, recall: r, f1, ap: Math.min(0.95, p * 0.9 + 0.05), lead_time: lt }
}

function syntheticOffline(model: ModelName): OfflineReport {
  const daily = model === 'daily'
  const lead = daily ? 12 * DAY / 1000 : 140
  const curve = Array.from({ length: 50 }, (_, i) => {
    const recall = 0.02 + (i / 49) * 0.93
    return { threshold: Math.round((0.95 - recall * 0.9) * 1000) / 1000, recall, precision: Math.max(0.2, 0.97 - 0.55 * recall ** 2.2) }
  })
  const q = (quarter: string, role: 'early_stop' | 'threshold' | 'test', p: number, r: number) => ({ quarter, role, ...block(2000, 150, p, r, lead) })
  return {
    computed_at: '2026-09-26T04:16:12+00:00',
    model,
    version: 'v0',
    period: { from: '2026-01-01', to: '2026-07-01', label: daily ? '1 января — 2 мая 2026' : 'I полугодие 2026' },
    threshold: daily ? 0.64 : 0.431,
    test: block(8258, 542, 0.85, 0.816, lead),
    seen: block(7254, 531, 0.858, 0.832, lead),
    unseen: block(1004, 11, 0, 0, null),
    seen_rule: 'каналы с решениями в обучении 2023–2024',
    lead_time: block(1, 1, 1, 1, lead).lead_time,
    pr_curve: curve,
    by_quarter: [
      q('2025Q1', 'early_stop', 0.62, 0.3),
      q('2025Q2', 'early_stop', 0.64, 0.27),
      q('2025Q3', 'threshold', 0.74, 0.5),
      q('2025Q4', 'threshold', 0.78, 0.58),
      q('2026Q1', 'test', 0.86, 0.8),
      q('2026Q2', 'test', 0.84, 0.83),
    ],
    by_type: daily
      ? DAILY_TYPES.map((t, i) => ({ sensor_type: t, base_rate: 0.002 + i * 0.004, ...block(90000, 400 + i * 50, 0.7 + (i % 3) * 0.05, 0.4 + (i % 4) * 0.08, lead) }))
      : null,
    rule: { name: 'одиночная тревога на объекте за час → неисправность', ...block(8258, 542, 0.154, 0.105, null), ap: null },
    by_episode_kind: daily
      ? null
      : [
          { kind: 'ppr', ...block(7300, 390, 0.87, 0.78, lead) },
          { kind: 'group', ...block(580, 70, 0.78, 0.9, lead) },
          { kind: 'solitary', ...block(1083, 82, 0.83, 0.95, lead) },
        ],
  }
}

/**
 * Offline check of an external model in the shape of ml/models/external/magdeburg-fire/offline.json, synthetic
 * numbers: by class, by experiment, fire against nuisance.
 */
function syntheticExternalOffline(): OfflineReport {
  const curve = Array.from({ length: 40 }, (_, i) => {
    const recall = 0.02 + (i / 39) * 0.95
    return { threshold: Math.round((0.99 - recall * 0.8) * 1000) / 1000, recall, precision: Math.max(0.3, 0.99 - 0.4 * recall ** 3) }
  })
  const exp = (experiment: number, label: string, subtype: string, minutes: number, fire_alerts: number, nodes_alerted: number, first_alert_s: number | null, predicted: string) => ({
    experiment,
    label,
    subtype,
    subtypes: [subtype],
    minutes,
    decisions: Math.round(minutes * 9),
    fire_alerts,
    nodes_alerted,
    first_alert_s,
    predicted,
  })
  return {
    computed_at: '2026-09-27T00:40:00+00:00',
    model: 'magdeburg_fire',
    version: 'v0',
    period: { from: '2023-07-10', to: '2023-07-13', label: 'мок: опыты набора' },
    threshold: 0.5,
    test: block(9000, 1100, 0.74, 0.79, 150),
    seen: null,
    unseen: null,
    seen_rule: null,
    lead_time: block(1, 1, 1, 1, 150).lead_time,
    pr_curve: curve,
    by_quarter: null,
    by_type: null,
    rule: null,
    eval_scheme: 'leave-one-experiment-out',
    fire_vs_nuisance: block(1500, 1100, 0.83, 0.79, 150),
    by_class: [
      { class: 'fire', ...block(9000, 1100, 0.74, 0.79, null) },
      { class: 'nuisance', ...block(9000, 500, 0.7, 0.4, null) },
    ],
    by_experiment: [
      exp(1, 'fire', 'Wood', 30, 240, 9, 120, 'fire'),
      exp(2, 'nuisance', 'Welding', 10, 70, 9, 60, 'fire'),
      exp(3, 'fire', 'Candles', 28, 150, 9, 600, 'fire'),
      exp(4, 'nuisance', 'Deodorant', 12, 0, 0, null, 'nuisance'),
      exp(5, 'fire', 'Cable', 7, 0, 0, null, 'nuisance'),
      exp(6, 'nuisance', 'Exhaust', 10, 0, 0, null, 'background'),
    ],
  }
}

// ---------- routes

const routes: [string, RegExp, Handler][] = [
  ['GET', /^\/health$/, (): Health => ({
    status: 'ok',
    components: {
      db: { status: 'ok', timescaledb: 'mock' },
      redis: { status: 'ok' },
      worker: { status: 'ok', heartbeat_age_s: 1.2 },
      gateway: { status: 'ok', source: 'mock' },
      ml_service: { status: 'ok', models_loaded: 2, scoring: 'mock' },
    },
  })],
  ['GET', /^\/clock$/, ({ world }) => {
    const rs = world.activeRun()
    return rs ? { mode: 'stream', now: iso(rs.simNow) } : { mode: 'real', now: new Date().toISOString() }
  }],
  ['GET', /^\/objects$/, ({ world, rs }) => [objectNode(world, rs, world.topo.district)]],
  ['GET', /^\/objects\/(\d+)$/, ({ world, rs, params }): ObjectDetail => {
    const o = world.topo.byId.get(Number(params[0]))
    if (!o) throw notFound('Объект')
    const { children, ...node } = objectNode(world, rs, o)
    const path: ObjectDetail['path'] = []
    for (let p: MockObject | undefined = o; p; p = p.parent_id ? world.topo.byId.get(p.parent_id) : undefined)
      path.unshift({ id: p.id, name: p.name, level: p.level })
    return {
      ...node,
      path,
      sub_objects: children.map(({ children: _c, ...rest }) => rest),
      by_sensor_type: bySensorType(world, rs, channelsUnder(world, o.id)),
    }
  }],
  ['GET', /^\/channels$/, ({ world, rs, q }): Page<Channel> => {
    const obj = num(q, 'object_id')
    let cs = obj !== null ? channelsUnder(world, obj) : world.topo.channels
    const types = list(q, 'sensor_type')
    if (types.length) cs = cs.filter((c) => types.includes(c.sensor_type))
    const text = q.get('q')?.toLowerCase()
    if (text) cs = cs.filter((c) => c.name.toLowerCase().includes(text) || c.tag.includes(text) || String(c.id).includes(text))
    let out = cs.map((c) => toChannel(world, rs, c))
    const states = list(q, 'state')
    if (states.length) out = out.filter((c) => states.includes(c.status))
    const riskMin = num(q, 'risk_min')
    if (riskMin !== null) out = out.filter((c) => c.risk !== null && c.risk >= riskMin)
    if (q.get('sort') === '-risk') out.sort((a, b) => (b.risk ?? -1) - (a.risk ?? -1))
    return paginate(out, q)
  }],
  ['GET', /^\/channels\/(\d+)$/, (ctx): ChannelDetail => {
    const { world, rs, params } = ctx
    const c = world.topo.channelById.get(Number(params[0]))
    if (!c) throw notFound('Канал')
    const run = requireRun(ctx)
    const t = run.simNow
    const alarms = run.alarms.filter((a) => a.channel_id === c.id && t - parseIso(a.ts) < 30 * DAY)
    const faults = world.faultEpisodes(run, c.id)
    const preds = run.predictions.filter((p) => p.channel_id === c.id)
    const lastOf = (m: ModelName) => [...preds].reverse().find((p) => p.model === m) ?? null
    const rng = createRng(c.id + 1)
    const baseFaults = Math.round(c.noise * 30 * rng())
    return {
      ...toChannel(world, rs, c),
      counters: {
        alarms_30d: alarms.length + Math.round(c.noise * 6 * rng()),
        solitary_alarms_30d: alarms.filter((a) => a.episode?.kind === 'solitary').length,
        faults_365d: baseFaults + faults.length,
        fault_hours_30d: Math.round(faults.reduce((s, f) => s + ((f.end ?? t) - f.start), 0) / HOUR + baseFaults * rng() * 3),
        last_fault_at: faults.length ? iso(faults[faults.length - 1].start) : baseFaults ? iso(parseIso(run.run.sim_start) - randInt(rng, 2, 200) * DAY) : null,
      },
      last_alarm_prediction: lastOf('smoke_alarm'),
      last_daily_prediction: lastOf('daily'),
      open_work_orders: run.workOrders
        .filter((w) => w.channel_id === c.id && (w.status === 'new' || w.status === 'in_progress'))
        .map(plainWo),
      neighbors: (world.topo.channelsBySub.get(c.sub_object_id) ?? [])
        .filter((p) => p.id !== c.id && p.tag_group === c.tag_group)
        .slice(0, 40)
        .map((p) => ({ id: p.id, name: p.name, sensor_type: p.sensor_type, status: world.effective(run, p).status, picket_number: p.picket_number })),
    }
  }],
  ['GET', /^\/channels\/(\d+)\/timeline$/, timeline],
  ['GET', /^\/channels\/(\d+)\/predictions$/, (ctx) => {
    const q = new URLSearchParams(ctx.q)
    q.set('channel_id', ctx.params[0])
    const items = (ctx.rs?.predictions ?? []).filter((p) => predictionMatches(p, q, ctx.world)).reverse()
    return paginate(items, q)
  }],
  ['GET', /^\/graph$/, ({ world, rs }) => overviewGraph(world, rs)],
  ['GET', /^\/graph\/(\d+)$/, ({ world, rs, params }) => subObjectGraph(world, rs, Number(params[0]))],
  ['GET', /^\/alarms$/, alarms],
  ['GET', /^\/alarms\/(\d+)$/, alarmDetail],
  ['GET', /^\/predictions$/, (ctx) => {
    const items = (ctx.rs?.predictions ?? []).filter((p) => predictionMatches(p, ctx.q, ctx.world)).reverse()
    return paginate(items, ctx.q)
  }],
  ['GET', /^\/predictions\/(\d+)$/, (ctx) => {
    const p = requireRun(ctx).predictionById.get(Number(ctx.params[0]))
    if (!p) throw notFound('Прогноз')
    return p
  }],
  ['GET', /^\/metrics$/, metrics],
  // Stage 15: both event-model versions on the same offline decisions — the numbers of ml/models/compare_smoke_alarm.json
  // (lct-25, I half of 2026), point metrics rounded through `block`.
  ['GET', /^\/models\/compare$/, (): ModelCompare => {
    const counts = (decisions: number, positives: number, tp: number, fp: number) => ({
      decisions,
      positives,
      alerts: tp + fp,
      tp,
      fp,
      fn: positives - tp,
      precision: tp + fp ? tp / (tp + fp) : null,
      recall: positives ? tp / positives : null,
    })
    return {
      period: { from: '2026-01-01', to: '2026-06-30', label: 'I полугодие 2026, решения, допустимые для обеих моделей' },
      decisions: 6512,
      positives: 434,
      versions: { [V0]: { ...block(6512, 434, 0.8265, 0.7903, 139), ap: 0.8423 }, [V3]: { ...block(6512, 434, 0.8482, 0.7465, 139), ap: 0.7877 } },
      disagree: {
        decisions: 49,
        alert_only: { [V0]: 41, [V3]: 8 },
        right: { [V0]: 27, [V3]: 22 },
        alert_only_fault: { [V0]: 26, [V3]: 7 },
        by_kind: {
          group: { [V0]: { fault: 25, no_fault: 15 }, [V3]: { fault: 0, no_fault: 0 } },
          ppr: { [V0]: { fault: 0, no_fault: 0 }, [V3]: { fault: 5, no_fault: 1 } },
          solitary: { [V0]: { fault: 1, no_fault: 0 }, [V3]: { fault: 2, no_fault: 0 } },
        },
      },
      ci: {
        [V0]: { precision: [0.7636, 0.882], recall: [0.634, 0.9097], ap: [0.7268, 0.9131] },
        [V3]: { precision: [0.798, 0.9019], recall: [0.5719, 0.8775], ap: [0.662, 0.8711] },
      },
      diff: {
        a: V3,
        b: V0,
        precision: { value: 0.0217, ci90: [0.0012, 0.0538] },
        recall: { value: -0.0438, ci90: [-0.1171, 0.0036] },
        ap: { value: -0.0546, ci90: [-0.086, -0.0179] },
      },
      clusters: { object_days: 321, with_faults: 64 },
      by_kind: {
        group: { [V0]: counts(1378, 192, 182, 28), [V3]: counts(1378, 192, 157, 13) },
        ppr: { [V0]: counts(4905, 225, 148, 27), [V3]: counts(4905, 225, 153, 28) },
        solitary: { [V0]: counts(229, 17, 13, 17), [V3]: counts(229, 17, 14, 17) },
      },
      coverage: {
        version: V3,
        silent_days: 8,
        gap_days: ['2026-06-01'],
        from: '2026-06-02',
        to: '2026-06-08',
        decisions: 1051,
        positives: 103,
        others: { [V0]: { alerts: 105, tp: 99, fp: 6, fn: 4, precision: 0.9429, recall: 0.9612 } },
      },
      // The chain «v3 where it decided, else v0» on the half year (lct-25, fallback.json, #241).
      fallback: {"half_year": {"v0": {"decisions": 7563, "silent": 0, "positives": 537, "alerts": 520, "tp": 442, "fp": 78, "fn": 95, "precision": 0.85, "recall": 0.8231, "ci90": {"P": [0.7842, 0.902], "R": [0.6726, 0.9204]}}, "v3": {"decisions": 6512, "silent": 1051, "positives": 537, "alerts": 382, "tp": 324, "fp": 58, "fn": 213, "precision": 0.8482, "recall": 0.6034, "ci90": {"P": [0.7946, 0.9025], "R": [0.3846, 0.8251]}}, "combo": {"decisions": 7563, "silent": 0, "positives": 537, "alerts": 487, "tp": 423, "fp": 64, "fn": 114, "precision": 0.8686, "recall": 0.7877, "ci90": {"P": [0.8094, 0.9175], "R": [0.6232, 0.8951]}}, "combo_minus_v0": {"P": {"value": 0.0186, "ci90": [0.0017, 0.0468]}, "R": {"value": -0.0354, "ci90": [-0.095, 0.0039]}}, "combo_minus_v3": {"P": {"value": 0.0204, "ci90": [-0.0071, 0.0559]}, "R": {"value": 0.1844, "ci90": [0.0017, 0.4191]}}, "clusters": {"object_days": 344, "with_faults": 69}}},
    }
  }],
  ['GET', /^\/models$/, (): ModelsResponse => ({
    items: [
      {
        model: 'smoke_alarm',
        version: 'v3',
        family: 'v3',
        author: 'ML-разработчик',
        title: 'Событийная v3: неисправность после дымовой тревоги',
        loaded: true,
        scoring: true,
        // Stage 15: scores every alarm in the shadow of the primary baseline-v0.
        active: false,
        threshold: ALARM_THRESHOLD + 0.03,
        features: 227,
        trained_on: '2023–2024',
        horizon: '[T, T+24ч)',
        sensor_types: ['Датчик дыма'],
        artifact_sha256: '9c1f…mock',
        offline_metrics: null,
      },
      {
        model: 'smoke_alarm',
        version: 'baseline-v0',
        family: 'baseline',
        author: 'команда',
        title: 'Базовая модель команды: 38 признаков истории канала, соседей и шлейфа',
        loaded: true,
        scoring: true,
        // The chain is the primary of new runs (lct-95), its parts are not.
        active: false,
        // Numbers of ml/reports/baseline-v0/REPORT.md, check on the first half of 2026.
        threshold: 0.431,
        features: 38,
        trained_on: '2023–2024',
        horizon: '[T, T+24ч)',
        sensor_types: ['Датчик дыма'],
        artifact_sha256: '0d2ee22ffaacc3f066d40e9df6107a4713f367c00b86415a5b55fc02602d5bb3',
        offline_metrics: {
          period: 'I полугодие 2026',
          precision: 0.85,
          recall: 0.816,
          f1: 0.83,
          ap: 0.86,
          tp: 442,
          fp: 78,
          decisions: 8258,
          positives: 542,
        },
        offline: syntheticOffline('smoke_alarm'),
      },
      {
        // The owner's chain (lct-95): v3 where it decided, else v0.
        model: 'smoke_alarm',
        version: CHAIN,
        family: 'chain',
        author: 'ML-разработчик и команда',
        chain: [V3, V0],
        title: 'v3, где оценила, иначе v0',
        loaded: true,
        scoring: true,
        active: true,
        threshold: null,
        features: 0,
        trained_on: '',
        horizon: '[T, T+24ч)',
        sensor_types: ['Датчик дыма'],
        artifact_sha256: null,
        // compare.fallback.half_year.combo (#246, lct-25): the chain's own check, no ranking — AP is not defined.
        offline_metrics: {
          period: 'I полугодие 2026, общие решения и неделя после пропуска журнала',
          precision: 0.8686,
          recall: 0.7877,
          f1: 0.8262,
          ap: null,
          tp: 423,
          fp: 64,
          decisions: 7563,
          positives: 537,
        },
      },
      {
        // Task C: the team's daily v0 scores six types of the routing table; the ML developer's models — two more.
        model: 'daily',
        version: V0,
        family: 'baseline',
        author: 'команда',
        routes: DAILY_ROUTING.routes.filter((r) => r.model === V0).map((r) => r.sensor_type),
        title: 'Ежедневная базовая модель команды',
        loaded: true,
        scoring: true,
        active: true,
        threshold: 0.639,
        features: 41,
        trained_on: '2022–2024',
        horizon: '[T+24ч, T+30д)',
        sensor_types: DAILY_TYPES.filter((t) => DAILY_ROUTES[t] === V0),
        artifact_sha256: null,
        // v0 alone on the rows common with the routing (daily_routing.offline.v0_only).
        offline_metrics: {
          period: '1 января — 2 мая 2026',
          precision: 0.7768,
          recall: 0.5222,
          f1: 0.6245,
          ap: 0.6,
          tp: 39326,
          fp: 11302,
          decisions: 731642,
          positives: 75313,
        },
        offline: syntheticOffline('daily'),
      },
      ...EACH_CHANNEL.map(
        ([version, title, scoring, threshold]): ModelInfo => ({
          model: 'daily',
          version,
          family: 'each_channel',
          author: 'ML-разработчик',
          routes: DAILY_ROUTING.routes.filter((r) => r.model === version).map((r) => r.sensor_type),
          title,
          loaded: true,
          scoring,
          active: false,
          threshold,
          features: 0,
          trained_on: '2022–2025',
          horizon: '[T+24ч, T+30д)',
          sensor_types: [],
          artifact_sha256: null,
          offline_metrics: null,
        }),
      ),
      {
        model: 'magdeburg_fire',
        version: 'v0',
        family: 'external',
        author: null,
        title: 'Пожар против помехи на мультисенсорных узлах цеха',
        loaded: true,
        scoring: true,
        active: true,
        threshold: 0.5,
        features: 90,
        trained_on: '10–13.07.2023',
        horizon: 'now',
        sensor_types: [],
        artifact_sha256: '95e4…mock',
        offline_metrics: null,
        offline: syntheticExternalOffline(),
        dataset: 'magdeburg',
        synthetic: false,
        trigger: 'every_60s',
        eval_scheme: 'leave-one-experiment-out',
      },
    ],
    daily_routing: DAILY_ROUTING,
  })],
  ['GET', /^\/dashboard\/summary$/, (ctx): DashboardSummary => {
    const rs = ctx.rs
    const now = rs?.simNow ?? Date.now()
    const day = (rs?.alarms ?? []).filter((a) => now - parseIso(a.ts) < DAY)
    const hours: DashboardSummary['alarms_by_hour'] = []
    for (let h = 23; h >= 0; h--) {
      const start = Math.floor(now / HOUR) * HOUR - h * HOUR
      const inHour = day.filter((a) => parseIso(a.ts) >= start && parseIso(a.ts) < start + HOUR)
      const c = (v: string) => inHour.filter((a) => a.verdict === v).length
      hours.push({ hour: iso(start), ppr: c('ppr'), sensor_fault: c('sensor_fault'), reaction: c('reaction'), no_score: c('no_score') + c('pending') })
    }
    const all = aggregate(ctx.world, rs, ctx.world.topo.channels)
    const wos = rs?.workOrders ?? []
    const open = wos.filter((w) => w.status === 'new' || w.status === 'in_progress')
    return {
      now: iso(now),
      active_alarms: all.active_alarms,
      alarms_24h: day.length,
      ppr_share_24h: day.length ? day.filter((a) => a.verdict === 'ppr').length / day.length : null,
      alert_predictions_24h: (rs?.predictions ?? []).filter((p) => p.alert && now - parseIso(p.decision_time) < DAY).length,
      open_work_orders: open.length,
      overdue_work_orders: open.filter((w) => parseIso(`${w.due_date}T23:59:59+03:00`) < now).length,
      channels_total: all.channels,
      channels_in_fault: all.in_fault,
      channels_at_risk: all.at_risk,
      live: { smoke_alarm: livePR(ctx, 'smoke_alarm'), daily: livePR(ctx, 'daily') },
      alarms_by_hour: hours,
    }
  }],
  ['GET', /^\/dashboard\/risk$/, ({ world, rs }): DashboardRisk => {
    const top = world.topo.channels
      .map((c) => toChannel(world, rs, c))
      .filter((c) => c.risk !== null && c.status !== 'fault')
      .sort((a, b) => b.risk! - a.risk!)
      .slice(0, 20)
    return {
      top,
      by_object: world.topo.objects.map((o) => {
        const cs = channelsUnder(world, o.id)
        const a = aggregate(world, rs, cs)
        return {
          object_id: o.id,
          object_name: o.name,
          channels: a.channels,
          at_risk: a.at_risk,
          in_fault: a.in_fault,
          active_alarms: a.active_alarms,
          max_risk: a.max_risk,
          by_sensor_type: bySensorType(world, rs, cs),
        }
      }),
    }
  }],
  ['GET', /^\/work-orders$/, woList],
  ['GET', /^\/work-orders\/export\.csv$/, woCsv],
  ['POST', /^\/work-orders$/, (ctx) => {
    const rs = requireRun(ctx)
    const body = ctx.body as WorkOrderCreate
    const c = ctx.world.topo.channelById.get(body.channel_id)
    if (!c) throw new MockHttpError(422, 'validation_error', 'Канал не найден')
    const wo = ctx.world.addManualWorkOrder(rs, c, body)
    if (!wo) throw new MockHttpError(409, 'duplicate', 'По каналу уже есть открытая ручная заявка')
    return plainWo(wo)
  }],
  ['GET', /^\/work-orders\/(\d+)$/, (ctx): WorkOrderDetail => {
    const wo = requireRun(ctx).workOrders.find((w) => w.id === Number(ctx.params[0]))
    if (!wo) throw notFound('Заявка')
    return wo
  }],
  ['PATCH', /^\/work-orders\/(\d+)$/, (ctx): WorkOrderDetail => {
    const rs = requireRun(ctx)
    const wo = rs.workOrders.find((w) => w.id === Number(ctx.params[0]))
    if (!wo) throw notFound('Заявка')
    const patch = ctx.body as WorkOrderPatch
    Object.assign(wo, Object.fromEntries(Object.entries(patch).filter(([, v]) => v !== undefined)))
    wo.updated_at = iso(rs.simNow)
    wo.closed_at = wo.status === 'done' || wo.status === 'rejected' ? wo.updated_at : null
    wo.history.push({ ts: wo.updated_at, status: wo.status, comment: patch.comment ?? null, assignee: wo.assignee })
    return wo
  }],
  ['GET', /^\/equipment-episodes$/, (ctx): EquipmentPage => {
    const rs = requireRun(ctx)
    const q = ctx.q
    const from = q.get('from') ? parseIso(q.get('from')!) : null
    const to = q.get('to') ? parseIso(q.get('to')!) : null
    const obj = num(q, 'object_id')
    const active = q.get('active')
    const kinds = list(q, 'kind')
    // Only episodes that reached the threshold exist for the API.
    const base = rs.equipment
      .filter((e) => e.created)
      .map((e) => e.ep)
      .filter((e) => {
        if (from !== null && e.end_ts !== null && parseIso(e.end_ts) < from) return false
        if (to !== null && parseIso(e.start_ts) >= to) return false
        if (obj !== null && e.object_id !== obj && !e.sub_objects.some((x) => x.id === obj)) return false
        if (active && String(e.end_ts === null) !== active) return false
        return true
      })
      .sort((a, b) => parseIso(b.start_ts) - parseIso(a.start_ts))
    const items = base.filter((e) => !kinds.length || kinds.includes(e.kind))
    return {
      ...paginate(items.map((e) => ({ ...e })), q),
      counts: {
        active: base.filter((e) => e.end_ts === null).length,
        power_outage: base.filter((e) => e.kind === 'power_outage').length,
        equipment_fault: base.filter((e) => e.kind === 'equipment_fault').length,
      },
    }
  }],
  ['GET', /^\/equipment-episodes\/(\d+)$/, (ctx): EquipmentEpisodeDetail => {
    const eq = requireRun(ctx).equipment.find((e) => e.created && e.ep.id === Number(ctx.params[0]))
    if (!eq) throw notFound('Эпизод оборудования')
    return { ...eq.ep, members: eq.members.map((m) => ({ ...m })) }
  }],
  ['GET', /^\/access-incidents$/, (ctx): AccessIncidentPage => {
    const rs = requireRun(ctx)
    const q = ctx.q
    const from = q.get('from') ? parseIso(q.get('from')!) : null
    const to = q.get('to') ? parseIso(q.get('to')!) : null
    const obj = num(q, 'object_id')
    const statuses = list(q, 'status')
    const priorities = list(q, 'priority')
    // Candidates never reach the API; `dismissed` only when asked for. Counts — the same filters without the status.
    const base = rs.access
      .map((a) => a.inc)
      .filter((i) => {
        if (i.status === 'candidate') return false
        if (from !== null && i.end_ts !== null && parseIso(i.end_ts) < from) return false
        if (to !== null && parseIso(i.start_ts) >= to) return false
        if (obj !== null && i.object_id !== obj && i.sub_object_id !== obj) return false
        if (priorities.length && (!i.priority || !priorities.includes(i.priority))) return false
        return true
      })
      .sort((a, b) => parseIso(b.start_ts) - parseIso(a.start_ts))
    const shown = statuses.length ? statuses : ['open', 'closed']
    return {
      ...paginate(base.filter((i) => shown.includes(i.status)).map((i) => ({ ...i })), q),
      counts: {
        open: base.filter((i) => i.status === 'open').length,
        closed: base.filter((i) => i.status === 'closed').length,
        dismissed: base.filter((i) => i.status === 'dismissed').length,
      },
    }
  }],
  ['GET', /^\/access-incidents\/(\d+)$/, (ctx): AccessIncidentDetail => {
    const a = requireRun(ctx).access.find((x) => x.inc.status !== 'candidate' && x.inc.id === Number(ctx.params[0]))
    if (!a) throw new MockHttpError(404, 'access_incident_not_found', 'Инцидента нет')
    return { ...a.inc, opening_events: a.openings.map((o) => ({ ...o })), guard_events: a.guard.map((g) => ({ ...g })) }
  }],
  ['PATCH', /^\/access-incidents\/(\d+)$/, (ctx) => {
    const inc = ctx.world.reviewAccess(requireRun(ctx), Number(ctx.params[0]), ctx.body as AccessIncidentPatch)
    if (!inc) throw new MockHttpError(404, 'access_incident_not_found', 'Инцидента нет')
    return inc
  }],
  ['GET', /^\/datasets$/, ({ world }) => ({ items: DATASETS.map((d) => datasetOut(world, d)) })],
  ['GET', /^\/emulator\/scenarios$/, () => ({ items: SCENARIOS })],
  ['GET', /^\/scenario-uploads\/(\w+)$/, ({ params }) => getUpload(params[0])],
  ['PUT', /^\/scenario-uploads\/(\w+)$/, ({ params, body }) => putUpload(params[0], body as ScenarioUploadPut)],
  ['GET', /^\/emulator\/scenarios\/([\w-]+)\/summary$/, ({ world, params }) => {
    const sc = SCENARIOS.find((x) => x.id === params[0])
    if (!sc) throw new MockHttpError(404, 'scenario_not_found', 'Нет такого сценария')
    return scenarioSummary(sc, world.topo)
  }],
  ['GET', /^\/emulator\/scenarios\/([\w-]+)\/events$/, ({ world, params, q }) => {
    const sc = SCENARIOS.find((x) => x.id === params[0])
    if (!sc) throw new MockHttpError(404, 'scenario_not_found', 'Нет такого сценария')
    return scenarioEvents(sc, world.topo, q)
  }],
  ['GET', /^\/emulator\/state$/, ({ world }) => world.emulatorState()],
  ['POST', /^\/emulator\/start$/, ({ world, body }) => {
    const { scenario, speed, primary_model } = body as { scenario: string; speed?: number; primary_model?: string }
    if (primary_model && !PRIMARY_CHOICES.includes(primary_model)) throw new MockHttpError(422, 'unknown_version', 'Такой версии событийной модели нет')
    const sc = SCENARIOS.find((s) => s.id === scenario)
    if (!sc) throw new MockHttpError(404, 'scenario_not_found', 'Нет такого сценария')
    if (!sc.ready) throw new MockHttpError(409, 'slice_not_ready', 'Срез сценария не подготовлен: make emulator-slices')
    const v = speed ?? sc.default_speed
    if (v < 1 || v > sc.max_speed) throw new MockHttpError(422, 'speed_out_of_range', `Скорость от 1 до ${sc.max_speed}`)
    world.startScenario(scenario, v, primary_model)
    return world.emulatorState()
  }],
  ['POST', /^\/emulator\/(pause|resume|stop)$/, ({ world, params }) => {
    world.setState(params[0] as 'pause' | 'resume' | 'stop')
    return world.emulatorState()
  }],
  ['POST', /^\/emulator\/speed$/, ({ world, body }) => {
    const { speed } = body as { speed: number }
    const max = world.emulatorState().max_speed
    if (!(speed >= 1 && speed <= max)) throw new MockHttpError(422, 'speed_out_of_range', `Скорость от 1 до ${max}`)
    const rs = world.activeRun()
    if (rs) rs.run.speed = speed
    world.targetSpeed = speed
    return world.emulatorState()
  }],
  ['GET', /^\/runs$/, ({ world }) => ({ items: [...world.runs.values()].map((rs) => runOut(world, rs)).reverse() })],
  ['GET', /^\/runs\/(\d+)$/, ({ world, params }) => {
    const rs = world.runs.get(Number(params[0]))
    if (!rs) throw notFound('Прогон')
    return runOut(world, rs)
  }],
  ['PATCH', /^\/runs\/(\d+)$/, ({ world, params, body }) => {
    const rs = world.runs.get(Number(params[0]))
    if (!rs) throw notFound('Прогон')
    const version = (body as { primary_model?: string }).primary_model
    if (!version || !PRIMARY_CHOICES.includes(version)) throw new MockHttpError(422, 'unknown_version', 'Такой версии событийной модели нет или она не скорит')
    if (rs.run.status === 'finished' || rs.run.status === 'stopped') throw new MockHttpError(409, 'run_finished', 'Прогон завершён — менять основную модель незачем')
    const { reverdict } = world.setPrimary(rs.run.id, version, (body as { apply_to_past?: boolean }).apply_to_past === true)!
    return { ...runOut(world, rs), reverdict }
  }],
  ['GET', /^\/runs\/(\d+)\/audit$/, ({ world, params }) => {
    const rs = world.runs.get(Number(params[0]))
    if (!rs) throw notFound('Прогон')
    return { items: rs.audit }
  }],
]

const ctxFor = (world: MockWorld, rs: RunState): Ctx => ({ world, rs, q: new URLSearchParams(), params: [], body: null })

function runOut(world: MockWorld, rs: RunState): Run {
  const ctx = ctxFor(world, rs)
  return {
    ...rs.run,
    live: {
      smoke_alarm: livePR(ctx, 'smoke_alarm'),
      daily: livePR(ctx, 'daily'),
      external: {},
      versions: Object.fromEntries(EVENT_VERSIONS.map((v) => [v, livePR(ctx, 'smoke_alarm', v)])),
    },
  }
}

export function handle(method: string, path: string, q: URLSearchParams, body: unknown): unknown {
  const world = getWorld()
  const runParam = q.get('run')
  const rs = world.runState(runParam ? Number(runParam) : null)
  for (const [m, re, handler] of routes) {
    if (m !== method) continue
    const match = re.exec(path)
    if (match) return handler({ world, rs, q, params: match.slice(1), body })
  }
  throw new MockHttpError(404, 'not_found', `Нет мок-обработчика для ${method} ${path}`)
}
