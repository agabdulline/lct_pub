import { GitCompareArrows } from 'lucide-react'

import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import type { ModelScore } from '@/lib/api/types'
import { fmtScore } from '@/lib/format'
import { versionLabel, versionSkipText } from '@/lib/labels'
import { cn } from '@/lib/utils'

/** A version's score: its own, an earlier decision that still holds («0,43 ранее», like the primary's), or a skip. */
const scoreText = (s: ModelScore) =>
  s.inherited && s.score !== null ? `${fmtScore(s.score)} ранее` : s.status !== 'scored' || s.score === null ? 'пропуск' : fmtScore(s.score)
/** Decided: scored now or by an earlier decision of the channel within 24 h. */
const decided = (s: ModelScore) => s.status === 'scored' || !!s.inherited
/** Why a version made no decision: its reason (`skip_reason`, lct-95) or, without it, the general case. */
const skipText = (s: ModelScore) => versionSkipText(s.skip_reason)

const SKIP_NOTE =
  'Пропуск — версия решения не выносила: тревога не прошла её правила допустимости. Например, по каналу уже было решение за сутки, канал неисправен или у него мало истории; у v3 ещё — день без данных журнала за 8 суток до тревоги или канал перед тревогой не в «Норме». Вердикт — по основной.'

/**
 * The other event-model versions' decisions next to the one shown (stage 15): «v3 0,42», above the threshold in the
 * risk colour, and a mark when the versions disagree — one above its threshold, the other not. A skip says why in the
 * tooltip; `detailed` (the alarm and forecast panels) says it in a line under the scores. `current` — the version
 * already shown in the row, the primary one by default.
 */
export function VersionScores({
  scores,
  current,
  disagree,
  detailed = false,
  className,
}: {
  scores: ModelScore[] | undefined
  current?: string
  disagree?: boolean | null
  detailed?: boolean
  className?: string
}) {
  const shown = current ?? scores?.find((s) => s.primary)?.model_version
  const others = scores?.filter((s) => s.model_version !== shown) ?? []
  if (!others.length) return null
  const primary = scores?.find((s) => s.model_version === shown)
  const skipped = others.filter((s) => !decided(s))
  const scoresLine = (
    <Tooltip>
      <TooltipTrigger render={<span className={cn('inline-flex cursor-default items-center gap-1.5 text-xs', className)} />}>
        {disagree && <GitCompareArrows className="size-3.5 text-status-risk" />}
        {others.map((s) => (
          <span
            key={s.model_version}
            className={cn(
              'tabular-nums',
              s.alert ? 'font-medium text-status-risk' : 'text-muted-foreground',
              !decided(s) && 'underline decoration-dotted underline-offset-2',
            )}
          >
            {versionLabel(s.model_version)} {scoreText(s)}
          </span>
        ))}
        {disagree && <span className="text-status-risk">расходятся</span>}
      </TooltipTrigger>
      <TooltipContent className="max-w-80">
        {[primary, ...others]
          .filter((s): s is ModelScore => !!s)
          .map((s) => (
            <div key={s.model_version}>
              {versionLabel(s.model_version)}
              {s.primary ? ' (основная)' : ''}: {scoreText(s)}
              {s.threshold !== null && decided(s) && ` · порог ${fmtScore(s.threshold)} — ${s.alert ? 'тревога' : 'ниже порога'}`}
              {s.inherited && ' · решение по более ранней тревоге канала за сутки'}
              {!decided(s) && ` · ${skipText(s)}`}
            </div>
          ))}
        {disagree && <div className="pt-1">Модели расходятся: одна выше своего порога, другая нет. Вердикт — по основной.</div>}
        {disagree === null && <div className="pt-1">Сравнить нельзя: одна из моделей решение пропустила.</div>}
        {skipped.length > 0 && <div className="pt-1 text-muted-foreground">{SKIP_NOTE}</div>}
      </TooltipContent>
    </Tooltip>
  )
  if (!detailed || !skipped.length) return scoresLine
  return (
    <span className="inline-flex flex-col items-end gap-0.5">
      {scoresLine}
      {skipped.map((s) => (
        <span key={s.model_version} className="text-xs text-muted-foreground">
          {versionLabel(s.model_version)} пропустила: {skipText(s).charAt(0).toLowerCase() + skipText(s).slice(1)}
        </span>
      ))}
    </span>
  )
}
