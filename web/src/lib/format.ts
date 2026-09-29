// Formatting: Moscow time (assumption A1), Russian numbers.

const TZ = 'Europe/Moscow'

const dateTime = new Intl.DateTimeFormat('ru-RU', { timeZone: TZ, dateStyle: 'short', timeStyle: 'medium' })
const dateTimeShort = new Intl.DateTimeFormat('ru-RU', {
  timeZone: TZ,
  day: '2-digit',
  month: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
})
const time = new Intl.DateTimeFormat('ru-RU', { timeZone: TZ, timeStyle: 'medium' })
const date = new Intl.DateTimeFormat('ru-RU', { timeZone: TZ, dateStyle: 'short' })
const hourFmt = new Intl.DateTimeFormat('ru-RU', { timeZone: TZ, hour: '2-digit', minute: '2-digit' })
const integer = new Intl.NumberFormat('ru-RU')

const parse = (value: string | number | Date) => (value instanceof Date ? value : new Date(value))

export const fmtDateTime = (v: string | null | undefined) => (v ? dateTime.format(parse(v)) : '—')
export const fmtDateTimeShort = (v: string | null | undefined) => (v ? dateTimeShort.format(parse(v)) : '—')
export const fmtTime = (v: string | null | undefined) => (v ? time.format(parse(v)) : '—')
export const fmtDate = (v: string | null | undefined) => (v ? date.format(parse(v.length === 10 ? `${v}T12:00:00+03:00` : v)) : '—')
export const fmtHour = (v: string | null | undefined) => (v ? hourFmt.format(parse(v)) : '—')
export const fmtInt = (v: number | null | undefined) => (v === null || v === undefined ? '—' : integer.format(v))

export const fmtPercent = (v: number | null | undefined, digits = 1) =>
  v === null || v === undefined || Number.isNaN(v) ? '—' : `${(v * 100).toFixed(digits).replace('.', ',')}%`

const factorNumber = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 3 })
/** A model feature value: numbers rounded to 3 decimals, categories as they are. */
export const fmtFactorValue = (v: number | string | boolean | null | undefined) =>
  v === null || v === undefined ? '—' : typeof v === 'number' ? factorNumber.format(v) : typeof v === 'boolean' ? (v ? 'да' : 'нет') : v

export const fmtScore = (v: number | null | undefined) => (v === null || v === undefined ? '—' : v.toFixed(2).replace('.', ','))

/** Seconds → «2,3 мин», «5,4 ч», «12,5 сут». */
export function fmtDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '—'
  const s = Math.abs(seconds)
  const f = (v: number) => (v >= 10 ? Math.round(v).toString() : v.toFixed(1).replace('.', ','))
  if (s < 60) return `${Math.round(s)} с`
  if (s < 3600) return `${f(s / 60)} мин`
  if (s < 86_400 * 2) return `${f(s / 3600)} ч`
  return `${f(s / 86_400)} сут`
}

/** Days, one decimal below ten: «1,8 сут», «23 сут». */
export const fmtDays = (v: number | null | undefined): string =>
  v === null || v === undefined ? '—' : `${v >= 10 ? Math.round(v) : v.toFixed(1).replace('.', ',')} сут`

/** Time from `from` to `to` (both ISO), in words. */
export const fmtSince = (from: string | null | undefined, to: string | null | undefined) =>
  from && to ? fmtDuration((parse(to).getTime() - parse(from).getTime()) / 1000) : '—'

export const toIso = (ms: number) => new Date(ms).toISOString()

const dayKey = new Intl.DateTimeFormat('en-CA', { timeZone: TZ, year: 'numeric', month: '2-digit', day: '2-digit' })

/** Moscow calendar day `YYYY-MM-DD` — to compare whether two moments fall on the same day. */
export const mskDay = (v: string | null | undefined) => (v ? dayKey.format(parse(v)) : null)

const pluralRules = new Intl.PluralRules('ru-RU')
/** Russian noun form for a count: plural(3, ['тревога', 'тревоги', 'тревог']) → «тревоги». */
export const plural = (n: number, [one, few, many]: [string, string, string]) => {
  const rule = pluralRules.select(n)
  return rule === 'one' ? one : rule === 'few' ? few : many
}
