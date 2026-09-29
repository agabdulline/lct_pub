import type { LucideIcon } from 'lucide-react'
import type { ReactNode } from 'react'

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import type { Status } from '@/lib/api/types'
import { cn } from '@/lib/utils'

const TONE: Record<Status, string> = {
  ok: 'text-status-ok',
  risk: 'text-status-risk',
  fault: 'text-status-fault',
  alarm: 'text-status-alarm',
  ppr: 'text-status-ppr',
  unknown: 'text-foreground',
}

type Props = {
  title: string
  value: ReactNode
  /** Short explanation under the value: denominator, period, caveat. */
  hint?: ReactNode
  icon?: LucideIcon
  tone?: Status
  loading?: boolean
  className?: string
  children?: ReactNode
}

// KPI tile. Metrics always go with their denominator in `hint` (AGENTS.md §5, «Честные метрики»).
export function MetricCard({ title, value, hint, icon: Icon, tone, loading, className, children }: Props) {
  return (
    <Card size="sm" className={className}>
      <CardHeader>
        <CardDescription className="flex items-center gap-1.5">
          {Icon && <Icon className="size-3.5" />}
          {title}
        </CardDescription>
        {/* The small card shrinks its title with a group variant: override it too, or KPI digits stay body-sized. */}
        <CardTitle className={cn('text-3xl font-semibold group-data-[size=sm]/card:text-3xl', tone && TONE[tone])}>
          {loading ? <Skeleton className="h-8 w-20" /> : value}
        </CardTitle>
      </CardHeader>
      {(hint || children) && (
        <CardContent className="space-y-2 text-xs text-muted-foreground">
          {hint && <div>{hint}</div>}
          {children}
        </CardContent>
      )}
    </Card>
  )
}
