import { fmtPercent } from '@/lib/format'
import { cn } from '@/lib/utils'

// A ratio against its requirement (Precision > 70%, Recall > 50%): the tick is the requirement,
// the fill is ok when met and risk when not; the number is always printed next to it.
// Without a target (external models: another task, the ТЗ requirement does not apply) — a neutral bar, no tick.
export function TargetMeter({ value, target, className }: { value: number | null | undefined; target?: number; className?: string }) {
  if (value === null || value === undefined) return <span className={cn('text-sm text-muted-foreground', className)}>—</span>
  const met = target !== undefined && value >= target
  const fill = target === undefined ? 'bg-muted-foreground' : met ? 'bg-status-ok' : 'bg-status-risk'
  return (
    <span className={cn('inline-flex items-center gap-2', className)} title={target === undefined ? undefined : `требование > ${fmtPercent(target, 0)}`}>
      <span className="relative h-1.5 w-24 overflow-hidden rounded-full bg-muted">
        <span className={cn('absolute inset-y-0 left-0 rounded-full', fill)} style={{ width: `${Math.min(100, value * 100)}%` }} />
        {target !== undefined && <span className="absolute inset-y-0 w-px bg-foreground/80" style={{ left: `${target * 100}%` }} />}
      </span>
      <span className="w-12 text-right text-sm tabular-nums">{fmtPercent(value)}</span>
    </span>
  )
}
