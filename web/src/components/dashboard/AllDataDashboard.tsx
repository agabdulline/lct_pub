import { MonthlyChart, type MonthlySeries } from '@/components/dashboard/MonthlyChart'
import { DirectionBadge } from '@/components/domain/DirectionBadge'
import { QueryError } from '@/components/domain/QueryError'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { useDatasets, useDatasetsHistory, useHistorySummary } from '@/lib/api/queries'
import type { DatasetHistory } from '@/lib/api/types'
import { fmtDate, fmtInt, fmtPercent } from '@/lib/format'
import { Link } from 'react-router'

// Adjacent pairs pass the colour-vision check in the light theme (ΔE 24.3 for ППР / вне ППР — the history dashboard).
const ALARMS: MonthlySeries[] = [
  { key: 'ppr', label: 'Плановая проверка', color: 'var(--status-ppr)' },
  { key: 'other', label: 'Вне ППР', color: 'var(--status-alarm)' },
]
const EVENTS: MonthlySeries[] = [
  { key: 'plain', label: 'События', color: 'var(--status-ppr)' },
  { key: 'alarm', label: 'С флагом тревоги', color: 'var(--status-alarm)' },
]

function Fact({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="font-semibold tabular-nums">{value}</div>
    </div>
  )
}

/** The hackathon journal by month — the same base as «Вся история», shortened to sit beside the datasets. */
function HackathonBlock() {
  const q = useHistorySummary()
  const h = q.data
  if (q.isError) return <QueryError error={q.error} onRetry={() => void q.refetch()} what="История хакатона" compact />
  const months = h?.months ?? []
  const sum = (f: (m: (typeof months)[number]) => number) => months.reduce((s, m) => s + f(m), 0)
  const alarms = sum((m) => m.smoke_alarms)
  return (
    <Card className="relative">
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-2">
          Журнал хакатона <DirectionBadge direction="sensor" /> <DirectionBadge direction="access" />
        </CardTitle>
        <CardDescription>{h?.from ? `${fmtDate(h.from)} — ${fmtDate(h.to)} · дымовые тревоги по месяцам` : ' '}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Fact label="Дымовые тревоги" value={h ? fmtInt(alarms) : '—'} />
          <Fact label="Плановых проверок" value={h ? fmtPercent(alarms ? sum((m) => m.ppr_alarms) / alarms : null, 0) : '—'} />
          <Fact label="Неисправностей" value={h ? fmtInt(sum((m) => m.faults_started)) : '—'} />
          <Fact label="Вскрытий" value={h ? fmtInt(sum((m) => m.access_incidents)) : '—'} />
        </div>
        {h ? (
          <MonthlyChart
            data={months.map((m) => ({ month: m.month, ppr: m.ppr_alarms, other: m.smoke_alarms - m.ppr_alarms }))}
            series={ALARMS}
            unit="тревог"
          />
        ) : (
          <Skeleton className="h-56" />
        )}
        <Link to="/dashboard?run=0" className="text-sm underline-offset-4 hover:underline">
          Подробно — вся история хакатона
        </Link>
      </CardContent>
    </Card>
  )
}

/** A dataset's base run by month: its own time scale, never mixed with the hackathon's. */
function DatasetBlock({ d }: { d: DatasetHistory }) {
  const info = useDatasets().data?.find((x) => x.key === d.dataset)
  const events = d.months.reduce((s, m) => s + m.events, 0)
  const alarmEvents = d.months.reduce((s, m) => s + m.alarm_events, 0)
  const decisions = d.months.reduce((s, m) => s + m.decisions, 0)
  return (
    <Card className="relative">
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-2">
          {info?.short ?? info?.title ?? d.dataset} <DirectionBadge scenarioId={`dataset:${d.dataset}`} dataset={d.dataset} />
        </CardTitle>
        <CardDescription>
          {fmtDate(d.from)} — {fmtDate(d.to)} · внешний набор{info?.synthetic ? ', синтетика' : ''} · события по месяцам
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Fact label="Объектов · каналов" value={`${fmtInt(d.objects)} · ${fmtInt(d.channels)}`} />
          <Fact label="Событий" value={fmtInt(events)} />
          <Fact label="С флагом тревоги" value={fmtInt(alarmEvents)} />
          <Fact label="Решений моделей" value={decisions ? fmtInt(decisions) : 'не записаны'} />
        </div>
        <MonthlyChart
          data={d.months.map((m) => ({ month: m.month, plain: m.events - m.alarm_events, alarm: m.alarm_events }))}
          series={EVENTS}
          unit="событий"
        />
        <Link to={`/dashboard?run=${d.run_id}`} className="text-sm underline-offset-4 hover:underline">
          Подробно — история набора
        </Link>
      </CardContent>
    </Card>
  )
}

/**
 * «Все данные» (`?run=all`): the hackathon and every loaded dataset side by side — each block on its own monthly scale
 * (the datasets are 2014–2023, the hackathon 2019–2026), never one shared time axis.
 */
export function AllDataDashboard() {
  const datasets = useDatasetsHistory()
  return (
    <div className="mx-auto w-full max-w-7xl space-y-4">
      <div className="grid gap-4 lg:grid-cols-2">
        <HackathonBlock />
        {datasets.isError && <QueryError error={datasets.error} onRetry={() => void datasets.refetch()} what="История наборов" compact />}
        {datasets.isPending && <Skeleton className="h-80" />}
        {datasets.data?.map((d) => (
          <DatasetBlock key={d.dataset} d={d} />
        ))}
      </div>
    </div>
  )
}
