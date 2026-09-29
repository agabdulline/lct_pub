import { CircleAlert, RefreshCw } from 'lucide-react'
import { Link } from 'react-router'

import { Button } from '@/components/ui/button'
import { Empty, EmptyContent, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from '@/components/ui/empty'
import { describeError } from '@/lib/api/errors'
import { cn } from '@/lib/utils'

type Props = { error: unknown; onRetry?: () => void; what?: string; compact?: boolean; className?: string }

// Error state of a data block: what failed, why, retry and a link to the system state.
export function QueryError({ error, onRetry, what, compact, className }: Props) {
  const { title, hint } = describeError(error)
  if (compact)
    return (
      <div className={cn('flex items-start gap-2 rounded-lg bg-status-alarm/5 p-3 text-sm', className)}>
        <CircleAlert className="mt-0.5 size-4 shrink-0 text-status-alarm" />
        <div className="min-w-0 flex-1">
          <div className="font-medium">
            {what ? `${what}: ` : ''}
            {title.toLowerCase()}
          </div>
          <div className="text-muted-foreground">{hint}</div>
        </div>
        {onRetry && (
          <Button variant="ghost" size="icon-sm" aria-label="Повторить" onClick={onRetry}>
            <RefreshCw />
          </Button>
        )}
      </div>
    )
  return (
    <Empty className={className}>
      <EmptyHeader>
        <EmptyMedia variant="icon">
          <CircleAlert className="text-status-alarm" />
        </EmptyMedia>
        <EmptyTitle>
          {what ? `${what}: ` : ''}
          {title.toLowerCase()}
        </EmptyTitle>
        <EmptyDescription>{hint}</EmptyDescription>
      </EmptyHeader>
      <EmptyContent className="flex-row justify-center gap-2">
        {onRetry && (
          <Button variant="outline" size="sm" onClick={onRetry}>
            <RefreshCw /> Повторить
          </Button>
        )}
        <Button variant="ghost" size="sm" nativeButton={false} render={<Link to="/system" />}>
          Состояние системы
        </Button>
      </EmptyContent>
    </Empty>
  )
}
