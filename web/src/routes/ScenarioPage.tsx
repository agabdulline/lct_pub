import {
  Activity,
  ArrowLeft,
  BellRing,
  CircleAlert,
  DoorOpen,
  Flag,
  Hand,
  MoonStar,
  Play,
  PlugZap,
  RotateCcw,
  Search,
  Users,
  Wind,
  Wrench,
  X,
  type LucideIcon,
} from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { useParams } from 'react-router'

import { MetricCard } from '@/components/domain/MetricCard'
import { QueryError } from '@/components/domain/QueryError'
import { RunLink } from '@/components/domain/RunLink'
import { SensorTypeIcon } from '@/components/domain/SensorTypeIcon'
import { StatusBadge } from '@/components/domain/StatusBadge'
import { DensityChart } from '@/components/emulator/DensityChart'
import { DatasetFacts } from '@/components/emulator/DatasetFacts'
import { ScenarioSourceBadge } from '@/components/emulator/ScenarioSourceBadge'
import { StartDialog } from '@/components/emulator/StartDialog'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from '@/components/ui/empty'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { Spinner } from '@/components/ui/spinner'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import type { ApiError } from '@/lib/api/client'
import { useScenarioEvents, useScenarioSummary, useScenarios } from '@/lib/api/queries'
import type { Scenario, ScenarioMoment, ScenarioSummary } from '@/lib/api/types'
import { fmtDateTime, fmtDateTimeShort, fmtDuration, fmtInt, fmtTime, plural } from '@/lib/format'
import { useSearchState } from '@/lib/search-state'
import { cn } from '@/lib/utils'

const MOMENT_VIEW: Record<ScenarioMoment['kind'], { icon: LucideIcon; label: string }> = {
  smoke_single: { icon: BellRing, label: 'Одиночная дымовая' },
  smoke_group: { icon: Users, label: 'Группа дымовых' },
  lever: { icon: Hand, label: 'Рычаг' },
  state_wave: { icon: PlugZap, label: 'Волна состояния' },
  state_return: { icon: RotateCcw, label: 'Возврат' },
  flapping: { icon: Activity, label: 'Дребезг' },
  gas_peak: { icon: Wind, label: 'Пик газа' },
  silence: { icon: MoonStar, label: 'Тишина' },
  first_event: { icon: Play, label: 'Первое событие' },
  last_event: { icon: Flag, label: 'Последнее событие' },
  code_series: { icon: CircleAlert, label: 'Серия кодов' },
  access_peak: { icon: DoorOpen, label: 'Пик доступа' },
  failure_record: { icon: Wrench, label: 'Отказ' },
}

const ALARM_FILTER = [
  { value: 'all', label: 'Все' },
  { value: 'true', label: 'Тревожные' },
  { value: 'false', label: 'Нетревожные' },
]

type Option = { value: string; label: string }

/** Signals per minute from a per-second rate, like the peak next to it: «118», or «0,3» for a sparse journal. */
function perMinute(perSecond: number) {
  const v = perSecond * 60
  return v >= 10 ? fmtInt(Math.round(v)) : v.toFixed(1).replace('.', ',')
}

/** A bucket of the flow chart in whole units — «1 мин», «2 мин», «1 ч», not «1,0 мин» of fmtDuration. */
function bucketText(seconds: number) {
  if (seconds % 86_400 === 0) return `${seconds / 86_400} сут`
  if (seconds % 3600 === 0) return `${seconds / 3600} ч`
  if (seconds % 60 === 0) return `${seconds / 60} мин`
  return fmtDuration(seconds)
}

function FilterSelect({ options, value, onChange, label, className }: { options: Option[]; value: string; onChange: (v: string) => void; label: string; className?: string }) {
  return (
    <Select items={options} value={value} onValueChange={(v) => onChange(v ?? 'all')}>
      <SelectTrigger size="sm" className={cn('w-48', className)} aria-label={label}>
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {options.map((o) => (
          <SelectItem key={o.value} value={o.value}>
            {o.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

function Header({ scenario, summary, onStart }: { scenario: Scenario | undefined; summary: ScenarioSummary | undefined; onStart: () => void }) {
  const upload = scenario?.source === 'upload'
  return (
    <div className="space-y-2">
      <RunLink to="/emulator" className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground">
        <ArrowLeft className="size-3.5" /> Эмулятор СМВУ
      </RunLink>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="text-xl font-semibold">{scenario?.title ?? 'Сценарий'}</h1>
            <ScenarioSourceBadge source={scenario?.source} dataset={scenario?.dataset} />
          </div>
          {scenario?.dataset && <DatasetFacts dataset={scenario.dataset.key} className="w-fit" />}
          <p className="text-sm text-muted-foreground">
            {upload && scenario?.source_file
              ? `Файл ${scenario.source_file}${scenario.uploaded_at ? `, загружен ${fmtDateTimeShort(scenario.uploaded_at)}` : ''}. `
              : ''}
            Что будет передано в систему — до запуска: состав данных, поток, ключевые моменты и все сигналы.
          </p>
        </div>
        {scenario && (
          <div className="flex flex-col items-end gap-1">
            <Button onClick={onStart} disabled={!scenario.ready}>
              <Play /> Запустить · ×{scenario.default_speed}
            </Button>
            {summary && (
              <span className="text-xs text-muted-foreground">
                при ×{scenario.default_speed} — {fmtDuration((Date.parse(summary.period_end) - Date.parse(summary.period_start)) / 1000 / scenario.default_speed)}
              </span>
            )}
          </div>
        )}
      </div>
      {scenario && !scenario.ready && (
        <p className="flex items-center gap-1.5 text-sm text-status-risk">
          <CircleAlert className="size-4" /> Срез сценария не подготовлен — запустить нельзя.
        </p>
      )}
    </div>
  )
}

/**
 * A moment's identity in the list and in the URL: time and kind are not enough — a data set has several moments of one
 * kind in one second (Azure PdM: error codes of two units at 06:00).
 */
const momentKey = (m: ScenarioMoment) => `${m.ts}|${m.kind}|${m.channel_id ?? m.object_id ?? m.channel_ids.join('.')}`

function Moments({ moments, active, onPick }: { moments: ScenarioMoment[]; active: string | null; onPick: (m: ScenarioMoment) => void }) {
  if (!moments.length) return <p className="text-sm text-muted-foreground">В данных не нашлось заметных моментов.</p>
  return (
    <ol className="max-h-80 space-y-1 overflow-y-auto pr-1">
      {moments.map((m) => {
        const view = MOMENT_VIEW[m.kind] ?? { icon: Activity, label: m.kind }
        const Icon = view.icon
        const key = momentKey(m)
        return (
          <li key={key}>
            <button
              type="button"
              onClick={() => onPick(m)}
              aria-pressed={active === key}
              className={cn(
                'grid w-full grid-cols-[1rem_1fr] gap-2 rounded-lg px-2 py-1.5 text-left text-sm transition-colors hover:bg-muted/50',
                active === key && 'bg-muted',
              )}
              title="Показать сигналы этого момента"
            >
              <Icon className="mt-0.5 size-4 text-muted-foreground" />
              <span className="min-w-0">
                <span className="block">{m.title}</span>
                {(m.detail || m.events) && (
                  <span className="block text-xs text-muted-foreground">
                    {[view.label, m.detail, m.events ? `${fmtInt(m.events)} ${plural(m.events, ['сигнал', 'сигнала', 'сигналов'])}` : null].filter(Boolean).join(' · ')}
                  </span>
                )}
              </span>
            </button>
          </li>
        )
      })}
    </ol>
  )
}

function CountTable({ head, rows }: { head: string; rows: { key: string; label: React.ReactNode; channels?: number; events: number; alarm?: number; onClick?: () => void; nested?: boolean }[] }) {
  const withChannels = rows.some((r) => r.channels !== undefined)
  const withAlarm = rows.some((r) => r.alarm !== undefined)
  return (
    <div className="max-h-72 overflow-y-auto">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>{head}</TableHead>
            {withChannels && <TableHead className="text-right">Каналов</TableHead>}
            <TableHead className="text-right">Сигналов</TableHead>
            {withAlarm && <TableHead className="text-right">Тревожных</TableHead>}
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((r) => (
            <TableRow key={r.key} className={cn(r.onClick && 'cursor-pointer')} onClick={r.onClick}>
              <TableCell className={cn('max-w-56 truncate', r.nested && 'pl-6 text-muted-foreground')}>{r.label}</TableCell>
              {withChannels && <TableCell className="text-right tabular-nums">{r.channels === undefined ? '' : fmtInt(r.channels)}</TableCell>}
              <TableCell className="text-right tabular-nums">{fmtInt(r.events)}</TableCell>
              {withAlarm && <TableCell className="text-right tabular-nums">{r.alarm === undefined ? '' : fmtInt(r.alarm)}</TableCell>}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}

/** A moment with more channels than this filters the table by its object: the list would not fit a link. */
const MOMENT_CHANNELS_MAX = 30

function Signals({ id, summary, multiDay }: { id: string; summary: ScenarioSummary; multiDay: boolean }) {
  const s = useSearchState()
  const object = s.get('object') ?? 'all'
  const type = s.get('type') ?? 'all'
  const value = s.get('value') ?? 'all'
  const alarm = s.get('alarm') ?? 'all'
  const from = s.get('from')
  const to = s.get('to')
  // One channel or the channels of a moment, comma-separated — the API takes lists as they are.
  const channels = (s.get('channel') ?? '').split(',').filter((c) => /^\d+$/.test(c))
  const channel = channels.length ? channels.join(',') : null
  const [text, setText] = useState(s.get('q') ?? '')
  // The channel search reaches the server after a pause in typing.
  useEffect(() => {
    const t = setTimeout(() => s.set({ q: text.trim() || null }), 350)
    return () => clearTimeout(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [text])

  const filters = {
    object_id: object === 'all' ? null : object,
    sensor_type: type === 'all' ? null : type,
    value: value === 'all' ? null : value,
    alarm: alarm === 'all' ? null : alarm,
    q: s.get('q'),
    from,
    to,
    channel_id: channel,
  }
  const events = useScenarioEvents(id, filters)
  const items = useMemo(() => events.data?.pages.flatMap((p) => p.items) ?? [], [events.data])
  const total = events.data?.pages[0]?.total ?? null

  // Endless scroll: the next page is asked for when the end of the table comes into view.
  const sentinel = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const el = sentinel.current
    if (!el) return
    const io = new IntersectionObserver((entries) => {
      if (entries[0]?.isIntersecting && events.hasNextPage && !events.isFetchingNextPage) void events.fetchNextPage()
    })
    io.observe(el)
    return () => io.disconnect()
  }, [events])

  const objectItems: Option[] = [
    { value: 'all', label: 'Все объекты' },
    ...summary.objects.flatMap((o) => [
      { value: String(o.id), label: o.name },
      ...o.sub_objects.map((sub) => ({ value: String(sub.id), label: `— ${sub.name}` })),
    ]),
  ]
  const typeItems: Option[] = [{ value: 'all', label: 'Все типы' }, ...summary.by_type.map((t) => ({ value: t.sensor_type, label: t.sensor_type }))]
  const valueItems: Option[] = [
    { value: 'all', label: 'Все значения' },
    ...summary.by_value.filter((v) => v.value !== 'прочие').map((v) => ({ value: v.value, label: v.value })),
  ]
  const when = (ts: string) => (multiDay ? fmtDateTimeShort(ts) : fmtTime(ts))
  const windowSet = from || to || channel !== null

  return (
    <Card className="py-0">
      <CardHeader className="pt-4">
        <CardTitle>Все сигналы</CardTitle>
        <CardDescription>
          В порядке передачи. {total !== null ? `Найдено ${fmtInt(total)} ${plural(total, ['сигнал', 'сигнала', 'сигналов'])}.` : ''}
        </CardDescription>
        <div className="flex flex-wrap items-center gap-2 pt-2">
          <FilterSelect label="Объект" options={objectItems} value={object} onChange={(v) => s.set({ object: v === 'all' ? null : v })} className="w-56" />
          <FilterSelect label="Тип канала" options={typeItems} value={type} onChange={(v) => s.set({ type: v === 'all' ? null : v })} />
          <FilterSelect label="Значение" options={valueItems} value={value} onChange={(v) => s.set({ value: v === 'all' ? null : v })} />
          <ToggleGroup value={[alarm]} onValueChange={(v) => v[0] && s.set({ alarm: v[0] === 'all' ? null : v[0] })} size="sm" variant="outline">
            {ALARM_FILTER.map((a) => (
              <ToggleGroupItem key={a.value} value={a.value}>
                {a.label}
              </ToggleGroupItem>
            ))}
          </ToggleGroup>
          <div className="relative">
            <Search className="pointer-events-none absolute top-1/2 left-2 size-3.5 -translate-y-1/2 text-muted-foreground" />
            <Input value={text} onChange={(e) => setText(e.target.value)} placeholder="Канал" className="h-8 w-44 pl-7" aria-label="Поиск канала" />
          </div>
          {windowSet && (
            <Button variant="outline" size="sm" onClick={() => s.set({ from: null, to: null, channel: null, moment: null })}>
              {from ? `${when(from)}${to && to !== from ? ` — ${when(to)}` : ''}` : 'Каналы момента'}
              {channels.length > 1 && ` · ${fmtInt(channels.length)} ${plural(channels.length, ['канал', 'канала', 'каналов'])}`} <X />
            </Button>
          )}
        </div>
      </CardHeader>
      <CardContent className="px-0">
        {events.isError && <QueryError error={events.error} onRetry={() => void events.refetch()} what="Сигналы" compact className="mx-4 mb-4" />}
        {events.isPending && <Skeleton className="mx-4 mb-4 h-64" />}
        {events.data && items.length === 0 && (
          <Empty>
            <EmptyHeader>
              <EmptyTitle>Сигналов нет</EmptyTitle>
              <EmptyDescription>С такими фильтрами в сценарии сигналов нет.</EmptyDescription>
            </EmptyHeader>
          </Empty>
        )}
        {items.length > 0 && (
          <Table>
            <TableHeader className="sticky top-0 z-10 bg-card">
              <TableRow>
                <TableHead className="w-16 pl-4 text-right">№</TableHead>
                <TableHead className="w-24">Время</TableHead>
                <TableHead>Объект</TableHead>
                <TableHead>Канал</TableHead>
                <TableHead>Значение</TableHead>
                <TableHead className="pr-4">Тревожное</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {items.map((e) => (
                <TableRow key={e.seq}>
                  <TableCell className="pl-4 text-right text-xs text-muted-foreground tabular-nums">{fmtInt(e.seq)}</TableCell>
                  <TableCell className="font-mono text-xs tabular-nums">{when(e.ts)}</TableCell>
                  <TableCell className="max-w-56">
                    <div className="truncate">{e.object_name}</div>
                    <div className="truncate text-xs text-muted-foreground">{e.sub_object_name}</div>
                  </TableCell>
                  <TableCell className="max-w-64">
                    <span className="flex min-w-0 items-center gap-1.5">
                      <SensorTypeIcon type={e.sensor_type} className="size-3.5 shrink-0 text-muted-foreground" />
                      <span className="truncate">{e.channel_name}</span>
                    </span>
                    <div className="truncate text-xs text-muted-foreground">{e.sensor_type}</div>
                  </TableCell>
                  <TableCell className={cn(/^-?\d/.test(e.value) && 'font-mono')}>{e.value}</TableCell>
                  <TableCell className="pr-4">{e.is_alarm && <StatusBadge status="alarm">тревожное</StatusBadge>}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
        <div ref={sentinel} className="flex h-10 items-center justify-center text-xs text-muted-foreground">
          {events.isFetchingNextPage ? <Spinner /> : events.hasNextPage ? '' : items.length > 0 ? 'Это все сигналы' : ''}
        </div>
      </CardContent>
    </Card>
  )
}

// A scenario before its start (13.2): what is in the data and every signal that will be transmitted. Facts of the
// journal only — how the system should react is not described here (docs/testing/scenarios.md).
export function ScenarioPage() {
  const id = useParams().id ?? ''
  const s = useSearchState()
  const scenarios = useScenarios()
  const scenario = scenarios.data?.items.find((x) => x.id === id)
  const summary = useScenarioSummary(id)
  // A new upload may not be in the loaded list yet: ask again until it is there.
  const { refetch: refetchScenarios } = scenarios
  const listed = !!scenario
  useEffect(() => {
    if (listed || !scenarios.data) return
    const t = setInterval(() => void refetchScenarios(), 2000)
    return () => clearInterval(t)
  }, [listed, scenarios.data, refetchScenarios])
  const [starting, setStarting] = useState(false)
  const signalsRef = useRef<HTMLDivElement>(null)
  const sm = summary.data
  // The period is already in the header and the «Период» tile — it is not a remark about the file.
  const notes = (scenario?.warnings ?? []).filter((w) => w.code !== 'period')

  const multiDay = !!sm && Date.parse(sm.period_end) - Date.parse(sm.period_start) > 36 * 3_600_000
  const pickMoment = (m: ScenarioMoment) => {
    const bucket = (sm?.density.bucket_seconds ?? 300) * 1000
    // `to` is exclusive and `until` is the moment's last signal: one second more keeps it in the table.
    const until = m.until ? new Date(Date.parse(m.until) + 1000).toISOString() : new Date(Date.parse(m.ts) + Math.max(bucket, 5 * 60_000)).toISOString()
    const object = m.sub_object_id ?? m.object_id
    // The moment's own channels; a wide one (a planned check of a whole object) — by its object instead.
    const ids = m.channel_ids.length ? m.channel_ids : m.channel_id !== null ? [m.channel_id] : []
    const byChannels = ids.length > 0 && (ids.length <= MOMENT_CHANNELS_MAX || object === null)
    s.set({
      moment: momentKey(m),
      from: m.ts,
      to: until,
      object: !byChannels && object !== null ? String(object) : null,
      channel: byChannels ? ids.join(',') : null,
      type: null,
      value: null,
      alarm: null,
    })
    signalsRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }

  if (summary.isError && (summary.error as ApiError).status === 404)
    return (
      <Empty>
        <EmptyHeader>
          <EmptyTitle>Сценарий не найден</EmptyTitle>
          <EmptyDescription>
            Сценария «{id}» нет в эмуляторе. <RunLink to="/emulator" className="underline underline-offset-4">К списку сценариев</RunLink>
          </EmptyDescription>
        </EmptyHeader>
      </Empty>
    )
  if (summary.isError) return <QueryError error={summary.error} onRetry={() => void summary.refetch()} what="Сценарий" />

  return (
    <div className="mx-auto w-full max-w-7xl space-y-4">
      <Header scenario={scenario} summary={sm} onStart={() => setStarting(true)} />

      {notes.length > 0 && (
        <Card size="sm">
          <CardContent className="space-y-1 text-sm">
            {notes.map((w) => (
              <p key={w.code} className="flex items-start gap-1.5">
                <CircleAlert className="mt-0.5 size-4 shrink-0 text-status-risk" /> {w.message}
              </p>
            ))}
          </CardContent>
        </Card>
      )}

      {!sm && summary.failureCount > 0 && (
        <p className="flex items-center gap-2 text-sm text-muted-foreground">
          <Spinner /> Готовим сценарий — эмулятор принимает файл…
        </p>
      )}
      {!sm ? (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
          {Array.from({ length: 5 }, (_, i) => (
            <Skeleton key={i} className="h-28" />
          ))}
        </div>
      ) : (
        <>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
            <MetricCard
              title="Период"
              value={fmtDuration((Date.parse(sm.period_end) - Date.parse(sm.period_start)) / 1000)}
              hint={`${fmtDateTime(sm.period_start)} — ${fmtDateTime(sm.period_end)}`}
            />
            <MetricCard
              title="Сигналов"
              value={fmtInt(sm.events)}
              hint={`из них тревожных ${fmtInt(sm.alarm_events)}`}
            />
            <MetricCard
              title="Каналов"
              value={fmtInt(sm.channels)}
              hint={sm.unknown_channels ? `ещё ${fmtInt(sm.unknown_channels)} вне справочника — их сигналы не передаются` : `${sm.by_type.length} ${plural(sm.by_type.length, ['тип', 'типа', 'типов'])} каналов`}
            />
            <MetricCard
              title="Объектов"
              value={fmtInt(sm.objects.length)}
              hint={`подобъектов ${fmtInt(sm.objects.reduce((n, o) => n + o.sub_objects.length, 0))}`}
            />
            <MetricCard
              title="Пик потока"
              value={`${fmtInt(sm.density.peak_minute_events)}/мин`}
              hint={`в среднем ${perMinute(sm.density.avg_per_second)}/мин`}
            />
          </div>

          <div className="grid gap-4 lg:grid-cols-[3fr_2fr]">
            <Card className="relative">
              <CardHeader>
                <CardTitle>Поток сигналов</CardTitle>
                <CardDescription>
                  Сигналов за отрезок {bucketText(sm.density.bucket_seconds)}; пунктир — ключевые моменты
                </CardDescription>
              </CardHeader>
              <CardContent>
                <DensityChart density={sm.density} moments={sm.moments} multiDay={multiDay} />
              </CardContent>
            </Card>
            <Card>
              <CardHeader>
                <CardTitle>Ключевые моменты</CardTitle>
                <CardDescription>Найдены в данных. Клик — сигналы этого момента в таблице ниже</CardDescription>
              </CardHeader>
              <CardContent>
                <Moments moments={sm.moments} active={s.get('moment')} onPick={pickMoment} />
              </CardContent>
            </Card>
          </div>

          <div className="grid gap-4 lg:grid-cols-3">
            <Card size="sm">
              <CardHeader>
                <CardTitle>Объекты</CardTitle>
              </CardHeader>
              <CardContent className="px-0">
                <CountTable
                  head="Объект и подобъекты"
                  rows={sm.objects.flatMap((o) => [
                    { key: `o${o.id}`, label: o.name, channels: o.channels, events: o.events, onClick: () => s.set({ object: String(o.id) }) },
                    ...o.sub_objects.map((sub) => ({
                      key: `s${sub.id}`,
                      label: sub.name,
                      channels: sub.channels,
                      events: sub.events,
                      nested: true,
                      onClick: () => s.set({ object: String(sub.id) }),
                    })),
                  ])}
                />
              </CardContent>
            </Card>
            <Card size="sm">
              <CardHeader>
                <CardTitle>Типы каналов</CardTitle>
                <CardDescription>
                  {sm.by_subsystem.map((x) => `${x.subsystem} — ${fmtInt(x.channels)}`).join(' · ')}
                </CardDescription>
              </CardHeader>
              <CardContent className="px-0">
                <CountTable
                  head="Тип"
                  rows={sm.by_type.map((t) => ({
                    key: t.sensor_type,
                    label: (
                      <span className="inline-flex items-center gap-1.5">
                        <SensorTypeIcon type={t.sensor_type} className="size-3.5 text-muted-foreground" /> {t.sensor_type}
                      </span>
                    ),
                    channels: t.channels,
                    events: t.events,
                    onClick: () => s.set({ type: t.sensor_type }),
                  }))}
                />
              </CardContent>
            </Card>
            <Card size="sm">
              <CardHeader>
                <CardTitle>Значения</CardTitle>
                <CardDescription>
                  {scenario?.dataset ? 'Числовые показания датчиков набора' : 'Числовые показания газа и температуры'} — одной строкой
                </CardDescription>
              </CardHeader>
              <CardContent className="px-0">
                <CountTable
                  head="Значение"
                  rows={sm.by_value.map((v) => ({
                    key: v.value,
                    label: v.numeric || v.value === 'прочие' ? <span className="text-muted-foreground">{v.value}</span> : v.value,
                    events: v.events,
                    alarm: v.alarm_events,
                    onClick: v.value === 'прочие' ? undefined : () => s.set({ value: v.value }),
                  }))}
                />
              </CardContent>
            </Card>
          </div>

          <div ref={signalsRef} className="scroll-mt-16">
            <Signals id={id} summary={sm} multiDay={multiDay} />
          </div>
        </>
      )}

      <StartDialog scenario={starting && scenario ? scenario : null} onClose={() => setStarting(false)} />
    </div>
  )
}
