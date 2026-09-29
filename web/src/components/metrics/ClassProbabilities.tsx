import type { Prediction } from '@/lib/api/types'
import { fmtDuration, fmtFactorValue, fmtPercent } from '@/lib/format'
import { externalClassLabel, FEATURE_LABEL } from '@/lib/labels'
import { cn } from '@/lib/utils'

import { classesOf, topClassWithoutAlert } from './prediction-context'

/** Probabilities of an external model's classes (Magdeburg: fire, nuisance, background): inline or as a list. */
export function ClassProbabilities({ context, list = false, className }: { context: Prediction['context']; list?: boolean; className?: string }) {
  const rows = classesOf(context)
  if (!rows.length) return <span className={cn('text-xs text-muted-foreground', className)}>—</span>
  if (list)
    return (
      <div className={cn('space-y-0.5', className)}>
        {rows.map(([c, p], i) => (
          <div key={c} className="flex justify-between gap-3">
            <span className={i === 0 ? 'font-medium' : 'text-muted-foreground'}>{externalClassLabel(c)}</span>
            <span className="tabular-nums">{fmtPercent(p)}</span>
          </div>
        ))}
      </div>
    )
  return (
    <span className={cn('text-xs', className)}>
      {rows.map(([c, p], i) => (
        <span key={c} className={i === 0 ? 'font-medium' : 'text-muted-foreground'}>
          {i > 0 && ' · '}
          {externalClassLabel(c).toLowerCase()} {fmtPercent(p)}
        </span>
      ))}
    </span>
  )
}

/**
 * «Fire» is the most likely class, yet no alert: the class is the most likely of three, the alert needs its probability
 * to reach the threshold. Said next to the classes so the row does not read as a model error.
 */
export function ClassWithoutAlert({ prediction: p, list = false, className }: { prediction: Prediction; list?: boolean; className?: string }) {
  const cls = topClassWithoutAlert(p)
  if (!cls || p.score === null) return null
  const label = externalClassLabel(cls).toLowerCase()
  const threshold = p.threshold === null ? '' : ` ${fmtPercent(p.threshold, 0)}`
  const why = `Класс решения — самый вероятный из классов модели, а тревога — только когда вероятность «${label}» не ниже порога${threshold}. Здесь «${label}» самый вероятный, но ${fmtPercent(p.score)} — ниже порога: тревоги нет, и это не ошибка модели.`
  return (
    <div className={cn('text-xs text-muted-foreground', className)} title={list ? undefined : why}>
      {list ? why : `самый вероятный — ${label}, но ниже порога${threshold}: тревоги нет`}
    </div>
  )
}

/**
 * Why an external model without classes (VAST) scored a decision: its strongest factor with the value — the route,
 * the segment time, no permit.
 */
export function TopReason({ factors, className }: { factors: Prediction['factors']; className?: string }) {
  const top = factors?.length ? [...factors].sort((a, b) => Math.abs(b.contribution ?? Infinity) - Math.abs(a.contribution ?? Infinity))[0] : null
  if (!top) return <span className={cn('text-xs text-muted-foreground', className)}>—</span>
  const value = top.feature.startsWith('segment_time') && typeof top.value === 'number' ? fmtDuration(top.value) : fmtFactorValue(top.value)
  return (
    <span className={cn('text-xs', className)}>
      <span className="font-medium">{FEATURE_LABEL[top.feature] ?? top.feature}</span>
      {top.feature !== 'no_permit' && <span className="text-muted-foreground"> · {value}</span>}
    </span>
  )
}
