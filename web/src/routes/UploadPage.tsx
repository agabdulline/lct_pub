import { ArrowLeft, CircleAlert, CircleCheck, FileSpreadsheet, Info, Upload } from 'lucide-react'
import { useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { useNavigate, useParams } from 'react-router'
import { toast } from 'sonner'

import { QueryError } from '@/components/domain/QueryError'
import { RunLink } from '@/components/domain/RunLink'
import { StatusBadge } from '@/components/domain/StatusBadge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Progress } from '@/components/ui/progress'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { Spinner } from '@/components/ui/spinner'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { apiPut, apiUpload, type ApiError } from '@/lib/api/client'
import { useScenarioUpload } from '@/lib/api/queries'
import type { ScenarioUpload, ScenarioUploadCreated, ScenarioUploadPut, UploadField, UploadMatch } from '@/lib/api/types'
import { fmtDateTime, fmtDuration, fmtInt, plural } from '@/lib/format'
import { useRunTo } from '@/lib/run'
import { cn } from '@/lib/utils'

const JOURNAL_COLUMNS = 'ид_события, ид_канала_данных, дата, время, тревожное, значение_датчика'

const MATCH_VIEW: Record<Exclude<UploadMatch, null>, { label: string; status: 'ok' | 'unknown' | 'risk' }> = {
  exact: { label: 'как в журнале', status: 'ok' },
  synonym: { label: 'по названию', status: 'unknown' },
  guess: { label: 'по порядку — проверьте', status: 'risk' },
}

const fmtBytes = (n: number) => (n >= 1024 * 1024 ? `${(n / 1024 / 1024).toFixed(1).replace('.', ',')} МБ` : `${Math.max(1, Math.round(n / 1024))} КБ`)

function BackLink({ to, children }: { to: string; children: React.ReactNode }) {
  return (
    <RunLink to={to} className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground">
      <ArrowLeft className="size-3.5" /> {children}
    </RunLink>
  )
}

/** Step 1: the file. Drag and drop or choose; upload progress, then the analysis by the backend. */
function ChooseFile() {
  const navigate = useNavigate()
  const runTo = useRunTo()
  const input = useRef<HTMLInputElement>(null)
  const [drag, setDrag] = useState(false)
  const [progress, setProgress] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)

  const send = async (file: File | undefined) => {
    if (!file) return
    setError(null)
    setProgress(0)
    try {
      const res = await apiUpload<ScenarioUpload>('/scenario-uploads', file, setProgress)
      navigate(runTo(`/emulator/upload/${res.id}`))
    } catch (e) {
      setError((e as ApiError).message)
      setProgress(null)
    }
  }

  return (
    <div className="mx-auto w-full max-w-3xl space-y-4">
      <BackLink to="/emulator">Эмулятор СМВУ</BackLink>
      <div>
        <h1 className="text-xl font-semibold">Загрузить журнал</h1>
        <p className="text-sm text-muted-foreground">
          Свой журнал событий станет сценарием эмулятора: его сигналы будут переданы в систему так же, как встроенные.
        </p>
      </div>
      <Card>
        <CardContent>
          <button
            type="button"
            onClick={() => input.current?.click()}
            onDragOver={(e) => {
              e.preventDefault()
              setDrag(true)
            }}
            onDragLeave={() => setDrag(false)}
            onDrop={(e) => {
              e.preventDefault()
              setDrag(false)
              void send(e.dataTransfer.files[0])
            }}
            disabled={progress !== null}
            className={cn(
              'flex w-full flex-col items-center gap-2 rounded-xl border-2 border-dashed px-6 py-12 text-center transition-colors hover:bg-muted/40',
              drag && 'border-ring bg-muted/40',
            )}
          >
            <Upload className="size-8 text-muted-foreground" />
            <span className="font-medium">Перетащите файл сюда или выберите на компьютере</span>
            <span className="text-sm text-muted-foreground">CSV или XLSX, до 300 МБ</span>
          </button>
          <input ref={input} type="file" accept=".csv,.xlsx" className="hidden" onChange={(e) => void send(e.target.files?.[0])} />
          {progress !== null && (
            <div className="mt-4 space-y-1.5">
              <Progress value={Math.round(progress * 100)} />
              <p className="flex items-center gap-2 text-sm text-muted-foreground">
                {progress < 1 ? (
                  `Загружаем — ${Math.round(progress * 100)}%`
                ) : (
                  <>
                    <Spinner /> Разбираем файл: колонки, каналы, период…
                  </>
                )}
              </p>
            </div>
          )}
          {error && (
            <p className="mt-4 flex items-start gap-1.5 text-sm text-status-alarm">
              <CircleAlert className="mt-0.5 size-4 shrink-0" /> {error}
            </p>
          )}
        </CardContent>
        <CardFooter className="flex-col items-start gap-1 text-sm text-muted-foreground">
          <span>Ожидаемые колонки журнала СМВУ:</span>
          <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs">{JOURNAL_COLUMNS}</code>
          <span>
            Порядок и названия могут отличаться, время может быть одной колонкой «дата и время». Сопоставление предложим сами — его
            можно поправить на следующем шаге.
          </span>
        </CardFooter>
      </Card>
    </div>
  )
}

function FieldRow({ f, columns, onChange }: { f: ScenarioUpload['fields'][number]; columns: ScenarioUpload['columns']; onChange: (col: number | null) => void }) {
  const items = [{ value: 'none', label: '— не назначено' }, ...columns.map((c) => ({ value: String(c.index), label: `${c.index + 1}. ${c.name}` }))]
  const match = f.column === null ? null : f.match
  return (
    <div className="grid grid-cols-[9rem_1fr] items-center gap-3 py-1.5">
      <div>
        <div className="font-medium">
          {f.label}
          {f.required && <span className="text-status-alarm"> *</span>}
        </div>
        <div className="text-xs">
          {match ? (
            <span className={cn(MATCH_VIEW[match].status === 'risk' ? 'text-status-risk' : 'text-muted-foreground')}>{MATCH_VIEW[match].label}</span>
          ) : (
            <span className={cn(f.required ? 'text-status-alarm' : 'text-muted-foreground')}>{f.required ? 'не найдено' : 'нет в файле'}</span>
          )}
        </div>
      </div>
      <Select items={items} value={f.column === null ? 'none' : String(f.column)} onValueChange={(v) => onChange(!v || v === 'none' ? null : Number(v))}>
        <SelectTrigger size="sm" className="w-full" aria-label={`Колонка для поля «${f.label}»`}>
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {items.map((i) => {
            const col = i.value === 'none' ? null : columns[Number(i.value)]
            return (
              <SelectItem key={i.value} value={i.value}>
                <span className="flex min-w-0 flex-col">
                  <span>{i.label}</span>
                  {col && <span className="truncate text-xs text-muted-foreground">{col.samples.slice(0, 3).join(' · ')}</span>}
                </span>
              </SelectItem>
            )
          })}
        </SelectContent>
      </Select>
    </div>
  )
}

/** Step 2: which file column is which journal field, what the scenario will contain, and «Создать сценарий». */
function MapColumns({ id }: { id: string }) {
  const navigate = useNavigate()
  const runTo = useRunTo()
  const qc = useQueryClient()
  const initial = useScenarioUpload(id)
  // The latest dry run replaces the analysis loaded with the page.
  const [checked, setA] = useState<ScenarioUpload | null>(null)
  const [typed, setTitle] = useState<string | null>(null)
  const [checking, setChecking] = useState(false)
  const [creating, setCreating] = useState(false)
  const pending = useRef<ReturnType<typeof setTimeout> | null>(null)
  const seq = useRef(0)

  const a = checked ?? initial.data ?? null
  if (initial.isError) return <QueryError error={initial.error} onRetry={() => void initial.refetch()} what="Загрузка файла" />
  if (!a)
    return (
      <div className="mx-auto w-full max-w-7xl space-y-4">
        <Skeleton className="h-10 w-80" />
        <Skeleton className="h-96" />
      </div>
    )

  const mapping = Object.fromEntries(a.fields.map((f) => [f.field, f.column])) as Partial<Record<UploadField, number | null>>

  // The analysis follows the mapping: a dry run 400 ms after the last change, the latest answer wins.
  const recheck = (patch: Partial<ScenarioUploadPut>) => {
    const next: ScenarioUpload = { ...a, fields: a.fields.map((f) => (patch.mapping && f.field in patch.mapping ? { ...f, column: patch.mapping[f.field] ?? null } : f)) }
    setA(next)
    if (pending.current) clearTimeout(pending.current)
    pending.current = setTimeout(async () => {
      const my = ++seq.current
      setChecking(true)
      try {
        // Another sheet has other columns: its mapping is proposed anew, the current one is not sent.
        const body: ScenarioUploadPut = patch.sheet
          ? { sheet: patch.sheet, dry_run: true }
          : { mapping: Object.fromEntries(next.fields.map((f) => [f.field, f.column])), sheet: a.format.sheet, dry_run: true }
        const res = await apiPut<ScenarioUpload>(`/scenario-uploads/${id}`, body)
        if (my === seq.current) setA(res)
      } catch (e) {
        toast.error('Не удалось пересчитать', { description: (e as ApiError).message })
      } finally {
        if (my === seq.current) setChecking(false)
      }
    }, 400)
  }

  const title = typed ?? a.name_suggestion
  const create = async () => {
    setCreating(true)
    try {
      const res = await apiPut<ScenarioUploadCreated>(`/scenario-uploads/${id}`, { mapping, sheet: a.format.sheet, title: title.trim() || a.name_suggestion, dry_run: false }, 300_000)
      // The new scenario must be in the list before its page reads it.
      await qc.invalidateQueries({ queryKey: ['emulator-scenarios'] })
      toast.success('Сценарий создан', {
        description: `${res.title}: ${fmtInt(res.events)} ${plural(res.events, ['сигнал', 'сигнала', 'сигналов'])}, ${fmtInt(res.channels)} ${plural(res.channels, ['канал', 'канала', 'каналов'])}`,
      })
      navigate(runTo(`/emulator/scenarios/${res.scenario_id}`))
    } catch (e) {
      toast.error('Сценарий не создан', { description: (e as ApiError).message })
    } finally {
      setCreating(false)
    }
  }

  const missing = a.fields.filter((f) => a.required_missing.includes(f.field)).map((f) => f.label)
  const mappedLabel = new Map(a.fields.filter((f) => f.column !== null).map((f) => [f.column!, f.label]))
  const span = a.period ? (Date.parse(a.period.end) - Date.parse(a.period.start)) / 1000 : null
  const sheetItems = (a.sheets ?? []).map((s) => ({ value: s, label: s }))
  // The period and the run time are the «Период» and «Длительность» rows above — not a remark about the file.
  const notes = a.warnings.filter((w) => w.code !== 'period')

  return (
    <div className="mx-auto w-full max-w-7xl space-y-4">
      <BackLink to="/emulator/upload">Другой файл</BackLink>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-xl font-semibold">
            <FileSpreadsheet className="size-5 text-muted-foreground" /> {a.file_name}
          </h1>
          <p className="text-sm text-muted-foreground">
            {a.format.kind.toUpperCase()} · {fmtBytes(a.size_bytes)} · {fmtInt(a.rows)} {plural(a.rows, ['строка', 'строки', 'строк'])}
            {a.format.delimiter ? ` · разделитель «${a.format.delimiter === '\t' ? 'табуляция' : a.format.delimiter}»` : ''}
            {a.format.encoding ? ` · ${a.format.encoding}` : ''}
            {a.format.header ? '' : ' · без строки заголовка'}
          </p>
        </div>
        {sheetItems.length > 1 && (
          <div className="flex items-center gap-2">
            <Label className="text-sm font-normal">Лист</Label>
            <Select items={sheetItems} value={a.format.sheet ?? sheetItems[0].value} onValueChange={(v) => v && recheck({ sheet: v })}>
              <SelectTrigger size="sm" className="w-48">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {sheetItems.map((s) => (
                  <SelectItem key={s.value} value={s.value}>
                    {s.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        )}
      </div>

      <div className="grid gap-4 lg:grid-cols-[3fr_2fr]">
        <Card>
          <CardHeader>
            <CardTitle>Какая колонка — какое поле</CardTitle>
            <CardDescription>
              Звёздочка — обязательно. Время — одной колонкой «Дата и время» или двумя: «Дата» и «Время».
            </CardDescription>
          </CardHeader>
          <CardContent className="divide-y">
            {a.fields.map((f) => (
              <FieldRow key={f.field} f={f} columns={a.columns} onChange={(col) => recheck({ mapping: { [f.field]: col } })} />
            ))}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              Что получится {checking && <Spinner className="size-3.5 text-muted-foreground" />}
            </CardTitle>
            <CardDescription>Пересчитывается при каждой смене колонки</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3 text-sm">
            <div className="space-y-1">
              <div className="flex justify-between gap-3">
                <span className="text-muted-foreground">Период</span>
                <span className="text-right">{a.period ? `${fmtDateTime(a.period.start)} — ${fmtDateTime(a.period.end)}` : '—'}</span>
              </div>
              {span !== null && (
                <div className="flex justify-between gap-3">
                  <span className="text-muted-foreground">Длительность</span>
                  <span>
                    {fmtDuration(span)} · при ×60 — {fmtDuration(span / 60)}
                  </span>
                </div>
              )}
              <div className="flex justify-between gap-3">
                <span className="text-muted-foreground">Сигналов к передаче</span>
                <span className="font-medium tabular-nums">{fmtInt(a.stats.events)}</span>
              </div>
              <div className="flex justify-between gap-3">
                <span className="text-muted-foreground">Каналов</span>
                <span className="tabular-nums">
                  {fmtInt(a.stats.channels)}
                  {a.stats.unknown_channels > 0 && <span className="text-status-risk"> · вне справочника {fmtInt(a.stats.unknown_channels)}</span>}
                </span>
              </div>
              <div className="flex justify-between gap-3">
                <span className="text-muted-foreground">Объекты</span>
                <span className="text-right">
                  {a.stats.objects.length
                    ? a.stats.objects
                        .slice(0, 5)
                        .map((o) => `${o.name} · ${fmtInt(o.channels)}`)
                        .join(', ') + (a.stats.objects.length > 5 ? ` и ещё ${a.stats.objects.length - 5}` : '')
                    : '—'}
                </span>
              </div>
            </div>
            {notes.length > 0 ? (
              <ul className="space-y-1.5 border-t pt-3">
                {notes.map((w, i) => (
                  <li key={`${w.code}-${w.field ?? i}`} className="flex items-start gap-1.5">
                    {w.code === 'field_missing' && a.required_missing.includes(w.field as UploadField) ? (
                      <CircleAlert className="mt-0.5 size-4 shrink-0 text-status-alarm" />
                    ) : (
                      <Info className="mt-0.5 size-4 shrink-0 text-status-risk" />
                    )}
                    <span>
                      {w.message}
                      {!!w.examples?.length && <span className="block text-xs text-muted-foreground">например: {w.examples.join(', ')}</span>}
                    </span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="flex items-center gap-1.5 border-t pt-3 text-status-ok">
                <CircleCheck className="size-4" /> Замечаний нет
              </p>
            )}
          </CardContent>
        </Card>
      </div>

      <Card className="py-0">
        <CardHeader className="pt-4">
          <CardTitle>Первые строки файла</CardTitle>
          <CardDescription>Как они лежат в файле; назначенные колонки подписаны полем журнала</CardDescription>
        </CardHeader>
        <CardContent className="overflow-x-auto px-0">
          <Table>
            <TableHeader>
              <TableRow>
                {a.columns.map((c) => (
                  <TableHead key={c.index} className={cn('align-bottom first:pl-4', !mappedLabel.has(c.index) && 'text-muted-foreground')}>
                    {mappedLabel.has(c.index) && (
                      <div className="mb-1">
                        <StatusBadge status="ok">{mappedLabel.get(c.index)}</StatusBadge>
                      </div>
                    )}
                    {c.name}
                  </TableHead>
                ))}
              </TableRow>
            </TableHeader>
            <TableBody>
              {a.preview.map((row, r) => (
                <TableRow key={r}>
                  {a.columns.map((c) => (
                    <TableCell key={c.index} className={cn('font-mono text-xs first:pl-4', !mappedLabel.has(c.index) && 'text-muted-foreground')}>
                      {row[c.index] ?? ''}
                    </TableCell>
                  ))}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <Card>
        <CardContent className="flex flex-wrap items-end gap-3">
          <div className="min-w-72 flex-1 space-y-1.5">
            <Label htmlFor="scenario-title">Название сценария</Label>
            <Input id="scenario-title" value={title} onChange={(e) => setTitle(e.target.value)} placeholder={a.name_suggestion} />
          </div>
          <div className="flex flex-col items-end gap-1">
            <Button onClick={() => void create()} disabled={!a.ready || creating || checking}>
              {creating ? <Spinner /> : <FileSpreadsheet />} {creating ? 'Создаём сценарий…' : 'Создать сценарий'}
            </Button>
            {!a.ready && (
              <span className="text-xs text-status-alarm">
                {missing.length ? `Назначьте: ${missing.join(', ')}` : 'В файле нет строк, которые можно передать'}
              </span>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  )
}

// A scenario from one's own journal (13.1): the file, then the column mapping; the scenario opens at /emulator/scenarios.
export function UploadPage() {
  const id = useParams().id ?? null
  return id ? <MapColumns id={id} /> : <ChooseFile />
}
