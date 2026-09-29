import type { ReactNode } from 'react'

import { Badge } from '@/components/ui/badge'
import type { Status } from '@/lib/api/types'
import { STATUS_LABEL } from '@/lib/labels'
import { STATUS_BADGE_CLASS, STATUS_DOT_CLASS } from '@/lib/status'
import { cn } from '@/lib/utils'

export type { Status }

export function StatusBadge({ status, children, className }: { status: Status; children?: ReactNode; className?: string }) {
  return (
    <Badge variant="outline" className={cn('border-transparent font-medium', STATUS_BADGE_CLASS[status], className)}>
      {children ?? STATUS_LABEL[status]}
    </Badge>
  )
}

export function StatusDot({ status, pulse, className }: { status: Status; pulse?: boolean; className?: string }) {
  return (
    <span className={cn('relative inline-flex size-2 shrink-0', className)} aria-label={STATUS_LABEL[status]}>
      {pulse && <span className={cn('absolute inline-flex size-full animate-ping rounded-full opacity-60', STATUS_DOT_CLASS[status])} />}
      <span className={cn('relative inline-flex size-2 rounded-full', STATUS_DOT_CLASS[status])} />
    </span>
  )
}
