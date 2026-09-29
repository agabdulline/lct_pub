// Context of an external model's forecast (stage 14): what the model put next to its score.

import type { Prediction } from '@/lib/api/types'

/** `context.classes` of an external forecast, most likely first; `[]` — no classes in the context. */
export function classesOf(context: Prediction['context']): [string, number][] {
  const classes = context?.classes
  if (!classes || typeof classes !== 'object') return []
  return Object.entries(classes as Record<string, unknown>)
    .filter((e): e is [string, number] => typeof e[1] === 'number')
    .sort((a, b) => b[1] - a[1])
}

/**
 * The decision's class is the most likely one, the alert is its score against the threshold (Magdeburg: P(fire) ≥ 0.5).
 * With three classes «fire» can be the most likely at P 0.42–0.49 — the class of an alert, yet no alert. Returns that
 * class when it happens: the alert class is the one whose probability is the score.
 */
export function topClassWithoutAlert(p: Pick<Prediction, 'context' | 'score' | 'alert' | 'status'>): string | null {
  const score = p.score
  if (p.status !== 'scored' || p.alert !== false || score === null) return null
  const classes = classesOf(p.context)
  if (!classes.length) return null
  const named = typeof p.context?.class === 'string' ? p.context.class : null
  const top = named ?? classes[0][0]
  const scored = classes.find(([, prob]) => Math.abs(prob - score) < 1e-6)?.[0]
  return scored === top ? top : null
}
