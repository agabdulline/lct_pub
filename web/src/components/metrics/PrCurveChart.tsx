import { CartesianGrid, Line, LineChart, ReferenceArea, ReferenceDot, XAxis, YAxis } from 'recharts'

import { ChartContainer, ChartTooltip, type ChartConfig } from '@/components/ui/chart'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import type { OfflineReport } from '@/lib/api/types'
import { fmtPercent } from '@/lib/format'
import { REQUIREMENT } from '@/lib/offline-metrics'

const config = { precision: { label: 'Precision', color: 'var(--foreground)' } } satisfies ChartConfig

const pct = (v: number) => `${Math.round(v * 100)}%`

type Point = NonNullable<OfflineReport['pr_curve']>[number]

function PointTooltip({ active, payload }: { active?: boolean; payload?: { payload: Point }[] }) {
  if (!active || !payload?.length) return null
  const p = payload[0].payload
  return (
    <div className="rounded-lg border border-border/50 bg-background px-2.5 py-1.5 text-xs shadow-xl">
      <div className="font-medium">порог {p.threshold.toFixed(3).replace('.', ',')}</div>
      <div>
        Precision <b>{fmtPercent(p.precision)}</b> · Recall <b>{fmtPercent(p.recall)}</b>
      </div>
    </div>
  )
}

// Precision–recall curve of the test period: one series, the requirement zone (P > 70%, R > 50%) and the working point.
// An external model has no requirement zone: the ТЗ requirement is about another task.
export function PrCurveChart({ report, requirement = true }: { report: OfflineReport & { pr_curve: Point[] }; requirement?: boolean }) {
  const work = report.test
  return (
    <Tabs defaultValue="chart">
      <TabsList>
        <TabsTrigger value="chart">Кривая</TabsTrigger>
        <TabsTrigger value="table">Таблица</TabsTrigger>
      </TabsList>
      <TabsContent value="chart">
        <ChartContainer config={config} className="aspect-auto h-64 w-full">
          <LineChart data={report.pr_curve} margin={{ left: 0, right: 16, top: 12, bottom: 12 }}>
            <CartesianGrid />
            {requirement && (
              <ReferenceArea
                x1={REQUIREMENT.recall}
                x2={1}
                y1={REQUIREMENT.precision}
                y2={1}
                fill="var(--status-ok)"
                fillOpacity={0.08}
                stroke="none"
                label={{ value: 'требование ТЗ', position: 'insideTopRight', fontSize: 11, fill: 'var(--muted-foreground)' }}
              />
            )}
            <XAxis
              type="number"
              dataKey="recall"
              domain={[0, 1]}
              ticks={[0, 0.25, 0.5, 0.75, 1]}
              tickFormatter={pct}
              tickLine={false}
              axisLine={false}
              label={{ value: 'Recall', position: 'insideBottom', offset: -8, fontSize: 11, fill: 'var(--muted-foreground)' }}
            />
            <YAxis
              type="number"
              domain={[0, 1]}
              ticks={[0, 0.25, 0.5, 0.75, 1]}
              tickFormatter={pct}
              tickLine={false}
              axisLine={false}
              width={44}
            />
            <ChartTooltip content={<PointTooltip />} />
            <Line dataKey="precision" type="monotone" stroke="var(--color-precision)" strokeWidth={2} dot={false} isAnimationActive={false} />
            {work.precision !== null && work.recall !== null && (
              <ReferenceDot
                x={work.recall}
                y={work.precision}
                r={5}
                fill="var(--status-ok)"
                stroke="var(--card)"
                strokeWidth={2}
                label={{ value: `рабочий порог ${report.threshold.toFixed(2).replace('.', ',')}`, position: 'left', fontSize: 11, fill: 'var(--foreground)' }}
              />
            )}
          </LineChart>
        </ChartContainer>
      </TabsContent>
      <TabsContent value="table">
        <div className="max-h-64 overflow-y-auto">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Порог</TableHead>
                <TableHead className="text-right">Precision</TableHead>
                <TableHead className="text-right">Recall</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {report.pr_curve.map((p) => (
                <TableRow key={p.threshold} data-state={Math.abs(p.threshold - report.threshold) < 1e-9 ? 'selected' : undefined}>
                  <TableCell className="font-mono text-xs">{p.threshold.toFixed(3)}</TableCell>
                  <TableCell className="text-right tabular-nums">{fmtPercent(p.precision)}</TableCell>
                  <TableCell className="text-right tabular-nums">{fmtPercent(p.recall)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      </TabsContent>
    </Tabs>
  )
}
