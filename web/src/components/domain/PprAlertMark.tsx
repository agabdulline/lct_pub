import { TriangleAlert } from 'lucide-react'

import { RunLink } from '@/components/domain/RunLink'
import { Badge } from '@/components/ui/badge'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { STATUS_BADGE_CLASS } from '@/lib/status'
import { cn } from '@/lib/utils'

type Props = { workOrderId?: number | null; count?: number; className?: string }

/**
 * Assumption A12: an alarm inside a planned check with the event model above the threshold — the detector will
 * probably fail the check. Warning token, not the fault colour: the channel is not faulty yet.
 */
export function PprAlertMark({ workOrderId, count, className }: Props) {
  return (
    <span className={cn('inline-flex flex-wrap items-center gap-1.5', className)}>
      <Tooltip>
        <TooltipTrigger render={<Badge variant="outline" className={cn('cursor-default border-transparent font-medium', STATUS_BADGE_CLASS.risk)} />}>
          <TriangleAlert />
          {count !== undefined ? `${count} вероятно не пройдут проверку` : 'вероятно не пройдёт проверку'}
        </TooltipTrigger>
        <TooltipContent>
          Событийная модель выше порога внутри плановой проверки: датчик, скорее всего, уйдёт в «Неисправен» — на тесте 2026
          больше половины отказов после тревоги случались именно так.
        </TooltipContent>
      </Tooltip>
      {workOrderId ? (
        <RunLink to={`/work-orders/${workOrderId}`} className="text-xs font-medium underline-offset-4 hover:underline" onClick={(e) => e.stopPropagation()}>
          → заявка №{workOrderId}
        </RunLink>
      ) : null}
    </span>
  )
}
