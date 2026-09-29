// Experiments of an external data set (by_experiment in the offline report): read defensively, the fields are the
// data set's own (Magdeburg: experiment, label, subtypes, fire_alerts, first_alert_s, predicted).

import type { OfflineReport } from '@/lib/api/types'

/** One experiment of an external data set, read defensively: the fields are the data set's own. */
export type Experiment = {
  n: number
  label: string
  kinds: string[]
  minutes: number | null
  decisions: number | null
  fireAlerts: number | null
  nodes: number | null
  firstAlert: number | null
  predicted: string | null
}

const numOr = (v: unknown) => (typeof v === 'number' ? v : null)
const strOr = (v: unknown) => (typeof v === 'string' ? v : null)

function readExperiment(r: Record<string, unknown>): Experiment | null {
  const n = numOr(r.experiment)
  const label = strOr(r.label)
  if (n === null || label === null) return null
  const subtype = strOr(r.subtype)
  const kinds = Array.isArray(r.subtypes) ? r.subtypes.filter((k): k is string => typeof k === 'string') : subtype ? [subtype] : []
  return {
    n,
    label,
    kinds,
    minutes: numOr(r.minutes),
    decisions: numOr(r.decisions),
    fireAlerts: numOr(r.fire_alerts),
    nodes: numOr(r.nodes_alerted),
    firstAlert: numOr(r.first_alert_s),
    predicted: strOr(r.predicted),
  }
}

/** At least one «fire» alert in the experiment: for a nuisance — a false call, what the operator pays for. */
const alerted = (r: Experiment) => (r.fireAlerts !== null ? r.fireAlerts > 0 : r.firstAlert !== null)

export type ExperimentStats = {
  total: number
  matched: number
  fires: number
  firesAlerted: number
  firesPredicted: number
  nuisances: number
  nuisancesAlerted: number
  nuisancesPredictedFire: number
}

export function experimentStatsOf(rows: Experiment[]): ExperimentStats {
  const fires = rows.filter((r) => r.label === 'fire')
  const nuisances = rows.filter((r) => r.label === 'nuisance')
  return {
    total: rows.length,
    matched: rows.filter((r) => r.predicted === r.label).length,
    fires: fires.length,
    firesAlerted: fires.filter(alerted).length,
    firesPredicted: fires.filter((r) => r.predicted === 'fire').length,
    nuisances: nuisances.length,
    nuisancesAlerted: nuisances.filter(alerted).length,
    nuisancesPredictedFire: nuisances.filter((r) => r.predicted === 'fire').length,
  }
}

export const readExperiments = (report: OfflineReport) => (report.by_experiment ?? []).map(readExperiment).filter((e): e is Experiment => e !== null)

/** Experiment counts of an external report (Magdeburg); `null` — the report has no experiments of that shape. */
export function experimentStats(report: OfflineReport): ExperimentStats | null {
  const rows = readExperiments(report)
  return rows.length ? experimentStatsOf(rows) : null
}
