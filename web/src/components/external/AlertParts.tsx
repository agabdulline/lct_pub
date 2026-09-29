// Pieces of a data set run's alert rows, shared by its feed and its dashboard (stage 14).

import { BellRing, Flame, Route, ShieldAlert, Wrench, type LucideIcon } from 'lucide-react'

import { StatusBadge } from '@/components/domain/StatusBadge'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { fmtInt, plural } from '@/lib/format'
import { cn } from '@/lib/utils'

import type { AlertGroup } from './external-alerts'

const CLASS_ICON: Record<string, LucideIcon> = { fire: Flame, violation: ShieldAlert, suspicious: Route, failure: Wrench }

export function ClassMark({ cls, className }: { cls: string; className?: string }) {
  const Icon = CLASS_ICON[cls] ?? BellRing
  return <Icon className={cn('size-4 shrink-0 text-status-alarm', className)} />
}

/** Where the alert is: one channel by name, several — how many, with the names in the tooltip. */
export function Where({ g }: { g: AlertGroup }) {
  if (g.channels.length === 1) return <span className="truncate">{g.channels[0].name}</span>
  const nodes = g.channels.every((c) => c.sensorType === 'Узел')
  const n = g.channels.length
  return (
    <Tooltip>
      <TooltipTrigger render={<span className="cursor-default underline decoration-dotted underline-offset-4" />}>
        {fmtInt(n)} {plural(n, nodes ? ['узел', 'узла', 'узлов'] : ['канал', 'канала', 'каналов'])}
      </TooltipTrigger>
      <TooltipContent className="max-w-80">{g.channels.map((c) => c.name).join(', ')}</TooltipContent>
    </Tooltip>
  )
}

/** Outcome of a row by the data set's labels; for an episode — how many of its alerts came true. */
export function GroupOutcome({ g }: { g: AlertGroup }) {
  const n = g.alerts.length
  const badge =
    g.open === n ? (
      <StatusBadge status="unknown">{g.horizon > 0 ? 'Ожидается' : 'Без разметки'}</StatusBadge>
    ) : g.fault === n - g.open ? (
      <StatusBadge status="ok">Сбылась</StatusBadge>
    ) : g.noFault === n - g.open ? (
      <StatusBadge status="risk">Ложная тревога</StatusBadge>
    ) : (
      <StatusBadge status="risk">
        Сбылась в {fmtInt(g.fault)} из {fmtInt(n - g.open)}
      </StatusBadge>
    )
  return (
    <span className="flex flex-col items-start gap-0.5">
      {badge}
      <span className="text-xs text-muted-foreground">
        {g.horizon > 0 ? `разметка набора, горизонт ${fmtInt(g.horizon)} ч${g.open && g.open < n ? ` · без исхода ${fmtInt(g.open)}` : ''}` : 'разметка набора'}
      </span>
    </span>
  )
}
