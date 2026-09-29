import { CalendarCheck, CircleHelp, LoaderCircle, Siren, Wrench, type LucideIcon } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import type { SkipReason, Verdict } from '@/lib/api/types'
import { skipReasonLabel, VERDICT_ACTION, VERDICT_LABEL, VERDICT_SHORT, VERDICT_STATUS } from '@/lib/labels'
import { STATUS_BADGE_CLASS } from '@/lib/status'
import { cn } from '@/lib/utils'

const ICON: Record<Verdict, LucideIcon> = {
  ppr: CalendarCheck,
  sensor_fault: Wrench,
  reaction: Siren,
  no_score: CircleHelp,
  pending: LoaderCircle,
}

type Props = {
  verdict: Verdict
  /** Changed to «плановая проверка» after the episode was recognised (A6). */
  refined?: boolean
  /** Decision of an earlier alarm of the channel. */
  inherited?: boolean
  skipReason?: SkipReason | null
  short?: boolean
  className?: string
}

// Feed verdict (docs/product/v1-scope.md, «Лента тревог»): color is the status token of the verdict.
export function VerdictBadge({ verdict, refined, inherited, skipReason, short, className }: Props) {
  const Icon = ICON[verdict]
  const notes = [
    VERDICT_ACTION[verdict],
    refined && 'Уточнён: эпизод распознан как плановая проверка',
    inherited && 'Решение по более ранней тревоге канала — одно решение в сутки',
    verdict === 'no_score' && skipReason && `Причина: ${skipReasonLabel(skipReason).toLowerCase()}`,
  ].filter(Boolean) as string[]

  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <Badge
            variant="outline"
            className={cn('cursor-default border-transparent font-medium', STATUS_BADGE_CLASS[VERDICT_STATUS[verdict]], className)}
          />
        }
      >
        <Icon className={cn(verdict === 'pending' && 'animate-spin')} />
        {short ? VERDICT_SHORT[verdict] : VERDICT_LABEL[verdict]}
        {(refined || inherited) && <span className="opacity-70">{refined ? '· уточнён' : '· ранее'}</span>}
      </TooltipTrigger>
      <TooltipContent>
        <div className="space-y-0.5">
          {notes.map((n) => (
            <div key={n}>{n}</div>
          ))}
        </div>
      </TooltipContent>
    </Tooltip>
  )
}
