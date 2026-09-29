import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { fmtScore } from '@/lib/format'
import { cn } from '@/lib/utils'

type Props = {
  /** Model score 0…1; `null` — no forecast. */
  value: number | null | undefined
  threshold?: number | null
  /** Hide the number next to the bar. */
  compact?: boolean
  className?: string
  label?: string
}

// Model score with the decision threshold. Above the threshold the bar takes the risk token.
export function RiskMeter({ value, threshold, compact, className, label = 'Оценка модели' }: Props) {
  if (value === null || value === undefined) {
    return <span className={cn('text-xs text-muted-foreground', className)}>—</span>
  }
  const above = threshold !== null && threshold !== undefined && value >= threshold
  // A score that is not a probability (VAST: rarity, threshold 2.68): the bar is scaled so the threshold sits mid-bar.
  const scale = threshold !== null && threshold !== undefined && threshold > 1 ? threshold * 2 : 1
  const pct = Math.round(Math.min(1, Math.max(0, value / scale)) * 100)
  return (
    <Tooltip>
      <TooltipTrigger
        render={<span className={cn('inline-flex cursor-default items-center gap-2', className)} />}
      >
        <span className="relative h-1.5 w-16 overflow-hidden rounded-full bg-muted">
          <span
            className={cn('absolute inset-y-0 left-0 rounded-full', above ? 'bg-status-risk' : 'bg-muted-foreground/60')}
            style={{ width: `${pct}%` }}
          />
          {threshold !== null && threshold !== undefined && (
            <span className="absolute inset-y-0 w-px bg-foreground/70" style={{ left: `${Math.round((threshold / scale) * 100)}%` }} />
          )}
        </span>
        {!compact && (
          <span className={cn('font-mono text-xs tabular-nums', above ? 'text-status-risk' : 'text-muted-foreground')}>
            {fmtScore(value)}
          </span>
        )}
      </TooltipTrigger>
      <TooltipContent>
        {label}: {fmtScore(value)}
        {threshold !== null && threshold !== undefined && <> · порог {fmtScore(threshold)}{above ? ' — выше порога' : ''}</>}
      </TooltipContent>
    </Tooltip>
  )
}
