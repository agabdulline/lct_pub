import { BellRing, Map as MapIcon, ScrollText } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'

import { QueryError } from '@/components/domain/QueryError'
import { RiskMeter } from '@/components/domain/RiskMeter'
import { RunLink } from '@/components/domain/RunLink'
import { StatusDot } from '@/components/domain/StatusBadge'
import { TopReason } from '@/components/metrics/ClassProbabilities'
import { PredictionPanel } from '@/components/metrics/PredictionPanel'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from '@/components/ui/empty'
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { useExternalModels, useModels } from '@/lib/api/queries'
import { fmtDate, fmtDateTimeShort, fmtInt, fmtTime, plural } from '@/lib/format'
import { useRun } from '@/lib/run'
import { useSearchState } from '@/lib/search-state'
import { useSimNow } from '@/lib/sim-now'
import { useStreamEvent } from '@/lib/sse/bus'
import { cn } from '@/lib/utils'

import { ClassMark, GroupOutcome, Where } from './AlertParts'
import { alertClassLabel, ALERTS_LIMIT, isOngoing, useRunAlertGroups } from './external-alerts'

/**
 * Feed of a data set run (stage 14): the alerts of the data set's models instead of SMVU alarms — the data set has
 * none. A row is an alert or a periodic model's episode; the outcome is the data set's label, not a confirmed fault.
 */
export function ExternalFeed() {
  const s = useSearchState()
  const { run } = useRun()
  const simNow = useSimNow()
  const models = useExternalModels(run?.dataset)
  const modelsPending = useModels().isPending
  const cls = s.get('class')
  const selected = s.num('prediction')

  const { list, groups } = useRunAlertGroups(models)
  const running = run?.status === 'running' || run?.status === 'paused'
  const ongoing = groups.filter((g) => isOngoing(g, simNow, running)).length
  const classes = [...new Set(groups.map((g) => g.cls))]
  const shown = cls ? groups.filter((g) => g.cls === cls) : groups
  const classLabel = (c: string) => alertClassLabel(c, models)

  // New alerts flash for a few seconds.
  const [fresh, setFresh] = useState<Set<number>>(new Set())
  const timers = useRef<ReturnType<typeof setTimeout>[]>([])
  useStreamEvent('prediction.created', (p) => {
    if (!p.alert) return
    setFresh((prev) => new Set(prev).add(p.id))
    timers.current.push(
      setTimeout(() => {
        setFresh((prev) => {
          const next = new Set(prev)
          next.delete(p.id)
          return next
        })
      }, 6000),
    )
  })
  useEffect(() => () => timers.current.forEach(clearTimeout), [])

  return (
    <div className={cn('mx-auto w-full max-w-7xl space-y-4', selected !== null && '2xl:mr-[26rem] 2xl:w-[calc(100%-26rem)]')}>
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="flex items-center gap-2 text-sm">
          <StatusDot status="alarm" pulse={ongoing > 0} />
          Сейчас идёт: <span className="font-semibold tabular-nums">{list.data ? fmtInt(ongoing) : '—'}</span>
        </div>
      </div>

      {/* Class counters are filters as well. */}
      {classes.length > 0 && (
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
          {classes.map((c) => {
            const on = cls === c
            const of = groups.filter((g) => g.cls === c)
            const alerts = of.reduce((n, g) => n + g.alerts.length, 0)
            return (
              <button
                key={c}
                type="button"
                onClick={() => s.set({ class: on ? null : c })}
                aria-pressed={on}
                className={cn(
                  'flex items-center justify-between rounded-xl bg-card px-3 py-2.5 text-left text-sm ring-1 ring-foreground/10 transition-colors hover:bg-muted/50',
                  on && 'ring-2 ring-ring',
                )}
              >
                <span className="flex items-center gap-2">
                  <ClassMark cls={c} />
                  <span>
                    {classLabel(c)}
                    {of.some((g) => g.episode) && (
                      <span className="block text-xs text-muted-foreground">
                        {fmtInt(alerts)} {plural(alerts, ['решение', 'решения', 'решений'])} с тревогой
                      </span>
                    )}
                  </span>
                </span>
                <span className="text-lg font-semibold tabular-nums">{fmtInt(of.length)}</span>
              </button>
            )
          })}
        </div>
      )}

      <Card className="py-0">
        <CardContent className="px-0">
          {(list.isPending || modelsPending) && (
            <div className="space-y-2 p-4">
              {Array.from({ length: 6 }, (_, i) => (
                <Skeleton key={i} className="h-10" />
              ))}
            </div>
          )}
          {list.isError && <QueryError error={list.error} onRetry={() => void list.refetch()} what="Лента тревог" />}
          {list.data && !modelsPending && shown.length === 0 && (
            <Empty>
              <EmptyHeader>
                <EmptyMedia variant="icon">
                  <BellRing />
                </EmptyMedia>
                <EmptyTitle>Тревог модели нет</EmptyTitle>
                <EmptyDescription>
                  {run?.kind === 'dataset'
                    ? 'Это вся история набора: модели на ней не запускаются. Тревоги модели — в прогонах сценариев набора на экране «Эмулятор СМВУ».'
                    : models.length === 0
                    ? 'Для этого набора в ML-сервисе нет модели — в прогоне только события набора.'
                    : running
                      ? 'Трансляция идёт — модель пока не подала ни одной тревоги. Новые появятся здесь сами.'
                      : 'За прогон модель тревог не подала. Все её решения — в журнале прогнозов.'}
                </EmptyDescription>
              </EmptyHeader>
            </Empty>
          )}
          {list.data && !modelsPending && shown.length > 0 && (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="pl-4">Время</TableHead>
                  <TableHead>Тревога</TableHead>
                  <TableHead>Где</TableHead>
                  <TableHead>Оценка</TableHead>
                  <TableHead>Исход</TableHead>
                  <TableHead className="pr-4 text-right" />
                </TableRow>
              </TableHeader>
              <TableBody>
                {shown.map((g) => {
                  const live = isOngoing(g, simNow, running)
                  const isFresh = g.alerts.some((a) => fresh.has(a.id))
                  return (
                    <TableRow
                      key={g.key}
                      className={cn('cursor-pointer', isFresh && 'bg-status-alarm/10')}
                      data-state={g.alerts.some((a) => a.id === selected) ? 'selected' : undefined}
                      onClick={() => s.set({ prediction: g.last.id }, true)}
                    >
                      <TableCell className="pl-4 font-mono text-xs tabular-nums">
                        {fmtDateTimeShort(g.first.decision_time)}
                        {g.episode && g.alerts.length > 1 && (
                          <div className="text-muted-foreground">
                            —{' '}
                            {live
                              ? 'идёт'
                              : fmtDate(g.last.decision_time) === fmtDate(g.first.decision_time)
                                ? fmtTime(g.last.decision_time).slice(0, 5)
                                : fmtDateTimeShort(g.last.decision_time)}
                          </div>
                        )}
                      </TableCell>
                      <TableCell>
                        <span className="flex items-center gap-2 font-medium">
                          <ClassMark cls={g.cls} />
                          {classLabel(g.cls)}
                          {live && <StatusDot status="alarm" pulse />}
                        </span>
                        {g.episode ? (
                          g.alerts.length > 1 && (
                            <span className="text-xs text-muted-foreground">
                              {fmtInt(g.alerts.length)} {plural(g.alerts.length, ['решение', 'решения', 'решений'])} с тревогой
                            </span>
                          )
                        ) : (
                          <TopReason factors={g.last.factors} className="block max-w-72 truncate" />
                        )}
                      </TableCell>
                      <TableCell className="max-w-56">
                        <div className="truncate">{g.objectName}</div>
                        <div className="text-xs text-muted-foreground">
                          <Where g={g} />
                        </div>
                      </TableCell>
                      <TableCell>
                        <RiskMeter value={g.maxScore} threshold={g.threshold} label={g.alerts.length > 1 ? 'Наибольшая оценка' : 'Оценка модели'} />
                      </TableCell>
                      <TableCell>
                        <GroupOutcome g={g} />
                      </TableCell>
                      <TableCell className="pr-4">
                        <span className="flex justify-end gap-1" onClick={(e) => e.stopPropagation()}>
                          <Button
                            variant="ghost"
                            size="icon-sm"
                            aria-label="В журнал прогнозов"
                            title="В журнал прогнозов"
                            nativeButton={false}
                            render={<RunLink to={`/predictions?alert=true&prediction=${g.last.id}`} />}
                          >
                            <ScrollText />
                          </Button>
                          <Button
                            variant="ghost"
                            size="icon-sm"
                            aria-label="На карте"
                            title="На карте"
                            nativeButton={false}
                            render={<RunLink to={`/objects/${g.objectId}?channel=${g.last.channel_id}`} />}
                          >
                            <MapIcon />
                          </Button>
                        </span>
                      </TableCell>
                    </TableRow>
                  )
                })}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
      {list.data && list.data.total > ALERTS_LIMIT && (
        <p className="text-xs text-muted-foreground">
          Показаны последние {fmtInt(ALERTS_LIMIT)} тревог из {fmtInt(list.data.total)} — остальные в журнале прогнозов.
        </p>
      )}

      <PredictionPanel id={selected} onClose={() => s.set({ prediction: null }, true)} />
    </div>
  )
}
