import { ChevronRight, ClipboardPlus, Map as MapIcon } from 'lucide-react'
import { useState } from 'react'
import { useParams } from 'react-router'

import { DailyValueChart } from '@/components/channel/DailyValueChart'
import { StateTape } from '@/components/channel/StateTape'
import { DatasetBadge } from '@/components/domain/DatasetBadge'
import { FactorList } from '@/components/domain/FactorList'
import { ObjectHistoryMark } from '@/components/domain/ObjectHistoryMark'
import { OutcomeBadge } from '@/components/domain/OutcomeBadge'
import { Pager } from '@/components/domain/Pager'
import { QueryError } from '@/components/domain/QueryError'
import { RiskMeter } from '@/components/domain/RiskMeter'
import { RunLink } from '@/components/domain/RunLink'
import { SensorTypeIcon } from '@/components/domain/SensorTypeIcon'
import { StatusBadge, StatusDot } from '@/components/domain/StatusBadge'
import { Button } from '@/components/ui/button'
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from '@/components/ui/empty'
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { CreateWorkOrderDialog } from '@/components/work-orders/CreateWorkOrderDialog'
import { ApiError } from '@/lib/api/client'
import { useAlarmDecision, useChannel, useChannelPredictions, useChannelTimeline, useLastScoredPrediction } from '@/lib/api/queries'
import type { Prediction } from '@/lib/api/types'
import { fmtDate, fmtDateTime, fmtDateTimeShort, fmtDuration, fmtInt, fmtSince } from '@/lib/format'
import { modelLabel, OBJECT_HISTORY_NOTE, PRIORITY_LABEL, PRIORITY_STATUS, RULE_LABEL, skipReasonLabel, WO_STATUS_LABEL, WO_STATUS_STATUS } from '@/lib/labels'
import { useSearchState } from '@/lib/search-state'
import { useSimNow } from '@/lib/sim-now'

const WINDOWS = [
  { value: '1', label: 'Сутки' },
  { value: '7', label: '7 суток' },
  { value: '30', label: '30 суток' },
]
const NUMERIC = new Set(['Газовый датчик', 'Датчик температуры'])

function Meta({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-3 py-1 text-sm">
      <span className="text-muted-foreground">{label}</span>
      <span className="min-w-0 text-right">{children}</span>
    </div>
  )
}

function PredictionSummary({ title, p, inherited }: { title: string; p: Prediction | null; inherited?: boolean }) {
  return (
    <div className="space-y-1">
      <div className="text-sm font-medium">{title}</div>
      {!p && <div className="text-sm text-muted-foreground">Решений не было</div>}
      {p && p.status === 'scored' && (
        <div className="flex flex-wrap items-center gap-3 text-sm">
          <RiskMeter value={p.score} threshold={p.threshold} />
          <OutcomeBadge outcome={p.outcome} alert={p.alert} outcomeAt={p.outcome_at} horizonEnd={p.horizon_end} source={p.outcome_source} />
          <span className="text-xs text-muted-foreground">
            {fmtDateTimeShort(p.decision_time)}
            {inherited && ' · повторные тревоги за сутки — без нового решения'}
          </span>
        </div>
      )}
      {p && p.status !== 'scored' && (
        <div className="text-sm text-muted-foreground">
          Пропуск: {p.skip_reason ? skipReasonLabel(p.skip_reason).toLowerCase() : '—'} · {fmtDateTimeShort(p.decision_time)}
        </div>
      )}
    </div>
  )
}

// Channel card (docs/product/v1-scope.md, экран 4): metadata, state, tape, faults, forecasts with outcomes, factors, orders.
export function ChannelPage() {
  const id = Number(useParams().id)
  const valid = Number.isInteger(id) && id > 0
  const s = useSearchState()
  const simNow = useSimNow()
  const days = Number(s.get('days') ?? 7)
  const page = s.num('page') ?? 1
  const [woOpen, setWoOpen] = useState(false)

  const channel = useChannel(valid ? id : null)
  // Window end is the run's current moment rounded to the hour: stable query key under a running clock.
  const to = simNow ? new Date(Math.ceil(new Date(simNow).getTime() / 3_600_000) * 3_600_000).toISOString() : undefined
  const from = to ? new Date(new Date(to).getTime() - days * 86_400_000).toISOString() : undefined
  const timeline = useChannelTimeline(valid && to ? id : null, { from, to })
  const predictions = useChannelPredictions(valid ? id : null, { page, size: 10 })
  const lastScored = useLastScoredPrediction(valid ? id : null)
  const alarm = useAlarmDecision(channel.data)

  const c = channel.data
  if (channel.isError && !(channel.error instanceof ApiError && channel.error.status === 404))
    return <QueryError error={channel.error} onRetry={() => void channel.refetch()} what="Карточка канала" />
  if (!valid || channel.isError)
    return (
      <Empty>
        <EmptyHeader>
          <EmptyTitle>Канал не найден</EmptyTitle>
          <EmptyDescription>{channel.error?.message ?? 'Неверный идентификатор канала'}</EmptyDescription>
        </EmptyHeader>
      </Empty>
    )
  if (!c) return <Skeleton className="mx-auto h-96 w-full max-w-7xl" />

  const faults = (timeline.data?.episodes ?? []).filter((e) => e.kind === 'fault').reverse()
  // The last decision is often skipped (one per channel per 24 h): factors come from the last scored one.
  const scored = lastScored.data?.items[0]
  const lastFactors = scored?.factors?.length ? scored : null

  return (
    <div className="mx-auto w-full max-w-7xl space-y-4">
      <div className="space-y-2">
        <div className="flex items-center gap-1 text-sm text-muted-foreground">
          <RunLink to={`/objects/${c.object_id}`} className="hover:text-foreground">
            {c.object_name}
          </RunLink>
          <ChevronRight className="size-3.5" />
          <RunLink to={`/objects/${c.sub_object_id}`} className="hover:text-foreground">
            {c.sub_object_name}
          </RunLink>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <SensorTypeIcon type={c.sensor_type} className="size-6" />
          <h1 className="text-xl font-semibold">{c.name}</h1>
          <StatusBadge status={c.status} />
          {c.source && c.source !== 'hackathon' && <DatasetBadge dataset={c.source} />}
          {c.object_history === false && <ObjectHistoryMark />}
          <div className="ml-auto flex gap-2">
            <Button variant="outline" size="sm" nativeButton={false} render={<RunLink to={{ pathname: `/objects/${c.sub_object_id}`, search: `?channel=${c.id}` }} />}>
              <MapIcon /> На карте
            </Button>
            <Button size="sm" onClick={() => setWoOpen(true)}>
              <ClipboardPlus /> Заявка
            </Button>
          </div>
        </div>
        {c.object_history === false && (
          <p className="text-sm text-muted-foreground">
            {OBJECT_HISTORY_NOTE} Для таких каналов работает правило «повторные одиночные тревоги» без ML.
          </p>
        )}
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <Card>
          <CardHeader>
            <CardTitle>Текущее состояние</CardTitle>
          </CardHeader>
          <CardContent>
            <Meta label="Значение">{c.value ?? '—'}</Meta>
            <Meta label="С">{c.value_ts ? `${fmtDateTimeShort(c.value_ts)} · ${fmtSince(c.value_ts, simNow)}` : '—'}</Meta>
            {c.data_gap && c.fault_since && c.fault_since < c.data_gap.end_ts && (
              <p className="py-1 text-xs text-muted-foreground">
                В неисправности на начало журнала, данных с {fmtDate(c.data_gap.start_ts)} по {fmtDate(c.data_gap.end_ts)} нет — длительность для
                заявки считается с начала журнала.
              </p>
            )}
            <Meta label="Тип">{c.sensor_type}</Meta>
            <Meta label="Подсистема">{c.subsystem}</Meta>
            <Meta label="Тег">
              <span className="font-mono text-xs">{c.tag}</span>
            </Meta>
            <Meta label="Группа по тегу">{c.tag_group ?? '—'}</Meta>
            <Meta label="Пикет">{c.picket_number !== null ? `ПК${c.picket_number}${c.picket_offset ? `+${c.picket_offset}` : ''}` : '—'}</Meta>
            <Meta label="id канала">
              <span className="font-mono text-xs">{c.id}</span>
            </Meta>
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Прогнозы</CardTitle>
            <CardDescription>Последние решения обеих моделей</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-1">
              <div className="text-sm font-medium">Риск отказа через 1–30 суток</div>
              {c.risk === null ? (
                <div className="text-sm text-muted-foreground">Тип канала не покрыт ежедневной моделью</div>
              ) : (
                <RiskMeter value={c.risk} threshold={c.risk_threshold} label="Ежедневная модель" />
              )}
            </div>
            <PredictionSummary title="Ежедневная модель" p={c.last_daily_prediction} />
            <PredictionSummary title="Событийная — после тревоги" p={alarm.decision} inherited={alarm.inherited} />
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>История</CardTitle>
          </CardHeader>
          <CardContent>
            <Meta label="Тревог за 30 суток">{fmtInt(c.counters.alarms_30d)}</Meta>
            <Meta label="из них одиночных">{fmtInt(c.counters.solitary_alarms_30d)}</Meta>
            <Meta label="Отказов за год">{fmtInt(c.counters.faults_365d)}</Meta>
            <Meta label="Часов в отказе за 30 суток">{fmtInt(c.counters.fault_hours_30d)}</Meta>
            <Meta label="Последний отказ">{fmtDateTime(c.counters.last_fault_at)}</Meta>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Лента состояний</CardTitle>
          <CardDescription>
            {from && to ? `${fmtDateTimeShort(from)} — ${fmtDateTimeShort(to)}` : ' '} · отсутствие событий — не признак исправности
          </CardDescription>
          <CardAction>
            <ToggleGroup value={[String(days)]} onValueChange={(v) => v[0] && s.set({ days: v[0] === '7' ? null : v[0] }, true)} size="sm" variant="outline">
              {WINDOWS.map((w) => (
                <ToggleGroupItem key={w.value} value={w.value}>
                  {w.label}
                </ToggleGroupItem>
              ))}
            </ToggleGroup>
          </CardAction>
        </CardHeader>
        <CardContent className="space-y-4">
          {timeline.data ? <StateTape timeline={timeline.data} /> : <Skeleton className="h-20" />}
          {timeline.data && NUMERIC.has(c.sensor_type) && (
            <div>
              <div className="mb-1 text-sm font-medium">Показания по суткам</div>
              <DailyValueChart daily={timeline.data.daily} unit={c.sensor_type === 'Датчик температуры' ? '°' : undefined} />
            </div>
          )}
        </CardContent>
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Неисправности за период</CardTitle>
            <CardDescription>{faults.length ? `эпизодов: ${faults.length}` : 'не было'}</CardDescription>
          </CardHeader>
          {faults.length > 0 && (
            <CardContent className="max-h-72 overflow-y-auto px-0">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="pl-4">Начало</TableHead>
                    <TableHead>Конец</TableHead>
                    <TableHead className="text-right">Длительность</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {faults.map((f) => (
                    <TableRow key={f.start_ts}>
                      <TableCell className="pl-4 text-xs tabular-nums">{fmtDateTimeShort(f.start_ts)}</TableCell>
                      <TableCell className="text-xs tabular-nums">{f.end_ts ? fmtDateTimeShort(f.end_ts) : <StatusBadge status="fault">продолжается</StatusBadge>}</TableCell>
                      <TableCell className="text-right text-xs tabular-nums">
                        {f.end_ts ? fmtDuration((new Date(f.end_ts).getTime() - new Date(f.start_ts).getTime()) / 1000) : '—'}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </CardContent>
          )}
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Факторы последней оценки</CardTitle>
            <CardDescription>
              {lastFactors
                ? `${modelLabel(lastFactors.model)} · ${lastFactors.model_version} · ${fmtDateTimeShort(lastFactors.decision_time)}`
                : lastScored.isPending
                  ? ' '
                  : c.last_alarm_prediction || c.last_daily_prediction
                    ? 'оценённых решений нет — все пропущены правилами допустимости'
                    : 'прогнозов нет'}
            </CardDescription>
          </CardHeader>
          <CardContent>{lastFactors && <FactorList factors={lastFactors.factors} />}</CardContent>
        </Card>
      </div>

      <Tabs defaultValue="predictions">
        <TabsList>
          <TabsTrigger value="predictions">Прогнозы с исходами {predictions.data ? `· ${fmtInt(predictions.data.total)}` : ''}</TabsTrigger>
          <TabsTrigger value="orders">Открытые заявки · {c.open_work_orders.length}</TabsTrigger>
          <TabsTrigger value="neighbors">Группа тега · {c.neighbors.length}</TabsTrigger>
        </TabsList>
        <TabsContent value="predictions" className="space-y-3">
          <Card className="py-0">
            <CardContent className="px-0">
              {predictions.data?.items.length === 0 && <p className="p-4 text-sm text-muted-foreground">По каналу прогнозов не было</p>}
              {!!predictions.data?.items.length && (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead className="pl-4">Решение</TableHead>
                      <TableHead>Модель</TableHead>
                      <TableHead>Оценка</TableHead>
                      <TableHead>Горизонт</TableHead>
                      <TableHead>Исход</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {predictions.data.items.map((p) => (
                      <TableRow key={p.id}>
                        <TableCell className="pl-4 text-xs tabular-nums">{fmtDateTimeShort(p.decision_time)}</TableCell>
                        <TableCell className="text-xs">
                          {modelLabel(p.model)} <span className="text-muted-foreground">· {p.model_version}</span>
                        </TableCell>
                        <TableCell>
                          {p.status === 'scored' ? (
                            <RiskMeter value={p.score} threshold={p.threshold} />
                          ) : (
                            <span className="text-xs text-muted-foreground">{p.skip_reason ? skipReasonLabel(p.skip_reason) : 'пропуск'}</span>
                          )}
                        </TableCell>
                        <TableCell className="text-xs text-muted-foreground tabular-nums">
                          {fmtDateTimeShort(p.horizon_start)} — {fmtDateTimeShort(p.horizon_end)}
                        </TableCell>
                        <TableCell>
                          <OutcomeBadge outcome={p.outcome} alert={p.alert} outcomeAt={p.outcome_at} horizonEnd={p.horizon_end} source={p.outcome_source} />
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
            </CardContent>
          </Card>
          {predictions.data && predictions.data.total > 10 && (
            <Pager page={page} size={10} total={predictions.data.total} onPage={(p) => s.set({ page: p > 1 ? p : null }, true)} noun="прогнозов" />
          )}
        </TabsContent>
        <TabsContent value="orders">
          <Card>
            <CardContent className="space-y-2">
              {c.open_work_orders.length === 0 && <p className="text-sm text-muted-foreground">Открытых заявок нет</p>}
              {c.open_work_orders.map((w) => (
                <RunLink key={w.id} to={`/work-orders/${w.id}`} className="flex items-center gap-3 rounded-lg p-2 ring-1 ring-foreground/10 hover:bg-muted">
                  <span className="font-medium">№{w.id}</span>
                  <span className="min-w-0 flex-1 truncate text-sm">{w.title}</span>
                  <span className="text-xs text-muted-foreground">{RULE_LABEL[w.rule]}{w.due_date ? ` · срок ${fmtDate(w.due_date)}` : ''}</span>
                  <StatusBadge status={PRIORITY_STATUS[w.priority]}>{PRIORITY_LABEL[w.priority]}</StatusBadge>
                  <StatusBadge status={WO_STATUS_STATUS[w.status]}>{WO_STATUS_LABEL[w.status]}</StatusBadge>
                </RunLink>
              ))}
            </CardContent>
          </Card>
        </TabsContent>
        <TabsContent value="neighbors">
          <Card>
            <CardHeader>
              <CardDescription>
                Каналы той же группы по тегу ({c.tag_group ?? '—'}) — соседство, на которое смотрит модель. Группа — гипотеза
                электрической группы, не подтверждённая схема.
              </CardDescription>
            </CardHeader>
            <CardContent className="grid gap-x-6 gap-y-1 sm:grid-cols-2 lg:grid-cols-3">
              {c.neighbors.map((n) => (
                <RunLink key={n.id} to={`/channels/${n.id}`} className="flex items-center gap-2 py-0.5 text-sm hover:underline">
                  <StatusDot status={n.status} />
                  <SensorTypeIcon type={n.sensor_type} withTooltip={false} className="size-3.5" />
                  <span className="truncate">{n.name}</span>
                </RunLink>
              ))}
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>

      {woOpen && <CreateWorkOrderDialog key={c.id} open={woOpen} onOpenChange={setWoOpen} channel={c} now={simNow} />}
    </div>
  )
}
