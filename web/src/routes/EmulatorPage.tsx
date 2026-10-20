import { CalendarRange, CircleAlert, FileSearch, Gauge, Pause, Play, Radio, Square, TriangleAlert, Upload, Wind } from 'lucide-react'
import { useState } from 'react'
import { useNavigate } from 'react-router'
import { toast } from 'sonner'

import { DirectionBadge } from '@/components/domain/DirectionBadge'
import { DatasetBadge } from '@/components/domain/DatasetBadge'
import { QueryError } from '@/components/domain/QueryError'
import { RunLink } from '@/components/domain/RunLink'
import { StatusBadge } from '@/components/domain/StatusBadge'
import { DatasetFacts } from '@/components/emulator/DatasetFacts'
import { ScenarioSourceBadge } from '@/components/emulator/ScenarioSourceBadge'
import { StartDialog } from '@/components/emulator/StartDialog'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { Slider } from '@/components/ui/slider'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { useEmulatorCommand, useEmulatorState, useModels, useScenarios } from '@/lib/api/queries'
import type { EmulatorStateName, LiveMetrics, Run, RunStatus, Scenario, Status } from '@/lib/api/types'
import { DIRECTION_ORDER, DIRECTION_TEXT, DIRECTION_TITLE, directionOf, type Direction } from '@/lib/directions'
import { eventsWord, lossWindowText } from '@/lib/event-loss'
import { fmtDateTime, fmtDateTimeShort, fmtInt, fmtPercent, fmtTime, plural } from '@/lib/format'
import { historyNote, RUN_STATUS_LABEL } from '@/lib/labels'
import { useRun } from '@/lib/run'
import { cn } from '@/lib/utils'

const SPEEDS = [1, 10, 30, 60]

const STATE_VIEW: Record<EmulatorStateName, { status: Status; label: string }> = {
  idle: { status: 'unknown', label: 'ожидает запуска' },
  running: { status: 'ok', label: 'транслирует' },
  paused: { status: 'risk', label: 'пауза' },
  finished: { status: 'unknown', label: 'сценарий доигран' },
  stopped: { status: 'unknown', label: 'остановлен' },
}

const RUN_STATUS_TONE: Record<RunStatus, Status> = { running: 'ok', paused: 'risk', finished: 'unknown', stopped: 'unknown' }

/**
 * Live P/R of the event model for a run. Metrics count only forecasts whose horizon (24 h) has passed, so a short
 * run has none: then the lower bound of Precision from alerts already confirmed by a fault (as in the journal),
 * or why there is nothing to count — never a bare dash.
 */
function RunPr({ run }: { run: Run }) {
  const models = useModels().data?.items
  // A data set run: its own models (live.external), never the hackathon event model.
  if (run.dataset) {
    const external = Object.entries(run.live?.external ?? {})
    if (!external.length) return <span className="text-muted-foreground">моделей набора нет</span>
    return (
      <div className="space-y-1.5">
        {external.map(([name, live]) => (
          <LivePr key={name} live={live} label={models?.find((m) => m.model === name)?.title ?? name} />
        ))}
      </div>
    )
  }
  // The service's decisions only (owner's decision: no model versions in the interface).
  return <LivePr live={run.live?.smoke_alarm} />
}

type LiveLine = { main: string; note: string; title?: string; extra?: string | null }

/** What the live P/R of one model says, or why there is nothing to count yet. */
function liveLine(live: LiveMetrics, digits = 0): LiveLine {
  if (live.decisions)
    return {
      main: `${live.alerts ? fmtPercent(live.precision, digits) : '—'} / ${fmtPercent(live.recall, digits)}`,
      note: `закрытых решений ${fmtInt(live.decisions)}${live.alerts ? '' : ', тревог модели нет'}`,
      extra: historyNote(live.history, live.decisions),
    }
  const c = { all: live.alerts, confirmed: live.alerts_confirmed, open: live.open }
  const open = c.open ? `открыто ${fmtInt(c.open)}` : ''
  // Every alert already came true: Precision is final, whatever the rest of the horizon brings.
  if (c.all > 0 && c.confirmed === c.all)
    return { main: '100% / —', note: `${c.all === 1 ? 'единственная тревога сбылась' : `все ${fmtInt(c.all)} ${plural(c.all, ['тревога', 'тревоги', 'тревог'])} сбылись`}${open ? ` · ${open}` : ''}` }
  if (c.confirmed > 0)
    return {
      main: `≥ ${fmtPercent(c.confirmed / c.all, digits)} / —`,
      note: `${fmtInt(c.confirmed)} из ${fmtInt(c.all)} ${plural(c.all, ['тревоги', 'тревог', 'тревог'])} уже сбылись${open ? ` · ${open}` : ''}`,
      title: 'Горизонт ещё не истёк: точные Precision и Recall — после его конца. Тревога, после которой отказ уже случился, окончательно верная — отсюда нижняя граница Precision.',
    }
  if (c.all > 0) return { main: '— / —', note: `тревог ${fmtInt(c.all)}, отказом пока не подтверждены${open ? ` · ${open}` : ''}` }
  return { main: 'тревог модели нет', note: open || 'решений модели нет' }
}

function LivePr({ live, label }: { live: LiveMetrics | undefined; label?: string }) {
  if (!live) return <span className="text-muted-foreground">—</span>
  const l = liveLine(live)
  return (
    <div title={l.title}>
      <div>
        {label && <span className="mr-1.5 text-muted-foreground">{label}</span>}
        {l.main}
      </div>
      <div className="text-muted-foreground">{l.note}</div>
      {l.extra && <div className="text-muted-foreground">{l.extra}</div>}
    </div>
  )
}


function ControlPanel() {
  const { data: em, isPending, isError, error, refetch } = useEmulatorState()
  const { runs } = useRun()
  const command = useEmulatorCommand()
  const [speed, setSpeed] = useState<number | null>(null)
  if (isError) return <QueryError error={error} onRetry={() => void refetch()} what="Эмулятор" compact />
  if (isPending || !em) return <Skeleton className="h-48" />

  const view = STATE_VIEW[em.state]
  const active = em.state === 'running' || em.state === 'paused'
  const run = runs.find((r) => r.id === em.run_id)
  const start = em.sim_start ? new Date(em.sim_start).getTime() : null
  const end = em.sim_end ? new Date(em.sim_end).getTime() : null
  const now = em.sim_time ? new Date(em.sim_time).getTime() : null
  const progress = start !== null && end !== null && now !== null && end > start ? Math.min(1, Math.max(0, (now - start) / (end - start))) : null
  const slowed = em.state === 'running' && em.effective_speed < em.target_speed
  const shown = speed ?? em.target_speed

  const run_ = (cmd: Parameters<typeof command.mutate>[0], done: string) =>
    command.mutate(cmd, { onSuccess: () => toast.success(done), onError: (e) => toast.error('Эмулятор не ответил', { description: e.message }) })

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-2">
          <Radio className="size-4" /> Трансляция
          <StatusBadge status={view.status}>{view.label}</StatusBadge>
          {run && <span className="text-sm font-normal text-muted-foreground">прогон №{run.id} · {run.scenario_title}</span>}
        </CardTitle>
        <CardDescription>
          OPC UA сервер СМВУ транслирует реальные серии журнала; шлюз сервиса принимает их тем же путём, что от реальной системы.
        </CardDescription>
      </CardHeader>
      <CardContent className="grid gap-6 lg:grid-cols-[1fr_22rem]">
        <div className="space-y-3">
          <div>
            <div className="text-xs text-muted-foreground">Симуляционное время</div>
            <div className="text-3xl font-semibold">{fmtDateTime(em.sim_time)}</div>
          </div>
          {progress !== null && (
            <div className="space-y-1">
              <div className="h-1.5 overflow-hidden rounded-full bg-muted">
                <div className="h-full rounded-full bg-primary" style={{ width: `${progress * 100}%` }} />
              </div>
              <div className="flex justify-between text-xs text-muted-foreground tabular-nums">
                <span>{fmtDateTimeShort(em.sim_start)}</span>
                <span>{fmtPercent(progress, 0)}</span>
                <span>{fmtDateTimeShort(em.sim_end)}</span>
              </div>
            </div>
          )}
          <div className="text-sm text-muted-foreground">
            Отправлено событий: {fmtInt(em.events_sent)}
            {em.events_total ? ` из ${fmtInt(em.events_total)}` : ''}
            {!!run?.events_lost && <span className="text-status-risk"> · шлюз не принял {fmtInt(run.events_lost)}</span>}
          </div>
        </div>
        <div className="space-y-3">
          <div className="flex items-center justify-between text-sm">
            <span className="flex items-center gap-1.5">
              <Gauge className="size-4" /> Скорость ×{shown}
            </span>
            {active && (
              <Tooltip>
                <TooltipTrigger render={<span className={slowed ? 'text-status-risk' : 'text-muted-foreground'} />}>
                  фактическая ×{em.effective_speed}
                </TooltipTrigger>
                <TooltipContent>
                  {slowed
                    ? 'Всплеск событий: эмулятор замедляет симуляционное время, чтобы не терять события и сохранить порядок (D18).'
                    : 'Фактическая скорость совпадает с целевой.'}
                </TooltipContent>
              </Tooltip>
            )}
          </div>
          <Slider
            min={1}
            max={em.max_speed}
            value={shown}
            onValueChange={(v) => setSpeed(Array.isArray(v) ? v[0] : v)}
            onValueCommitted={(v) => {
              const next = Array.isArray(v) ? v[0] : v
              if (active) run_({ action: 'speed', speed: next }, `Скорость ×${next}`)
            }}
            aria-label="Скорость"
          />
          <div className="flex gap-1">
            {SPEEDS.filter((s) => s <= em.max_speed).map((s) => (
              <Button
                key={s}
                size="xs"
                variant={shown === s ? 'secondary' : 'ghost'}
                onClick={() => {
                  setSpeed(s)
                  if (active) run_({ action: 'speed', speed: s }, `Скорость ×${s}`)
                }}
              >
                ×{s}
              </Button>
            ))}
          </div>
          <div className="flex gap-2 pt-1">
            {em.state === 'running' && (
              <Button variant="outline" disabled={command.isPending} onClick={() => run_({ action: 'pause' }, 'Пауза')}>
                <Pause /> Пауза
              </Button>
            )}
            {em.state === 'paused' && (
              <Button disabled={command.isPending} onClick={() => run_({ action: 'resume' }, 'Трансляция продолжена')}>
                <Play /> Продолжить
              </Button>
            )}
            {active && (
              <Button variant="outline" disabled={command.isPending} onClick={() => run_({ action: 'stop' }, 'Прогон остановлен, данные сохранены')}>
                <Square /> Стоп
              </Button>
            )}
          </div>
        </div>
      </CardContent>
    </Card>
  )
}

function ScenarioCard({ s, onStart, showDirection = false }: { s: Scenario; onStart: (s: Scenario) => void; showDirection?: boolean }) {
  return (
    <Card className="flex flex-col">
      <CardHeader>
        <CardTitle className="min-w-0 break-words">{s.title}</CardTitle>
        <div className="flex flex-wrap items-center gap-1.5">
          {showDirection && <DirectionBadge scenarioId={s.id} dataset={s.dataset} />}
          <ScenarioSourceBadge source={s.source} dataset={s.dataset} />
        </div>
        <CardDescription className="line-clamp-4">{s.description}</CardDescription>
      </CardHeader>
      <CardContent className="flex-1 space-y-1.5 text-sm">
        <div className="flex items-center gap-1.5 text-muted-foreground">
          <CalendarRange className="size-4" />
          {fmtDateTimeShort(s.period_start)} — {fmtDateTimeShort(s.period_end)}
        </div>
        <div className="text-muted-foreground">{s.objects.length ? s.objects.map((o) => o.name).join(', ') : 'все объекты'}</div>
        {s.dataset && <DatasetFacts dataset={s.dataset.key} />}
        {s.include_gas && (
          <div className="flex items-center gap-1.5 text-muted-foreground">
            <Wind className="size-4" /> с газовыми датчиками
          </div>
        )}
        {s.timeline.length > 0 && (
          <div className="space-y-1 pt-1">
            <div className="text-xs font-medium">Что увидеть</div>
            <ol className="space-y-1">
              {s.timeline.map((t) => (
                <li key={t.at + t.text} className="grid grid-cols-[3rem_1fr] gap-2 text-xs">
                  <span className="text-muted-foreground tabular-nums">{fmtTime(t.at).slice(0, 5)}</span>
                  <span>{t.text}</span>
                </li>
              ))}
            </ol>
          </div>
        )}
      </CardContent>
      <CardFooter className="flex-col items-stretch gap-2">
        {!s.ready && (
          <p className="flex items-start gap-1.5 text-xs text-status-risk">
            <CircleAlert className="mt-0.5 size-3.5 shrink-0" />
            Срез сценария не подготовлен — на сервере нужен make emulator-slices.
          </p>
        )}
        <div className="flex gap-2">
          <Button variant="outline" className="flex-1" nativeButton={false} render={<RunLink to={`/emulator/scenarios/${s.id}`} />}>
            <FileSearch /> Открыть
          </Button>
          <Button className="flex-1" onClick={() => onStart(s)} disabled={!s.ready}>
            <Play /> Запустить · ×{s.default_speed}
          </Button>
        </div>
      </CardFooter>
    </Card>
  )
}

/** Events the gateway did not take (#229): a mark under the run's events, the windows in the tooltip. */
function EventsLost({ run }: { run: Run }) {
  return (
    <Tooltip>
      <TooltipTrigger render={<div className="flex cursor-default items-center justify-end gap-1 text-xs text-status-risk" />}>
        <TriangleAlert className="size-3" />−{fmtInt(run.events_lost)}
      </TooltipTrigger>
      <TooltipContent className="max-w-80">
        Потеряно {fmtInt(run.events_lost)} {eventsWord(run.events_lost)}: эмулятор отправил, шлюз не принял. Данные за эти окна
        неполные: {(run.loss_windows ?? []).map(lossWindowText).join('; ') || 'окна не определены'}.
      </TooltipContent>
    </Tooltip>
  )
}

function RunsTable() {
  const { runs, runId, runsError, refetchRuns } = useRun()
  const navigate = useNavigate()
  const sorted = [...runs].sort((a, b) => b.id - a.id)
  const open = (r: Run) => navigate({ pathname: '/', search: `?run=${r.id}` })
  if (runsError) return <QueryError error={runsError} onRetry={refetchRuns} what="Прогоны" compact className="mx-4 mb-4" />
  if (!sorted.length) return <p className="p-4 text-sm text-muted-foreground">Прогонов ещё не было — запустите сценарий.</p>
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead className="pl-4">№</TableHead>
          <TableHead>Сценарий</TableHead>
          <TableHead>Источник</TableHead>
          <TableHead>Статус</TableHead>
          <TableHead>Симуляция</TableHead>
          <TableHead className="text-right">События</TableHead>
          <TableHead className="text-right">Тревоги</TableHead>
          <TableHead className="text-right">Прогнозы</TableHead>
          <TableHead className="text-right">Заявки</TableHead>
          <TableHead>P / R модели</TableHead>
          <TableHead className="pr-4" />
        </TableRow>
      </TableHeader>
      <TableBody>
        {sorted.map((r) => (
          <TableRow key={r.id} data-state={r.id === runId ? 'selected' : undefined}>
            <TableCell className="pl-4 font-medium">{r.id}</TableCell>
            <TableCell className="max-w-40">
              <div className="truncate" title={r.scenario_title}>
                {r.scenario_title}
              </div>
              <div className="text-xs text-muted-foreground">запущен {fmtDateTimeShort(r.started_at)} · ×{r.speed}</div>
            </TableCell>
            <TableCell>{r.dataset ? <DatasetBadge dataset={r.dataset} /> : <span className="text-xs text-muted-foreground">журнал СМВУ</span>}</TableCell>
            <TableCell>
              <StatusBadge status={RUN_STATUS_TONE[r.status]}>{RUN_STATUS_LABEL[r.status]}</StatusBadge>
            </TableCell>
            <TableCell className="text-xs tabular-nums text-muted-foreground">
              <div>{fmtDateTimeShort(r.sim_start)} —</div>
              <div>{fmtDateTimeShort(r.sim_now ?? r.sim_end)}</div>
            </TableCell>
            <TableCell className="text-right tabular-nums">
              {fmtInt(r.events)}
              {r.events_lost > 0 && <EventsLost run={r} />}
            </TableCell>
            <TableCell className="text-right tabular-nums">{fmtInt(r.alarms)}</TableCell>
            <TableCell className="text-right tabular-nums">{fmtInt(r.predictions)}</TableCell>
            <TableCell className="text-right tabular-nums">{fmtInt(r.work_orders)}</TableCell>
            <TableCell className="min-w-40 text-xs whitespace-normal tabular-nums">
              <RunPr run={r} />
            </TableCell>
            <TableCell className="pr-4 text-right">
              <Button size="sm" variant={r.id === runId ? 'secondary' : 'outline'} onClick={() => open(r)}>
                {r.id === runId ? 'Открыт' : 'Открыть'}
              </Button>
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

// SMVU emulator (docs/product/v1-scope.md, экран 7; docs/architecture/emulator.md): scenarios, start, speed, runs.
export function EmulatorPage() {
  const scenarios = useScenarios()
  const [starting, setStarting] = useState<Scenario | null>(null)
  const items = scenarios.data?.items ?? []
  const [filter, setFilter] = useState<Direction | 'all'>('all')
  const allGroups = DIRECTION_ORDER.map((direction) => ({
    direction,
    items: items.filter((s) => s.source !== 'upload' && directionOf(s.id, s.dataset) === direction),
  })).filter((g) => g.items.length > 0)
  const groups = allGroups.filter((g) => filter === 'all' || g.direction === filter)
  const uploaded = items.filter((s) => s.source === 'upload' && (filter === 'all' || directionOf(s.id, s.dataset) === filter))
  return (
    <div className="mx-auto w-full max-w-7xl space-y-4">
      <ControlPanel />
      <div>
        <div className="mb-2 flex items-center justify-between gap-2">
          <h2 className="text-base font-semibold">Сценарии</h2>
          <Button variant="outline" size="sm" nativeButton={false} render={<RunLink to="/emulator/upload" />}>
            <Upload /> Загрузить журнал
          </Button>
        </div>
        {scenarios.isPending && (
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
            {Array.from({ length: 4 }, (_, i) => (
              <Skeleton key={i} className="h-56" />
            ))}
          </div>
        )}
        {scenarios.isError && <QueryError error={scenarios.error} onRetry={() => void scenarios.refetch()} what="Сценарии" compact />}
        {allGroups.length > 1 && (
          <div className="mb-3 flex flex-wrap gap-1.5" role="group" aria-label="Направление ТЗ">
            <Button size="sm" variant={filter === 'all' ? 'secondary' : 'outline'} onClick={() => setFilter('all')}>
              Все
            </Button>
            {allGroups.map(({ direction, items }) => (
              <Button
                key={direction}
                size="sm"
                variant={filter === direction ? 'secondary' : 'outline'}
                className={DIRECTION_TEXT[direction]}
                onClick={() => setFilter(direction)}
              >
                {DIRECTION_TITLE[direction]} {items.length}
              </Button>
            ))}
          </div>
        )}
        <div className="space-y-4">
          {groups.map(({ direction, items }) => (
            <section key={direction}>
              <h3 className={cn('mb-2 flex items-center gap-2 text-sm font-semibold', DIRECTION_TEXT[direction])}>
                {DIRECTION_TITLE[direction]}
              </h3>
              <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
                {items.map((s) => (
                  <ScenarioCard key={s.id} s={s} onStart={setStarting} />
                ))}
              </div>
            </section>
          ))}
          {uploaded.length > 0 && (
            <section>
              <h3 className="mb-2 text-sm font-medium">Загруженные журналы</h3>
              <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
                {uploaded.map((s) => (
                  <ScenarioCard key={s.id} s={s} onStart={setStarting} showDirection />
                ))}
              </div>
            </section>
          )}
        </div>
      </div>
      <Card className="py-0">
        <CardHeader className="pt-4">
          <CardTitle>Прогоны</CardTitle>
          <CardDescription>
            Каждый запуск — отдельный прогон, данные прошлых прогонов остаются. «Открыть» переключает все экраны на прогон.
            P / R событийной модели считается по прогнозам с истёкшим горизонтом (24 ч). У коротких прогонов закрытых ещё нет —
            тогда показана нижняя граница Precision по тревогам, после которых отказ уже случился. У прогонов внешних наборов —
            модели набора, исход по разметке.
          </CardDescription>
        </CardHeader>
        <CardContent className="px-0">
          <RunsTable />
        </CardContent>
      </Card>
      <StartDialog key={starting?.id ?? 'none'} scenario={starting} onClose={() => setStarting(null)} />
    </div>
  )
}
