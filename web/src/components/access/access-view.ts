// Presentation helpers of access incidents — opened under guard (docs/architecture/access.md §5, «Лента»).

import type { AccessIncident, Status } from '@/lib/api/types'
import { fmtDateTimeShort, fmtHour, mskDay, plural } from '@/lib/format'
import { ACCESS_MARK, ACCESS_TITLE, PRIORITY_STATUS } from '@/lib/labels'

export const openingsText = (n: number) => `${n.toLocaleString('ru-RU')} ${plural(n, ['открытие', 'открытия', 'открытий'])}`

/** «КД люк 4-с ПК45» or «КД дверь отс. ПК50, КД дверь в ВК ПК50 и ещё 3» — doors and hatches by their first opening. */
export function channelsText(inc: AccessIncident, limit = 2): string {
  const shown = inc.channels.slice(0, limit).map((c) => c.name)
  const rest = inc.channels.length - shown.length
  return rest > 0 ? `${shown.join(', ')} и ещё ${rest}` : shown.join(', ')
}

/** «охрана с 16:58», with the date when the arming is not on the day of the start. */
export function armedText(inc: AccessIncident): string | null {
  if (!inc.armed_since) return null
  const sameDay = mskDay(inc.armed_since) === mskDay(inc.start_ts)
  return `охрана с ${sameDay ? fmtHour(inc.armed_since) : fmtDateTimeShort(inc.armed_since)}`
}

/** How it ended, or that it goes on: «снято через 17 мин», «до 22:45, тишина», «до конца прогона», «идёт». */
export function endText(inc: AccessIncident): string {
  if (inc.status === 'dismissed')
    return inc.disarmed_after_min !== null ? `снято через ${inc.disarmed_after_min} мин` : 'снято после открытия'
  if (inc.end_ts === null) return 'идёт'
  if (inc.end_reason === 'disarmed') return inc.disarmed_after_min !== null ? `снято через ${inc.disarmed_after_min} мин` : 'охрану сняли'
  if (inc.end_reason === 'run_end') return 'до конца прогона'
  return `до ${fmtHour(inc.end_ts)}, тишина`
}

/** Marks present on the incident, in the order of the contract: hatch, night, motion, guard faulty, repeated. */
export function marks(inc: AccessIncident): (keyof typeof ACCESS_MARK)[] {
  const out: (keyof typeof ACCESS_MARK)[] = []
  if (inc.hatch_openings > 0) out.push('hatch')
  if (inc.flags.off_hours) out.push('off_hours')
  if (inc.flags.motion) out.push('motion')
  if (inc.flags.guard_faulty) out.push('guard_faulty')
  if (inc.flags.repeated) out.push('repeated')
  return out
}

/** One line for a toast or the stream log: «Вскрытие под охраной · объект Дельта · КД люк 4-с ПК45 · 22:17 · идёт». */
export const accessLine = (inc: AccessIncident) =>
  `${ACCESS_TITLE} · ${inc.sub_object_name} · ${channelsText(inc, 1)} · ${fmtHour(inc.start_ts)} · ${endText(inc)}`

/** The incident goes on: it is in the feed and not closed yet. */
export const isOpen = (inc: AccessIncident) => inc.status === 'open'

/** Token of an incident: an open one by its priority, a finished one is neutral — like equipment episodes. */
export function accessStatus(inc: AccessIncident): Status {
  if (inc.status !== 'open') return 'unknown'
  return inc.priority ? PRIORITY_STATUS[inc.priority] : 'risk'
}

export const accessStateLabel = (inc: AccessIncident) =>
  inc.status === 'open' ? 'идёт' : inc.status === 'dismissed' ? 'снято после открытия' : 'закончился'

/** Headline of a row or card: a dismissed candidate is not an intrusion under guard — the guard was taken off in time. */
export const accessHeadline = (inc: AccessIncident) => (inc.status === 'dismissed' ? 'Снято после открытия' : ACCESS_TITLE)
