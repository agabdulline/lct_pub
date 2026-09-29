import { Bar, BarChart, CartesianGrid, ReferenceLine, XAxis, YAxis } from 'recharts'

import { ChartContainer, ChartLegend, ChartLegendContent, ChartTooltip, ChartTooltipContent, type ChartConfig } from '@/components/ui/chart'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import type { ScenarioMoment, ScenarioSummary } from '@/lib/api/types'
import { fmtDateTimeShort, fmtInt, fmtTime } from '@/lib/format'

// Two series stacked: all signals are the sum, the alarm ones are on top in the alarm token. Grey and red pass the
// colour-vision check by a wide margin; the legend and the table view carry the meaning too.
const config = {
  calm: { label: 'Нетревожные', color: 'var(--status-unknown)' },
  alarm: { label: 'Тревожные', color: 'var(--status-alarm)' },
} satisfies ChartConfig

/** Signal flow of a scenario over its period: how dense the transmission will be, where the bursts are. */
export function DensityChart({ density, moments, multiDay }: { density: ScenarioSummary['density']; moments: ScenarioMoment[]; multiDay: boolean }) {
  const label = (ts: string) => (multiDay ? fmtDateTimeShort(ts) : fmtTime(ts).slice(0, 5))
  const rows = density.items.map((r) => ({ ts: r.ts, label: label(r.ts), calm: r.events - r.alarm_events, alarm: r.alarm_events, events: r.events }))
  const bucketOf = (ts: string) => {
    const t = Date.parse(ts)
    let hit = rows[0]
    for (const r of rows) if (Date.parse(r.ts) <= t) hit = r
    return hit?.label
  }
  const marks = moments.filter((m) => m.kind !== 'first_event' && m.kind !== 'last_event')
  const minutes = density.bucket_seconds / 60
  return (
    <Tabs defaultValue="chart">
      <TabsList className="absolute top-3 right-3">
        <TabsTrigger value="chart">График</TabsTrigger>
        <TabsTrigger value="table">Таблица</TabsTrigger>
      </TabsList>
      <TabsContent value="chart">
        <ChartContainer config={config} className="aspect-auto h-56 w-full">
          <BarChart data={rows} margin={{ left: -8, right: 4, top: 8 }} barCategoryGap={1}>
            <CartesianGrid vertical={false} />
            <XAxis dataKey="label" tickLine={false} axisLine={false} minTickGap={32} tickMargin={6} />
            <YAxis allowDecimals={false} tickLine={false} axisLine={false} width={48} />
            <ChartTooltip
              cursor={{ fillOpacity: 0.4 }}
              content={<ChartTooltipContent labelFormatter={(v) => `${v}, сигналов за ${minutes < 60 ? `${minutes} мин` : `${minutes / 60} ч`}`} />}
            />
            <ChartLegend content={<ChartLegendContent />} />
            {/* One line per bucket: several moments of one bucket would draw the same line (Azure PdM: 3 at 06:00). */}
            {[...new Set(marks.map((m) => bucketOf(m.ts)))].map((x) => (
              <ReferenceLine key={String(x)} x={x} stroke="var(--foreground)" strokeOpacity={0.35} strokeDasharray="3 3" />
            ))}
            <Bar dataKey="calm" stackId="flow" fill="var(--color-calm)" isAnimationActive={false} />
            <Bar dataKey="alarm" stackId="flow" fill="var(--color-alarm)" radius={[2, 2, 0, 0]} isAnimationActive={false} />
          </BarChart>
        </ChartContainer>
      </TabsContent>
      <TabsContent value="table">
        <div className="max-h-56 overflow-y-auto">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Начало</TableHead>
                <TableHead className="text-right">Сигналов</TableHead>
                <TableHead className="text-right">Тревожных</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((r) => (
                <TableRow key={r.ts}>
                  <TableCell className="font-mono text-xs">{r.label}</TableCell>
                  <TableCell className="text-right tabular-nums">{fmtInt(r.events)}</TableCell>
                  <TableCell className="text-right tabular-nums">{fmtInt(r.alarm)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      </TabsContent>
    </Tabs>
  )
}
