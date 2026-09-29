import { RunLink } from '@/components/domain/RunLink'
import { SensorTypeIcon } from '@/components/domain/SensorTypeIcon'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import type { ObjectRisk, SensorType, SensorTypeSummary } from '@/lib/api/types'
import { fmtInt, fmtPercent, fmtScore } from '@/lib/format'
import { cn } from '@/lib/utils'

// Sequential scale — one hue (the risk token), lighter to darker. Capped at 75% so the cell text
// in the foreground token stays readable on both themes.
const BINS = [
  { max: 0.02, cls: 'bg-status-risk/12', label: '≤ 2%' },
  { max: 0.05, cls: 'bg-status-risk/25', label: '2–5%' },
  { max: 0.1, cls: 'bg-status-risk/40', label: '5–10%' },
  { max: 0.2, cls: 'bg-status-risk/55', label: '10–20%' },
  { max: Infinity, cls: 'bg-status-risk/75', label: '> 20%' },
]

const binOf = (share: number) => BINS.find((b) => share <= b.max)!

function Cell({ object, s }: { object: string; s: SensorTypeSummary | undefined }) {
  if (!s || !s.channels) return <td className="h-8 text-center text-muted-foreground/50">·</td>
  const share = s.at_risk / s.channels
  return (
    <td className="p-0.5">
      <Tooltip>
        <TooltipTrigger
          render={
            <div
              className={cn(
                'flex h-7 items-center justify-center rounded-sm text-xs tabular-nums outline-offset-1 hover:outline hover:outline-foreground/40',
                s.at_risk > 0 ? binOf(share).cls : 'bg-muted/40 text-muted-foreground',
              )}
              tabIndex={0}
            />
          }
        >
          {s.at_risk > 0 ? s.at_risk : ''}
        </TooltipTrigger>
        <TooltipContent>
          <div className="font-medium">
            {fmtInt(s.at_risk)} из {fmtInt(s.channels)} в риске · {fmtPercent(share)}
          </div>
          <div>
            {object} · {s.sensor_type}
          </div>
          <div>
            Неисправно {fmtInt(s.in_fault)} · макс. риск {fmtScore(s.max_risk)}
          </div>
        </TooltipContent>
      </Tooltip>
    </td>
  )
}

// «Объект × тип датчика» (v1-scope, Дашборд): share of channels at risk by the daily model.
export function RiskHeatmap({ rows, types }: { rows: ObjectRisk[]; types: SensorType[] }) {
  const sorted = [...rows].sort((a, b) => b.at_risk / Math.max(1, b.channels) - a.at_risk / Math.max(1, a.channels))
  return (
    <div className="space-y-3">
      <div className="overflow-x-auto">
        <table className="w-full border-separate border-spacing-0 text-sm">
          <thead>
            <tr>
              <th className="pb-1.5 text-left text-xs font-medium text-muted-foreground">Объект</th>
              {types.map((t) => (
                <th key={t} className="w-12 pb-1.5 text-center">
                  <span className="inline-flex justify-center">
                    <SensorTypeIcon type={t} />
                  </span>
                </th>
              ))}
              <th className="w-16 pb-1.5 text-right text-xs font-medium text-muted-foreground">Всего</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((o) => {
              const byType = new Map(o.by_sensor_type.map((s) => [s.sensor_type, s]))
              return (
                <tr key={o.object_id}>
                  <td className="max-w-40 truncate pr-2">
                    <RunLink to={`/objects/${o.object_id}`} className="underline-offset-4 hover:underline">
                      {o.object_name}
                    </RunLink>
                  </td>
                  {types.map((t) => (
                    <Cell key={t} object={o.object_name} s={byType.get(t)} />
                  ))}
                  <td className="text-right text-xs tabular-nums text-muted-foreground">
                    {fmtInt(o.at_risk)} / {fmtInt(o.channels)}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <div className="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
        <span>Доля каналов в риске:</span>
        {BINS.map((b) => (
          <span key={b.label} className="flex items-center gap-1">
            <span className={cn('size-3 rounded-sm', b.cls)} />
            {b.label}
          </span>
        ))}
        <span className="flex items-center gap-1">
          <span className="size-3 rounded-sm bg-muted/40" />
          нет в риске
        </span>
      </div>
    </div>
  )
}
