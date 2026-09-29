import { EyeOff } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { OBJECT_HISTORY_NOTE } from '@/lib/labels'
import { STATUS_BADGE_CLASS } from '@/lib/status'
import { cn } from '@/lib/utils'

/**
 * The event model's blind spot (`object_history === false`): the object has no outcome history and the model
 * never alerts there. Neutral token — it is not the channel's state, it is how far the model can be trusted.
 */
export function ObjectHistoryMark({ compact, className }: { compact?: boolean; className?: string }) {
  return (
    <Tooltip>
      <TooltipTrigger render={<Badge variant="outline" className={cn('cursor-default border-transparent font-medium', STATUS_BADGE_CLASS.unknown, className)} />}>
        <EyeOff />
        {compact ? 'нет истории объекта' : 'Нет истории объекта — модель здесь не даёт тревог'}
      </TooltipTrigger>
      <TooltipContent className="max-w-80">{OBJECT_HISTORY_NOTE}</TooltipContent>
    </Tooltip>
  )
}
