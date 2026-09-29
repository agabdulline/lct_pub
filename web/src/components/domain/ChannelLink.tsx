import { RunLink } from '@/components/domain/RunLink'
import { SensorTypeIcon } from '@/components/domain/SensorTypeIcon'
import { StatusDot } from '@/components/domain/StatusBadge'
import type { SensorType, Status } from '@/lib/api/types'
import { cn } from '@/lib/utils'

type Props = {
  id: number
  name: string
  sensorType?: SensorType | null
  status?: Status | null
  className?: string
}

// Link to the channel card; keeps the pinned run (?run=).
export function ChannelLink({ id, name, sensorType, status, className }: Props) {
  return (
    <RunLink
      to={`/channels/${id}`}
      className={cn('inline-flex min-w-0 items-center gap-1.5 font-medium underline-offset-4 hover:underline', className)}
    >
      {status && <StatusDot status={status} />}
      {sensorType && <SensorTypeIcon type={sensorType} withTooltip={false} className="size-3.5" />}
      <span className="truncate">{name}</span>
    </RunLink>
  )
}
