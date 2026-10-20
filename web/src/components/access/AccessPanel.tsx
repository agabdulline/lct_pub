import { ClipboardList, Map as MapIcon, Shield, ShieldAlert, ShieldOff } from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'

import { RunLink } from '@/components/domain/RunLink'
import { SensorTypeIcon } from '@/components/domain/SensorTypeIcon'
import { StatusBadge } from '@/components/domain/StatusBadge'
import { Button } from '@/components/ui/button'
import { Field, FieldLabel } from '@/components/ui/field'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { Skeleton } from '@/components/ui/skeleton'
import { Textarea } from '@/components/ui/textarea'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { useAccessIncident, usePatchAccessIncident } from '@/lib/api/queries'
import type { AccessIncidentDetail, AccessResolution } from '@/lib/api/types'
import { fmtDateTime, fmtDateTimeShort, fmtInt, fmtTime, mskDay } from '@/lib/format'
import { ACCESS_END_LABEL, ACCESS_NOTE, ACCESS_RESOLUTION_LABEL, ACCESS_RULE_TEXT, ACCESS_TITLE, PRIORITY_LABEL, PRIORITY_STATUS } from '@/lib/labels'

import { AccessMarks } from './AccessMarks'
import { ACCESS_MODEL_CAVEAT, accessHeadline, accessStateLabel, accessStatus, decisionText, endText, openingsText } from './access-view'

const RESOLUTIONS: AccessResolution[] = ['works', 'intrusion', 'false_alarm']

const num2 = (v: number | null | undefined) => (v === null || v === undefined ? '—' : v.toFixed(2).replace('.', ','))

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-3 py-0.5">
      <span className="text-muted-foreground">{label}</span>
      <span className="text-right">{children}</span>
    </div>
  )
}

type TimelineItem =
  | { kind: 'guard'; ts: string; value: string }
  | { kind: 'opening'; ts: string; o: AccessIncidentDetail['opening_events'][number] }

/** Openings and guard transitions in time: what happened on the sub-object from arming till the end. */
function Timeline({ inc }: { inc: AccessIncidentDetail }) {
  const items: TimelineItem[] = [
    ...inc.guard_events.map((g) => ({ kind: 'guard' as const, ts: g.ts, value: g.value })),
    ...inc.opening_events.map((o) => ({ kind: 'opening' as const, ts: o.ts, o })),
  ].sort((a, b) => Date.parse(a.ts) - Date.parse(b.ts) || (a.kind === 'guard' ? -1 : 1))
  // Times of the start day read as HH:MM:SS; the arming may be days before.
  const at = (ts: string) => (mskDay(ts) === mskDay(inc.start_ts) ? fmtTime(ts) : fmtDateTimeShort(ts))
  return (
    <ol className="space-y-1.5">
      {items.map((it, i) =>
        it.kind === 'guard' ? (
          <li key={`g-${i}`} className="flex items-center gap-2 text-muted-foreground">
            {it.value === 'Снято с охраны' ? <ShieldOff className="size-3.5 shrink-0" /> : <Shield className="size-3.5 shrink-0" />}
            <span>{it.value}</span>
            <span className="ml-auto shrink-0 text-xs tabular-nums">{at(it.ts)}</span>
          </li>
        ) : (
          <li key={`o-${i}`} className="flex items-start gap-2">
            <SensorTypeIcon type={it.o.sensor_type} className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" />
            <span className="min-w-0">
              <RunLink to={`/channels/${it.o.channel_id}`} className="block truncate underline-offset-4 hover:underline">
                {it.o.channel_name}
              </RunLink>
              <span className="text-xs text-muted-foreground">
                «Не замкнут»{it.o.is_alarm ? ' · флаг тревоги охраны' : ' · без флага тревоги'} ·{' '}
                {/* closings after the end are not in the incident: the door may have been closed later */}
                {it.o.closed_ts ? `закрыли в ${at(it.o.closed_ts)}` : inc.end_ts ? 'не закрыта к концу инцидента' : 'не закрыта'}
              </span>
            </span>
            <span className="ml-auto shrink-0 text-xs tabular-nums">{at(it.ts)}</span>
          </li>
        ),
      )}
    </ol>
  )
}

/**
 * The dispatcher's review: who opened. Separate from the work order — the order is closed on the orders screen.
 * Keyed by the saved review: a review saved elsewhere resets the draft, a new opening from the stream does not. The
 * mutation lives in the panel: the stream announces the saved review before the PATCH answers, this component is
 * re-created by its key, and the callbacks of the call would be lost with it.
 */
function Review({ inc, patch }: { inc: AccessIncidentDetail; patch: ReturnType<typeof usePatchAccessIncident> }) {
  const [resolution, setResolution] = useState<AccessResolution | null>(inc.resolution)
  const [comment, setComment] = useState(inc.comment ?? '')
  const changed = resolution !== inc.resolution || comment.trim() !== (inc.comment ?? '')
  const save = (body: { resolution: AccessResolution | null; comment: string | null }) =>
    patch.mutate(body, {
      onSuccess: (r) => toast.success(r.resolution ? `Разобрано: ${ACCESS_RESOLUTION_LABEL[r.resolution].toLowerCase()}` : 'Разбор снят'),
      onError: (e) => toast.error('Не сохранено', { description: e.message }),
    })
  return (
    <div className="space-y-2">
      <div className="font-medium">Разбор</div>
      <ToggleGroup
        value={resolution ? [resolution] : []}
        onValueChange={(v) => setResolution((v[0] as AccessResolution | undefined) ?? null)}
        size="sm"
        variant="outline"
        className="flex-wrap"
        aria-label="Кто открыл"
      >
        {RESOLUTIONS.map((r) => (
          <ToggleGroupItem key={r} value={r}>
            {ACCESS_RESOLUTION_LABEL[r]}
          </ToggleGroupItem>
        ))}
      </ToggleGroup>
      <Field>
        <FieldLabel htmlFor="access-comment">Комментарий</FieldLabel>
        <Textarea
          id="access-comment"
          rows={2}
          value={comment}
          onChange={(e) => setComment(e.target.value)}
          placeholder="Кто был на месте, по какому наряду…"
        />
      </Field>
      <div className="flex flex-wrap items-center gap-2">
        <Button disabled={!changed || patch.isPending} onClick={() => save({ resolution, comment: comment.trim() || null })}>
          Сохранить разбор
        </Button>
        {inc.resolution && (
          <Button variant="ghost" disabled={patch.isPending} onClick={() => save({ resolution: null, comment: null })}>
            Снять разбор
          </Button>
        )}
      </div>
      <p className="text-xs text-muted-foreground">Разбор заявку не закрывает — её закрывают в заявках.</p>
    </div>
  )
}

/**
 * Opened under guard beside the feed (docs/architecture/access.md §5): openings in time, guard transitions, marks,
 * the dispatcher's review, the work order. Non-modal, like the alarm and equipment cards. Who opened the incident — the
 * access model at once or the rule after 5 min — with what the model is not (a proxy, not intrusion detection).
 */
export function AccessPanel({ incidentId, onClose }: { incidentId: number | null; onClose: () => void }) {
  const { data: inc, isPending } = useAccessIncident(incidentId)
  const patch = usePatchAccessIncident(incidentId ?? 0)
  return (
    <Sheet open={incidentId !== null} onOpenChange={(o) => !o && onClose()} modal={false} disablePointerDismissal>
      <SheetContent
        showOverlay={false}
        className="w-[26rem] gap-0 overflow-y-auto data-[side=right]:top-12 data-[side=right]:h-[calc(100svh-3rem)] sm:max-w-md"
      >
        <SheetHeader className="pr-10">
          <SheetTitle className="flex items-center gap-2">
            <ShieldAlert className="size-4" />
            {inc ? accessHeadline(inc) : ACCESS_TITLE}
          </SheetTitle>
          <SheetDescription>{inc ? `${inc.sub_object_name} · ${fmtDateTime(inc.start_ts)}` : ' '}</SheetDescription>
        </SheetHeader>
        {isPending && incidentId !== null && (
          <div className="space-y-2 px-4">
            <Skeleton className="h-6 w-40" />
            <Skeleton className="h-40" />
          </div>
        )}
        {inc && (
          <div className="space-y-4 px-4 pb-4 text-sm">
            <div className="space-y-2">
              <div className="flex flex-wrap items-center gap-2">
                <StatusBadge status={accessStatus(inc)}>{accessStateLabel(inc)}</StatusBadge>
                {inc.priority && (
                  <StatusBadge status={inc.status === 'open' ? PRIORITY_STATUS[inc.priority] : 'unknown'}>
                    {PRIORITY_LABEL[inc.priority].toLowerCase()} приоритет
                  </StatusBadge>
                )}
                {inc.status !== 'open' && <span className="text-muted-foreground">{endText(inc)}</span>}
              </div>
              <AccessMarks incident={inc} />
              {inc.verdict || inc.model_note ? (
                <div className="space-y-1 rounded-lg p-3 ring-1 ring-foreground/10">
                  <p className="font-medium">{decisionText(inc).long}</p>
                  {inc.score !== null && inc.score !== undefined && (
                    <p className="text-xs text-muted-foreground">
                      Оценка модели {num2(inc.score)} при пороге {num2(inc.threshold)} · {inc.model_version}
                    </p>
                  )}
                  <p className="text-xs text-muted-foreground">{ACCESS_MODEL_CAVEAT}</p>
                  <p className="text-xs text-muted-foreground">{ACCESS_RULE_TEXT}</p>
                </div>
              ) : (
                <div className="rounded-lg p-3 ring-1 ring-foreground/10">
                  <p className="font-medium">{ACCESS_NOTE}.</p>
                  <p className="mt-1 text-xs text-muted-foreground">{ACCESS_RULE_TEXT}</p>
                </div>
              )}
            </div>

            <div className="rounded-lg p-3 ring-1 ring-foreground/10">
              <Row label="Подобъект">
                <RunLink to={`/objects/${inc.sub_object_id}`} className="underline-offset-4 hover:underline">
                  {inc.sub_object_name}
                </RunLink>
              </Row>
              <Row label="Объект">{inc.object_name}</Row>
              <Row label="На охране с">
                {inc.armed_since ? (
                  <RunLink to={`/channels/${inc.guard_channel_id}`} className="underline-offset-4 hover:underline">
                    {fmtDateTime(inc.armed_since)}
                  </RunLink>
                ) : (
                  '—'
                )}
              </Row>
              <Row label="Первое открытие">{fmtDateTime(inc.start_ts)}</Row>
              {inc.qualified_ts && (
                <Row label="Стал инцидентом">
                  {`${fmtTime(inc.qualified_ts)} — ${inc.decided_by === 'model' ? 'сразу по оценке модели' : '5 мин без снятия охраны'}`}
                </Row>
              )}
              <Row label="Конец">
                {inc.end_ts ? `${fmtTime(inc.end_ts)} · ${inc.end_reason ? ACCESS_END_LABEL[inc.end_reason] : '—'}` : 'идёт'}
              </Row>
              <Row label="Открытий">
                {fmtInt(inc.openings)}
                {inc.hatch_openings > 0 && ` · люков ${fmtInt(inc.hatch_openings)}`}
              </Row>
              <Row label="Заявка">
                {inc.work_order_id ? (
                  <RunLink to={`/work-orders/${inc.work_order_id}`} className="font-medium underline-offset-4 hover:underline">
                    №{inc.work_order_id}
                  </RunLink>
                ) : inc.run_id === null ? (
                  <span className="text-muted-foreground">нет — базовая история</span>
                ) : (
                  '—'
                )}
              </Row>
            </div>

            <div>
              <div className="mb-1.5 font-medium">
                {openingsText(inc.openings)} и охрана
              </div>
              <Timeline inc={inc} />
            </div>

            <Review key={`${inc.id}:${inc.resolution ?? ''}:${inc.comment ?? ''}`} inc={inc} patch={patch} />

            <div className="flex flex-col gap-2">
              {inc.work_order_id && (
                <Button variant="outline" nativeButton={false} render={<RunLink to={`/work-orders/${inc.work_order_id}`} />}>
                  <ClipboardList /> Заявка №{inc.work_order_id} — проверить вскрытие
                </Button>
              )}
              <Button variant="outline" nativeButton={false} render={<RunLink to={`/objects/${inc.sub_object_id}`} />}>
                <MapIcon /> На карте — {inc.sub_object_name}
              </Button>
            </div>
          </div>
        )}
      </SheetContent>
    </Sheet>
  )
}
