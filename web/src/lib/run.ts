// Selected run (docs/architecture/frontend.md §5, emulator.md §5).
// `?run=` pins a run; without it the screens follow the latest run and switch when a new one starts.
// `?run=0` — «Вся история»: the hackathon journal without a run (base, `run_id NULL`), explicit and never «latest».
// `?run=all` — «Все данные»: the hackathon base and every dataset's base run together, each on its own time scale.

import { createContext, useContext } from 'react'
import { useSearchParams, type To } from 'react-router'

import { apiGet } from '@/lib/api/client'
import type { Run } from '@/lib/api/types'

export type RunContextValue = {
  /** Run id for requests; `null` — no runs yet, base history. */
  runId: number | null
  /** Query-key part: the run id, `base` or `all`. */
  runKey: number | 'base' | 'all'
  /** `run` of the requests: the run id, or `all` in «Все данные». */
  runParam: number | 'all' | null
  /** The run is pinned by `?run=`. */
  pinned: boolean
  /** «Вся история» — `?run=0`: the hackathon journal 2019–2026 without a run; no feed, no clock. Also true in «Все данные». */
  history: boolean
  /** «Все данные» — `?run=all`: every source together; `runId` is 0 (the hackathon base) for run-id-only consumers. */
  all: boolean
  /** Runs are loaded: `runId` is final and `run` is known — screens render only after it. */
  ready: boolean
  /** Runs to choose from: scenario runs, without the service runs of external datasets. */
  runs: Run[]
  /** Service runs of external datasets (kind `dataset`): their whole history, listed next to «Вся история». */
  datasetRuns: Run[]
  /** The chosen run, also a dataset's service run opened by a link. */
  run: Run | null
  /** `/runs` failed — the list is unknown, not empty. */
  runsError: Error | null
  refetchRuns: () => void
}

export const RunContext = createContext<RunContextValue | null>(null)

export const runsQuery = { queryKey: ['runs'], queryFn: () => apiGet<{ items: Run[] }>('/runs'), refetchInterval: 10_000 }

export function useRun(): RunContextValue {
  const ctx = useContext(RunContext)
  if (!ctx) throw new Error('useRun must be used inside RunProvider')
  return ctx
}

/** Search string that keeps a pinned run on navigation: `?run=7` or empty. */
export function useRunSearch(): string {
  const [params] = useSearchParams()
  const run = params.get('run')
  return run ? `?run=${run}` : ''
}

export function withRun(to: To, search: string): To {
  if (!search) return to
  const run = new URLSearchParams(search).get('run')!
  if (typeof to === 'string') {
    const [path, qs = ''] = to.split('?')
    const merged = new URLSearchParams(qs)
    merged.set('run', run)
    return `${path}?${merged.toString()}`
  }
  const merged = new URLSearchParams(to.search ?? '')
  merged.set('run', run)
  return { ...to, search: `?${merged.toString()}` }
}

/** Adds the pinned run to a link target. */
export function useRunTo() {
  const search = useRunSearch()
  return (to: To) => withRun(to, search)
}
