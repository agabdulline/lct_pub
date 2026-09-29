// Events a run lost at the gateway (#229): the source sent them, the gateway did not take them (emulator.md §7).

import type { LossWindow } from '@/lib/api/types'
import { fmtDate, fmtDateTimeShort, fmtInt, fmtTime, plural } from '@/lib/format'

/** A loss window by the run's clock: «03.02, 10:05–10:11 · 1 240», the end without the date on the same day. */
export function lossWindowText(w: LossWindow): string {
  const start = w.start_ts ? fmtDateTimeShort(w.start_ts) : 'начало неизвестно'
  const end = !w.end_ts ? '' : w.start_ts && fmtDate(w.end_ts) === fmtDate(w.start_ts) ? fmtTime(w.end_ts).slice(0, 5) : fmtDateTimeShort(w.end_ts)
  return `${start}${end ? `–${end}` : ''} · ${fmtInt(w.events)}`
}

export const eventsWord = (n: number) => plural(n, ['событие', 'события', 'событий'])
