import { Badge } from '@/components/ui/badge'
import type { AccessIncident } from '@/lib/api/types'
import { ACCESS_MARK } from '@/lib/labels'
import { cn } from '@/lib/utils'

import { marks } from './access-view'

/** Marks of an incident as outline badges; what each one means — in the hint. Nothing when there are none. */
export function AccessMarks({ incident, className }: { incident: AccessIncident; className?: string }) {
  const keys = marks(incident)
  if (!keys.length) return null
  return (
    <span className={cn('flex flex-wrap gap-1', className)}>
      {keys.map((k) => (
        <Badge key={k} variant="outline" className="font-normal" title={ACCESS_MARK[k].hint}>
          {ACCESS_MARK[k].label}
        </Badge>
      ))}
    </span>
  )
}
