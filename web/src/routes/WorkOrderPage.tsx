import { ArrowLeft, Map as MapIcon } from 'lucide-react'
import { useState } from 'react'
import { useParams } from 'react-router'
import { toast } from 'sonner'

import { ChannelLink } from '@/components/domain/ChannelLink'
import { FactorList } from '@/components/domain/FactorList'
import { OutcomeBadge } from '@/components/domain/OutcomeBadge'
import { QueryError } from '@/components/domain/QueryError'
import { RiskMeter } from '@/components/domain/RiskMeter'
import { RunLink } from '@/components/domain/RunLink'
import { StatusBadge, StatusDot } from '@/components/domain/StatusBadge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from '@/components/ui/empty'
import { Field, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'
import { Textarea } from '@/components/ui/textarea'
import { ApiError } from '@/lib/api/client'
import { usePatchWorkOrder, useWorkOrder } from '@/lib/api/queries'
import type { WorkOrderStatus } from '@/lib/api/types'
import { fmtDate, fmtDateTime, fmtDateTimeShort } from '@/lib/format'
import { modelLabel, PRIORITY_LABEL, PRIORITY_STATUS, RULE_LABEL, WO_STATUS_LABEL, WO_STATUS_STATUS } from '@/lib/labels'
import { useSimNow } from '@/lib/sim-now'

/** Allowed transitions: new → in_progress → done | rejected; a new order can be rejected at once. */
const NEXT: Record<WorkOrderStatus, { to: WorkOrderStatus; label: string; variant?: 'outline' | 'destructive' }[]> = {
  new: [
    { to: 'in_progress', label: 'Взять в работу' },
    { to: 'rejected', label: 'Отклонить', variant: 'outline' },
  ],
  in_progress: [
    { to: 'done', label: 'Выполнена' },
    { to: 'rejected', label: 'Отклонить', variant: 'outline' },
  ],
  done: [],
  rejected: [],
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-3 py-1 text-sm">
      <span className="text-muted-foreground">{label}</span>
      <span className="min-w-0 text-right">{children}</span>
    </div>
  )
}

// Work order card: the basis (rule, forecast), status changes with a comment, history.
export function WorkOrderPage() {
  const id = Number(useParams().id)
  const valid = Number.isInteger(id) && id > 0
  const { data: w, isError, error, refetch } = useWorkOrder(valid ? id : null)
  const patch = usePatchWorkOrder(valid ? id : 0)
  const simNow = useSimNow()
  const [comment, setComment] = useState('')
  const [assignee, setAssignee] = useState<string | null>(null)

  if (isError && !(error instanceof ApiError && error.status === 404))
    return <QueryError error={error} onRetry={() => void refetch()} what="Заявка" />
  if (!valid || isError)
    return (
      <Empty>
        <EmptyHeader>
          <EmptyTitle>Заявка не найдена</EmptyTitle>
          <EmptyDescription>{error?.message ?? 'Неверный номер заявки'}</EmptyDescription>
        </EmptyHeader>
      </Empty>
    )
  if (!w) return <Skeleton className="mx-auto h-96 w-full max-w-5xl" />

  const open = w.status === 'new' || w.status === 'in_progress'
  const overdue = open && !!simNow && !!w.due_date && new Date(`${w.due_date}T23:59:59+03:00`) < new Date(simNow)
  const p = w.prediction
  const alarmId = typeof p?.context?.alarm_id === 'number' ? p.context.alarm_id : null
  const who = assignee ?? w.assignee ?? ''

  const apply = (to?: WorkOrderStatus) =>
    patch.mutate(
      {
        // Only what changed: an empty comment must not wipe the previous one.
        ...(to ? { status: to } : {}),
        ...(comment.trim() ? { comment: comment.trim() } : {}),
        ...(who.trim() !== (w.assignee ?? '') ? { assignee: who.trim() || null } : {}),
      },
      {
        onSuccess: () => {
          toast.success(to ? `Заявка №${w.id}: ${WO_STATUS_LABEL[to].toLowerCase()}` : `Заявка №${w.id} обновлена`)
          setComment('')
        },
        onError: (e) => toast.error('Не сохранено', { description: e.message }),
      },
    )

  return (
    <div className="mx-auto w-full max-w-5xl space-y-4">
      <RunLink to="/work-orders" className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground">
        <ArrowLeft className="size-4" /> Заявки
      </RunLink>
      <div className="flex flex-wrap items-start gap-3">
        <div className="min-w-0 flex-1">
          <h1 className="text-xl font-semibold">
            Заявка №{w.id}
          </h1>
          <p className="text-base">{w.title}</p>
        </div>
        <StatusBadge status={PRIORITY_STATUS[w.priority]}>{PRIORITY_LABEL[w.priority]} приоритет</StatusBadge>
        <StatusBadge status={WO_STATUS_STATUS[w.status]}>{WO_STATUS_LABEL[w.status]}</StatusBadge>
      </div>

      <div className="grid gap-4 lg:grid-cols-[1fr_22rem]">
        <div className="space-y-4">
          <Card>
            <CardHeader>
              <CardTitle>Основание · {RULE_LABEL[w.rule].toLowerCase()}</CardTitle>
              <CardDescription>{w.rule_description}</CardDescription>
            </CardHeader>
            {p && (
              <CardContent className="space-y-3">
                <div className="rounded-lg p-3 ring-1 ring-foreground/10">
                  <Row label="Прогноз">
                    №{p.id} · {modelLabel(p.model)} · {p.model_version}
                  </Row>
                  <Row label="Решение">{fmtDateTime(p.decision_time)}</Row>
                  <Row label="Горизонт">
                    {fmtDateTimeShort(p.horizon_start)} — {fmtDateTimeShort(p.horizon_end)}
                  </Row>
                  <Row label="Оценка">
                    <RiskMeter value={p.score} threshold={p.threshold} />
                  </Row>
                  <Row label="Исход">
                    <OutcomeBadge outcome={p.outcome} alert={p.alert} outcomeAt={p.outcome_at} horizonEnd={p.horizon_end} source={p.outcome_source} />
                  </Row>
                  {alarmId !== null && (
                    <Row label="Тревога">
                      <RunLink to={`/?alarm=${alarmId}`} className="underline-offset-4 hover:underline">
                        открыть в ленте
                      </RunLink>
                    </Row>
                  )}
                </div>
                <div>
                  <div className="mb-1.5 text-sm font-medium">Факторы прогноза</div>
                  <FactorList factors={p.factors} />
                </div>
              </CardContent>
            )}
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>История</CardTitle>
            </CardHeader>
            <CardContent>
              <ol className="space-y-3">
                {[...w.history].reverse().map((h, i) => (
                  <li key={i} className="flex gap-3 text-sm">
                    <StatusDot status={WO_STATUS_STATUS[h.status]} className="mt-1.5" />
                    <div className="min-w-0">
                      <div>
                        <span className="font-medium">{WO_STATUS_LABEL[h.status]}</span>
                        <span className="text-muted-foreground"> · {fmtDateTime(h.ts)}</span>
                        {h.assignee && <span className="text-muted-foreground"> · {h.assignee}</span>}
                      </div>
                      {h.comment && <div className="text-muted-foreground">{h.comment}</div>}
                    </div>
                  </li>
                ))}
              </ol>
            </CardContent>
          </Card>
        </div>

        <div className="space-y-4">
          <Card>
            <CardHeader>
              <CardTitle>Канал</CardTitle>
            </CardHeader>
            <CardContent>
              <ChannelLink id={w.channel_id} name={w.channel_name} sensorType={w.sensor_type} />
              <Row label="Объект">{w.object_name}</Row>
              {w.sub_object_name && <Row label="Подобъект">{w.sub_object_name}</Row>}
              <Row label="Тип">{w.sensor_type}</Row>
              <Button variant="outline" size="sm" className="mt-2 w-full" nativeButton={false} render={<RunLink to={`/channels/${w.channel_id}`} />}>
                <MapIcon /> Карточка канала
              </Button>
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Исполнение</CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              <Row label="Создана">{fmtDateTime(w.created_at)}</Row>
              <Row label="Срок">
                <span className={overdue ? 'font-medium text-status-alarm' : undefined}>
                  {fmtDate(w.due_date)}
                  {overdue && ' · просрочена'}
                </span>
              </Row>
              {w.closed_at && <Row label="Закрыта">{fmtDateTime(w.closed_at)}</Row>}
              {w.comment && <Row label="Комментарий">{w.comment}</Row>}
              {open && (
                <>
                  <Field>
                    <FieldLabel htmlFor="wo-assignee">Исполнитель</FieldLabel>
                    <Input id="wo-assignee" value={who} onChange={(e) => setAssignee(e.target.value)} placeholder="ФИО или смена" />
                  </Field>
                  <Field>
                    <FieldLabel htmlFor="wo-comment">Комментарий</FieldLabel>
                    <Textarea
                      id="wo-comment"
                      rows={3}
                      value={comment}
                      onChange={(e) => setComment(e.target.value)}
                      placeholder={w.status === 'in_progress' ? 'Что сделано: заменён датчик, очищен…' : 'необязательно'}
                    />
                  </Field>
                  <div className="flex flex-wrap gap-2">
                    {NEXT[w.status].map((n) => (
                      <Button key={n.to} variant={n.variant} disabled={patch.isPending} onClick={() => apply(n.to)}>
                        {n.label}
                      </Button>
                    ))}
                    <Button variant="ghost" disabled={patch.isPending || (!comment.trim() && who === (w.assignee ?? ''))} onClick={() => apply()}>
                      Сохранить
                    </Button>
                  </div>
                </>
              )}
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  )
}
