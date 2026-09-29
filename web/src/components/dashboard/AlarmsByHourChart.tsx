import { Bar, BarChart, CartesianGrid, XAxis, YAxis } from 'recharts'

import { ChartContainer, ChartLegend, ChartLegendContent, ChartTooltip, ChartTooltipContent, type ChartConfig } from '@/components/ui/chart'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import type { DashboardSummary } from '@/lib/api/types'
import { fmtHour, fmtInt } from '@/lib/format'
import { VERDICT_LABEL } from '@/lib/labels'

type Row = DashboardSummary['alarms_by_hour'][number]
type Key = 'reaction' | 'ppr' | 'sensor_fault' | 'no_score'

// Stack order from bottom: adjacent segments pass the colour-vision check with this order
// (worst adjacent ΔE 15.9 under protanopia). Colours are the verdict status tokens.
const ORDER: Key[] = ['reaction', 'ppr', 'sensor_fault', 'no_score']

const config = {
  reaction: { label: VERDICT_LABEL.reaction, color: 'var(--status-alarm)' },
  ppr: { label: VERDICT_LABEL.ppr, color: 'var(--status-ppr)' },
  sensor_fault: { label: VERDICT_LABEL.sensor_fault, color: 'var(--status-risk)' },
  no_score: { label: VERDICT_LABEL.no_score, color: 'var(--status-unknown)' },
} satisfies ChartConfig

type ShapeProps = { x: number; y: number; width: number; height: number; fill: string; payload: Row }

/** Segment with a 2px surface gap below it and a 4px rounded end only on the topmost non-empty segment. */
function segment(key: Key) {
  const i = ORDER.indexOf(key)
  const Shape = (raw: unknown) => {
    const { x, y, width, height, fill, payload } = raw as ShapeProps
    if (!height || height <= 0) return <g />
    const top = !ORDER.slice(i + 1).some((k) => payload[k] > 0)
    const bottom = ORDER.slice(0, i).every((k) => !payload[k])
    const h = Math.max(0, height - (bottom ? 0 : 2))
    const r = top ? Math.min(4, h, width / 2) : 0
    const y0 = y
    const y1 = y + h
    const d = `M${x},${y1}L${x},${y0 + r}Q${x},${y0} ${x + r},${y0}L${x + width - r},${y0}Q${x + width},${y0} ${x + width},${y0 + r}L${x + width},${y1}Z`
    return <path d={d} fill={fill} />
  }
  return Shape
}

export function AlarmsByHourChart({ data }: { data: Row[] }) {
  const rows = data.map((r) => ({ ...r, label: fmtHour(r.hour) }))
  return (
    <Tabs defaultValue="chart">
      <TabsList className="absolute top-3 right-3">
        <TabsTrigger value="chart">График</TabsTrigger>
        <TabsTrigger value="table">Таблица</TabsTrigger>
      </TabsList>
      <TabsContent value="chart">
        <ChartContainer config={config} className="aspect-auto h-64 w-full">
          <BarChart data={rows} margin={{ left: -16, right: 4, top: 8 }} barCategoryGap="20%">
            <CartesianGrid vertical={false} />
            <XAxis dataKey="label" tickLine={false} axisLine={false} interval={2} tickMargin={6} />
            <YAxis allowDecimals={false} tickLine={false} axisLine={false} width={40} />
            <ChartTooltip cursor={{ fillOpacity: 0.4 }} content={<ChartTooltipContent labelFormatter={(v) => `${v}, тревог`} />} />
            <ChartLegend content={<ChartLegendContent />} />
            {ORDER.map((k) => (
              <Bar key={k} dataKey={k} stackId="verdict" fill={`var(--color-${k})`} maxBarSize={24} shape={segment(k)} isAnimationActive={false} />
            ))}
          </BarChart>
        </ChartContainer>
      </TabsContent>
      <TabsContent value="table">
        <div className="max-h-64 overflow-y-auto">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Час</TableHead>
                {ORDER.map((k) => (
                  <TableHead key={k} className="text-right">
                    {config[k].label}
                  </TableHead>
                ))}
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows
                .slice()
                .reverse()
                .map((r) => (
                  <TableRow key={r.hour}>
                    <TableCell className="font-mono text-xs">{r.label}</TableCell>
                    {ORDER.map((k) => (
                      <TableCell key={k} className="text-right tabular-nums">
                        {fmtInt(r[k])}
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
