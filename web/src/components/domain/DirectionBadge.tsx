import { Badge } from '@/components/ui/badge'
import { DIRECTION_SHORT, DIRECTION_TITLE, directionOf, type Direction } from '@/lib/directions'
import { cn } from '@/lib/utils'

const TONE: Record<Direction, string> = {
  sensor: 'border-status-ppr/40 bg-status-ppr/10 text-status-ppr',
  fire: 'border-status-alarm/40 bg-status-alarm/10 text-status-alarm',
  access: 'border-status-fault/40 bg-status-fault/10 text-status-fault',
  wear: 'border-status-risk/40 bg-status-risk/10 text-status-risk',
  system: 'border-status-unknown/40 bg-status-unknown/10 text-status-unknown',
}

/** Task-direction badge of a scenario or run; pass either a ready direction or the scenario id (+ dataset). */
export function DirectionBadge({ direction, scenarioId, dataset, className }: { direction?: Direction; scenarioId?: string; dataset?: unknown; className?: string }) {
  const d = direction ?? directionOf(scenarioId ?? '', dataset)
  return (
    <Badge variant="outline" title={DIRECTION_TITLE[d]} className={cn('shrink-0 font-normal', TONE[d], className)}>
      {DIRECTION_SHORT[d]}
    </Badge>
  )
}
