import type { Alarm, ModelScore, Prediction } from '@/lib/api/types'
import { decisionText } from '@/lib/labels'
import { cn } from '@/lib/utils'

/**
 * Under a chain primary (`v3>baseline-v0`): whose decision made the verdict of an alarm, and why not the chain's
 * first version — «решение v0 — v3 пропустила: день без данных журнала». From `Alarm.prediction.decided_by` and
 * `fallback_reason` (lct-95); on an older backend — from the scores (`primary` is the version that decided).
 */
export function AlarmDecision({ alarm, chain, short = false, className }: { alarm: Alarm; chain: string[]; short?: boolean; className?: string }) {
  if (!alarm.prediction || alarm.verdict === 'pending') return null
  const decided = alarm.prediction.decided_by ?? alarm.scores.find((s) => s.primary && s.status === 'scored')?.model_version ?? null
  const reason = alarm.prediction.fallback_reason ?? alarm.scores.find((s) => s.model_version === chain[0])?.skip_reason
  // No part decided and nothing to say why — the skip itself is shown next to it.
  if (!decided && !reason) return null
  return <Decision chain={chain} by={decided} reason={reason} short={short} className={className} />
}

/**
 * The same for a forecast of the journal: shown only on the forecast the verdict of its alarm points at (`decided`),
 * a scored one — who decided; the last part's skip — that no part decided.
 */
export function PredictionDecision({ prediction: p, chain, short = false, className }: { prediction: Prediction; chain: string[]; short?: boolean; className?: string }) {
  if (p.decided !== true) return null
  const first: Pick<ModelScore, 'skip_reason'> | undefined = p.model_version === chain[0] ? undefined : p.paired.find((o) => o.model_version === chain[0])
  const reason = p.model_version === chain[0] && p.status !== 'scored' ? p.skip_reason : first?.skip_reason
  return <Decision chain={chain} by={p.status === 'scored' ? p.model_version : null} reason={reason} short={short} className={className} />
}

function Decision({ chain, by, reason, short, className }: { chain: string[]; by: string | null; reason: string | null | undefined; short: boolean; className?: string }) {
  const text = decisionText(chain, by, reason, short)
  return (
    <div className={cn('text-xs text-muted-foreground', short && 'max-w-64 truncate', className)} title={short ? decisionText(chain, by, reason) : undefined}>
      {text}
    </div>
  )
}
