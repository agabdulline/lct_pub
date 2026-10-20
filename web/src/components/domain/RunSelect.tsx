import { useSearchParams } from 'react-router'

import { DirectionBadge } from '@/components/domain/DirectionBadge'
import { Badge } from '@/components/ui/badge'
import { Select, SelectContent, SelectGroup, SelectItem, SelectLabel, SelectSeparator, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useDataset, useDatasets } from '@/lib/api/queries'
import { RUN_STATUS_LABEL } from '@/lib/labels'
import { useRun } from '@/lib/run'

const FOLLOW = 'latest'
const HISTORY = '0' // «Вся история»: the hackathon journal without a run (`?run=0`)
const HISTORY_LABEL = 'Вся история · журнал 2019 — 06.2026'
const ALL = 'all' // «Все данные»: the hackathon and every dataset together (`?run=all`)
const ALL_LABEL = 'Все данные · хакатон и наборы'

// Run switcher: `?run=` pins a run; «последний» follows new runs (emulator.md §5).
export function RunSelect() {
  const { runs, run, pinned, runId, ready, history, all, datasetRuns } = useRun()
  const datasets = useDatasets().data
  const datasetLabel = (r: { dataset?: string | null; scenario_title: string }) =>
    `Вся история · ${datasets?.find((d) => d.key === r.dataset)?.short ?? r.dataset ?? r.scenario_title}`
  const [params, setParams] = useSearchParams()
  // A dataset's service run is not in the list: it is opened by a link from the dataset card.
  const dataset = useDataset(run?.kind === 'dataset' ? run.dataset : null)

  const value = all ? ALL : history ? HISTORY : pinned && runId !== null ? String(runId) : FOLLOW
  const latest = runs.reduce<number | null>((m, r) => (m === null || r.id > m ? r.id : m), null)
  const unlisted = pinned && !history && runId !== null && !runs.some((r) => r.id === runId) && !datasetRuns.some((r) => r.id === runId)
  // Until /runs answers the pinned run is not «нет в списке» yet — just its number
  const unlistedLabel = run
    ? `№${runId} · ${dataset?.short ?? run.dataset ?? run.scenario_title}`
    : ready
      ? `№${runId} · нет в списке`
      : `№${runId}`
  const items = [
    { value: FOLLOW, label: latest !== null ? `Последний прогон · №${latest}` : 'Базовая история' },
    ...runs.map((r) => ({ value: String(r.id), label: `№${r.id} · ${r.scenario_title}` })),
    { value: ALL, label: ALL_LABEL },
    { value: HISTORY, label: HISTORY_LABEL },
    ...datasetRuns.map((r) => ({ value: String(r.id), label: datasetLabel(r) })),
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
      <SelectContent className="w-[min(26rem,calc(100vw-2rem))]">
        <SelectGroup>
          <SelectItem value={FOLLOW}>{items[0].label}</SelectItem>
        </SelectGroup>
        <SelectSeparator />
        {/* The whole history: the hackathon journal and each external dataset apart — never mixed. */}
        <SelectGroup>
          <SelectLabel>Вся история</SelectLabel>
          <SelectItem value={ALL}>
            <span className="flex min-w-0 flex-col gap-0.5">
              <span className="truncate">Все данные</span>
              <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
                <Badge variant="outline" className="font-normal">
                  Все направления
                </Badge>
                хакатон и наборы, у каждого своё время
              </span>
            </span>
          </SelectItem>
          <SelectItem value={HISTORY}>
            <span className="flex min-w-0 flex-col gap-0.5">
              <span className="truncate">Журнал хакатона</span>
              {/* the hackathon journal covers sensor faults and opened under guard */}
              <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
                <DirectionBadge direction="sensor" />
                <DirectionBadge direction="access" />
                2019 — 30.06.2026, без прогона
              </span>
            </span>
          </SelectItem>
          {datasetRuns.map((r) => (
            <SelectItem key={r.id} value={String(r.id)}>
              <span className="flex min-w-0 flex-col gap-0.5">
                <span className="truncate">{datasetLabel(r).replace('Вся история · ', '')}</span>
                <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
                  <DirectionBadge scenarioId={r.scenario} dataset={r.dataset} />
                  внешний набор
                </span>
              </span>
            </SelectItem>
          ))}
        </SelectGroup>
        {runs.length > 0 && <SelectSeparator />}
        <SelectGroup>
          {runs.length > 0 && <SelectLabel>Закрепить прогон</SelectLabel>}
          {runs
            .slice()
            .sort((a, b) => b.id - a.id)
            .map((r) => (
              <SelectItem key={r.id} value={String(r.id)}>
                <span className="flex min-w-0 flex-col gap-0.5">
                  <span className="truncate" title={r.scenario_title}>
                    №{r.id} · {r.scenario_title}
                  </span>
                  <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
                    <DirectionBadge scenarioId={r.scenario} dataset={r.dataset} />
                    {RUN_STATUS_LABEL[r.status]}
                  </span>
                </span>
              </SelectItem>
            ))}
        </SelectGroup>
      </SelectContent>
    </Select>
  )
}
