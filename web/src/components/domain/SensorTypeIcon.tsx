import { CircleHelp } from 'lucide-react'

import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import type { SensorType } from '@/lib/api/types'
import { SENSOR_ICON } from '@/lib/sensor-icons'
import { cn } from '@/lib/utils'

export function SensorTypeIcon({ type, className, withTooltip = true }: { type: SensorType | null; className?: string; withTooltip?: boolean }) {
  const Icon = (type && SENSOR_ICON[type]) || CircleHelp
  const icon = <Icon className={cn('size-4 shrink-0 text-muted-foreground', className)} aria-label={type ?? 'Тип неизвестен'} />
  if (!withTooltip) return icon
  return (
    <Tooltip>
      <TooltipTrigger render={<span className="inline-flex" />}>{icon}</TooltipTrigger>
      <TooltipContent>{type ?? 'Тип неизвестен'}</TooltipContent>
    </Tooltip>
  )
}
