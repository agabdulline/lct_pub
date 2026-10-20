import { StatusBadge } from '@/components/domain/StatusBadge'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import type { Outcome, OutcomeSource } from '@/lib/api/types'
import { fmtDateTimeShort } from '@/lib/format'
import { outcomeKind } from '@/lib/outcome'

type Props = {
  outcome: Outcome | null
  alert: boolean | null
  outcomeAt?: string | null
  horizonEnd?: string
  source?: OutcomeSource | null
  /** An external model: the outcome is the data set's label at the moment of decision, not a fault later. */
  external?: boolean
  /**
   * The decision is in the metrics (`inMetrics` of lib/outcome). `false` with a known outcome — the horizon is not over
   * by the run's «now»: the metrics card counts it among «ещё N — горизонт не истёк». A fault that has already started
   * is final, so the row shows «Сбылся» / «Пропуск» at once with a note; a recounted «no fault» shows «Горизонт не истёк».
   */
  counted?: boolean
}

const VIEW = {
  tp: { status: 'ok', label: 'Сбылся', hint: 'Модель предупредила, отказ случился' },
  fp: { status: 'risk', label: 'Ложная тревога', hint: 'Модель предупредила, отказа не было' },
  fn: { status: 'alarm', label: 'Пропуск', hint: 'Модель не предупредила, отказ случился' },
  tn: { status: 'unknown', label: 'Верно: норма', hint: 'Модель не предупредила, отказа не было' },
  open: { status: 'unknown', label: 'Ожидается', hint: 'Горизонт прогноза ещё не истёк' },
  skipped: { status: 'unknown', label: 'Без оценки', hint: 'Прогноз пропущен правилами допустимости' },
} as const

const VIEW_EXTERNAL = {
  tp: { status: 'ok', label: 'Сбылся', hint: 'Модель подала тревогу, по разметке набора событие шло' },
  fp: { status: 'risk', label: 'Ложная тревога', hint: 'Модель подала тревогу, по разметке события не было' },
  fn: { status: 'alarm', label: 'Пропуск', hint: 'Модель промолчала, по разметке событие шло' },
  tn: { status: 'unknown', label: 'Верно: норма', hint: 'Модель промолчала, по разметке события не было' },
  open: { status: 'unknown', label: 'Без разметки', hint: 'Интервал набора без разметки: исхода нет, в метрики не входит' },
  skipped: { status: 'unknown', label: 'Без оценки', hint: 'Решение пропущено' },
} as const

// Outcome of a forecast against the journal (docs/api.md, «Исход прогноза»).
export function OutcomeBadge({ outcome, alert, outcomeAt, horizonEnd, source, external = false, counted = true }: Props) {
  const kind = outcomeKind(outcome, alert)
  const early = !counted && outcome === 'fault'
  if (!counted && !early && kind !== 'open' && kind !== 'skipped')
    return (
      <Tooltip>
        <TooltipTrigger render={<span className="inline-flex cursor-default items-center gap-1.5" />}>
          <StatusBadge status="unknown">Горизонт не истёк</StatusBadge>
          <span className="text-xs text-muted-foreground">в метрики не входит</span>
        </TooltipTrigger>
        <TooltipContent>
          <div>
            Горизонт{horizonEnd ? ` — до ${fmtDateTimeShort(horizonEnd)}` : ''}: по часам прогона он не истёк, решение в
            метрики не входит.
          </div>
          <div>
            Исход уже известен: {outcome === 'fault' ? `отказ начался ${fmtDateTimeShort(outcomeAt)}` : 'отказа не было'}
            {source === 'history' ? ' — досчитан после конца прогона' : ''}.
          </div>
        </TooltipContent>
      </Tooltip>
    )
  const v = (external ? VIEW_EXTERNAL : VIEW)[kind]
  const when = external
    ? null
    : outcome === 'fault'
      ? `Отказ ${fmtDateTimeShort(outcomeAt)}`
      : kind === 'open' && horizonEnd
        ? `Горизонт до ${fmtDateTimeShort(horizonEnd)}`
        : null
  return (
    <Tooltip>
      <TooltipTrigger render={<span className="inline-flex cursor-default items-center gap-1.5" />}>
        <StatusBadge status={v.status}>{v.label}</StatusBadge>
        {source === 'history' && <span className="text-xs text-muted-foreground">по журналу</span>}
        {early && <span className="text-xs text-muted-foreground">в метрики — после горизонта</span>}
      </TooltipTrigger>
      <TooltipContent>
        <div>{v.hint}</div>
        {when && <div>{when}</div>}
        {source === 'history' && <div>Досчитан по журналу после конца прогона: горизонт выходит за его часы.</div>}
        {early && (
          <div>
            Горизонт{horizonEnd ? ` — до ${fmtDateTimeShort(horizonEnd)}` : ''} не истёк: в метрики решение войдёт после
            него, исход уже не изменится.
          </div>
        )}
      </TooltipContent>
    </Tooltip>
  )
}
