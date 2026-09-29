// Mock of what a scenario will transmit (13.2): synthetic signals of the scenario's objects over its period, the
// summary counted from them and key moments. Deterministic per scenario; no hackathon data.

import type { Scenario, ScenarioEvent, ScenarioMoment, ScenarioSummary } from '@/lib/api/types'

import { createRng, lognormal, pick, randInt, shuffle, type Rng } from './random'
import { normalValue, SUBSYSTEM_OF, type MockChannel, type Topology } from './topology'
import { SCENARIOS, iso, parseIso } from './world'

const MIN = 60_000
const NUMERIC = new Set(['Газовый датчик', 'Датчик температуры'])
const POWER = new Set(['Состояние фазы', 'Состояние насоса', 'Состояние вентилятора'])

type Raw = { t: number; c: MockChannel; value: string; alarm: boolean }
type Data = { events: ScenarioEvent[]; moments: ScenarioMoment[] }

const cache = new Map<string, Data>()
const unknownChannels = new Map<string, number>()

/** Moments of an uploaded journal: the first and last signal and waves of one state over several channels. */
function uploadedMoments(events: ScenarioEvent[]): ScenarioMoment[] {
  const out: ScenarioMoment[] = []
  const byValue = new Map<string, ScenarioEvent[]>()
  for (const e of events) if (['Обесточен', 'Неисправен', 'Отключено устройство', 'Обнаружен дым'].includes(e.value)) byValue.set(e.value, [...(byValue.get(e.value) ?? []), e])
  for (const [value, list] of byValue) {
    for (let i = 0; i < list.length; ) {
      let j = i
      while (j + 1 < list.length && parseIso(list[j + 1].ts) - parseIso(list[i].ts) <= 5 * MIN) j++
      const group = list.slice(i, j + 1)
      const chans = [...new Set(group.map((e) => e.channel_id))]
      if (chans.length >= 5) {
        out.push(
          moment({
            ts: group[0].ts,
            until: group[group.length - 1].ts,
            kind: value === 'Обнаружен дым' ? 'smoke_group' : 'state_wave',
            state: value === 'Обнаружен дым' ? null : value,
            title: `${group[0].ts.slice(11, 16)} — ${chans.length} каналов объекта ${group[0].object_name}: «${value}» за ${Math.max(1, Math.round((parseIso(group[group.length - 1].ts) - parseIso(group[0].ts)) / MIN))} мин`,
            object_id: group[0].object_id,
            channel_ids: chans,
            events: group.length,
          }),
        )
      }
      i = j + 1
    }
  }
  if (events.length) {
    out.push(moment({ ts: events[0].ts, kind: 'first_event', title: `${events[0].ts.slice(11, 16)} — первое событие: ${events[0].channel_name}` }))
    const last = events[events.length - 1]
    out.push(moment({ ts: last.ts, kind: 'last_event', title: `${last.ts.slice(11, 16)} — последнее событие: ${last.channel_name}` }))
  }
  return out.sort((a, b) => parseIso(a.ts) - parseIso(b.ts)).slice(0, 30)
}

/** A scenario made from an uploaded journal (13.1): in the list, with the file's signals of known channels. */
export function registerUploaded(sc: Scenario, events: ScenarioEvent[], unknown: number) {
  SCENARIOS.push(sc)
  cache.set(sc.id, { events, moments: uploadedMoments(events) })
  unknownChannels.set(sc.id, unknown)
}

function channelsOf(sc: Scenario, topo: Topology, rng: Rng): MockChannel[] {
  const ids = sc.objects.length ? sc.objects.map((o) => o.id) : shuffle(rng, topo.objects.map((o) => o.id)).slice(0, 3)
  return ids.flatMap((id) => topo.channelsByObject.get(id) ?? [])
}

function moment(m: Partial<ScenarioMoment> & Pick<ScenarioMoment, 'ts' | 'kind' | 'title'>): ScenarioMoment {
  return { until: null, state: null, detail: null, object_id: null, sub_object_id: null, channel_id: null, channel_ids: [], events: null, ...m }
}

const hhmm = (t: number) => iso(t).slice(11, 16)

function generate(sc: Scenario, topo: Topology): Data {
  const rng = createRng([...sc.id].reduce((h, ch) => (h * 31 + ch.charCodeAt(0)) >>> 0, 7))
  const start = parseIso(sc.period_start)
  const end = parseIso(sc.period_end)
  const span = end - start
  const channels = channelsOf(sc, topo, rng)
  const raw: Raw[] = []
  const moments: ScenarioMoment[] = []
  const objName = (id: number) => topo.byId.get(id)?.name ?? `объект ${id}`

  // Background: numeric sensors report every few minutes, the rest now and then.
  for (const c of channels) {
    const every = NUMERIC.has(c.sensor_type) ? 6 * MIN : c.sensor_type === 'Датчик дыма' ? 4 * 60 * MIN : 90 * MIN
    for (let t = start + rng() * every; t < end; t += lognormal(rng, every, 0.5)) raw.push({ t, c, value: normalValue(c.sensor_type, rng), alarm: false })
  }

  const smoke = channels.filter((c) => c.sensor_type === 'Датчик дыма')
  if (sc.id === 'ppr_check' && smoke.length) {
    // A planned check: detectors of one object fire one after another, the lever is pulled.
    const at = start + Math.min(span / 4, 64 * MIN)
    const checked = shuffle(rng, smoke).slice(0, Math.min(40, smoke.length))
    checked.forEach((c, i) => {
      const t = at + i * randInt(rng, 60, 200) * 1000
      raw.push({ t, c, value: 'Обнаружен дым', alarm: true }, { t: t + randInt(rng, 40, 180) * 1000, c, value: 'Дыма нет', alarm: false })
    })
    const lever = channels.find((c) => c.sensor_type === 'Состояние УИР-Р')
    if (lever) raw.push({ t: at + 12 * MIN, c: lever, value: 'Рычаг сдернут', alarm: true })
    const last = at + (checked.length - 1) * 130_000
    moments.push(
      moment({
        ts: iso(at),
        until: iso(last),
        kind: 'smoke_group',
        title: `${hhmm(at)} — ${checked.length} дымовых датчиков объекта ${objName(checked[0].object_id)} срабатывают по очереди`,
        detail: `С ${hhmm(at)} до ${hhmm(last)}, по одному раз в 1–3 минуты.`,
        object_id: checked[0].object_id,
        channel_ids: checked.map((c) => c.id),
        events: checked.length * 2,
      }),
    )
    if (lever) moments.push(moment({ ts: iso(at + 12 * MIN), kind: 'lever', title: `${hhmm(at + 12 * MIN)} — «Рычаг сдернут» на ${lever.name}`, object_id: lever.object_id, channel_id: lever.id, channel_ids: [lever.id], events: 1 }))
  }
  if (sc.id === 'noisy_detector' && smoke.length) {
    // One detector fires five times in three minutes, then goes «Неисправен».
    const c = pick(rng, smoke)
    const at = start + Math.min(span / 3, 56 * MIN)
    for (let i = 0; i < 5; i++) raw.push({ t: at + i * 40_000, c, value: 'Обнаружен дым', alarm: true }, { t: at + i * 40_000 + 20_000, c, value: 'Дыма нет', alarm: false })
    raw.push({ t: at + 2 * 60 * MIN, c, value: 'Неисправен', alarm: false })
    moments.push(
      moment({ ts: iso(at), until: iso(at + 180_000), kind: 'smoke_single', title: `${hhmm(at)} — ${c.name}: 5 срабатываний за 3 минуты, соседи молчат`, object_id: c.object_id, sub_object_id: c.sub_object_id, channel_id: c.id, channel_ids: [c.id], events: 10 }),
      moment({ ts: iso(at + 2 * 60 * MIN), kind: 'state_wave', state: 'Неисправен', title: `${hhmm(at + 2 * 60 * MIN)} — ${c.name} переходит в «Неисправен»`, object_id: c.object_id, channel_id: c.id, channel_ids: [c.id], events: 1 }),
    )
  }
  if (sc.id === 'power_loss') {
    // An outage: phases, pumps and fans of the object go «Обесточен» within a minute, most come back later.
    const at = start + Math.min(span / 3, 62 * MIN)
    const down = channels.filter((c) => POWER.has(c.sensor_type) && rng() < 0.8)
    let back = 0
    for (const [i, c] of down.entries()) {
      const t = at + Math.round((i / down.length) * 60_000)
      raw.push({ t, c, value: 'Обесточен', alarm: true })
      if (rng() < 0.8) {
        back++
        raw.push({ t: t + (10 + rng() * 20) * MIN, c, value: normalValue(c.sensor_type, rng), alarm: false })
      }
    }
    if (down.length) {
      moments.push(
        moment({
          ts: iso(at),
          until: iso(at + 60_000),
          kind: 'state_wave',
          state: 'Обесточен',
          title: `${hhmm(at)} — ${down.length} каналов объекта ${objName(down[0].object_id)} перешли в «Обесточен» за 1 мин`,
          object_id: down[0].object_id,
          channel_ids: down.map((c) => c.id),
          events: down.length,
        }),
        moment({
          ts: iso(at + 10 * MIN),
          until: iso(at + 30 * MIN),
          kind: 'state_return',
          state: 'Обесточен',
          title: `${hhmm(at + 10 * MIN)}–${hhmm(at + 30 * MIN)} — ${back} из ${down.length} каналов вернулись из «Обесточен»`,
          object_id: down[0].object_id,
          events: back,
        }),
      )
    }
  }
  if (sc.id === 'ordinary_day' && smoke.length) {
    for (let i = 0; i < 4; i++) {
      const c = pick(rng, smoke)
      const t = start + rng() * span
      raw.push({ t, c, value: 'Обнаружен дым', alarm: true }, { t: t + 90_000, c, value: 'Дыма нет', alarm: false })
      moments.push(moment({ ts: iso(t), kind: 'smoke_single', title: `${hhmm(t)} — ${c.name}: одиночная тревога, соседи молчат`, object_id: c.object_id, sub_object_id: c.sub_object_id, channel_id: c.id, channel_ids: [c.id], events: 2 }))
    }
  }

  raw.sort((a, b) => a.t - b.t)
  const events: ScenarioEvent[] = raw
    .filter((r) => r.t >= start && r.t < end)
    .map((r, i) => ({
      seq: i + 1,
      ts: iso(r.t),
      event_id: 4_500_000_000 + i * 7,
      channel_id: r.c.id,
      channel_name: r.c.name,
      sensor_type: r.c.sensor_type,
      object_id: r.c.object_id,
      object_name: objName(r.c.object_id),
      sub_object_id: r.c.sub_object_id,
      sub_object_name: topo.byId.get(r.c.sub_object_id)?.name ?? '',
      value: r.value,
      is_alarm: r.alarm,
    }))
  if (events.length) {
    moments.push(moment({ ts: events[0].ts, kind: 'first_event', title: `${events[0].ts.slice(11, 16)} — первое событие: ${events[0].channel_name}` }))
    const last = events[events.length - 1]
    moments.push(moment({ ts: last.ts, kind: 'last_event', title: `${last.ts.slice(11, 16)} — последнее событие: ${last.channel_name}` }))
  }
  moments.sort((a, b) => parseIso(a.ts) - parseIso(b.ts))
  return { events, moments }
}

function data(sc: Scenario, topo: Topology): Data {
  let d = cache.get(sc.id)
  if (!d) cache.set(sc.id, (d = generate(sc, topo)))
  return d
}

export function scenarioSummary(sc: Scenario, topo: Topology): ScenarioSummary {
  const { events, moments } = data(sc, topo)
  const start = parseIso(sc.period_start)
  const end = parseIso(sc.period_end)
  const count = <K>(key: (e: ScenarioEvent) => K) => {
    const m = new Map<K, { channels: Set<number>; events: number; alarm: number }>()
    for (const e of events) {
      const k = key(e)
      const v = m.get(k) ?? { channels: new Set<number>(), events: 0, alarm: 0 }
      v.channels.add(e.channel_id)
      v.events++
      if (e.is_alarm) v.alarm++
      m.set(k, v)
    }
    return [...m.entries()].sort((a, b) => b[1].events - a[1].events)
  }
  const bySub = new Map(count((e) => e.sub_object_id))
  const subsOf = (objectId: number) =>
    [...new Set(events.filter((e) => e.object_id === objectId).map((e) => e.sub_object_id))].map((id) => ({
      id,
      name: events.find((e) => e.sub_object_id === id)!.sub_object_name,
      channels: bySub.get(id)!.channels.size,
      events: bySub.get(id)!.events,
    }))
  const values = count((e) => (Number.isFinite(Number(e.value)) ? 'числовые показания' : e.value))
  const byValue = values.slice(0, 20).map(([value, v]) => ({ value, events: v.events, alarm_events: v.alarm, numeric: value === 'числовые показания' }))
  const rest = values.slice(20)
  if (rest.length)
    byValue.push({ value: 'прочие', events: rest.reduce((n, [, v]) => n + v.events, 0), alarm_events: rest.reduce((n, [, v]) => n + v.alarm, 0), numeric: false })

  // Density: about 150 buckets, a whole number of minutes each.
  const bucket = Math.max(60, Math.ceil((end - start) / 150 / MIN) * 60) * 1000
  const buckets = new Map<number, { events: number; alarm: number }>()
  const minutes = new Map<number, number>()
  for (const e of events) {
    const t = parseIso(e.ts)
    const b = start + Math.floor((t - start) / bucket) * bucket
    const v = buckets.get(b) ?? { events: 0, alarm: 0 }
    v.events++
    if (e.is_alarm) v.alarm++
    buckets.set(b, v)
    const m = Math.floor(t / MIN)
    minutes.set(m, (minutes.get(m) ?? 0) + 1)
  }
  const items = []
  for (let b = start; b < end; b += bucket) items.push({ ts: iso(b), events: buckets.get(b)?.events ?? 0, alarm_events: buckets.get(b)?.alarm ?? 0 })

  return {
    scenario_id: sc.id,
    period_start: sc.period_start,
    period_end: sc.period_end,
    events: events.length,
    alarm_events: events.filter((e) => e.is_alarm).length,
    channels: new Set(events.map((e) => e.channel_id)).size,
    unknown_channels: unknownChannels.get(sc.id) ?? 0,
    objects: count((e) => e.object_id).map(([id, v]) => ({ id, name: events.find((e) => e.object_id === id)!.object_name, channels: v.channels.size, events: v.events, sub_objects: subsOf(id) })),
    by_type: count((e) => e.sensor_type).map(([sensor_type, v]) => ({ sensor_type, channels: v.channels.size, events: v.events })),
    by_subsystem: count((e) => SUBSYSTEM_OF[e.sensor_type]).map(([subsystem, v]) => ({ subsystem, channels: v.channels.size, events: v.events })),
    by_value: byValue,
    density: {
      bucket_seconds: bucket / 1000,
      peak_minute_events: Math.max(0, ...minutes.values()),
      avg_per_second: Math.round((events.length / Math.max(1, (end - start) / 1000)) * 100) / 100,
      items,
    },
    moments,
  }
}

/** GET /emulator/scenarios/{id}/events: filters of the contract, the cursor is the next position. */
export function scenarioEvents(sc: Scenario, topo: Topology, q: URLSearchParams) {
  const { events } = data(sc, topo)
  const list = (k: string) => q.getAll(k).flatMap((v) => v.split(',')).filter(Boolean)
  const from = q.get('from') ? parseIso(q.get('from')!) : null
  const to = q.get('to') ? parseIso(q.get('to')!) : null
  const obj = q.get('object_id') ? Number(q.get('object_id')) : null
  const chans = list('channel_id').map(Number)
  const types = list('sensor_type')
  const values = list('value')
  const alarm = q.get('alarm')
  const text = q.get('q')?.toLowerCase() ?? ''
  const filtered = events.filter((e) => {
    const t = parseIso(e.ts)
    if (from !== null && t < from) return false
    if (to !== null && t >= to) return false
    if (obj !== null && e.object_id !== obj && e.sub_object_id !== obj) return false
    if (chans.length && !chans.includes(e.channel_id)) return false
    if (types.length && !types.includes(e.sensor_type)) return false
    if (values.length && !values.includes(e.value) && !(values.includes('числовые показания') && Number.isFinite(Number(e.value)))) return false
    if (alarm && String(e.is_alarm) !== alarm) return false
    if (text && !e.channel_name.toLowerCase().includes(text)) return false
    return true
  })
  const at = Number(q.get('cursor') ?? 0)
  const limit = Math.min(1000, Number(q.get('limit') ?? 200))
  const items = filtered.slice(at, at + limit)
  return { items, next_cursor: at + limit < filtered.length ? String(at + limit) : null, total: filtered.length }
}
