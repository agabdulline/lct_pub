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
  if (inc.status === 'candidate') return 'ждём снятия охраны'
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
  inc.status === 'candidate'
    ? 'вероятно свой'
    : inc.status === 'open'
      ? 'идёт'
      : inc.status === 'dismissed'
        ? 'снято после открытия'
        : 'закончился'

/** Headline of a row or card: a dismissed candidate is not an intrusion under guard — the guard was taken off in time. */
export const accessHeadline = (inc: AccessIncident) =>
  inc.status === 'dismissed' ? 'Снято после открытия' : inc.status === 'candidate' ? 'Открытие под охраной' : ACCESS_TITLE

/** What the access model is and is not (docs/research/models-and-rules.md): said next to every model decision. */
export const ACCESS_MODEL_CAVEAT =
  'Модель «снимут ли охрану» — прокси «свой или чужой» по тому, снимут ли охрану за 5 мин, а не обнаружение проникновения. ' +
  'Снятие охраны — код или ключ, а не подтверждённый допуск; зона охраны — подобъект (Q23).'

const score = (v: number | null | undefined) => (v === null || v === undefined ? '' : ` (${v.toFixed(2).replace('.', ',')})`)

/**
 * Who opened the incident and why — the access model (lct-8f) is a proxy «own or not» by whether the guard is taken off
 * within 5 min, not intrusion detection; the rule backs it up. `short` — the feed cell, else — the incident panel.
 */
export function decisionText(inc: AccessIncident): { short: string; long: string } {
  const model = score(inc.score)
  if (inc.status === 'candidate')
    return {
      short: `модель: вероятно свой${model}`,
      long: `Модель сочла открытие «своим»${model}: заявки нет. Если охрану не снимут за 5 мин, инцидент заведёт правило.`,
    }
  if (inc.decided_by === 'model')
    return {
      short: `решила модель: нужна реакция${model}`,
      long: `Модель при первом открытии: нужна реакция${model} — инцидент и заявка сразу, не через 5 мин.${
        inc.end_reason === 'disarmed' && (inc.disarmed_after_min ?? 99) <= 5 ? ' Охрану сняли в течение 5 мин — тревога модели оказалась лишней.' : ''
      }`,
    }
  if (inc.decided_by === 'rule') {
    const why =
      inc.verdict === 'own'
        ? `модель сочла своим${model}, но охрану не сняли за 5 мин`
        : inc.model_note === 'ml_unavailable'
          ? 'модель была недоступна'
          : 'охрану не сняли за 5 мин'
    return { short: 'по правилам охраны', long: `Инцидент завело правило: ${why}.` }
  }
  return { short: 'по правилам охраны', long: 'Метка правил охраны: охрану не сняли за 5 мин.' }
}
