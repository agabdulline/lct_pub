import { ArrowUpRight } from 'lucide-react'

import { DatasetBadge } from '@/components/domain/DatasetBadge'
import { ObjectHistoryMark } from '@/components/domain/ObjectHistoryMark'
import { PprAlertMark } from '@/components/domain/PprAlertMark'
import { RiskMeter } from '@/components/domain/RiskMeter'
import { RunLink } from '@/components/domain/RunLink'
import { SensorTypeIcon } from '@/components/domain/SensorTypeIcon'
import { StatusBadge } from '@/components/domain/StatusBadge'
import { Button } from '@/components/ui/button'
import { Separator } from '@/components/ui/separator'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { Skeleton } from '@/components/ui/skeleton'
import { useAlarmDecision, useChannel } from '@/lib/api/queries'
import type { Prediction } from '@/lib/api/types'
import { fmtDate, fmtDateTimeShort, fmtInt } from '@/lib/format'
import { PRIORITY_LABEL, PRIORITY_STATUS, RULE_LABEL, skipReasonLabel } from '@/lib/labels'

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-3 py-1">
      <span className="text-muted-foreground">{label}</span>
      <span className="min-w-0 text-right">{children}</span>
    </div>
  )
}

function PredictionRow({ title, p, inherited }: { title: string; p: Prediction | null; inherited?: boolean }) {
  if (!p) return <Row label={title}>нет решений</Row>
  return (
    <Row label={title}>
      {p.status === 'scored' ? (
        <span className="inline-flex flex-col items-end gap-0.5">
          <RiskMeter value={p.score} threshold={p.threshold} />
          <span className="text-xs text-muted-foreground" title={inherited ? 'Повторные тревоги за сутки — без нового решения' : undefined}>
            {fmtDateTimeShort(p.decision_time)}
            {inherited && ' · ранее'}
          </span>
        </span>
      ) : (
        <span className="text-xs text-muted-foreground">пропуск: {p.skip_reason ? skipReasonLabel(p.skip_reason).toLowerCase() : '—'}</span>
      )}
    </Row>
  )
}

// Short channel card beside the graph (frontend.md §4): non-modal, the graph stays interactive.
export function ChannelPanel({ channelId, onClose }: { channelId: number | null; onClose: () => void }) {
  const { data: c, isPending, isError } = useChannel(channelId)
  const alarm = useAlarmDecision(c)
  return (
    <Sheet open={channelId !== null} onOpenChange={(open) => !open && onClose()} modal={false} disablePointerDismissal>
      <SheetContent
        showOverlay={false}
        className="w-96 gap-0 overflow-y-auto data-[side=right]:top-12 data-[side=right]:h-[calc(100svh-3rem)] sm:max-w-sm"
      >
        <SheetHeader className="pr-10">
          <SheetTitle className="flex items-center gap-2">
            {c && <SensorTypeIcon type={c.sensor_type} />}
            {c?.name ?? (isError ? 'Канал не найден' : 'Канал')}
          </SheetTitle>
          <SheetDescription>{c ? `${c.object_name} · ${c.sub_object_name}` : ' '}</SheetDescription>
        </SheetHeader>
        {isPending && channelId !== null && (
          <div className="space-y-2 px-4">
            <Skeleton className="h-6 w-32" />
            <Skeleton className="h-40" />
          </div>
        )}
        {c && (
          <div className="space-y-3 px-4 pb-4 text-sm">
            <div className="flex flex-wrap items-center gap-2">
              <StatusBadge status={c.status} />
              {c.source && c.source !== 'hackathon' && <DatasetBadge dataset={c.source} />}
              {c.status === 'ppr' && c.last_alarm_prediction?.alert && <PprAlertMark />}
              {c.object_history === false && <ObjectHistoryMark compact />}
            </div>
            <div>
              <Row label="Значение">
                <span className={c.value && /^-?\d/.test(c.value) ? 'font-mono' : undefined}>{c.value ?? '—'}</span>
                <div className="text-xs text-muted-foreground">{fmtDateTimeShort(c.value_ts)}</div>
              </Row>
              <Row label="Тип">{c.sensor_type}</Row>
              <Row label="Подсистема">{c.subsystem}</Row>
              <Row label="Тег">
                <span className="font-mono text-xs">{c.tag}</span>
              </Row>
              <Row label="Группа по тегу">{c.tag_group ?? '—'}</Row>
              <Row label="Пикет">{c.picket_number !== null ? `ПК${c.picket_number}${c.picket_offset ? `+${c.picket_offset}` : ''}` : '—'}</Row>
            </div>
            <Separator />
            <div>
              <div className="mb-1 font-medium">Прогнозы</div>
              <Row label="Риск отказа 1–30 сут">
                <RiskMeter value={c.risk} threshold={c.risk_threshold} />
              </Row>
              <PredictionRow title="Ежедневная модель" p={c.last_daily_prediction} />
              <PredictionRow title="После тревоги" p={alarm.decision} inherited={alarm.inherited} />
            </div>
            <Separator />
            <div>
              <div className="mb-1 font-medium">История</div>
              <Row label="Тревог за 30 сут">
                {fmtInt(c.counters.alarms_30d)}
                {c.counters.solitary_alarms_30d > 0 && <span className="text-muted-foreground"> · одиночных {c.counters.solitary_alarms_30d}</span>}
              </Row>
              <Row label="Отказов за год">{fmtInt(c.counters.faults_365d)}</Row>
              <Row label="Часов в отказе за 30 сут">{fmtInt(c.counters.fault_hours_30d)}</Row>
              <Row label="Последний отказ">{fmtDateTimeShort(c.counters.last_fault_at)}</Row>
            </div>
            {c.open_work_orders.length > 0 && (
              <>
                <Separator />
                <div className="space-y-1.5">
                  <div className="font-medium">Открытые заявки</div>
                  {c.open_work_orders.map((w) => (
                    <RunLink key={w.id} to={`/work-orders/${w.id}`} className="block rounded-md p-2 ring-1 ring-foreground/10 hover:bg-muted">
                      <div className="flex items-center justify-between gap-2">
                        <span className="font-medium">№{w.id}</span>
                        <StatusBadge status={PRIORITY_STATUS[w.priority]}>
                          {PRIORITY_LABEL[w.priority]}
                        </StatusBadge>
                      </div>
                      <div className="text-xs text-muted-foreground">{RULE_LABEL[w.rule]}{w.due_date ? ` · срок ${fmtDate(w.due_date)}` : ''}</div>
                    </RunLink>
                  ))}
                </div>
              </>
            )}
            <Button className="w-full" nativeButton={false} render={<RunLink to={`/channels/${c.id}`} />}>
              Полная карточка <ArrowUpRight />
            </Button>
          </div>
        )}
      </SheetContent>
    </Sheet>
  )
}
