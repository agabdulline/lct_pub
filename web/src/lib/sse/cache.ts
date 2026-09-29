// How stream events change the TanStack Query cache: point updates where the shape is known,
// throttled invalidation for aggregates (counters, metrics, graph aggregates).

import type { QueryClient, QueryKey } from '@tanstack/react-query'

import type { QueryParams } from '@/lib/api/client'
import type {
  AccessIncident,
  AccessIncidentDetail,
  AccessIncidentPage,
  Alarm,
  AlarmPage,
  ChannelDetail,
  ClockLive,
  Prediction,
  StreamEventMap,
  StreamEventType,
} from '@/lib/api/types'

const asList = (v: unknown): string[] =>
  v === undefined || v === null || v === '' ? [] : Array.isArray(v) ? v.map(String) : String(v).split(',')

export function alarmMatches(alarm: Alarm, params: QueryParams): boolean {
  const verdicts = asList(params.verdict)
  if (verdicts.length && !verdicts.includes(alarm.verdict)) return false
  const kinds = asList(params.kind)
  if (kinds.length && !kinds.includes(alarm.episode?.kind ?? 'solitary')) return false
  if (params.object_id != null && params.object_id !== '') {
    const id = Number(params.object_id)
    if (alarm.object_id !== id && alarm.sub_object_id !== id) return false
  }
  if (params.channel_id != null && params.channel_id !== '' && alarm.channel_id !== Number(params.channel_id)) return false
  if (typeof params.from === 'string' && new Date(alarm.ts) < new Date(params.from)) return false
  if (typeof params.to === 'string' && new Date(alarm.ts) >= new Date(params.to)) return false
  // «ППР: вероятно не пройдут проверку» (`alert`) and «Модели расходятся» (`disagree`, stage 15): without these a
  // streamed alarm is put into the filtered page and drops out only on the next refetch.
  if (params.alert != null && params.alert !== '' && alarm.prediction?.alert !== (String(params.alert) === 'true')) return false
  if (String(params.disagree) === 'true' && alarm.disagree !== true) return false
  return true
}

/** Inserts or replaces an alarm in every cached feed page of the run. */
function upsertAlarm(qc: QueryClient, runKey: number | 'base', alarm: Alarm) {
  for (const [key, page] of qc.getQueriesData<AlarmPage>({ queryKey: ['alarms', runKey] })) {
    if (!page) continue
    const params = ((key as QueryKey)[3] ?? {}) as QueryParams
    const i = page.items.findIndex((a) => a.id === alarm.id)
    const matches = alarmMatches(alarm, params)
    let items = page.items
    let total = page.total
    if (i >= 0 && matches) items = page.items.map((a) => (a.id === alarm.id ? alarm : a))
    else if (i >= 0) {
      items = page.items.filter((a) => a.id !== alarm.id)
      total--
    } else if (matches && Number(params.page ?? 1) === 1 && !params.to) {
      items = [alarm, ...page.items].slice(0, page.size)
      total++
    } else continue
    qc.setQueryData<AlarmPage>(key, { ...page, items, total })
  }
  qc.setQueriesData<Alarm>({ queryKey: ['alarm', runKey, `/alarms/${alarm.id}`] }, (old) => (old ? { ...old, ...alarm } : old))
}

/** Whether an access incident belongs to a cached `/access-incidents` query: status, object, priority, period. */
export function accessMatches(inc: AccessIncident, params: QueryParams): boolean {
  const statuses = asList(params.status)
  if (!(statuses.length ? statuses : ['open', 'closed']).includes(inc.status)) return false
  const priorities = asList(params.priority)
  if (priorities.length && (!inc.priority || !priorities.includes(inc.priority))) return false
  if (params.object_id != null && params.object_id !== '') {
    const id = Number(params.object_id)
    if (inc.object_id !== id && inc.sub_object_id !== id) return false
  }
  // The incident intersects [from, to).
  if (typeof params.from === 'string' && inc.end_ts !== null && new Date(inc.end_ts) < new Date(params.from)) return false
  if (typeof params.to === 'string' && new Date(inc.start_ts) >= new Date(params.to)) return false
  return true
}

/**
 * An incident from the stream goes into every cached list of the run at once — the feed row comes with the toast,
 * not 3 s later. Counts are adjusted where the previous state is known; the throttled refetch settles the rest.
 */
function upsertAccess(qc: QueryClient, runKey: number | 'base', inc: AccessIncident) {
  for (const [key, page] of qc.getQueriesData<AccessIncidentPage>({ queryKey: ['access-incidents', runKey] })) {
    if (!page) continue
    const params = ((key as QueryKey)[3] ?? {}) as QueryParams
    const prev = page.items.find((x) => x.id === inc.id)
    const matches = accessMatches(inc, params)
    const counts = { ...page.counts }
    let items = page.items
    let total = page.total
    if (prev) {
      if (prev.status !== inc.status && prev.status in counts && inc.status in counts) {
        counts[prev.status as keyof typeof counts]--
        counts[inc.status as keyof typeof counts]++
      }
      if (matches) items = page.items.map((x) => (x.id === inc.id ? inc : x))
      else {
        items = page.items.filter((x) => x.id !== inc.id)
        total--
      }
    } else if (matches && Number(params.page ?? 1) === 1 && !params.to) {
      items = [inc, ...page.items].slice(0, page.size)
      total++
      if (inc.status in counts) counts[inc.status as keyof typeof counts]++
    } else continue
    qc.setQueryData<AccessIncidentPage>(key, { ...page, items, total, counts })
  }
}

export class StreamCache {
  private pending = new Set<string>()
  private timer: ReturnType<typeof setTimeout> | null = null
  private readonly qc: QueryClient
  private readonly runKey: number | 'base'

  constructor(qc: QueryClient, runKey: number | 'base') {
    this.qc = qc
    this.runKey = runKey
  }

  /** Refetch resources at most every few seconds: a planned check brings dozens of alarms at once. */
  private invalidate(...resources: string[]) {
    resources.forEach((r) => this.pending.add(r))
    if (this.timer) return
    this.timer = setTimeout(() => {
      this.timer = null
      const resources = [...this.pending]
      this.pending.clear()
      for (const r of resources) this.qc.invalidateQueries({ queryKey: [r, this.runKey] })
    }, 3000)
  }

  dispose() {
    if (this.timer) clearTimeout(this.timer)
  }

  /**
   * The open channel panel or card must show a new decision at once: refetch that channel's queries now,
   * not after the 3 s throttle (the order toast would come first and the panel would still say «нет решений»).
   */
  private invalidateChannel(id: number) {
    this.qc.invalidateQueries({
      predicate: (q) => {
        const [resource, run, path, params] = q.queryKey as [string, unknown, string | undefined, QueryParams | undefined]
        if (run !== this.runKey) return false
        if (resource === 'channel') return path === `/channels/${id}`
        if (resource === 'channel-predictions') return path === `/channels/${id}/predictions`
        return resource === 'predictions' && Number(params?.channel_id) === id
      },
    })
  }

  apply<T extends StreamEventType>(type: T, data: StreamEventMap[T]) {
    switch (type) {
      case 'run.updated':
        // The run list is not run-scoped: the switcher, the header and the runs table read it. A switch is written
        // to the run's audit as well.
        void this.qc.invalidateQueries({ queryKey: ['runs'] })
        void this.qc.invalidateQueries({ queryKey: ['run-audit', (data as StreamEventMap['run.updated']).id] })
        break
      case 'clock.tick': {
        // Only the clock is written. A cache write restarts the observers' refetchInterval: writing the polled
        // emulator state every second starved its 3 s poll and froze «Отправлено событий» at the value from
        // page load. useEmulatorState merges the tick into the polled state instead.
        const tick = data as StreamEventMap['clock.tick']
        this.qc.setQueryData<ClockLive>(['clock'], {
          mode: tick.mode,
          now: tick.now,
          run_id: tick.run_id,
          effective_speed: tick.effective_speed,
          state: tick.state,
        })
        break
      }
      case 'alarm.created':
        upsertAlarm(this.qc, this.runKey, data as Alarm)
        this.invalidate('alarms', 'dashboard-summary', 'objects')
        break
      case 'alarm.scored':
        upsertAlarm(this.qc, this.runKey, data as Alarm)
        this.invalidate('alarms', 'dashboard-summary', 'objects')
        // A scored alarm carries the event model's decision (prediction.created comes right before it).
        if ((data as Alarm).prediction) {
          this.invalidateChannel((data as Alarm).channel_id)
          this.invalidate('predictions', 'metrics', 'channel-predictions', 'channel')
        }
        break
      case 'alarm_episode.updated':
        this.invalidate('alarms')
        break
      case 'prediction.created':
        this.invalidateChannel((data as Prediction).channel_id)
        this.invalidate('predictions', 'metrics', 'channel-predictions', 'channel', 'dashboard-summary')
        break
      case 'prediction.updated': {
        // The backend closes outcomes on its tick (every 15 s): the open card gets the outcome at once,
        // lists and metrics are refetched — a mass close of daily forecasts is capped at 500 events per tick.
        const ev = data as StreamEventMap['prediction.updated']
        this.qc.setQueriesData<Prediction>({ queryKey: ['prediction', this.runKey, `/predictions/${ev.id}`] }, (old) =>
          old ? { ...old, outcome: ev.outcome, outcome_at: ev.outcome_at, outcome_source: ev.outcome_source } : old,
        )
        this.invalidateChannel(ev.channel_id)
        this.invalidate('predictions', 'metrics', 'channel-predictions', 'channel', 'prediction', 'work-order', 'dashboard-summary')
        break
      }
      case 'daily_forecast.completed':
        this.invalidate('predictions', 'metrics', 'dashboard-summary', 'dashboard-risk', 'channels', 'graph', 'graph-sub', 'objects')
        break
      case 'equipment_episode.created':
      case 'equipment_episode.updated':
        this.invalidate('equipment-episodes', 'equipment-episode', 'objects')
        break
      case 'access_incident.created':
      case 'access_incident.updated': {
        // The open card gets the new state at once (openings count, end, review); its opening and guard lists and the
        // feed are refetched with the throttle — `updated` may come after every recount of the rule while it goes on.
        const inc = data as AccessIncident
        upsertAccess(this.qc, this.runKey, inc)
        this.qc.setQueriesData<AccessIncidentDetail>({ queryKey: ['access-incident', this.runKey, `/access-incidents/${inc.id}`] }, (old) =>
          old ? { ...old, ...inc } : old,
        )
        this.invalidate('access-incidents', 'access-incident')
        break
      }
      case 'work_order.created':
        this.invalidate('work-orders', 'dashboard-summary', 'channel')
        break
      case 'channel.status': {
        const ev = data as StreamEventMap['channel.status']
        this.qc.setQueriesData<ChannelDetail>({ queryKey: ['channel', this.runKey, `/channels/${ev.channel_id}`] }, (old) =>
          old ? { ...old, status: ev.status, value: ev.value, value_ts: ev.ts, risk: ev.risk ?? old.risk } : old,
        )
        this.invalidate('dashboard-summary', 'objects', 'channel-timeline')
        break
      }
    }
  }
}
