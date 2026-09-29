import { History } from 'lucide-react'

import { QueryError } from '@/components/domain/QueryError'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger } from '@/components/ui/dialog'
import { Skeleton } from '@/components/ui/skeleton'
import { useRunAudit } from '@/lib/api/queries'
import type { ReverdictSummary, RunAuditItem } from '@/lib/api/types'
import { fmtDateTimeShort, fmtInt } from '@/lib/format'
import { versionLabel } from '@/lib/labels'

import { reverdictText } from './reverdict'

function Entry({ item }: { item: RunAuditItem }) {
  const d = item.details as Record<string, unknown>
  const when = (
    <div className="text-xs text-muted-foreground tabular-nums">
      {item.sim_ts ? `${fmtDateTimeShort(item.sim_ts)} прогона · ` : ''}
      {fmtDateTimeShort(item.ts)}
    </div>
  )
  if (item.action === 'primary_model') {
    const from = typeof d.from === 'string' ? d.from : null
    const to = typeof d.to === 'string' ? d.to : null
    return (
      <li className="space-y-0.5">
        <div>
          Основная модель: {from && from !== to ? `${versionLabel(from)} → ${versionLabel(to)}` : versionLabel(to)}
          <span className="text-muted-foreground">
            {d.apply_to_past ? ' · с пересчётом прошлых тревог' : ' · по новым тревогам, прошлые вердикты не менялись'}
          </span>
        </div>
        {when}
      </li>
    )
  }
  const summary = d as unknown as ReverdictSummary
  return (
    <li className="space-y-0.5">
      <div>
        Пересчёт вердиктов по {versionLabel(summary.version)}
        <div className="text-xs text-muted-foreground">{reverdictText(summary)}</div>
      </div>
      {when}
    </li>
  )
}

/** The run's audit (15.2, `/runs/{id}/audit`): who switched the primary version when, and what re-verdicting changed. */
export function RunAudit({ runId, limit }: { runId: number; limit?: number }) {
  const audit = useRunAudit(runId)
  if (audit.isError) return <QueryError error={audit.error} onRetry={() => void audit.refetch()} what="Журнал прогона" compact />
  if (audit.isPending) return <Skeleton className="h-12" />
  const items = audit.data.items
  if (!items.length) return <p className="text-sm text-muted-foreground">Основную модель в этом прогоне не меняли.</p>
  const shown = limit ? items.slice(0, limit) : items
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-1.5 text-sm font-medium">
        <History className="size-4" /> Журнал прогона
        <span className="font-normal text-muted-foreground">· {fmtInt(items.length)}</span>
      </div>
      <ol className="space-y-2 text-sm">
        {shown.map((it) => (
          <Entry key={it.id} item={it} />
        ))}
      </ol>
      {shown.length < items.length && <p className="text-xs text-muted-foreground">Ещё {fmtInt(items.length - shown.length)} — в журнале из таблицы прогонов.</p>}
    </div>
  )
}

/** The whole audit of a run from the runs table: a link opening it in a dialog. */
export function RunAuditLink({ runId, title }: { runId: number; title: string }) {
  return (
    <Dialog>
      <DialogTrigger render={<button type="button" className="text-left text-foreground underline-offset-4 hover:underline" />}>Журнал изменений</DialogTrigger>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Журнал прогона №{runId}</DialogTitle>
          <DialogDescription>{title}: смены основной модели и пересчёты вердиктов, новые сверху.</DialogDescription>
        </DialogHeader>
        <div className="max-h-[60vh] overflow-y-auto">
          <RunAudit runId={runId} />
        </div>
      </DialogContent>
    </Dialog>
  )
}
