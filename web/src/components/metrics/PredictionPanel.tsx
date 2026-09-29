import { ArrowUpRight } from 'lucide-react'

import { PredictionDecision } from '@/components/domain/ChainDecision'
import { FactorList } from '@/components/domain/FactorList'
import { ObjectHistoryMark } from '@/components/domain/ObjectHistoryMark'
import { OutcomeBadge } from '@/components/domain/OutcomeBadge'
import { RiskMeter } from '@/components/domain/RiskMeter'
import { VersionScores } from '@/components/domain/VersionScores'
import { RunLink } from '@/components/domain/RunLink'
import { SensorTypeIcon } from '@/components/domain/SensorTypeIcon'
import { Button } from '@/components/ui/button'
import { Separator } from '@/components/ui/separator'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { Skeleton } from '@/components/ui/skeleton'
import { useModels, usePrediction } from '@/lib/api/queries'
import { fmtDateTime, fmtSince } from '@/lib/format'
import { chainOf, externalClassLabel, externalHorizonHours, isHackathonModel, MODEL_DESCRIPTION, MODEL_LABEL, skipReasonLabel, versionLabel } from '@/lib/labels'
import { useRun } from '@/lib/run'

import { ClassProbabilities, ClassWithoutAlert } from './ClassProbabilities'
import { classesOf } from './prediction-context'

const CONTEXT_LABEL: Record<string, string> = {
  alarm_kind: 'Тип эпизода',
  neighbors_alarmed_1h: 'Соседей с тревогой за час',
  alarm_id: 'Тревога',
  date: 'Дата прогноза',
  target: 'Объект цели',
  event_ts: 'Время события',
  value: 'Значение события',
  class: 'Класс решения',
}

const contextValue = (k: string, v: unknown) =>
  k === 'event_ts' && typeof v === 'string'
    ? fmtDateTime(v)
    : k === 'class' && typeof v === 'string'
      ? externalClassLabel(v)
      : v !== null && typeof v === 'object'
        ? JSON.stringify(v)
        : String(v)

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-3 py-0.5">
      <span className="text-muted-foreground">{label}</span>
      <span className="text-right">{children}</span>
    </div>
  )
}

// Forecast detail beside the journal: decision, horizon, outcome, factors, context.
export function PredictionPanel({ id, onClose }: { id: number | null; onClose: () => void }) {
  const { data: p, isPending } = usePrediction(id)
  const { run } = useRun()
  const chain = chainOf(run?.primary_model)
  const models = useModels().data?.items
  const external = p ? !isHackathonModel(p.model) : false
  const info = external ? models?.find((m) => m.model === p?.model) : undefined
  // An external detector judges the moment of decision; an external forecaster (Azure PdM) has a horizon like ours.
  const detector = external && externalHorizonHours(info) === 0
  return (
    <Sheet open={id !== null} onOpenChange={(open) => !open && onClose()} modal={false} disablePointerDismissal>
      <SheetContent
        showOverlay={false}
        className="w-[26rem] gap-0 overflow-y-auto data-[side=right]:top-12 data-[side=right]:h-[calc(100svh-3rem)] sm:max-w-md"
      >
        <SheetHeader className="pr-10">
          <SheetTitle className="flex items-center gap-2">
            {p && <SensorTypeIcon type={p.sensor_type} />}
            {p ? `Прогноз №${p.id}` : 'Прогноз'}
          </SheetTitle>
          <SheetDescription>{p ? `${p.channel_name} · ${p.object_name}` : ' '}</SheetDescription>
        </SheetHeader>
        {isPending && id !== null && <Skeleton className="mx-4 h-40" />}
        {p && (
          <div className="space-y-4 px-4 pb-4 text-sm">
            <div className="rounded-lg p-3 ring-1 ring-foreground/10">
              <Row label="Модель">
                {isHackathonModel(p.model) ? MODEL_LABEL[p.model] : p.model} · {versionLabel(p.model_version)}
              </Row>
              <div className="pb-1 text-xs text-muted-foreground">
                {isHackathonModel(p.model) ? MODEL_DESCRIPTION[p.model] : `Модель внешнего набора${info?.title ? `: ${info.title}` : ''}`}
              </div>
              <Row label="Решение">{fmtDateTime(p.decision_time)}</Row>
              {detector && p.horizon_start !== p.horizon_end && <Row label="Событие">{fmtDateTime(p.horizon_start)}</Row>}
              <Row label="Горизонт">
                {detector ? (
                  'в момент решения'
                ) : (
                  <>
                    {fmtDateTime(p.horizon_start)}
                    <br />— {fmtDateTime(p.horizon_end)}
                  </>
                )}
              </Row>
              {p.status === 'scored' ? (
                <Row label="Оценка">
                  <RiskMeter value={p.score} threshold={p.threshold} />
                </Row>
              ) : (
                <Row label="Пропуск">{p.skip_reason ? skipReasonLabel(p.skip_reason) : '—'}</Row>
              )}
              {!!p.paired?.length && (
                <Row label="Другая модель">
                  <VersionScores
                    scores={[
                      { model_version: p.model_version, prediction_id: p.id, status: p.status, score: p.score, threshold: p.threshold, alert: p.alert, primary: false, inherited: false, skip_reason: p.skip_reason },
                      ...p.paired,
                    ]}
                    current={p.model_version}
                    disagree={
                      p.status === 'scored' && p.paired.every((o) => o.status === 'scored') ? p.paired.some((o) => o.alert !== p.alert) : null
                    }
                    detailed
                  />
                </Row>
              )}
              {chain && p.decided === true && (
                <Row label={`Решение ${versionLabel(run?.primary_model)}`}>
                  <PredictionDecision prediction={p} chain={chain} className="text-sm text-foreground" />
                </Row>
              )}
              <Row label="Исход">
                <OutcomeBadge
                  outcome={p.outcome}
                  alert={p.alert}
                  outcomeAt={p.outcome_at}
                  horizonEnd={p.horizon_end}
                  source={p.outcome_source}
                  external={detector}
                />
              </Row>
              {detector ? (
                <div className="pt-1 text-xs text-muted-foreground">
                  Исход — разметка набора в момент решения. Упреждения нет: модель обнаруживает, а не предсказывает.
                </div>
              ) : external ? (
                <>
                  {p.outcome === 'fault' && p.outcome_at && (
                    <Row label="Упреждение">{p.alert ? fmtSince(p.decision_time, p.outcome_at) : '— (тревоги не было)'}</Row>
                  )}
                  <div className="pt-1 text-xs text-muted-foreground">
                    Исход — начался ли отказ по разметке набора в горизонте; упреждение — от решения до его начала.
                  </div>
                </>
              ) : (
                <>
                  {p.outcome === 'fault' && p.outcome_at && (
                    <Row label="Упреждение">{p.alert ? fmtSince(p.decision_time, p.outcome_at) : '— (тревоги не было)'}</Row>
                  )}
                  <Row label="История объекта">
                    {p.object_history === null ? '—' : p.object_history ? 'есть' : <ObjectHistoryMark compact />}
                  </Row>
                  <Row label="Канал в обучении">{p.seen_channel === null ? '—' : p.seen_channel ? 'был' : 'не был'}</Row>
                </>
              )}
            </div>
            {external && classesOf(p.context).length > 0 && (
              <div>
                <div className="mb-1 font-medium">Вероятности классов</div>
                <ClassProbabilities context={p.context} list />
                <ClassWithoutAlert prediction={p} list className="mt-1.5" />
              </div>
            )}
            {p.context && Object.keys(p.context).some((k) => !(external && k === 'classes')) && (
              <div>
                <div className="mb-1 font-medium">Контекст решения</div>
                {Object.entries(p.context)
                  .filter(([k]) => !(external && k === 'classes'))
                  .map(([k, v]) => (
                    <Row key={k} label={CONTEXT_LABEL[k] ?? k}>
                      <span className="font-mono text-xs">{contextValue(k, v)}</span>
                    </Row>
                  ))}
              </div>
            )}
            <Separator />
            <div>
              <div className="mb-1.5 font-medium">Факторы</div>
              <FactorList factors={p.factors} />
            </div>
            <Button className="w-full" nativeButton={false} render={<RunLink to={`/channels/${p.channel_id}`} />}>
              Карточка канала <ArrowUpRight />
            </Button>
          </div>
        )}
      </SheetContent>
    </Sheet>
  )
}
