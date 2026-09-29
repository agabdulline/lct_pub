import type { Alarm } from '@/lib/api/types'

/** A12: the alarm is inside a planned check, and the event model says the detector will likely fail it. */
export const isPprAlert = (a: Pick<Alarm, 'verdict' | 'prediction'>) => a.verdict === 'ppr' && a.prediction?.alert === true
