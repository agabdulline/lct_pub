import type { Factor } from '@/lib/api/types'
import { fmtFactorValue } from '@/lib/format'
import { FEATURE_LABEL } from '@/lib/labels'
import { cn } from '@/lib/utils'

// Top factors of a forecast: bar length is the contribution, direction — raises or lowers the score.
export function FactorList({ factors, className }: { factors: Factor[] | null; className?: string }) {
  if (!factors?.length) return <div className={cn('text-sm text-muted-foreground', className)}>Факторов нет — прогноз без объяснения</div>
  // External rule-like models (vast_gate) give factors without a contribution: they explain, the bar is empty
  const max = Math.max(...factors.map((f) => Math.abs(f.contribution ?? 0)), 1e-9)
  return (
    <ul className={cn('space-y-1.5 text-sm', className)}>
      {factors.map((f) => {
        const up = (f.contribution ?? 0) >= 0
        const w = Math.round((Math.abs(f.contribution ?? 0) / max) * 100)
        return (
          <li key={f.feature} className="grid grid-cols-[1fr_6rem] items-center gap-3">
            <span className="min-w-0">
              <span className="block truncate">{FEATURE_LABEL[f.feature] ?? f.feature}</span>
              <span className="font-mono text-xs text-muted-foreground">
                {f.feature} = {fmtFactorValue(f.value)}
              </span>
            </span>
            <span className="flex items-center gap-1.5">
              <span className="relative h-1.5 flex-1 overflow-hidden rounded-full bg-muted">
                <span className={cn('absolute inset-y-0 left-0 rounded-full', up ? 'bg-status-risk' : 'bg-status-ok')} style={{ width: `${w}%` }} />
              </span>
              <span className={cn('w-4 text-xs', up ? 'text-status-risk' : 'text-status-ok')}>{up ? '↑' : '↓'}</span>
            </span>
          </li>
        )
      })}
    </ul>
  )
}
