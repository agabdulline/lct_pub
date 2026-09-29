import type { Outcome, Prediction, Run } from '@/lib/api/types'

/** Confusion-matrix cell of a closed forecast: what the model said against what happened. */
export function outcomeKind(outcome: Outcome | null, alert: boolean | null): 'tp' | 'fp' | 'fn' | 'tn' | 'open' | 'skipped' {
  if (alert === null) return 'skipped'
  if (outcome === null) return 'open'
  if (alert) return outcome === 'fault' ? 'tp' : 'fp'
  return outcome === 'fault' ? 'fn' : 'tn'
}

/**
 * «Now» of a run's metrics (backend `closing_now`): the run's clock or the end of the journal recount
 * (`Metrics.outcomes_until`), whichever is later; `null` — the base history, its forecasts close by the data.
 */
export function closingNow(run: Run | null, outcomesUntil: string | null | undefined): number | null {
  if (!run) return null
  const clock = Date.parse(run.sim_now ?? run.sim_start)
  return outcomesUntil ? Math.max(clock, Date.parse(outcomesUntil)) : clock
}

/** The forecast is in the metrics: its outcome is known and the horizon is over by the run's «now». */
export const inMetrics = (p: Pick<Prediction, 'outcome' | 'horizon_end'>, now: number | null) =>
  p.outcome !== null && (now === null || Date.parse(p.horizon_end) <= now)
