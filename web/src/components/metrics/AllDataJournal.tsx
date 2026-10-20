import { ScrollText } from 'lucide-react'
import { Link } from 'react-router'

import { ChannelLink } from '@/components/domain/ChannelLink'
import { DirectionBadge } from '@/components/domain/DirectionBadge'
import { OutcomeBadge } from '@/components/domain/OutcomeBadge'
import { Pager } from '@/components/domain/Pager'
import { QueryError } from '@/components/domain/QueryError'
import { RiskMeter } from '@/components/domain/RiskMeter'
import { PredictionPanel } from '@/components/metrics/PredictionPanel'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from '@/components/ui/empty'
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { useDatasets, usePredictions } from '@/lib/api/queries'
import type { Prediction } from '@/lib/api/types'
import { fmtDateTimeShort } from '@/lib/format'
import { isHackathonModel, MODEL_LABEL, skipReasonLabel } from '@/lib/labels'
import { useRun } from '@/lib/run'
import { useSearchState } from '@/lib/search-state'

const PAGE_SIZE = 50
const ALL = 'all'
const HACKATHON = 'hackathon'

/**
 * «Все данные» (`?run=all`): decisions of the hackathon history and of the datasets' base runs in one journal, filtered
 * by source; every row carries its source badge. Metrics stay per source — in «Вся история» of that source.
 */
export function AllDataJournal() {
  const s = useSearchState()
  const { datasetRuns } = useRun()
  const datasets = useDatasets().data
  const source = s.get('source') ?? ALL
  const page = s.num('page') ?? 1
  const selected = s.num('prediction')
  const list = usePredictions({ source: source === ALL ? null : source, page, size: PAGE_SIZE })
  const data = list.data
  const name = (key: string | null | undefined) => datasets?.find((d) => d.key === key)?.short ?? key ?? '—'
  const datasetOf = (p: Prediction) => datasetRuns.find((r) => r.id === p.run_id)?.dataset ?? null
  const sources = [
    { value: HACKATHON, label: 'Хакатон' },
    ...datasetRuns.filter((r) => r.dataset).map((r) => ({ value: r.dataset as string, label: name(r.dataset) })),
  ]
  // per-source metrics live in that source's «Вся история»: the hackathon base or the dataset's base run
  const historyOf = (v: string) => (v === HACKATHON ? '?run=0' : `?run=${datasetRuns.find((r) => r.dataset === v)?.id ?? 0}`)

  return (
    <div className="mx-auto w-full max-w-7xl space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <ToggleGroup value={[source]} onValueChange={(v) => v[0] && s.set({ source: v[0] === ALL ? null : v[0], page: null })} variant="outline" aria-label="Источник">
          <ToggleGroupItem value={ALL}>Все</ToggleGroupItem>
          {sources.map((o) => (
            <ToggleGroupItem key={o.value} value={o.value}>
              {o.label}
            </ToggleGroupItem>
          ))}
        </ToggleGroup>
      </div>

      <Card className="py-0">
        <CardHeader className="pt-4">
          <CardTitle>{source === ALL ? 'Все источники' : (sources.find((o) => o.value === source)?.label ?? source)}</CardTitle>
          <CardDescription>
            {data ? `${data.total.toLocaleString('ru-RU')} решений` : ' '}
            {source !== ALL && (
              <>
                {' · '}
                <Link to={`/predictions${historyOf(source)}`} className="underline-offset-4 hover:underline">
                  метрики источника
                </Link>
              </>
            )}
          </CardDescription>
        </CardHeader>
        <CardContent className="px-0">
          {list.isPending && <Skeleton className="m-4 h-80" />}
          {list.isError && <QueryError error={list.error} onRetry={() => void list.refetch()} what="Журнал прогнозов" />}
          {data && data.items.length === 0 && (
            <Empty>
              <EmptyHeader>
                <EmptyMedia variant="icon">
                  <ScrollText />
                </EmptyMedia>
                <EmptyTitle>Решений нет</EmptyTitle>
                <EmptyDescription>
                  {source !== ALL && source !== HACKATHON
                    ? 'Модели набора в его основе не запускались — их решения есть в прогонах сценариев набора.'
                    : 'С такими фильтрами решений нет.'}
                </EmptyDescription>
              </EmptyHeader>
            </Empty>
          )}
          {data && data.items.length > 0 && (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="pl-4">Источник</TableHead>
                  <TableHead>Решение</TableHead>
                  <TableHead>Модель</TableHead>
                  <TableHead>Канал</TableHead>
                  <TableHead>Объект</TableHead>
                  <TableHead>Оценка</TableHead>
                  <TableHead className="pr-4">Исход</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.items.map((p) => {
                  const ds = datasetOf(p)
                  return (
                    <TableRow key={p.id} className="cursor-pointer" data-state={selected === p.id ? 'selected' : undefined} onClick={() => s.set({ prediction: p.id }, true)}>
                      <TableCell className="pl-4">
                        {ds ? (
                          <span className="flex items-center gap-1.5">
                            <DirectionBadge scenarioId={`dataset:${ds}`} dataset={ds} />
                            {name(ds)}
                          </span>
                        ) : (
                          <Badge variant="outline" className="font-normal">
                            Хакатон
                          </Badge>
                        )}
                      </TableCell>
                      <TableCell className="font-mono text-xs tabular-nums">{fmtDateTimeShort(p.decision_time)}</TableCell>
                      <TableCell className="text-sm">{isHackathonModel(p.model) ? MODEL_LABEL[p.model] : p.model}</TableCell>
                      <TableCell className="max-w-56">
                        <ChannelLink id={p.channel_id} name={p.channel_name} sensorType={p.sensor_type} />
                      </TableCell>
                      <TableCell className="max-w-44 truncate">{p.object_name}</TableCell>
                      <TableCell>
                        {p.status === 'scored' ? (
                          <RiskMeter value={p.score} threshold={p.threshold} />
                        ) : (
                          <span className="text-xs text-muted-foreground">{p.skip_reason ? skipReasonLabel(p.skip_reason) : 'пропуск'}</span>
                        )}
                      </TableCell>
                      <TableCell className="pr-4">
                        <OutcomeBadge outcome={p.outcome} alert={p.alert} outcomeAt={p.outcome_at} horizonEnd={p.horizon_end} source={p.outcome_source} external={!!ds} />
                      </TableCell>
                    </TableRow>
                  )
                })}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
      {data && data.total > PAGE_SIZE && (
        <Pager page={page} size={PAGE_SIZE} total={data.total} onPage={(p) => s.set({ page: p > 1 ? p : null }, true)} noun="прогнозов" />
      )}
      <PredictionPanel id={selected} onClose={() => s.set({ prediction: null }, true)} />
    </div>
  )
}
