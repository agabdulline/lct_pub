// Mock of /scenario-uploads (13.1, contract of lct-bc): a CSV journal is read in the browser, columns are matched to
// journal fields by the SMVU names, synonyms or column order; the analysis follows the mapping. XLSX is not parsed
// by the mock — it gets a canned sheet. Channels are checked against the mock directory.

import type { Scenario, ScenarioEvent, ScenarioUpload, ScenarioUploadCreated, ScenarioUploadPut, UploadField, UploadMatch, UploadWarning } from '@/lib/api/types'

import { plural } from '@/lib/format'

import { MockHttpError } from './http-error'
import { registerUploaded } from './scenario-data'
import { getWorld, iso } from './world'

const MAX_BYTES = 300 * 1024 * 1024
const MAX_ROWS = 200_000

const FIELDS: { field: UploadField; label: string; required: boolean; exact: string; synonyms: string[] }[] = [
  { field: 'event_id', label: 'ID события', required: false, exact: 'ид_события', synonyms: ['id', 'event_id', 'ид', 'номер_события'] },
  { field: 'channel_id', label: 'Канал', required: true, exact: 'ид_канала_данных', synonyms: ['channel', 'channel_id', 'канал', 'ид_канала', 'tag_id'] },
  { field: 'datetime', label: 'Дата и время', required: false, exact: 'дата_время', synonyms: ['datetime', 'timestamp', 'ts', 'время_события', 'дата и время'] },
  { field: 'date', label: 'Дата', required: false, exact: 'дата', synonyms: ['date', 'day'] },
  { field: 'time', label: 'Время', required: false, exact: 'время', synonyms: ['time'] },
  { field: 'is_alarm', label: 'Тревожное', required: false, exact: 'тревожное', synonyms: ['alarm', 'is_alarm', 'тревога', 'флаг_тревоги'] },
  { field: 'value', label: 'Значение', required: true, exact: 'значение_датчика', synonyms: ['value', 'значение', 'state', 'состояние'] },
]
/** Column order of the SMVU journal — used when the file has no header. */
const JOURNAL_ORDER: UploadField[] = ['event_id', 'channel_id', 'date', 'time', 'is_alarm', 'value']

type Stored = { id: string; file: File; header: string[]; rows: string[][]; hasHeader: boolean; delimiter: string; mapping: Partial<Record<UploadField, number | null>>; matches: Partial<Record<UploadField, UploadMatch>>; title: string | null }
const uploads = new Map<string, Stored>()

const norm = (s: string) => s.trim().replace(/^"|"$/g, '').toLowerCase().replace(/\s+/g, '_')

function splitCsv(line: string, d: string): string[] {
  const out: string[] = []
  let cur = ''
  let q = false
  for (let i = 0; i < line.length; i++) {
    const ch = line[i]
    if (ch === '"') q = !q
    else if (ch === d && !q) {
      out.push(cur)
      cur = ''
    } else cur += ch
  }
  out.push(cur)
  return out.map((c) => c.trim())
}

function guessMapping(header: string[], hasHeader: boolean) {
  const mapping: Partial<Record<UploadField, number | null>> = {}
  const matches: Partial<Record<UploadField, UploadMatch>> = {}
  for (const f of FIELDS) {
    const exact = header.findIndex((h) => norm(h) === f.exact)
    const syn = exact >= 0 ? -1 : header.findIndex((h) => f.synonyms.includes(norm(h)))
    const order = hasHeader ? -1 : JOURNAL_ORDER.indexOf(f.field)
    const col = exact >= 0 ? exact : syn >= 0 ? syn : order >= 0 && order < header.length ? order : null
    mapping[f.field] = col
    matches[f.field] = exact >= 0 ? 'exact' : syn >= 0 ? 'synonym' : col !== null ? 'guess' : null
  }
  // One time source: a datetime column wins over date + time only when those are not both found.
  if (mapping.date != null && mapping.time != null) mapping.datetime = null
  return { mapping, matches }
}

function parseTs(date: string | undefined, time: string | undefined, datetime: string | undefined): number | null {
  const raw = datetime ?? (date && time ? `${date}T${time}` : undefined)
  if (!raw) return null
  const s = raw.replace(' ', 'T')
  const t = Date.parse(/[+-]\d\d:?\d\d$|Z$/.test(s) ? s : `${s}+03:00`)
  return Number.isNaN(t) ? null : t
}

const truthy = (v: string | undefined) => (v === undefined ? null : ['t', 'true', '1', 'да', 'yes'].includes(v.toLowerCase()) ? true : ['f', 'false', '0', 'нет', 'no'].includes(v.toLowerCase()) ? false : null)

type Parsed = { ts: number; channel: number; value: string; alarm: boolean; eventId: number | null }

function parseRows(u: Stored) {
  const m = u.mapping
  const cell = (r: string[], f: UploadField) => (m[f] != null ? r[m[f]!] : undefined)
  const seen = new Set<string>()
  const ok: Parsed[] = []
  let invalid = 0
  let duplicates = 0
  let badAlarm = 0
  for (const r of u.rows) {
    const ts = parseTs(cell(r, 'date'), cell(r, 'time'), cell(r, 'datetime'))
    const channel = Number(cell(r, 'channel_id'))
    const value = cell(r, 'value')
    if (ts === null || !Number.isInteger(channel) || value === undefined) {
      invalid++
      continue
    }
    const alarmRaw = cell(r, 'is_alarm')
    const alarm = truthy(alarmRaw)
    if (alarmRaw !== undefined && alarm === null) badAlarm++
    const eventId = cell(r, 'event_id') ? Number(cell(r, 'event_id')) : null
    const key = `${eventId}|${channel}|${ts}|${value}`
    if (seen.has(key)) {
      duplicates++
      continue
    }
    seen.add(key)
    ok.push({ ts, channel, value, alarm: alarm ?? false, eventId })
  }
  return { ok, invalid, duplicates, badAlarm }
}

const fmtDur = (ms: number) => (ms >= 2 * 86_400_000 ? `${Math.round(ms / 86_400_000)} сут` : ms >= 3_600_000 ? `${Math.round(ms / 3_600_000)} ч` : `${Math.round(ms / 60_000)} мин`)

function analysis(u: Stored): ScenarioUpload {
  const topo = getWorld().topo
  const { ok, invalid, duplicates, badAlarm } = parseRows(u)
  const hasTime = u.mapping.datetime != null || (u.mapping.date != null && u.mapping.time != null)
  const missing: UploadField[] = [
    ...(u.mapping.channel_id == null ? (['channel_id'] as UploadField[]) : []),
    ...(u.mapping.value == null ? (['value'] as UploadField[]) : []),
    ...(!hasTime ? ([u.mapping.date == null ? 'date' : 'time'] as UploadField[]) : []),
  ]
  const channels = new Set(ok.map((p) => p.channel))
  const unknown = [...channels].filter((c) => !topo.channelById.has(c))
  const byObject = new Map<number, Set<number>>()
  for (const c of channels) {
    const ch = topo.channelById.get(c)
    if (!ch) continue
    const set = byObject.get(ch.object_id) ?? new Set<number>()
    set.add(c)
    byObject.set(ch.object_id, set)
  }
  const ts = ok.map((p) => p.ts)
  const period = ts.length ? { start: iso(Math.min(...ts)), end: iso(Math.max(...ts)) } : null
  const label = (f: UploadField) => FIELDS.find((x) => x.field === f)!.label
  const warnings: UploadWarning[] = []
  for (const f of missing) warnings.push({ code: 'field_missing', field: f, message: `Не нашли колонку «${label(f)}» — выберите её вручную.` })
  if (!u.hasHeader) warnings.push({ code: 'no_header', message: 'В файле нет строки заголовка — колонки сопоставлены по порядку журнала СМВУ, проверьте.' })
  if (u.mapping.is_alarm == null) warnings.push({ code: 'field_missing', field: 'is_alarm', message: 'Нет колонки «Тревожное» — все события будут считаться нетревожными.' })
  if (unknown.length)
    warnings.push({ code: 'unknown_channels', count: unknown.length, examples: unknown.slice(0, 5).map(String), message: `${unknown.length} ${plural(unknown.length, ['канала', 'каналов', 'каналов'])} нет в справочнике — их события не будут переданы.` })
  if (duplicates) warnings.push({ code: 'duplicates', count: duplicates, message: `${duplicates} ${plural(duplicates, ['строка повторяется', 'строки повторяются', 'строк повторяются'])} — дубли будут пропущены.` })
  if (invalid) warnings.push({ code: 'bad_dates', count: invalid, message: `${invalid} ${plural(invalid, ['строка', 'строки', 'строк'])} без распознанной даты или канала — будут пропущены.` })
  if (badAlarm) warnings.push({ code: 'bad_alarm', count: badAlarm, message: `${badAlarm} ${plural(badAlarm, ['строка', 'строки', 'строк'])} с непонятным признаком тревоги — считаются нетревожными.` })
  if (period) {
    const span = Date.parse(period.end) - Date.parse(period.start)
    if (span > 2 * 86_400_000) warnings.push({ code: 'period', message: `Период ${fmtDur(span)} — при ×60 прогон займёт ${fmtDur(span / 60)}.` })
  }
  return {
    id: u.id,
    file_name: u.file.name,
    size_bytes: u.file.size,
    format: { kind: 'csv', encoding: 'utf-8', delimiter: u.delimiter, sheet: null, header: u.hasHeader },
    sheets: null,
    columns: u.header.map((name, index) => ({ index, name, samples: u.rows.slice(0, 5).map((r) => r[index] ?? '') })),
    fields: FIELDS.map((f) => ({ field: f.field, label: f.label, required: f.required, column: u.mapping[f.field] ?? null, match: u.matches[f.field] ?? null })),
    required_missing: missing,
    ready: missing.length === 0 && ok.length > 0,
    rows: u.rows.length,
    period,
    stats: {
      events: ok.length,
      channels: channels.size,
      unknown_channels: unknown.length,
      duplicates,
      invalid_rows: invalid,
      objects: [...byObject.entries()].map(([id, set]) => ({ id, name: topo.byId.get(id)?.name ?? `объект ${id}`, channels: set.size })),
    },
    warnings,
    preview: u.rows.slice(0, 20),
    name_suggestion: u.title ?? `Журнал ${u.file.name.replace(/\.[^.]+$/, '')}`,
  }
}

export async function analyzeUpload(file: File, onProgress?: (share: number) => void): Promise<ScenarioUpload> {
  const ext = file.name.toLowerCase().split('.').pop()
  if (ext !== 'csv' && ext !== 'xlsx') throw new MockHttpError(415, 'unsupported_media_type', 'Нужен файл CSV или XLSX')
  if (file.size > MAX_BYTES) throw new MockHttpError(413, 'too_large', 'Файл больше 300 МБ')
  for (const share of [0.25, 0.5, 0.75, 1]) {
    await new Promise((r) => setTimeout(r, 120))
    onProgress?.(share)
  }
  if (ext === 'xlsx') throw new MockHttpError(400, 'bad_file', 'Моки не читают XLSX — проверьте на стенде или сохраните как CSV')
  let text: string
  try {
    text = await file.text()
  } catch {
    throw new MockHttpError(400, 'bad_file', 'Файл не читается — проверьте, что он не открыт в другой программе, и выберите снова')
  }
  const lines = text.split(/\r?\n/).filter((l) => l.trim())
  if (!lines.length) throw new MockHttpError(400, 'bad_file', 'Файл пустой')
  const first = lines[0]
  const delimiter = [';', '\t', ','].reduce((best, d) => (first.split(d).length > first.split(best).length ? d : best), ',')
  const head = splitCsv(first, delimiter)
  const hasHeader = head.some((h) => /[a-zа-яё]/i.test(h) && Number.isNaN(Number(h.replace(/"/g, ''))) && !/^\d{4}-\d\d-\d\d/.test(h.replace(/"/g, '')))
  const header = hasHeader ? head.map((h) => h.replace(/^"|"$/g, '')) : head.map((_, i) => `Колонка ${i + 1}`)
  const rows = lines.slice(hasHeader ? 1 : 0, (hasHeader ? 1 : 0) + MAX_ROWS).map((l) => splitCsv(l, delimiter).map((c) => c.replace(/^"|"$/g, '')))
  const { mapping, matches } = guessMapping(header, hasHeader)
  const id = Math.random().toString(16).slice(2, 10)
  const u: Stored = { id, file, header, rows, hasHeader, delimiter, mapping, matches, title: null }
  uploads.set(id, u)
  return analysis(u)
}

export function getUpload(id: string): ScenarioUpload {
  const u = uploads.get(id)
  if (!u) throw new MockHttpError(404, 'upload_not_found', 'Загрузка не найдена — загрузите файл заново')
  return analysis(u)
}

export function putUpload(id: string, body: ScenarioUploadPut): ScenarioUpload | ScenarioUploadCreated {
  const u = uploads.get(id)
  if (!u) throw new MockHttpError(404, 'upload_not_found', 'Загрузка не найдена — загрузите файл заново')
  for (const [f, col] of Object.entries(body.mapping ?? {})) {
    u.mapping[f as UploadField] = col
    u.matches[f as UploadField] = col == null ? null : u.matches[f as UploadField] ?? 'guess'
  }
  if (body.title) u.title = body.title
  const a = analysis(u)
  if (body.dry_run) return a
  if (!a.ready) throw new MockHttpError(422, 'required_missing', `Не назначены обязательные поля: ${a.required_missing.join(', ')}`)
  return createScenario(u, a, body.title || a.name_suggestion)
}

function createScenario(u: Stored, a: ScenarioUpload, title: string): ScenarioUploadCreated {
  const topo = getWorld().topo
  const { ok } = parseRows(u)
  ok.sort((x, y) => x.ts - y.ts)
  const events: ScenarioEvent[] = []
  for (const p of ok) {
    const c = topo.channelById.get(p.channel)
    if (!c) continue
    events.push({
      seq: events.length + 1,
      ts: iso(p.ts),
      event_id: p.eventId ?? events.length + 1,
      channel_id: c.id,
      channel_name: c.name,
      sensor_type: c.sensor_type,
      object_id: c.object_id,
      object_name: topo.byId.get(c.object_id)?.name ?? '',
      sub_object_id: c.sub_object_id,
      sub_object_name: topo.byId.get(c.sub_object_id)?.name ?? '',
      value: p.value,
      is_alarm: p.alarm,
    })
  }
  const scenario: Scenario = {
    id: `upload-${u.id}`,
    title,
    description: `Журнал из файла ${u.file.name}: ${a.stats.events} событий, ${a.stats.channels} каналов.`,
    period_start: a.period?.start ?? iso(Date.now()),
    period_end: a.period?.end ?? iso(Date.now()),
    objects: a.stats.objects.map((o) => ({ id: o.id, name: o.name })),
    default_speed: 60,
    max_speed: 60,
    include_gas: events.some((e) => e.sensor_type === 'Газовый датчик'),
    timeline: [],
    ready: true,
    source: 'upload',
    source_file: u.file.name,
    uploaded_at: new Date().toISOString(),
    warnings: a.warnings.filter((w) => w.code !== 'field_missing').map((w) => ({ code: w.code, count: w.count ?? null, message: w.message })),
  }
  registerUploaded(scenario, events, a.stats.unknown_channels)
  return {
    scenario_id: scenario.id,
    title,
    period: a.period,
    events: events.length,
    channels: a.stats.channels,
    objects: scenario.objects,
    warnings: a.warnings,
  }
}
