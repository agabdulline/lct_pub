import { Bar, BarChart, CartesianGrid, XAxis, YAxis } from 'recharts'

import { ChartContainer, ChartLegend, ChartLegendContent, ChartTooltip, ChartTooltipContent, type ChartConfig } from '@/components/ui/chart'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { fmtInt } from '@/lib/format'

/** A series of a monthly stack: bottom first, a status token for its colour. */
export type MonthlySeries = { key: string; label: string; color: string }

type Row = { month: string } & Record<string, number | string | null>

const MONTH = new Intl.DateTimeFormat('ru-RU', { month: 'short', year: 'numeric', timeZone: 'UTC' })
const SHORT = new Intl.DateTimeFormat('ru-RU', { month: 'short', year: '2-digit', timeZone: 'UTC' })
/** An axis tick that fits: «850 тыс.», «3,4 млн» — a dataset has millions of events a month. */
const axisNumber = (v: number) =>
  v >= 1e6 ? `${(v / 1e6).toLocaleString('ru-RU', { maximumFractionDigits: 1 })} млн` : v >= 1e4 ? `${Math.round(v / 1e3)} тыс.` : fmtInt(v)
const monthLabel = (m: string) => MONTH.format(new Date(`${m}T00:00:00Z`)).replace(' г.', '')

type ShapeProps = { x: number; y: number; width: number; height: number; fill: string; payload: Row }

/** Segment with a 2px surface gap below it and a 4px rounded end only on the topmost non-empty segment. */
function segment(order: string[], key: string) {
  const i = order.indexOf(key)
  const Shape = (raw: unknown) => {
    const { x, y, width, height, fill, payload } = raw as ShapeProps
    if (!height || height <= 0) return <g />
    const top = !order.slice(i + 1).some((k) => Number(payload[k]) > 0)
    const bottom = order.slice(0, i).every((k) => !Number(payload[k]))
    const h = Math.max(0, height - (bottom ? 0 : 2))
    const r = top ? Math.min(4, h, width / 2) : 0
    const y1 = y + h
    const d = `M${x},${y1}L${x},${y + r}Q${x},${y} ${x + r},${y}L${x + width - r},${y}Q${x + width},${y} ${x + width},${y + r}L${x + width},${y1}Z`
    return <path d={d} fill={fill} />
  }
  return Shape
}

/**
 * Months of the whole history as a stack of a few series, one y-axis — each measure gets its own chart. The x-axis is
 * labelled by year (its January); the exact month is in the tooltip and in the table tab.
 */
export function MonthlyChart({ data, series, unit }: { data: Row[]; series: MonthlySeries[]; unit: string }) {
  const config = Object.fromEntries(series.map((s) => [s.key, { label: s.label, color: s.color }])) satisfies ChartConfig
  const order = series.map((s) => s.key)
  return (
    <Tabs defaultValue="chart">
      <TabsList className="absolute top-3 right-3">
        <TabsTrigger value="chart">График</TabsTrigger>
        <TabsTrigger value="table">Таблица</TabsTrigger>
      </TabsList>
      <TabsContent value="chart">
        <ChartContainer config={config} className="aspect-auto h-56 w-full">
          <BarChart data={data} margin={{ left: 0, right: 4, top: 8 }} barCategoryGap="15%">
            <CartesianGrid vertical={false} />
            <XAxis
              dataKey="month"
              tickLine={false}
              axisLine={false}
              // a long scale — by year (its January); a short one (a dataset's months) — by month, thinned to fit
              interval={data.length <= 13 ? 'preserveStartEnd' : 0}
              tickMargin={6}
              tickFormatter={(m: string) => (data.length <= 13 ? SHORT.format(new Date(`${m}T00:00:00Z`)) : m.endsWith('-01-01') ? m.slice(0, 4) : '')}
            />
            <YAxis allowDecimals={false} tickLine={false} axisLine={false} width={60} tickFormatter={axisNumber} />
            <ChartTooltip cursor={{ fillOpacity: 0.4 }} content={<ChartTooltipContent labelFormatter={(m) => `${monthLabel(String(m))}, ${unit}`} />} />
            {/* one series is named by the card title: no legend box */}
            {series.length > 1 && <ChartLegend content={<ChartLegendContent />} />}
            {series.map((s) => (
              <Bar key={s.key} dataKey={s.key} stackId="m" fill={`var(--color-${s.key})`} shape={segment(order, s.key)} isAnimationActive={false} />
            ))}
          </BarChart>
        </ChartContainer>
      </TabsContent>
      <TabsContent value="table">
        <div className="max-h-56 overflow-y-auto">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Месяц</TableHead>
                {series.map((s) => (
                  <TableHead key={s.key} className="text-right">
                    {s.label}
                  </TableHead>
                ))}
              </TableRow>
            </TableHeader>
            <TableBody>
              {data
                .slice()
                .reverse()
                .map((r) => (
                  <TableRow key={r.month}>
                    <TableCell>{monthLabel(r.month)}</TableCell>
                    {series.map((s) => (
                      <TableCell key={s.key} className="text-right tabular-nums">
                        {fmtInt(Number(r[s.key]))}
                      </TableCell>
                    ))}
                  </TableRow>
                ))}
            </TableBody>
          </Table>
        </div>
      </TabsContent>
    </Tabs>
  )
}
