import { BellRing, ClipboardList, FileText, Gauge, ScrollText } from 'lucide-react'

import { MetricCard } from '@/components/domain/MetricCard'
import { QueryError } from '@/components/domain/QueryError'
import { RunLink } from '@/components/domain/RunLink'
import { StatusBadge } from '@/components/domain/StatusBadge'
import { DatasetFacts } from '@/components/emulator/DatasetFacts'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { useExternalModels, useMetrics, useModels, useWorkOrders } from '@/lib/api/queries'
import type { Metrics, ModelInfo, WorkOrderRule } from '@/lib/api/types'
import { fmtDate, fmtDateTime, fmtDateTimeShort, fmtDuration, fmtInt, fmtPercent, fmtTime, plural } from '@/lib/format'
import { externalHorizonHours, PRIORITY_LABEL, RULE_LABEL, triggerLabel, WO_STATUS_LABEL, WO_STATUS_STATUS } from '@/lib/labels'
import { useRun } from '@/lib/run'
import { useSimNow } from '@/lib/sim-now'
import { cn } from '@/lib/utils'

import { ClassMark, GroupOutcome, Where } from './AlertParts'
import { alertClassLabel, ALERTS_LIMIT, isOngoing, useRunAlertGroups, type AlertGroup } from './external-alerts'

/** Work orders opened by the data sets' models — a run of one data set has only its own. */
const EXT_RULES: WorkOrderRule[] = ['ext_fire_alert', 'ext_access_violation', 'ext_pdm_failure']
const RECENT = 8

/**
 * Live P/R of an external model by the data set's labels, always with its denominators (AGENTS.md §5). A forecaster
 * (Azure PdM) keeps decisions open until its horizon ends; a detector (Magdeburg, VAST) closes them at once.
 */
function ModelTile({ m, metrics }: { m: ModelInfo; metrics: Metrics | undefined }) {
  const b = metrics?.all
  const horizon = externalHorizonHours(m, metrics)
  const value =
    b && b.decisions ? (
      <span className="whitespace-nowrap text-2xl 2xl:text-3xl">
        {b.alerts ? fmtPercent(b.precision, 0) : '—'} / {b.positives ? fmtPercent(b.recall, 0) : '—'}
      </span>
    ) : (
      '—'
    )
  const hint = !metrics
    ? undefined
    : !b?.decisions
      ? horizon > 0
        ? `закрытых решений пока нет — исход после конца горизонта ${fmtInt(horizon)} ч`
        : 'решений с разметкой пока нет'
      : [
          `по ${fmtInt(b.decisions)} ${horizon > 0 ? 'закрытым решениям' : 'решениям с разметкой'}, ${horizon > 0 ? 'отказов' : 'событий'} по разметке ${fmtInt(b.positives)}`,
          b.positives === 0 ? 'Recall не определён' : b.positives < 20 ? 'мало для устойчивой оценки' : null,
          metrics.open
            ? horizon > 0
              ? `ещё ${fmtInt(metrics.open)} — горизонт не истёк к концу прогона`
              : `ещё ${fmtInt(metrics.open)} — без разметки`
            : null,
        ]
          .filter(Boolean)
          .join('; ')
  return <MetricCard title={`${m.title || m.model}: P / R`} icon={Gauge} value={value} loading={!metrics} hint={hint} />
}

/** One model of the data set: how it is called, what its outcome is, live against offline, where to read more. */
function ModelRow({ m, metrics }: { m: ModelInfo; metrics: Metrics | undefined }) {
  const b = metrics?.all
  const horizon = externalHorizonHours(m, metrics)
  const ref = m.offline?.fire_vs_nuisance ?? m.offline?.test
  return (
    <div className="space-y-2 rounded-lg border p-3 text-sm">
      <div>
        <div className="font-medium">{m.title || m.model}</div>
        <div className="text-xs text-muted-foreground">
          {m.model} · {m.version} · решение {triggerLabel(m.trigger)} ·{' '}
          {horizon > 0 ? `прогноз отказа в ближайшие ${fmtInt(horizon)} ч` : 'обнаружение в момент решения'}
        </div>
      </div>
      <div className="grid grid-cols-[auto_1fr_1fr] gap-x-4 gap-y-1 tabular-nums">
        <span />
        <span className="text-xs text-muted-foreground">Precision</span>
        <span className="text-xs text-muted-foreground">Recall</span>
        <span className="text-muted-foreground">Этот прогон</span>
        <span>{b?.decisions && b.alerts ? fmtPercent(b.precision) : '—'}</span>
        <span>{b?.decisions && b.positives ? fmtPercent(b.recall) : '—'}</span>
        <span className="text-muted-foreground">Офлайн{m.offline?.fire_vs_nuisance ? ', пожар против помехи' : ''}</span>
        <span>{fmtPercent(ref?.precision)}</span>
        <span>{fmtPercent(ref?.recall)}</span>
      </div>
      {horizon > 0 && b?.lead_time && (
        <div className="text-xs text-muted-foreground">
          Упреждение до начала отказа по разметке: медиана {fmtDuration(b.lead_time.p50_s)}, 10–90% {fmtDuration(b.lead_time.p10_s)} — {fmtDuration(b.lead_time.p90_s)}.
        </div>
      )}
      <div className="flex flex-wrap gap-x-4 gap-y-1">
        <RunLink to={`/predictions?model=${m.model}`} className="inline-flex items-center gap-1 underline-offset-4 hover:underline">
          <ScrollText className="size-3.5" /> Журнал прогнозов
        </RunLink>
        <RunLink to="/models?model=external" className="inline-flex items-center gap-1 underline-offset-4 hover:underline">
          <FileText className="size-3.5" /> Отчёт модели
        </RunLink>
      </div>
    </div>
  )
}

function useModelMetrics(models: ModelInfo[]) {
  // A data set has one or two models: a fixed number of hooks, idle without a model.
  const first = useMetrics(models[0]?.model ?? null)
  const second = useMetrics(models[1]?.model ?? null)
  return (m: ModelInfo) => (m.model === models[0]?.model ? first.data : m.model === models[1]?.model ? second.data : undefined)
}

const when = (g: AlertGroup) => {
  const first = fmtDateTimeShort(g.first.decision_time)
  if (!g.episode || g.alerts.length === 1) return first
  const sameDay = fmtDate(g.last.decision_time) === fmtDate(g.first.decision_time)
  return `${first} — ${sameDay ? fmtTime(g.last.decision_time).slice(0, 5) : fmtDateTimeShort(g.last.decision_time)}`
}

/**
 * Dashboard of a data set run (stage 14): the data set's models instead of the hackathon blocks — its alerts and
 * episodes, the work orders they opened, live P/R by the data set's labels with the same horizon caveat as the
 * journal, and where the model's report is. Nothing about the smoke alarm or the daily model: they do not run here.
 */
export function DatasetDashboard() {
  const { run } = useRun()
  const simNow = useSimNow()
  const models = useExternalModels(run?.dataset)
  const modelsPending = useModels().isPending
  const { list, groups } = useRunAlertGroups(models)
  const metricsOf = useModelMetrics(models)
  const orders = useWorkOrders({ rule: EXT_RULES.join(','), size: 5 })
  const openOrders = useWorkOrders({ rule: EXT_RULES.join(','), status: 'new,in_progress', size: 1 })

  const running = run?.status === 'running' || run?.status === 'paused'
  const alerts = groups.reduce((n, g) => n + g.alerts.length, 0)
  const episodes = groups.filter((g) => g.episode).length
  const ongoing = groups.filter((g) => isOngoing(g, simNow, running)).length
  const classes = [...new Set(groups.map((g) => g.cls))].map((c) => {
    const of = groups.filter((g) => g.cls === c)
    return { cls: c, rows: of.length, alerts: of.reduce((n, g) => n + g.alerts.length, 0), fault: of.reduce((n, g) => n + g.fault, 0) }
  })
  const truncated = (list.data?.total ?? 0) > ALERTS_LIMIT
  // The work orders page filters by one rule: a data set's orders have one, link with it.
  const rules = [...new Set(orders.data?.items.map((w) => w.rule) ?? [])]
  const ordersLink = rules.length === 1 ? `/work-orders?rule=${rules[0]}` : '/work-orders'

  return (
    <div className="mx-auto w-full max-w-7xl space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-2">
        {simNow && <span className="text-xs text-muted-foreground">на {fmtDateTime(simNow)}</span>}
      </div>

      <div className={cn('grid gap-3 sm:grid-cols-2', models.length > 1 ? 'lg:grid-cols-4' : 'lg:grid-cols-3')}>
        <MetricCard
          title="Тревоги моделей набора"
          icon={BellRing}
          tone={alerts ? 'alarm' : undefined}
          value={
            <RunLink to="/" className="underline-offset-4 hover:underline">
              {fmtInt(alerts)}
            </RunLink>
          }
          loading={!list.data}
          hint={
            list.data &&
            [
              episodes ? `${fmtInt(episodes)} ${plural(episodes, ['эпизод', 'эпизода', 'эпизодов'])}` : `${fmtInt(groups.length)} ${plural(groups.length, ['строка', 'строки', 'строк'])} ленты`,
              running ? `сейчас идёт ${fmtInt(ongoing)}` : null,
              truncated ? `показаны последние ${fmtInt(ALERTS_LIMIT)} из ${fmtInt(list.data.total)}` : null,
            ]
              .filter(Boolean)
              .join(' · ')
          }
        />
        <MetricCard
          title="Заявки по тревогам"
          icon={ClipboardList}
          value={
            <RunLink to={ordersLink} className="underline-offset-4 hover:underline">
              {fmtInt(openOrders.data?.total)}
            </RunLink>
          }
          loading={!openOrders.data}
          hint={orders.data && `открыто; всего ${fmtInt(orders.data.total)} — заявки моделей набора`}
        />
        {models.map((m) => (
          <ModelTile key={m.model} m={m} metrics={metricsOf(m)} />
        ))}
        {!models.length && !modelsPending && (
          <MetricCard title="Модели набора" icon={Gauge} value="—" hint="модели этого набора в ML-сервис не загружены" />
        )}
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader>
            <CardTitle>Тревоги и эпизоды</CardTitle>
            <CardDescription>
              Подряд идущие тревоги периодической модели — один эпизод; исход — по разметке набора.{' '}
              <RunLink to="/" className="text-foreground underline-offset-4 hover:underline">
                Вся лента
              </RunLink>
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4 px-0">
            {list.isError && <QueryError error={list.error} onRetry={() => void list.refetch()} what="Тревоги прогона" compact className="mx-4" />}
            {!list.data && !list.isError && <Skeleton className="mx-4 h-48" />}
            {list.data && groups.length === 0 && (
              <p className="px-4 text-sm text-muted-foreground">
                {run?.kind === 'dataset' ? 'Это вся история набора: модели на ней не запускаются, тревог нет.' : 'Тревог моделей набора в этом прогоне нет.'}
              </p>
            )}
            {classes.length > 0 && (
              <div className="grid gap-2 px-4 sm:grid-cols-2 xl:grid-cols-3">
                {classes.map((c) => (
                  <RunLink key={c.cls} to={`/?class=${c.cls}`} className="flex items-center justify-between gap-3 rounded-lg border p-3 text-sm hover:bg-muted/50">
                    <span className="flex items-center gap-2">
                      <ClassMark cls={c.cls} />
                      <span>
                        {alertClassLabel(c.cls, models)}
                        <span className="block text-xs text-muted-foreground">
                          {fmtInt(c.alerts)} {plural(c.alerts, ['решение', 'решения', 'решений'])} с тревогой · сбылись {fmtInt(c.fault)}
                        </span>
                      </span>
                    </span>
                    <span className="text-lg font-semibold tabular-nums">{fmtInt(c.rows)}</span>
                  </RunLink>
                ))}
              </div>
            )}
            {groups.length > 0 && (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="pl-4">Время</TableHead>
                    <TableHead>Тревога</TableHead>
                    <TableHead>Где</TableHead>
                    <TableHead className="pr-4">Исход</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {groups.slice(0, RECENT).map((g) => (
                    <TableRow key={g.key}>
                      <TableCell className="pl-4 font-mono text-xs tabular-nums">{when(g)}</TableCell>
                      <TableCell>
                        <span className="flex items-center gap-2 font-medium">
                          <ClassMark cls={g.cls} />
                          {alertClassLabel(g.cls, models)}
                        </span>
                        {g.alerts.length > 1 && (
                          <span className="text-xs text-muted-foreground">
                            {fmtInt(g.alerts.length)} {plural(g.alerts.length, ['решение', 'решения', 'решений'])} с тревогой
                          </span>
                        )}
                      </TableCell>
                      <TableCell className="max-w-56">
                        <div className="truncate">{g.objectName}</div>
                        <div className="text-xs text-muted-foreground">
                          <Where g={g} />
                        </div>
                      </TableCell>
                      <TableCell className="pr-4 whitespace-normal">
                        <GroupOutcome g={g} />
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
            {groups.length > RECENT && <p className="px-4 text-xs text-muted-foreground">Последние {RECENT} из {fmtInt(groups.length)} — остальные в ленте.</p>}
          </CardContent>
        </Card>

        <div className="space-y-4">
          <Card>
            <CardHeader>
              <CardTitle>Модели набора</CardTitle>
              <CardDescription>Проверка по разметке набора, требования ТЗ к ним не применяются: другой набор и другая задача.</CardDescription>
            </CardHeader>
            <CardContent className="space-y-3">
              {modelsPending && <Skeleton className="h-32" />}
              {models.map((m) => (
                <ModelRow key={m.model} m={m} metrics={metricsOf(m)} />
              ))}
              {run?.dataset && <DatasetFacts dataset={run.dataset} />}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Заявки</CardTitle>
              <CardDescription>
                Открытые моделями набора, новые сверху.{' '}
                <RunLink to={ordersLink} className="text-foreground underline-offset-4 hover:underline">
                  Все заявки
                </RunLink>
              </CardDescription>
            </CardHeader>
            <CardContent className="px-0">
              {orders.isError && <QueryError error={orders.error} onRetry={() => void orders.refetch()} what="Заявки" compact className="mx-4" />}
              {!orders.data && !orders.isError && <Skeleton className="mx-4 h-32" />}
              {orders.data && orders.data.items.length === 0 && <p className="px-4 text-sm text-muted-foreground">Заявок моделей набора нет.</p>}
              {orders.data && orders.data.items.length > 0 && (
                <Table>
                  <TableBody>
                    {orders.data.items.map((w) => (
                      <TableRow key={w.id}>
                        <TableCell className="max-w-52 pl-4">
                          <RunLink to={`/work-orders/${w.id}`} className="block truncate font-medium underline-offset-4 hover:underline">
                            {w.channel_name}
                          </RunLink>
                          <div className="truncate text-xs text-muted-foreground">
                            {RULE_LABEL[w.rule] ?? w.rule} · {PRIORITY_LABEL[w.priority].toLowerCase()} · {fmtDateTimeShort(w.created_at)}
                          </div>
                        </TableCell>
                        <TableCell className="pr-4 text-right">
                          <StatusBadge status={WO_STATUS_STATUS[w.status]}>{WO_STATUS_LABEL[w.status]}</StatusBadge>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  )
}
