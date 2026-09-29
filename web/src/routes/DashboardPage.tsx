import { BellRing, CalendarCheck, ClipboardList, Gauge, ScrollText, Wrench } from 'lucide-react'

import { AlarmsByHourChart } from '@/components/dashboard/AlarmsByHourChart'
import { RiskHeatmap } from '@/components/dashboard/RiskHeatmap'
import { ChannelLink } from '@/components/domain/ChannelLink'
import { MetricCard } from '@/components/domain/MetricCard'
import { QueryError } from '@/components/domain/QueryError'
import { RiskMeter } from '@/components/domain/RiskMeter'
import { RunLink } from '@/components/domain/RunLink'
import { StatusDot } from '@/components/domain/StatusBadge'
import { DatasetDashboard } from '@/components/external/DatasetDashboard'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { useDashboardRisk, useDashboardSummary, useModels } from '@/lib/api/queries'
import type { LivePR, SensorType } from '@/lib/api/types'
import { fmtDateTime, fmtInt, fmtPercent } from '@/lib/format'
import { historyNote } from '@/lib/labels'
import { useRun } from '@/lib/run'
import { cn } from '@/lib/utils'

type Confirmed = { all: number; confirmed: number } | null

// Alerts already confirmed by a fault come with the live summary itself.
const confirmedOf = (pr: LivePR | undefined): Confirmed => (pr ? { all: pr.alerts, confirmed: pr.alerts_confirmed } : null)

/** Live P/R tile text: always with its denominator (AGENTS.md §5, «Честные метрики»). */
function prHint(pr: LivePR | undefined, c: Confirmed) {
  if (!pr) return '—'
  if (!pr.decisions && c?.confirmed)
    return `${fmtInt(c.confirmed)} из ${fmtInt(c.all)} тревог уже подтвердились отказом — P не ниже этой доли; закрытых решений пока нет`
  if (!pr.decisions) return 'Закрытых решений пока нет — горизонт прогноза не истёк'
  const base = `по ${fmtInt(pr.decisions)} закрытым решениям, положительных ${fmtInt(pr.positives)}`
  const history = historyNote(pr.history, pr.decisions)
  const full = history ? `${base}; ${history}` : base
  return pr.positives < 20 ? `${full} — мало положительных для устойчивой оценки` : full
}

// Nothing closed yet: the lower bound of Precision from alerts already confirmed by faults (MetricsPanel).
// «94% / 100%» does not fit a sixth of the row below 1536 px at full KPI size: one line, a step smaller.
const prValue = (pr: LivePR | undefined, c: Confirmed) => (
  <span className="whitespace-nowrap text-2xl 2xl:text-3xl">
    {pr && pr.decisions
      ? `${pr.alerts ? fmtPercent(pr.precision, 0) : '—'} / ${fmtPercent(pr.recall, 0)}`
      : pr && c?.confirmed
        ? `≥ ${fmtPercent(c.confirmed / c.all, 0)} / —`
        : '—'}
  </span>
)

function FleetBar({ total, risk, fault }: { total: number; risk: number; fault: number }) {
  const pct = (n: number) => `${(n / Math.max(1, total)) * 100}%`
  return (
    <div className="space-y-2">
      <div className="flex h-2.5 w-full gap-0.5 overflow-hidden rounded-full bg-status-ok/25">
        <span className="h-full rounded-l-full bg-status-fault" style={{ width: pct(fault) }} />
        <span className="h-full bg-status-risk" style={{ width: pct(risk) }} />
      </div>
      <div className="space-y-1 text-sm">
        {[
          { status: 'fault' as const, label: 'Неисправны', n: fault },
          { status: 'risk' as const, label: 'В риске отказа', n: risk },
          { status: 'ok' as const, label: 'Остальные', n: total - risk - fault },
        ].map((row) => (
          <div key={row.status} className="flex items-center gap-2">
            <StatusDot status={row.status} />
            {row.label}
            <span className="ml-auto font-medium tabular-nums">{fmtInt(row.n)}</span>
            <span className="w-14 text-right text-xs text-muted-foreground tabular-nums">{fmtPercent(row.n / Math.max(1, total))}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

// Dashboard (docs/product/v1-scope.md, экран 2). A data set run gets its models' dashboard instead (stage 14).
export function DashboardPage() {
  const { run } = useRun()
  return run?.dataset ? <DatasetDashboard /> : <HackathonDashboard />
}

// The hackathon dashboard: KPI, alarms by hour, top channels by the daily model, object × type.
function HackathonDashboard() {
  const summary = useDashboardSummary()
  const risk = useDashboardRisk()
  const models = useModels()
  const s = summary.data
  const r = risk.data

  const daily = models.data?.items.find((m) => m.model === 'daily' && m.active)
  const types: SensorType[] = daily?.sensor_types.length
    ? daily.sensor_types
    : [...new Set(r?.by_object.flatMap((o) => o.by_sensor_type.filter((t) => t.max_risk !== null).map((t) => t.sensor_type)) ?? [])]

  return (
    <div className="mx-auto w-full max-w-7xl space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <h1 className="text-xl font-semibold">Дашборд</h1>
          <p className="text-sm text-muted-foreground">Оперативная сводка и каналы с наибольшим риском отказа через 1–30 суток.</p>
        </div>
        {s && <span className="text-xs text-muted-foreground">на {fmtDateTime(s.now)}</span>}
      </div>

      {summary.isError && <QueryError error={summary.error} onRetry={() => void summary.refetch()} what="Сводка дашборда" compact />}
      <div className={cn('grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6', summary.isError && 'hidden', summary.isFetching && s && 'opacity-90')}>
        <MetricCard
          title="Каналы в тревоге"
          icon={BellRing}
          tone={s?.active_alarms ? 'alarm' : undefined}
          value={fmtInt(s?.active_alarms)}
          loading={!s}
          hint={
            s &&
            `сейчас, все типы датчиков — тревога может длиться сутками; новых дымовых тревог за 24 ч ${s.alarms_24h ? `— ${fmtInt(s.alarms_24h)}` : 'не было'}`
          }
        />
        <MetricCard
          title="Доля ППР за 24 ч"
          icon={CalendarCheck}
          tone="ppr"
          value={fmtPercent(s?.ppr_share_24h, 0)}
          loading={!s}
          hint={s && (s.alarms_24h ? `${fmtInt(Math.round((s.ppr_share_24h ?? 0) * s.alarms_24h))} из ${fmtInt(s.alarms_24h)} — плановая проверка` : 'тревог за сутки не было')}
        />
        <MetricCard
          title="Прогнозы с тревогой за 24 ч"
          icon={ScrollText}
          tone={s?.alert_predictions_24h ? 'risk' : undefined}
          value={fmtInt(s?.alert_predictions_24h)}
          loading={!s}
          hint="обе модели, выше порога"
        />
        <MetricCard
          title="Открытые заявки"
          icon={ClipboardList}
          value={
            <RunLink to="/work-orders?status=new,in_progress" className="underline-offset-4 hover:underline">
              {fmtInt(s?.open_work_orders)}
            </RunLink>
          }
          loading={!s}
          hint={s && (s.overdue_work_orders ? <span className="text-status-alarm">просрочено {fmtInt(s.overdue_work_orders)}</span> : 'просроченных нет')}
        />
        <MetricCard title="Событийная: P / R" icon={Gauge} value={prValue(s?.live.smoke_alarm, confirmedOf(s?.live.smoke_alarm))} loading={!s} hint={prHint(s?.live.smoke_alarm, confirmedOf(s?.live.smoke_alarm))} />
        <MetricCard title="Ежедневная: P / R" icon={Wrench} value={prValue(s?.live.daily, confirmedOf(s?.live.daily))} loading={!s} hint={prHint(s?.live.daily, confirmedOf(s?.live.daily))} />
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <Card className="relative lg:col-span-2">
          <CardHeader>
            <CardTitle>Тревоги по часам</CardTitle>
            <CardDescription>24 часа до текущего времени прогона, по вердиктам</CardDescription>
          </CardHeader>
          <CardContent>
            {s ? (
              <AlarmsByHourChart data={s.alarms_by_hour} />
            ) : summary.isError ? (
              <p className="text-sm text-muted-foreground">Нет данных сводки</p>
            ) : (
              <Skeleton className="h-64" />
            )}
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Состояние каналов</CardTitle>
            <CardDescription>{s ? `всего ${fmtInt(s.channels_total)}` : ' '}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            {s ? (
              <FleetBar total={s.channels_total} risk={s.channels_at_risk} fault={s.channels_in_fault} />
            ) : summary.isError ? null : (
              <Skeleton className="h-16" />
            )}
            <p className="text-xs text-muted-foreground">
              {daily
                ? `В риске — оценка ежедневной модели ${daily.version} выше порога${daily.threshold !== null ? ` ${daily.threshold.toFixed(2).replace('.', ',')}` : ''}.${
                    daily.sensor_types.length ? ` Модель покрывает ${daily.sensor_types.length} типов каналов; остальные в риск не попадают.` : ''
                  }`
                : 'В риске — оценка ежедневной модели выше порога. Ежедневная модель в сервис пока не загружена.'}
            </p>
            {daily?.offline_metrics && (
              <p className="text-xs text-muted-foreground">
                Офлайн {daily.version}: Precision {fmtPercent(daily.offline_metrics.precision, 0)}, Recall{' '}
                {fmtPercent(daily.offline_metrics.recall, 0)} — модель находит {fmtPercent(daily.offline_metrics.recall, 0)} будущих
                отказов. Список ниже — «на что посмотреть в первую очередь», а не все, что откажут.
              </p>
            )}
          </CardContent>
        </Card>
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Топ-20 каналов по риску</CardTitle>
            <CardDescription>Ежедневная модель, отказ через 1–30 суток; неисправные сейчас не показаны</CardDescription>
          </CardHeader>
          <CardContent className="px-0">
            {!r && !risk.isError && <Skeleton className="mx-4 h-80" />}
            {risk.isError && <QueryError error={risk.error} onRetry={() => void risk.refetch()} what="Риск каналов" compact className="mx-4" />}
            {r && (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="w-8 pl-4">#</TableHead>
                    <TableHead>Канал</TableHead>
                    <TableHead>Объект</TableHead>
                    <TableHead className="w-32">Риск</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {r.top.length === 0 && (
                    <TableRow>
                      <TableCell colSpan={4} className="py-6 text-center text-sm text-muted-foreground">
                        Ежедневного прогноза в этом прогоне ещё не было — оценок риска нет
                      </TableCell>
                    </TableRow>
                  )}
                  {r.top.map((c, i) => (
                    <TableRow key={c.id}>
                      <TableCell className="pl-4 text-xs text-muted-foreground tabular-nums">{i + 1}</TableCell>
                      <TableCell className="max-w-48">
                        <ChannelLink id={c.id} name={c.name} sensorType={c.sensor_type} status={c.status} />
                      </TableCell>
                      <TableCell className="max-w-48">
                        <div className="truncate">{c.object_name}</div>
                        <div className="truncate text-xs text-muted-foreground">{c.sub_object_name}</div>
                      </TableCell>
                      <TableCell>
                        <RiskMeter value={c.risk} threshold={c.risk_threshold} />
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Риск: объект × тип датчика</CardTitle>
            <CardDescription>Число каналов в риске; цвет — их доля среди каналов типа на объекте</CardDescription>
          </CardHeader>
          <CardContent>
            {r ? (
              types.length ? (
                <RiskHeatmap rows={r.by_object} types={types} />
              ) : (
                <p className="text-sm text-muted-foreground">Оценок риска по типам датчиков пока нет — ежедневный прогноз не выполнялся.</p>
              )
            ) : risk.isError ? (
              <p className="text-sm text-muted-foreground">Нет данных риска</p>
            ) : (
              <Skeleton className="h-80" />
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
