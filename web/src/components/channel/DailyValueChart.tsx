import { Area, CartesianGrid, ComposedChart, Line, XAxis, YAxis } from 'recharts'

import { ChartContainer, ChartTooltip, ChartTooltipContent, type ChartConfig } from '@/components/ui/chart'
import type { DailyTotal } from '@/lib/api/types'
import { fmtDate } from '@/lib/format'

const config = {
  mean: { label: 'Среднее', color: 'var(--foreground)' },
  range: { label: 'Мин–макс', color: 'var(--muted-foreground)' },
} satisfies ChartConfig

// Daily min–max band and mean of a numeric channel (gas, temperature) — from channel_daily, before the dead band.
export function DailyValueChart({ daily, unit }: { daily: DailyTotal[]; unit?: string }) {
  const rows = daily
    .filter((d) => d.mean !== null)
    .map((d) => ({ date: fmtDate(d.date).slice(0, 5), mean: d.mean, range: [d.min, d.max] as [number | null, number | null] }))
  if (!rows.length) return <p className="text-sm text-muted-foreground">Числовых показаний за период нет</p>
  return (
    <ChartContainer config={config} className="aspect-auto h-48 w-full">
      <ComposedChart data={rows} margin={{ left: 4, right: 8, top: 8 }}>
        <CartesianGrid vertical={false} />
        <XAxis dataKey="date" tickLine={false} axisLine={false} minTickGap={24} />
        <YAxis tickLine={false} axisLine={false} width={52} domain={['auto', 'auto']} unit={unit} />
        <ChartTooltip content={<ChartTooltipContent indicator="line" />} />
        <Area dataKey="range" type="monotone" fill="var(--color-range)" fillOpacity={0.15} stroke="none" isAnimationActive={false} />
        <Line dataKey="mean" type="monotone" stroke="var(--color-mean)" strokeWidth={2} dot={false} isAnimationActive={false} />
      </ComposedChart>
    </ChartContainer>
  )
}
