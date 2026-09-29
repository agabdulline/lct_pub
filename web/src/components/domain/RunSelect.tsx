import { useSearchParams } from 'react-router'

import { Select, SelectContent, SelectGroup, SelectItem, SelectLabel, SelectSeparator, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useDataset } from '@/lib/api/queries'
import { RUN_STATUS_LABEL } from '@/lib/labels'
import { useRun } from '@/lib/run'

const FOLLOW = 'latest'

// Run switcher: `?run=` pins a run; «последний» follows new runs (emulator.md §5).
export function RunSelect() {
  const { runs, run, pinned, runId, ready } = useRun()
  const [params, setParams] = useSearchParams()
  // A dataset's service run is not in the list: it is opened by a link from the dataset card.
  const dataset = useDataset(run?.kind === 'dataset' ? run.dataset : null)

  const value = pinned && runId !== null ? String(runId) : FOLLOW
  const latest = runs.reduce<number | null>((m, r) => (m === null || r.id > m ? r.id : m), null)
  const unlisted = pinned && runId !== null && !runs.some((r) => r.id === runId)
  // Until /runs answers the pinned run is not «нет в списке» yet — just its number
  const unlistedLabel = run
    ? `№${runId} · ${dataset?.short ?? run.dataset ?? run.scenario_title}`
    : ready
      ? `№${runId} · нет в списке`
      : `№${runId}`
  const items = [
    { value: FOLLOW, label: latest !== null ? `Последний прогон · №${latest}` : 'Базовая история' },
    ...runs.map((r) => ({ value: String(r.id), label: `№${r.id} · ${r.scenario_title}` })),
    ...(unlisted ? [{ value: String(runId), label: unlistedLabel }] : []),
  ]

  const onChange = (next: string | null) => {
    const p = new URLSearchParams(params)
    if (!next || next === FOLLOW) p.delete('run')
    else p.set('run', next)
    setParams(p)
  }

  return (
    <Select items={items} value={value} onValueChange={onChange}>
      {/* Shrinks on a phone: the run's name ends with an ellipsis instead of pushing the header off the screen. */}
      <SelectTrigger size="sm" className="max-w-44 min-w-0 sm:max-w-64" aria-label="Прогон">
        <SelectValue className="block! min-w-0 truncate" />
      </SelectTrigger>
      <SelectContent>
        <SelectGroup>
          <SelectItem value={FOLLOW}>{items[0].label}</SelectItem>
        </SelectGroup>
        {runs.length > 0 && <SelectSeparator />}
        <SelectGroup>
          {runs.length > 0 && <SelectLabel>Закрепить прогон</SelectLabel>}
          {runs
            .slice()
            .sort((a, b) => b.id - a.id)
            .map((r) => (
              <SelectItem key={r.id} value={String(r.id)}>
                <span className="flex flex-col">
                  <span>
                    №{r.id} · {r.scenario_title}
                  </span>
                  <span className="text-xs text-muted-foreground">{RUN_STATUS_LABEL[r.status]}</span>
                </span>
              </SelectItem>
            ))}
        </SelectGroup>
      </SelectContent>
    </Select>
  )
}
