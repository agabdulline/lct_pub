// Alerts of external models in a data set run (stage 14): the rows of the feed.

import { classesOf } from '@/components/metrics/prediction-context'
import { usePredictions } from '@/lib/api/queries'
import type { ModelInfo, Prediction } from '@/lib/api/types'
import { EXTERNAL_CLASS_LABEL, externalHorizonHours } from '@/lib/labels'
import { useRun } from '@/lib/run'

/** Seconds between decisions of a periodic model (`every_60s` → 60, `hourly` → 3600); `null` — a decision per event. */
export function triggerInterval(trigger: string | null | undefined): number | null {
  if (trigger === 'hourly') return 3600
  if (trigger === 'daily') return 86_400
  const every = trigger ? /^every_(\d+)s$/.exec(trigger) : null
  return every ? Number(every[1]) : null
}

const PASS_REASONS = new Set(['route', 'segment_time_fast', 'segment_time_slow'])

/**
 * What an alert is about: `context.class` when the model names it; the most likely class (Magdeburg: fire); VAST
 * without classes — no permit is a violation, an unusual route or segment time an unusual pass; else the model's
 * task (the feed shows its title).
 */
export function alertClass(p: Prediction): string {
  const named = p.context?.class
  if (typeof named === 'string') return named
  const classes = classesOf(p.context)
  if (classes.length) return classes[0][0]
  if (p.factors?.some((f) => f.feature === 'no_permit')) return 'violation'
  if (p.factors?.some((f) => PASS_REASONS.has(f.feature))) return 'suspicious'
  return p.model
}

export type AlertGroup = {
  key: string
  model: string
  cls: string
  objectId: number
  objectName: string
  /** Distinct channels of the group, in order of the first alert. */
  channels: { id: number; name: string; sensorType: string }[]
  alerts: Prediction[]
  first: Prediction
  last: Prediction
  maxScore: number | null
  threshold: number | null
  /** Alerts by the data set's labels: event was there, was not, no label. */
  fault: number
  noFault: number
  open: number
  /** A periodic model's episode: consecutive alerts of one object, or of one channel for a forecasting model. */
  episode: boolean
  /** The model's horizon, h: 0 — detection at the moment of decision; more — a forecast of one unit, its episode is
   * one channel's (Azure PdM) and an alert without an outcome waits for the horizon to end. */
  horizon: number
  /** Decision interval of the model, s; `null` — per event. */
  interval: number | null
}

const time = (p: Prediction) => Date.parse(p.decision_time)

function summarize(key: string, alerts: Prediction[], episode: boolean, interval: number | null, horizon = 0): AlertGroup {
  const first = alerts[0]
  const last = alerts[alerts.length - 1]
  const channels = new Map<number, AlertGroup['channels'][number]>()
  for (const a of alerts) if (!channels.has(a.channel_id)) channels.set(a.channel_id, { id: a.channel_id, name: a.channel_name, sensorType: a.sensor_type })
  const scores = alerts.map((a) => a.score).filter((v): v is number => v !== null)
  return {
    key,
    model: first.model,
    cls: alertClass(first),
    objectId: first.object_id,
    objectName: first.object_name,
    channels: [...channels.values()],
    alerts,
    first,
    last,
    maxScore: scores.length ? Math.max(...scores) : null,
    threshold: first.threshold,
    fault: alerts.filter((a) => a.outcome === 'fault').length,
    noFault: alerts.filter((a) => a.outcome === 'no_fault').length,
    open: alerts.filter((a) => a.outcome === null).length,
    episode,
    horizon,
    interval,
  }
}

/**
 * Feed rows from the alerts of a run, newest activity first. A periodic model (Magdeburg: every 60 s on 9 nodes)
 * alerts every minute while a fire lasts: consecutive alerts of one object and class — a gap of at most two
 * decision intervals — are one episode. A forecasting model (Azure PdM: hourly, «failure within 24 h») alerts every
 * hour before a unit fails: its episode is one channel's, since the object groups many units. A model deciding per
 * event (VAST: a pass) gives a row per alert.
 */
export function groupAlerts(alerts: Prediction[], models: ModelInfo[]): AlertGroup[] {
  const interval = new Map(models.map((m) => [m.model, triggerInterval(m.trigger)]))
  const horizon = new Map(models.map((m) => [m.model, externalHorizonHours(m)]))
  const perChannel = (model: string) => (horizon.get(model) ?? 0) > 0
  const sorted = [...alerts].sort((a, b) => time(a) - time(b))
  const groups: AlertGroup[] = []
  const open = new Map<string, Prediction[]>()
  const flush = (k: string) => {
    const list = open.get(k)
    if (list) groups.push(summarize(`${k}:${list[0].id}`, list, true, interval.get(list[0].model) ?? null, horizon.get(list[0].model) ?? 0))
    open.delete(k)
  }
  for (const a of sorted) {
    const step = interval.get(a.model) ?? null
    if (step === null) {
      groups.push(summarize(`${a.model}:${a.id}`, [a], false, null, horizon.get(a.model) ?? 0))
      continue
    }
    const k = `${a.model}:${perChannel(a.model) ? `ch${a.channel_id}` : a.object_id}:${alertClass(a)}`
    const list = open.get(k)
    if (list && time(a) - time(list[list.length - 1]) > (2 * step + 1) * 1000) flush(k)
    const current = open.get(k)
    if (current) current.push(a)
    else open.set(k, [a])
  }
  for (const k of [...open.keys()]) flush(k)
  return groups.sort((a, b) => time(b.last) - time(a.last))
}

/** The episode still goes on: its last alert is within two decision intervals of the run's clock. */
export function isOngoing(g: AlertGroup, now: string | null | undefined, running: boolean): boolean {
  if (!running || !now || g.interval === null) return false
  return Date.parse(now) - time(g.last) <= (2 * g.interval + 1) * 1000
}

/** The API returns at most 500 per page: a data set run has fewer alerts, the rest is in the journal. */
export const ALERTS_LIMIT = 500

/**
 * The alerts of the chosen data set run as feed rows (its own models only — a run never mixes them with the hackathon
 * ones, but the API filters by one model). A scenario run counts from its start, the data set's base run — all of it.
 */
export function useRunAlertGroups(models: ModelInfo[]) {
  const { run } = useRun()
  const list = usePredictions({ alert: 'true', size: ALERTS_LIMIT, from: run?.kind === 'dataset' ? null : (run?.sim_start ?? null) }, !!run)
  const own = new Set(models.map((m) => m.model))
  const groups = list.data ? groupAlerts(list.data.items.filter((p) => own.has(p.model)), models) : []
  return { list, groups }
}

/** A class the model names, or the model's task when it has none (its title from /models). */
export const alertClassLabel = (c: string, models: ModelInfo[]) => EXTERNAL_CLASS_LABEL[c] ?? models.find((m) => m.model === c)?.title ?? c
