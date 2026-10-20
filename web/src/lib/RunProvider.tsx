import { useQuery } from '@tanstack/react-query'
import { useMemo, type ReactNode } from 'react'
import { useSearchParams } from 'react-router'

import type { Run } from '@/lib/api/types'
import { RunContext, runsQuery, type RunContextValue } from '@/lib/run'

/** `?run=0` — the base history of the hackathon (backend: «0 — только базовая история»). */
const HISTORY = 0
/** `?run=all` — «Все данные». */
const ALL = 'all'

export function RunProvider({ children }: { children: ReactNode }) {
  const [params] = useSearchParams()
  const raw = params.get('run')
  const allData = raw === ALL
  // «Все данные» stands on the hackathon base for whatever needs one run id (the stream, links)
  const pinnedId = allData ? HISTORY : raw && /^\d+$/.test(raw) ? Number(raw) : null
  const runs = useQuery(runsQuery)

  const value = useMemo<RunContextValue>(() => {
    // The history of an external dataset is a service run (kind 'dataset'): it is never started, listed or «latest»,
    // but a link to it (the dataset card on the emulator page) opens it like any run.
    const all = runs.data?.items ?? []
    const items = all.filter((r) => r.kind !== 'dataset')
    const latest = items.reduce<Run | null>((best, r) => (best === null || r.id > best.id ? r : best), null)
    const runId = pinnedId ?? latest?.id ?? null
    return {
      runId,
      runKey: allData ? 'all' : (runId ?? 'base'),
      runParam: allData ? ALL : runId,
      all: allData,
      pinned: pinnedId !== null,
      history: pinnedId === HISTORY,
      // The screens wait for the list even with a pinned run: the run's dataset picks the screen (the hackathon feed or
      // the dataset's), and without it a dataset run showed «нет в списке» and an empty hackathon feed until /runs came.
      // On error they fall back to the default run.
      ready: runs.isSuccess || runs.isError,
      runs: items,
      datasetRuns: all.filter((r) => r.kind === 'dataset'),
      run: all.find((r) => r.id === runId) ?? null,
      runsError: runs.error,
      refetchRuns: () => void runs.refetch(),
    }
  }, [pinnedId, runs, allData])

  return <RunContext.Provider value={value}>{children}</RunContext.Provider>
}
