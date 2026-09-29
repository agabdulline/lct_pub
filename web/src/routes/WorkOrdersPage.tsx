import { ClipboardList, Download, Plus } from 'lucide-react'
import { useState } from 'react'
import { useNavigate } from 'react-router'
import { toast } from 'sonner'

import { ChannelLink } from '@/components/domain/ChannelLink'
import { ObjectSelect } from '@/components/domain/ObjectSelect'
import { Pager } from '@/components/domain/Pager'
import { QueryError } from '@/components/domain/QueryError'
import { StatusBadge, StatusDot } from '@/components/domain/StatusBadge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from '@/components/ui/empty'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { ChannelPickerDialog } from '@/components/work-orders/ChannelPickerDialog'
import { CreateWorkOrderDialog } from '@/components/work-orders/CreateWorkOrderDialog'
import { apiDownload } from '@/lib/api/client'
import { useWorkOrders } from '@/lib/api/queries'
import type { Channel, WorkOrderPriority, WorkOrderRule, WorkOrderStatus } from '@/lib/api/types'
import { fmtDate, fmtDateTimeShort, fmtInt } from '@/lib/format'
import { placeLabel, PRIORITY_LABEL, PRIORITY_STATUS, RULE_LABEL, WO_STATUS_LABEL, WO_STATUS_STATUS } from '@/lib/labels'
import { useRun, useRunTo } from '@/lib/run'
import { useSearchState } from '@/lib/search-state'
import { useSimNow } from '@/lib/sim-now'
import { cn } from '@/lib/utils'

const PAGE_SIZE = 50
const STATUSES: WorkOrderStatus[] = ['new', 'in_progress', 'done', 'rejected']
const RULES: WorkOrderRule[] = ['alarm_model_alert', 'daily_model_alert', 'solitary_repeat', 'fault_persisting', 'manual']
const PRIORITIES: WorkOrderPriority[] = ['high', 'medium', 'low']

const isOverdue = (due: string | null, status: WorkOrderStatus, now: string | null) =>
  (status === 'new' || status === 'in_progress') && !!due && !!now && new Date(`${due}T23:59:59+03:00`) < new Date(now)

function StatusCounter({ status, active, onClick, count }: { status: WorkOrderStatus; active: boolean; onClick: () => void; count: number | undefined }) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      className={cn(
        'flex items-center justify-between rounded-xl bg-card px-3 py-2.5 text-left text-sm ring-1 ring-foreground/10 transition-colors hover:bg-muted/50',
        active && 'ring-2 ring-ring',
      )}
    >
      <span className="flex items-center gap-2">
        <StatusDot status={WO_STATUS_STATUS[status]} />
        {WO_STATUS_LABEL[status]}
      </span>
      <span className="text-lg font-semibold">{count === undefined ? '—' : fmtInt(count)}</span>
    </button>
  )
}

// Work orders (docs/product/v1-scope.md, экран 6): list with filters and statuses, CSV export, manual orders.
export function WorkOrdersPage() {
  const s = useSearchState()
  const navigate = useNavigate()
  const runTo = useRunTo()
  const { runId } = useRun()
  const simNow = useSimNow()
  const statuses = s.list('status')
  const priority = s.get('priority') ?? 'all'
  const rule = s.get('rule') ?? 'all'
  const objectId = s.num('object')
  const page = s.num('page') ?? 1

  const [picker, setPicker] = useState(false)
  const [channel, setChannel] = useState<Channel | null>(null)
  const [exporting, setExporting] = useState(false)

  const filters = {
    status: statuses,
    priority: priority === 'all' ? null : priority,
    rule: rule === 'all' ? null : rule,
    object_id: objectId,
  }
  const list = useWorkOrders({ ...filters, page, size: PAGE_SIZE })
  // Counters: one light request per status under the same filters.
  const base = { priority: filters.priority, rule: filters.rule, object_id: objectId, size: 1 }
  const counts = {
    new: useWorkOrders({ ...base, status: 'new' }).data?.total,
    in_progress: useWorkOrders({ ...base, status: 'in_progress' }).data?.total,
    done: useWorkOrders({ ...base, status: 'done' }).data?.total,
    rejected: useWorkOrders({ ...base, status: 'rejected' }).data?.total,
  }

  const toggleStatus = (st: WorkOrderStatus) => {
    const next = statuses.includes(st) ? statuses.filter((x) => x !== st) : [...statuses, st]
    s.set({ status: next.join(',') || null })
  }

  const exportCsv = async () => {
    setExporting(true)
    try {
      await apiDownload('/work-orders/export.csv', { ...filters, run: runId }, `zayavki-to${runId ? `-progon-${runId}` : ''}.csv`)
    } catch (e) {
      toast.error('Выгрузка не удалась', { description: (e as Error).message })
    } finally {
      setExporting(false)
    }
  }

  const ruleItems = [{ value: 'all', label: 'Все основания' }, ...RULES.map((r) => ({ value: r, label: RULE_LABEL[r] }))]
  const priorityItems = [{ value: 'all', label: 'Любой приоритет' }, ...PRIORITIES.map((p) => ({ value: p, label: PRIORITY_LABEL[p] }))]
  const data = list.data

  return (
    <div className="mx-auto w-full max-w-7xl space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Заявки на ТО</h1>
          <p className="text-sm text-muted-foreground">
            Автоматические — по правилам и прогнозам моделей, одна открытая на канал и правило; ручные — от диспетчера.
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" onClick={exportCsv} disabled={exporting}>
            <Download /> {exporting ? 'Выгружаем…' : 'Выгрузить CSV'}
          </Button>
          <Button onClick={() => setPicker(true)}>
            <Plus /> Новая заявка
          </Button>
        </div>
      </div>

      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
        {STATUSES.map((st) => (
          <StatusCounter key={st} status={st} active={statuses.includes(st)} onClick={() => toggleStatus(st)} count={counts[st]} />
        ))}
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <ObjectSelect value={objectId} onChange={(id) => s.set({ object: id })} className="w-56" />
        <Select items={ruleItems} value={rule} onValueChange={(v) => s.set({ rule: !v || v === 'all' ? null : v })}>
          <SelectTrigger size="sm" className="w-56" aria-label="Основание">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {ruleItems.map((i) => (
              <SelectItem key={i.value} value={i.value}>
                {i.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select items={priorityItems} value={priority} onValueChange={(v) => s.set({ priority: !v || v === 'all' ? null : v })}>
          <SelectTrigger size="sm" className="w-44" aria-label="Приоритет">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {priorityItems.map((i) => (
              <SelectItem key={i.value} value={i.value}>
                {i.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      <Card className="py-0">
        <CardContent className="px-0">
          {list.isPending && <Skeleton className="m-4 h-80" />}
          {list.isError && <QueryError error={list.error} onRetry={() => void list.refetch()} what="Заявки" />}
          {data && data.items.length === 0 && (
            <Empty>
              <EmptyHeader>
                <EmptyMedia variant="icon">
                  <ClipboardList />
                </EmptyMedia>
                <EmptyTitle>Заявок нет</EmptyTitle>
                <EmptyDescription>С такими фильтрами заявок нет.</EmptyDescription>
              </EmptyHeader>
            </Empty>
          )}
          {data && data.items.length > 0 && (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="pl-4">№</TableHead>
                  <TableHead>Создана</TableHead>
                  <TableHead>Что сделать</TableHead>
                  <TableHead>Канал</TableHead>
                  <TableHead>Основание</TableHead>
                  <TableHead>Приоритет</TableHead>
                  <TableHead>Срок</TableHead>
                  <TableHead>Статус</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.items.map((w) => {
                  const overdue = isOverdue(w.due_date, w.status, simNow)
                  return (
                    <TableRow key={w.id} className="cursor-pointer" onClick={() => navigate(runTo(`/work-orders/${w.id}`))}>
                      <TableCell className="pl-4 font-medium tabular-nums">{w.id}</TableCell>
                      <TableCell className="text-xs tabular-nums">{fmtDateTimeShort(w.created_at)}</TableCell>
                      <TableCell className="max-w-72">
                        <div className="truncate">{w.title}</div>
                        {w.assignee && <div className="truncate text-xs text-muted-foreground">{w.assignee}</div>}
                      </TableCell>
                      <TableCell className="max-w-48">
                        <span onClick={(e) => e.stopPropagation()}>
                          <ChannelLink id={w.channel_id} name={w.channel_name} sensorType={w.sensor_type} />
                        </span>
                        <div className="truncate text-xs text-muted-foreground" title={w.sub_object_name ? `${w.object_name} · ${w.sub_object_name}` : w.object_name}>
                          {placeLabel(w)}
                        </div>
                      </TableCell>
                      <TableCell className="text-xs">{RULE_LABEL[w.rule]}</TableCell>
                      <TableCell>
                        <StatusBadge status={PRIORITY_STATUS[w.priority]}>{PRIORITY_LABEL[w.priority]}</StatusBadge>
                      </TableCell>
                      <TableCell className={cn('text-xs tabular-nums', overdue && 'font-medium text-status-alarm')}>
                        {fmtDate(w.due_date)}
                        {overdue && <div>просрочена</div>}
                      </TableCell>
                      <TableCell>
                        <StatusBadge status={WO_STATUS_STATUS[w.status]}>{WO_STATUS_LABEL[w.status]}</StatusBadge>
                      </TableCell>
                    </TableRow>
                  )
                })}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
      {data && data.total > PAGE_SIZE && (
        <Pager page={page} size={PAGE_SIZE} total={data.total} onPage={(p) => s.set({ page: p > 1 ? p : null }, true)} noun="заявок" />
      )}

      <ChannelPickerDialog open={picker} onOpenChange={setPicker} onPick={setChannel} />
      {channel && (
        <CreateWorkOrderDialog
          key={channel.id}
          open
          onOpenChange={(open) => !open && setChannel(null)}
          channel={channel}
          now={simNow}
          onCreated={(id) => navigate(runTo(`/work-orders/${id}`))}
        />
      )}
    </div>
  )
}
