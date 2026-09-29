import { ArrowUpRight, Map as MapIcon } from 'lucide-react'

import { AlarmDecision } from '@/components/domain/ChainDecision'
import { FactorList } from '@/components/domain/FactorList'
import { ObjectHistoryMark } from '@/components/domain/ObjectHistoryMark'
import { PprAlertMark } from '@/components/domain/PprAlertMark'
import { RiskMeter } from '@/components/domain/RiskMeter'
import { VersionScores } from '@/components/domain/VersionScores'
import { RunLink } from '@/components/domain/RunLink'
import { SensorTypeIcon } from '@/components/domain/SensorTypeIcon'
import { StatusDot } from '@/components/domain/StatusBadge'
import { VerdictBadge } from '@/components/domain/VerdictBadge'
import { Button } from '@/components/ui/button'
import { Separator } from '@/components/ui/separator'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { Skeleton } from '@/components/ui/skeleton'
import { isPprAlert } from '@/lib/alarm'
import { useAlarm, useEmulatorState } from '@/lib/api/queries'
import type { AlarmDetail } from '@/lib/api/types'
import { fmtDateTime, fmtScore, fmtSince, fmtTime } from '@/lib/format'
import { ALARM_KIND_LABEL, chainOf, skipReasonLabel, STATUS_LABEL, versionLabel } from '@/lib/labels'
import { useRun } from '@/lib/run'

// Scoring takes up to ~10 real seconds (object debounce 5 s + model); on an accelerated run
// that is minutes of run time.
function pendingNote(speed: number | null): string {
  const base = 'Тревога принята, модель её оценивает: дебаунс по объекту 5 с плюс расчёт — до 10 реальных секунд.'
  if (!speed || speed <= 1) return base
  const minutes = Math.max(1, Math.ceil((10 * speed) / 60))
  return `${base} При текущем ускорении ×${speed} это до ${minutes} мин по часам прогона.`
}

/** Why the verdict is what it is — in words (assumption A6). */
function explain(a: AlarmDetail, speed: number | null): string {
  const p = a.prediction
  switch (a.verdict) {
    case 'ppr': {
      const base = a.verdict_refined
        ? 'Эпизод распознан как плановая проверка сигнализации — вердикт уточнён. Модель оценивала тревогу до распознавания.'
        : 'Тревога внутри эпизода плановой проверки: много датчиков объекта срабатывают по очереди.'
      return isPprAlert(a)
        ? `${base} Модель при этом выше порога (${fmtScore(p?.score)} при ${fmtScore(p?.threshold)}): датчик, вероятно, не пройдёт проверку.`
        : base
    }
    case 'sensor_fault':
      return `Событийная модель: ${fmtScore(p?.score)} при пороге ${fmtScore(p?.threshold)} — вероятно, датчик уходит в неисправность в ближайшие 24 ч.${
        p?.inherited ? ' Оценка — по более ранней тревоге канала: одно решение на канал в сутки.' : ''
      }`
    case 'reaction':
      if (a.episode?.kind === 'group')
        return p?.object_history === false
          ? 'Групповой эпизод вне плановой проверки — несколько датчиков объекта. Штатная реакция. Модель здесь слепа: у объекта нет истории исходов, её «ниже порога» ничего не говорит о датчиках.'
          : 'Групповой эпизод вне плановой проверки — несколько датчиков объекта. Штатная реакция.'
      if (p?.object_history === false)
        return `Модель ниже порога (${fmtScore(p.score)} < ${fmtScore(p.threshold)}), но у объекта нет истории исходов — здесь она тревог не даёт, и «ниже порога» ничего не говорит о датчике. Штатная реакция.`
      return `Модель ниже порога (${fmtScore(p?.score)} < ${fmtScore(p?.threshold)}): признаков неисправности датчика нет. Штатная реакция.`
    case 'no_score':
      return `Модель тревогу не оценивала: ${p?.skip_reason ? skipReasonLabel(p.skip_reason).toLowerCase() : 'нет решения'}. Штатная реакция.`
    default:
      return pendingNote(speed)
  }
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-3 py-0.5">
      <span className="text-muted-foreground">{label}</span>
      <span className="text-right">{children}</span>
    </div>
  )
}

// Alarm detail beside the feed: non-modal, the feed stays clickable.
export function AlarmPanel({ alarmId, onClose }: { alarmId: number | null; onClose: () => void }) {
  const { data: a, isPending } = useAlarm(alarmId)
  const { run } = useRun()
  const chain = chainOf(run?.primary_model)
  const em = useEmulatorState().data
  const speed = em?.state === 'running' ? Math.round(em.target_speed) : null
  return (
    <Sheet open={alarmId !== null} onOpenChange={(open) => !open && onClose()} modal={false} disablePointerDismissal>
      <SheetContent
        showOverlay={false}
        className="w-[26rem] gap-0 overflow-y-auto data-[side=right]:top-12 data-[side=right]:h-[calc(100svh-3rem)] sm:max-w-md"
      >
        <SheetHeader className="pr-10">
          <SheetTitle className="flex items-center gap-2">
            {a && <SensorTypeIcon type={a.sensor_type} />}
            {a ? a.channel_name : 'Тревога'}
          </SheetTitle>
          <SheetDescription>{a ? `${a.object_name} · ${a.sub_object_name} · ${fmtDateTime(a.ts)}` : ' '}</SheetDescription>
        </SheetHeader>
        {isPending && alarmId !== null && (
          <div className="space-y-2 px-4">
            <Skeleton className="h-6 w-40" />
            <Skeleton className="h-32" />
          </div>
        )}
        {a && (
          <div className="space-y-4 px-4 pb-4 text-sm">
            <div className="space-y-2">
              <div className="flex flex-wrap items-center gap-2">
                <VerdictBadge verdict={a.verdict} refined={a.verdict_refined} inherited={a.prediction?.inherited} skipReason={a.prediction?.skip_reason} />
                {isPprAlert(a) && <PprAlertMark workOrderId={a.work_order_id} />}
                {a.prediction?.object_history === false && <ObjectHistoryMark compact />}
              </div>
              <p>{explain(a, speed)}</p>
              <p className="text-xs text-muted-foreground">Сервис не гасит тревогу: вердикт — подсказка, решение за диспетчером.</p>
            </div>

            <div className="rounded-lg p-3 ring-1 ring-foreground/10">
              <Row label="Значение">{a.value}</Row>
              {a.prediction?.score !== null && a.prediction?.score !== undefined && (
                <Row label={`Оценка модели${a.scores?.length ? ` ${versionLabel(a.scores.find((x) => x.primary)?.model_version)}` : ''}`}>
                  <RiskMeter value={a.prediction.score} threshold={a.prediction.threshold} />
                </Row>
              )}
              {(a.scores?.length ?? 0) > 1 && (
                <Row label="Другая модель">
                  <VersionScores scores={a.scores} disagree={a.disagree} detailed />
                </Row>
              )}
              {chain && (a.scores?.length ?? 0) > 0 && (
                <Row label={`Решение ${versionLabel(run?.primary_model)}`}>
                  <AlarmDecision alarm={a} chain={chain} className="text-sm text-foreground" />
                </Row>
              )}
              <Row label="Эпизод">
                {a.episode ? `${ALARM_KIND_LABEL[a.episode.kind]} · ${a.episode.n_channels} датч.` : '—'}
              </Row>
              {a.episode && a.episode.n_channels > 1 && (
                <Row label="Начало эпизода">
                  {fmtTime(a.episode.start_ts)} · за {fmtSince(a.episode.start_ts, a.ts)} до этой тревоги
                </Row>
              )}
              {a.work_order_id && (
                <Row label="Заявка">
                  <RunLink to={`/work-orders/${a.work_order_id}`} className="font-medium underline-offset-4 hover:underline">
                    №{a.work_order_id}
                  </RunLink>
                </Row>
              )}
            </div>

            <div>
              <div className="mb-1.5 font-medium">Соседи по пикету, тревоги за час до</div>
              {a.neighbors.length === 0 && <div className="text-muted-foreground">Соседей того же типа с пикетом нет</div>}
              <ul className="space-y-1">
                {a.neighbors.map((n) => (
                  <li key={n.channel_id} className="flex items-center gap-2">
                    <StatusDot status={n.status} />
                    <RunLink to={`/channels/${n.channel_id}`} className="truncate underline-offset-4 hover:underline">
                      {n.channel_name}
                    </RunLink>
                    <span className="ml-auto shrink-0 text-xs text-muted-foreground">
                      {n.ts ? `тревога ${fmtTime(n.ts)}` : `молчит · ${STATUS_LABEL[n.status].toLowerCase()}`}
                    </span>
                  </li>
                ))}
              </ul>
            </div>

            {a.prediction_detail && (
              <>
                <Separator />
                <div>
                  <div className="mb-1.5 font-medium">
                    Факторы прогноза <span className="font-normal text-muted-foreground">· {a.prediction_detail.model_version}</span>
                  </div>
                  <FactorList factors={a.prediction_detail.factors} />
                </div>
              </>
            )}

            <div className="flex gap-2">
              <Button className="flex-1" nativeButton={false} render={<RunLink to={`/channels/${a.channel_id}`} />}>
                Карточка канала <ArrowUpRight />
              </Button>
              <Button
                variant="outline"
                nativeButton={false}
                render={<RunLink to={{ pathname: `/objects/${a.sub_object_id}`, search: `?channel=${a.channel_id}` }} />}
              >
                <MapIcon /> На карте
              </Button>
            </div>
          </div>
        )}
      </SheetContent>
    </Sheet>
  )
}
