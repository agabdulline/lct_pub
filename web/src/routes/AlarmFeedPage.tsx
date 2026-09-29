import { BellRing, GitCompareArrows } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'

import { AccessPanel } from '@/components/access/AccessPanel'
import { AlarmPanel } from '@/components/alarms/AlarmPanel'
import { AlarmTable } from '@/components/alarms/AlarmTable'
import { ObjectSelect } from '@/components/domain/ObjectSelect'
import { Pager } from '@/components/domain/Pager'
import { QueryError } from '@/components/domain/QueryError'
import { StatusDot } from '@/components/domain/StatusBadge'
import { EquipmentPanel } from '@/components/equipment/EquipmentPanel'
import { ExternalFeed } from '@/components/external/ExternalFeed'
import { Card, CardContent } from '@/components/ui/card'
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from '@/components/ui/empty'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { Switch } from '@/components/ui/switch'
import { Toggle } from '@/components/ui/toggle'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { useAccessIncidents, useAlarms, useEquipmentEpisodes } from '@/lib/api/queries'
import type { Verdict } from '@/lib/api/types'
import { fmtDateTimeShort, fmtInt, mskDay } from '@/lib/format'
import { ACCESS_NOTE, ACCESS_RULE_TEXT, VERDICT_LABEL, VERDICT_STATUS } from '@/lib/labels'
import { useRun } from '@/lib/run'
import { useSearchState } from '@/lib/search-state'
import { useSimNow } from '@/lib/sim-now'
import { useStreamEvent } from '@/lib/sse/bus'
import { cn } from '@/lib/utils'

const PAGE_SIZE = 50
const VERDICTS: Exclude<Verdict, 'pending'>[] = ['reaction', 'sensor_fault', 'ppr', 'no_score']
const KINDS = [
  { value: 'all', label: 'Все' },
  { value: 'solitary', label: 'Одиночные' },
  { value: 'group', label: 'Группы' },
  { value: 'ppr', label: 'ППР' },
  { value: 'equipment', label: 'Оборудование' },
  { value: 'access', label: 'Доступ' },
]
const PERIODS = [
  { value: 'run', label: 'С начала прогона' },
  { value: '24', label: 'Последние 24 ч' },
  { value: '6', label: 'Последние 6 ч' },
  { value: '1', label: 'Последний час' },
  { value: 'all', label: 'Вся история' },
]

// Alarm feed — the main screen (docs/product/v1-scope.md, экран 1). A run of an external data set has no SMVU
// alarms: its feed is the alerts of the data set's models (stage 14).
export function AlarmFeedPage() {
  const { run } = useRun()
  return run?.dataset ? <ExternalFeed /> : <HackathonFeed />
}

// Filters are in the URL.
function HackathonFeed() {
  const s = useSearchState()
  const { run } = useRun()
  const simNow = useSimNow()
  const verdicts = s.list('verdict')
  const kind = s.get('kind') ?? 'all'
  const objectId = s.num('object')
  // A run's view includes the whole base history; the feed starts at the run start unless asked otherwise.
  const period = s.get('period') ?? (run ? 'run' : 'all')
  const from = period === 'run' ? (run?.sim_start ?? null) : period === 'all' ? null : s.get('from')
  const page = s.num('page') ?? 1
  const selected = s.num('alarm')
  const selectedEquipment = s.num('equipment')
  const selectedAccess = s.num('access')
  // «Снято после открытия» — a journal mark, not in the feed; shown only when asked for, under «Доступ».
  const withDismissed = s.get('dismissed') === '1'
  const pprAlert = s.get('ppr_alert') === '1'
  // Stage 15: only the alarms where v0 and v3 disagree.
  const disagree = s.get('disagree') === 'true'
  // A filter that picks single alarms out of planned checks shows them as rows, not folded into the episode.
  const collapsePpr = s.get('ppr') !== 'expand' && !pprAlert && !disagree
  // Equipment episodes and access incidents have no verdict: they stand among the alarms unless the feed is filtered
  // by a verdict or by an alarm kind.
  const equipmentOnly = kind === 'equipment'
  const accessOnly = kind === 'access'
  const eventsOnly = equipmentOnly || accessOnly
  const withEvents = kind === 'all' && !verdicts.length && !pprAlert && !disagree
  const withEquipment = equipmentOnly || withEvents

  const alarms = useAlarms({
    // «Оборудование» and «Доступ» show their rows only; the alarm query then only keeps the verdict counters.
    page: eventsOnly ? 1 : page,
    size: eventsOnly ? 1 : PAGE_SIZE,
    verdict: pprAlert ? ['ppr'] : verdicts,
    alert: pprAlert ? true : null,
    kind: kind === 'all' || eventsOnly ? null : kind,
    object_id: objectId,
    from,
    disagree: disagree ? 'true' : null,
  })
  const data = alarms.data
  const counts = data?.counts
  const items = eventsOnly ? [] : (data?.items ?? [])

  // A handful per run: the whole period in one request, spread over the alarm pages by time.
  const equipment = useEquipmentEpisodes(equipmentOnly ? { page, size: PAGE_SIZE, object_id: objectId, from } : { size: 200, object_id: objectId, from })
  const eq = equipment.data
  // The window of this alarm page: newer than its last alarm; not newer than its first, except on page 1; the last
  // page takes everything older.
  const lastPage = !data || page * PAGE_SIZE >= data.total
  const inWindow = (ts: string) => {
    const t = Date.parse(ts)
    if (!lastPage && items.length && t < Date.parse(items[items.length - 1].ts)) return false
    return page === 1 || !items.length || t <= Date.parse(items[0].ts)
  }
  const pageEquipment = !withEquipment ? [] : equipmentOnly ? (eq?.items ?? []) : (eq?.items ?? []).filter((e) => inWindow(e.start_ts))

  // Access incidents: the base history holds thousands, so among the alarms they are asked for the page window only.
  const windowFrom = !lastPage && items.length ? items[items.length - 1].ts : from
  const windowTo = page > 1 && items.length ? new Date(Date.parse(items[0].ts) + 1000).toISOString() : null
  const access = useAccessIncidents(
    accessOnly
      ? { page, size: PAGE_SIZE, object_id: objectId, from, status: withDismissed ? 'open,closed,dismissed' : null }
      : { size: 200, object_id: objectId, from: windowFrom, to: windowTo },
    accessOnly || (withEvents && !!data),
  )
  // Period totals for the filter and the header, whatever page is shown.
  const accessTotals = useAccessIncidents({ size: 1, object_id: objectId, from })
  const accessCounts = accessTotals.data?.counts
  const pageAccess = accessOnly ? (access.data?.items ?? []) : withEvents ? (access.data?.items ?? []).filter((i) => inWindow(i.start_ts)) : []

  // Live alarms flash for a few seconds.
  const [fresh, setFresh] = useState<Set<number>>(new Set())
  const timers = useRef<ReturnType<typeof setTimeout>[]>([])
  useStreamEvent('alarm.created', (a) => {
    setFresh((prev) => new Set(prev).add(a.id))
    timers.current.push(
      setTimeout(() => {
        setFresh((prev) => {
          const next = new Set(prev)
          next.delete(a.id)
          return next
        })
      }, 6000),
    )
  })
  const [freshEquipment, setFreshEquipment] = useState<Set<number>>(new Set())
  useStreamEvent('equipment_episode.created', (e) => {
    setFreshEquipment((prev) => new Set(prev).add(e.id))
    timers.current.push(
      setTimeout(() => {
        setFreshEquipment((prev) => {
          const next = new Set(prev)
          next.delete(e.id)
          return next
        })
      }, 6000),
    )
  })
  const [freshAccess, setFreshAccess] = useState<Set<number>>(new Set())
  useStreamEvent('access_incident.created', (inc) => {
    setFreshAccess((prev) => new Set(prev).add(inc.id))
    timers.current.push(
      setTimeout(() => {
        setFreshAccess((prev) => {
          const next = new Set(prev)
          next.delete(inc.id)
          return next
        })
      }, 6000),
    )
  })
  useEffect(() => () => timers.current.forEach(clearTimeout), [])

  // A relative period is fixed at the moment it is chosen, so the query does not change with every clock tick.
  const setPeriod = (v: string | null) => {
    if (!v || v === 'run') return s.set({ from: null, period: null })
    if (v === 'all') return s.set({ from: null, period: run ? 'all' : null })
    if (!simNow) return
    s.set({ from: new Date(new Date(simNow).getTime() - Number(v) * 3_600_000).toISOString(), period: v })
  }
  const periods = run ? PERIODS : PERIODS.filter((p) => p.value !== 'run')

  const toggleVerdict = (v: string) => {
    const next = verdicts.includes(v) ? verdicts.filter((x) => x !== v) : [...verdicts, v]
    s.set({ verdict: next.join(',') || null, ppr_alert: null, kind: eventsOnly ? null : kind === 'all' ? null : kind })
  }


  return (
    <div
      className={cn(
        'mx-auto w-full max-w-7xl space-y-4',
        (selected !== null || selectedEquipment !== null || selectedAccess !== null) && '2xl:mr-[26rem] 2xl:w-[calc(100%-26rem)]',
      )}
    >
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Лента тревог</h1>
          <p className="text-sm text-muted-foreground">
            Дымовые тревоги с вердиктом модели, события оборудования и вскрытия под охраной. Сервис не гасит тревоги —
            вердикт подсказывает, решает диспетчер.
          </p>
        </div>
        <div className="flex items-center gap-2 text-sm">
          <StatusDot status="alarm" pulse={!!counts?.active} />
          Сейчас активно: <span className="font-semibold tabular-nums">{counts ? fmtInt(counts.active) : '—'}</span>
          {!!eq?.counts.active && (
            <span className="flex items-center gap-2 border-l pl-2">
              <StatusDot status="risk" pulse />
              оборудование: <span className="font-semibold tabular-nums">{fmtInt(eq.counts.active)}</span>
            </span>
          )}
          {!!accessCounts?.open && (
            <span className="flex items-center gap-2 border-l pl-2" title={ACCESS_NOTE}>
              <StatusDot status="alarm" pulse />
              вскрытие под охраной: <span className="font-semibold tabular-nums">{fmtInt(accessCounts.open)}</span>
            </span>
          )}
        </div>
      </div>

      {/* Verdict counters are filters as well. */}
      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-5">
        {VERDICTS.map((v) => {
          const on = verdicts.includes(v)
          return (
            <button
              key={v}
              type="button"
              onClick={() => toggleVerdict(v)}
              aria-pressed={on}
              className={cn(
                'flex items-center justify-between rounded-xl bg-card px-3 py-2.5 text-left text-sm ring-1 ring-foreground/10 transition-colors hover:bg-muted/50',
                on && 'ring-2 ring-ring',
              )}
            >
              <span className="flex items-center gap-2">
                <StatusDot status={VERDICT_STATUS[v]} />
                {VERDICT_LABEL[v]}
              </span>
              <span className="text-lg font-semibold tabular-nums">{counts ? fmtInt(counts[v]) : '—'}</span>
            </button>
          )
        })}
        <button
          type="button"
          onClick={() => s.set({ ppr_alert: pprAlert ? null : '1', verdict: null, kind: eventsOnly ? null : kind === 'all' ? null : kind })}
          aria-pressed={pprAlert}
          className={cn(
            'flex items-center justify-between rounded-xl bg-card px-3 py-2.5 text-left text-sm ring-1 ring-foreground/10 transition-colors hover:bg-muted/50',
            pprAlert && 'ring-2 ring-ring',
          )}
        >
          <span className="flex items-center gap-2">
            <StatusDot status="risk" />
            ППР: вероятно не пройдут проверку
          </span>
          <span className="text-lg font-semibold tabular-nums">{counts ? fmtInt(counts.ppr_alert) : '—'}</span>
        </button>
      </div>
      {pprAlert && (
        <p className="text-xs text-muted-foreground">
          Тревоги плановых проверок, где событийная модель выше порога (допущение A12): датчик, вероятно, не пройдёт проверку.
        </p>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <ObjectSelect value={objectId} onChange={(id) => s.set({ object: id })} className="w-56" />
        {/* A joined group does not wrap well: on a phone it scrolls inside itself instead of widening the page. */}
        <div className="max-w-full overflow-x-auto">
          <ToggleGroup
            value={[kind]}
            onValueChange={(v) => v[0] && s.set({ kind: v[0] === 'all' ? null : v[0], page: null, dismissed: null })}
            size="sm"
            variant="outline"
          >
            {KINDS.map((k) => (
              <ToggleGroupItem key={k.value} value={k.value}>
                {k.label}
                {k.value === 'equipment' && !!eq?.total && <span className="text-muted-foreground tabular-nums">{fmtInt(eq.total)}</span>}
                {k.value === 'access' && !!accessTotals.data?.total && (
                  <span className="text-muted-foreground tabular-nums">{fmtInt(accessTotals.data.total)}</span>
                )}
              </ToggleGroupItem>
            ))}
          </ToggleGroup>
        </div>
        <Select items={periods} value={period} onValueChange={setPeriod}>
          <SelectTrigger size="sm" className="w-44" aria-label="Период">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {periods.map((p) => (
              <SelectItem key={p.value} value={p.value}>
                {p.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        {from && <span className="text-xs text-muted-foreground">с {fmtDateTimeShort(from)}</span>}
        {counts?.disagree !== undefined && (
          <Toggle
            variant="outline"
            size="sm"
            pressed={disagree}
            onPressedChange={(on) => s.set({ disagree: on ? 'true' : null, kind: on && eventsOnly ? null : kind === 'all' ? null : kind })}
            title="Тревоги, где одна модель выше своего порога, а другая нет"
          >
            <GitCompareArrows /> Модели расходятся
            <span className="tabular-nums opacity-70">{fmtInt(counts.disagree)}</span>
          </Toggle>
        )}
        {accessOnly ? (
          <div className="ml-auto flex items-center gap-2" title="Охрану сняли в течение 5 минут после открытия: свой опоздал снять. В ленте их нет — только отметка в журнале">
            <Switch id="access-dismissed" checked={withDismissed} onCheckedChange={(on) => s.set({ dismissed: on ? '1' : null, page: null }, true)} />
            <Label htmlFor="access-dismissed" className="text-sm font-normal">
              И «снято после открытия»
              {accessCounts && <span className="text-muted-foreground tabular-nums">{fmtInt(accessCounts.dismissed)}</span>}
            </Label>
          </div>
        ) : (
          <div className="ml-auto flex items-center gap-2">
            <Switch
              id="collapse-ppr"
              checked={collapsePpr}
              disabled={pprAlert || disagree}
              onCheckedChange={(on) => s.set({ ppr: on ? null : 'expand' }, true)}
            />
            <Label htmlFor="collapse-ppr" className="text-sm font-normal">
              Сворачивать ППР
            </Label>
          </div>
        )}
      </div>

      <Card className="py-0">
        <CardContent className="px-0">
          {alarms.isPending && (
            <div className="space-y-2 p-4">
              {Array.from({ length: 8 }, (_, i) => (
                <Skeleton key={i} className="h-10" />
              ))}
            </div>
          )}
          {alarms.isError && <QueryError error={alarms.error} onRetry={() => void alarms.refetch()} what="Лента тревог" />}
          {data && items.length === 0 && pageEquipment.length === 0 && pageAccess.length === 0 && !(accessOnly && access.isPending) && (
            <Empty>
              <EmptyHeader>
                <EmptyMedia variant="icon">
                  <BellRing />
                </EmptyMedia>
                <EmptyTitle>{equipmentOnly ? 'Событий оборудования нет' : accessOnly ? 'Вскрытий под охраной нет' : 'Тревог нет'}</EmptyTitle>
                <EmptyDescription>
                  {equipmentOnly
                    ? 'Эпизод — не меньше 5 каналов оборудования одного объекта за 5 минут в «Обесточен», «Неисправен» или «Отключено устройство».'
                    : accessOnly
                    ? `${ACCESS_RULE_TEXT} ${ACCESS_NOTE}.`
                    : pprAlert || disagree || verdicts.length || objectId || kind !== 'all' || (period !== 'run' && period !== 'all')
                    ? 'С такими фильтрами тревог нет — ослабьте фильтры.'
                    : run?.status === 'running'
                      ? 'Трансляция идёт — дымовых тревог с начала прогона пока не было. Новые появятся здесь сами.'
                      : period === 'run'
                        ? 'С начала прогона дымовых тревог не было. Тревоги базовой истории — в периоде «Вся история».'
                        : 'Дымовых тревог нет. Запустите сценарий на экране «Эмулятор СМВУ».'}
                </EmptyDescription>
              </EmptyHeader>
            </Empty>
          )}
          {data && (items.length > 0 || pageEquipment.length > 0 || pageAccess.length > 0) && (
            <AlarmTable
              items={items}
              selectedId={selected}
              onSelect={(id) => s.set({ alarm: id, equipment: null, access: null }, true)}
              collapsePpr={collapsePpr}
              fresh={fresh}
              today={mskDay(simNow)}
              equipment={pageEquipment}
              selectedEquipmentId={selectedEquipment}
              onSelectEquipment={(id) => s.set({ equipment: id, alarm: null, access: null }, true)}
              freshEquipment={freshEquipment}
              access={pageAccess}
              selectedAccessId={selectedAccess}
              onSelectAccess={(id) => s.set({ access: id, alarm: null, equipment: null }, true)}
              freshAccess={freshAccess}
            />
          )}
          {accessOnly && access.isError && <QueryError error={access.error} onRetry={() => void access.refetch()} what="Вскрытия под охраной" />}
        </CardContent>
      </Card>

      {equipmentOnly
        ? eq && eq.total > PAGE_SIZE && <Pager page={page} size={PAGE_SIZE} total={eq.total} onPage={(p) => s.set({ page: p > 1 ? p : null }, true)} noun="событий" />
        : accessOnly
          ? access.data &&
            access.data.total > PAGE_SIZE && (
              <Pager page={page} size={PAGE_SIZE} total={access.data.total} onPage={(p) => s.set({ page: p > 1 ? p : null }, true)} noun="инцидентов" />
            )
          : data && data.total > PAGE_SIZE && <Pager page={page} size={PAGE_SIZE} total={data.total} onPage={(p) => s.set({ page: p > 1 ? p : null }, true)} noun="тревог" />}

      <AlarmPanel alarmId={selected} onClose={() => s.set({ alarm: null }, true)} />
      <EquipmentPanel episodeId={selectedEquipment} onClose={() => s.set({ equipment: null }, true)} />
      <AccessPanel incidentId={selectedAccess} onClose={() => s.set({ access: null }, true)} />
    </div>
  )
}
