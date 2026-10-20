import { BellRing, CalendarCheck, DoorOpen, Wrench } from 'lucide-react'
import { useState } from 'react'

import { MonthlyChart, type MonthlySeries } from '@/components/dashboard/MonthlyChart'
import { MetricCard } from '@/components/domain/MetricCard'
import { QueryError } from '@/components/domain/QueryError'
import { RunLink } from '@/components/domain/RunLink'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { useHistorySummary } from '@/lib/api/queries'
import type { HistoryMonth } from '@/lib/api/types'
import { fmtDate, fmtInt, fmtPercent } from '@/lib/format'

// Adjacent pairs of each stack pass the colour-vision check in the light theme (scripts of the dataviz method):
// ППР / вне ППР ΔE 24.3, датчики / оборудование 33.1, средний / высокий 12.0; low-contrast orange — table tab.
const ALARMS: MonthlySeries[] = [
  { key: 'ppr', label: 'Плановая проверка', color: 'var(--status-ppr)' },
  { key: 'other', label: 'Вне ППР', color: 'var(--status-alarm)' },
]
// Smoke-detector faults have mass months (a 2021 wave ~10× the rest), stacked with equipment they flatten it: each
// kind gets its own chart and scale, switched by a toggle.
const FAULTS: Record<'equipment' | 'smoke', MonthlySeries[]> = {
  equipment: [{ key: 'equipment', label: 'Оборудование', color: 'var(--status-risk)' }],
  smoke: [{ key: 'smoke', label: 'Дымовые датчики', color: 'var(--status-fault)' }],
}
const ACCESS: MonthlySeries[] = [
  { key: 'medium', label: 'Средний приоритет', color: 'var(--status-risk)' },
  { key: 'high', label: 'Высокий: люк, ночь, выходной', color: 'var(--status-alarm)' },
]

const sum = (months: HistoryMonth[], f: (m: HistoryMonth) => number) => months.reduce((s, m) => s + f(m), 0)

/**
 * «Вся история» (`?run=0`): the hackathon journal 2019 — 30.06.2026 without a run, by month — only aggregates of the
 * base (alarms, fault episodes, access incidents), never raw events. No feed and no clock here.
 */
export function HistoryDashboard() {
  const q = useHistorySummary()
  const [faultKind, setFaultKind] = useState<'equipment' | 'smoke'>('equipment')
  const h = q.data
  const months = h?.months ?? []
  const last = months.slice(-12)
  const alarms = sum(months, (m) => m.smoke_alarms)
  const ppr = sum(months, (m) => m.ppr_alarms)
  const rows = {
    alarms: months.map((m) => ({ month: m.month, ppr: m.ppr_alarms, other: m.smoke_alarms - m.ppr_alarms })),
    faults: months.map((m) => ({ month: m.month, smoke: m.faults_started - m.faults_equipment, equipment: m.faults_equipment })),
    access: months.map((m) => ({ month: m.month, medium: m.access_incidents - m.access_high, high: m.access_high })),
  }

  if (q.isError) return <QueryError error={q.error} onRetry={() => void q.refetch()} what="Сводка всей истории" />

  return (
    <div className="mx-auto w-full max-w-7xl space-y-4">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <MetricCard
          title="Дымовые тревоги"
          icon={BellRing}
          value={fmtInt(alarms)}
          loading={!h}
          hint={h && `за последние 12 месяцев — ${fmtInt(sum(last, (m) => m.smoke_alarms))}`}
        />
        <MetricCard
          title="Доля плановых проверок"
          icon={CalendarCheck}
          tone="ppr"
          value={fmtPercent(alarms ? ppr / alarms : null, 0)}
          loading={!h}
          hint={h && `${fmtInt(ppr)} тревог — в эпизодах ППР`}
        />
        <MetricCard
          title="Начавшиеся неисправности"
          icon={Wrench}
          value={fmtInt(sum(months, (m) => m.faults_started))}
          loading={!h}
          hint={h && `оборудование — ${fmtInt(sum(months, (m) => m.faults_equipment))}, остальное — дымовые датчики`}
        />
        <MetricCard
          title="Вскрытия под охраной"
          icon={DoorOpen}
          value={fmtInt(sum(months, (m) => m.access_incidents))}
          loading={!h}
          hint={h && `высокий приоритет — ${fmtInt(sum(months, (m) => m.access_high))}; метка правил охраны, не проникновение`}
        />
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <Card className="relative">
          <CardHeader>
            <CardTitle>Дымовые тревоги по месяцам</CardTitle>
            <CardDescription>Плановая проверка — тревога в эпизоде ППР</CardDescription>
          </CardHeader>
          <CardContent>{h ? <MonthlyChart data={rows.alarms} series={ALARMS} unit="тревог" /> : <Skeleton className="h-56" />}</CardContent>
        </Card>
        <Card className="relative">
          <CardHeader>
            <CardTitle>Начавшиеся неисправности</CardTitle>
            <CardDescription>Эпизоды «Неисправен» по месяцу начала, у каждого вида — свой масштаб</CardDescription>
          </CardHeader>
          <CardContent className="space-y-2">
            <ToggleGroup value={[faultKind]} onValueChange={(v) => v[0] && setFaultKind(v[0] as 'equipment' | 'smoke')} variant="outline" size="sm">
              <ToggleGroupItem value="equipment">Оборудование</ToggleGroupItem>
              <ToggleGroupItem value="smoke">Дымовые датчики</ToggleGroupItem>
            </ToggleGroup>
            {h ? <MonthlyChart data={rows.faults} series={FAULTS[faultKind]} unit="эпизодов" /> : <Skeleton className="h-56" />}
          </CardContent>
        </Card>
        <Card className="relative">
          <CardHeader>
            <CardTitle>Вскрытия под охраной</CardTitle>
            <CardDescription>Инциденты правила охраны, без «снято после открытия»</CardDescription>
          </CardHeader>
          <CardContent>{h ? <MonthlyChart data={rows.access} series={ACCESS} unit="инцидентов" /> : <Skeleton className="h-56" />}</CardContent>
        </Card>
      </div>

      <div>
        <Card>
          <CardHeader>
            <CardTitle>Проблемные объекты</CardTitle>
            <CardDescription>{h ? `За год до конца данных, с ${fmtDate(h.top_from)}: больше всего начавшихся неисправностей` : ' '}</CardDescription>
          </CardHeader>
          <CardContent className="overflow-x-auto px-0">
            {h ? (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="pl-4">Объект</TableHead>
                    <TableHead className="text-right">Неисправностей</TableHead>
                    <TableHead className="text-right">из них оборуд.</TableHead>
                    <TableHead className="text-right">Тревог вне ППР</TableHead>
                    <TableHead className="pr-4 text-right">Вскрытий</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {h.top_objects.map((o) => (
                    <TableRow key={o.object_id}>
                      <TableCell className="pl-4">
                        <RunLink to={`/objects/${o.object_id}`} className="underline-offset-4 hover:underline">
                          {o.object_name}
                        </RunLink>
                        <div className="text-xs text-muted-foreground">{fmtInt(o.fault_channels)} каналов с неисправностью</div>
                      </TableCell>
                      <TableCell className="text-right tabular-nums">{fmtInt(o.faults)}</TableCell>
                      <TableCell className="text-right tabular-nums">{fmtInt(o.faults_equipment)}</TableCell>
                      <TableCell className="text-right tabular-nums">
                        {fmtInt(o.alarms_out_of_ppr)}
                        <span className="text-xs text-muted-foreground"> из {fmtInt(o.alarms)}</span>
                      </TableCell>
                      <TableCell className="pr-4 text-right tabular-nums">{fmtInt(o.access)}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            ) : (
              <Skeleton className="mx-4 h-56" />
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
