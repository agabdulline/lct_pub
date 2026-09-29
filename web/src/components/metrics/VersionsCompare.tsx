import { CircleAlert, GitCompareArrows } from 'lucide-react'
import { Fragment, type ReactNode } from 'react'
import { Link } from 'react-router'

import { ChannelLink } from '@/components/domain/ChannelLink'
import { QueryError } from '@/components/domain/QueryError'
import { RunLink } from '@/components/domain/RunLink'
import { TargetMeter } from '@/components/metrics/TargetMeter'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { useMetrics, useModelCompare, useModels, usePredictions } from '@/lib/api/queries'
import type { AlarmKind, CiRange, CiValue, CompareFallback, FallbackBlock, FallbackDiff, LiveMetrics, ModelCompare, Prediction, Run } from '@/lib/api/types'
import { fmtDate, fmtDateTimeShort, fmtDuration, fmtInt, fmtPercent, fmtScore, plural } from '@/lib/format'
import { ALARM_KIND_TITLE, byVersion, chainOf, historyNote, versionAuthor, versionLabel } from '@/lib/labels'
import { REQUIREMENT } from '@/lib/offline-metrics'
import { useRun } from '@/lib/run'
import { cn } from '@/lib/utils'

// Stage 15.3: the team's v0 and the ML developer's v3 side by side on the same decisions — offline (ml-service: the
// intersection of decisions admissible for both, each on its own threshold) and live in the chosen run (alarms scored
// by every version of the run, closed), then where they disagree and how.

const KINDS: AlarmKind[] = ['ppr', 'group', 'solitary']
const DISAGREE_SHOWN = 10

const fixed3 = (v: number | null | undefined) => (v === null || v === undefined ? '—' : v.toFixed(3).replace('.', ','))
const signed = (v: number, text: string) => `${v > 0 ? '+' : v < 0 ? '−' : ''}${text}`
const pp = (v: number) => signed(v, `${Math.abs(v * 100).toFixed(1).replace('.', ',')} п.п.`)
const ppRange = ([lo, hi]: CiRange) => `${signed(lo, Math.abs(lo * 100).toFixed(1).replace('.', ','))}…${signed(hi, Math.abs(hi * 100).toFixed(1).replace('.', ','))}`
const apDiff = (v: number) => signed(v, Math.abs(v).toFixed(3).replace('.', ','))
const apRange = ([lo, hi]: CiRange) => `${apDiff(lo)}…${apDiff(hi)}`
const pctRange = ([lo, hi]: CiRange) => `${(lo * 100).toFixed(1).replace('.', ',')}–${fmtPercent(hi)}`

/** What a paired difference says: its whole interval on one side of zero — there is a difference. */
/** A version in a difference: a chain is «связка» — «v3 → v0 − v0» would read as arithmetic. */
const diffName = (v: string) => (chainOf(v) ? 'связка' : versionLabel(v))
const diffOwner = (v: string) => (chainOf(v) ? 'связки' : versionLabel(v))

function diffVerdict(d: CiValue, a: string, b: string): string {
  if (d.ci90[0] > 0) return `у ${diffOwner(a)} выше`
  if (d.ci90[1] < 0) return `у ${diffOwner(b)} выше`
  return 'разница не доказана'
}

type Row = {
  version: string
  primary?: boolean
  precision: number | null
  recall: number | null
  f1: number | null
  ap?: number | null
  alerts: number
  tp: number
  fp: number
  fn: number
  leadP50: number | null | undefined
  ci?: { precision: CiRange; recall: CiRange; ap?: CiRange }
  /** The second line instead of the author (the chain's half year: «молчит на 1 051 — решает v0»). */
  note?: string
  /** No closed decisions yet: the lower bound of Precision from alerts already confirmed by a fault. */
  pending?: LiveMetrics
}

function Ci({ children }: { children: ReactNode }) {
  return <div className="text-xs text-muted-foreground tabular-nums">{children}</div>
}

function PrecisionCell({ r }: { r: Row }) {
  const p = r.pending
  if (p && p.alerts > 0 && p.alerts_confirmed > 0)
    return (
      <span
        className="text-sm tabular-nums"
        title="Горизонт ещё не истёк: тревога, после которой отказ уже случился, окончательно верная — отсюда нижняя граница"
      >
        ≥ {fmtPercent(p.alerts_confirmed / p.alerts)}
      </span>
    )
  return (
    <>
      <TargetMeter value={r.alerts ? r.precision : null} target={REQUIREMENT.precision} />
      {r.ci && <Ci>{pctRange(r.ci.precision)}</Ci>}
    </>
  )
}

function VersionRow({ r }: { r: Row }) {
  return (
    <TableRow>
      <TableCell className="pl-4">
        <div className="font-medium">
          {versionLabel(r.version)}
          {r.primary && <span className="ml-1.5 text-xs font-normal text-muted-foreground">основная</span>}
        </div>
        <div className="text-xs text-muted-foreground">{r.note ?? versionAuthor(r.version) ?? r.version}</div>
      </TableCell>
      <TableCell>
        <PrecisionCell r={r} />
      </TableCell>
      <TableCell>
        {r.pending ? (
          <span className="text-sm text-muted-foreground">—</span>
        ) : (
          <>
            <TargetMeter value={r.recall} target={REQUIREMENT.recall} />
            {r.ci && <Ci>{pctRange(r.ci.recall)}</Ci>}
          </>
        )}
      </TableCell>
      <TableCell className="text-right tabular-nums">{r.pending ? '—' : fixed3(r.f1)}</TableCell>
      <TableCell className="text-right tabular-nums">
        {r.ap === undefined ? <span className="text-muted-foreground" title="Живые метрики — на рабочем пороге, без ранжирования">—</span> : fixed3(r.ap)}
        {r.ci?.ap && <Ci>{`${fixed3(r.ci.ap[0])}–${fixed3(r.ci.ap[1])}`}</Ci>}
      </TableCell>
      <TableCell className="text-right tabular-nums">
        {r.pending ? `${fmtInt(r.pending.alerts)} · ${fmtInt(r.pending.alerts_confirmed)} сбылись` : `${fmtInt(r.alerts)} · ${fmtInt(r.tp)} · ${fmtInt(r.fp)} · ${fmtInt(r.fn)}`}
      </TableCell>
      <TableCell className="pr-4 text-right tabular-nums">{fmtDuration(r.leadP50)}</TableCell>
    </TableRow>
  )
}

/** A paired difference `a − b`; AP only where the check ranks (not the chain's half year). */
type Diff = { a: string; b: string; precision: CiValue; recall: CiValue; ap?: CiValue | null }

function DiffRow({ diff }: { diff: Diff }) {
  return (
    <TableRow className="bg-muted/40 hover:bg-muted/40">
      <TableCell className="pl-4">
        <div className="font-medium">
          {diffName(diff.a)} − {diffName(diff.b)}
        </div>
        <div className="text-xs text-muted-foreground">разность, 90% интервал</div>
      </TableCell>
      {(['precision', 'recall'] as const).map((k) => (
        <TableCell key={k} className="tabular-nums">
          <div className="text-sm">{pp(diff[k].value)}</div>
          <Ci>
            {ppRange(diff[k].ci90)} · {diffVerdict(diff[k], diff.a, diff.b)}
          </Ci>
        </TableCell>
      ))}
      <TableCell />
      <TableCell className="text-right tabular-nums">
        {diff.ap ? (
          <>
            <div className="text-sm">{apDiff(diff.ap.value)}</div>
            <Ci>{apRange(diff.ap.ci90)}</Ci>
            <Ci>{diffVerdict(diff.ap, diff.a, diff.b)}</Ci>
          </>
        ) : (
          <span className="text-muted-foreground">—</span>
        )}
      </TableCell>
      <TableCell colSpan={2} className="pr-4" />
    </TableRow>
  )
}

/** A block of rows under its own heading: offline on the shared check, live in the run. */
type Group = { key: string; title: ReactNode; note?: ReactNode; rows: Row[]; diffs?: Diff[]; error?: { error: Error; retry: () => void } }

/** One table for every block, so the numbers of offline and live stand in the same columns. */
function VersionsTable({ groups }: { groups: Group[] }) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead className="pl-4">Версия</TableHead>
          <TableHead>Precision</TableHead>
          <TableHead>Recall</TableHead>
          <TableHead className="text-right">F1</TableHead>
          <TableHead className="text-right">AP</TableHead>
          <TableHead className="w-36 text-right whitespace-normal" title="Тревог модели · из них верных · ложных · неисправностей без тревоги">
            Тревог · верных · ложных · пропущено
          </TableHead>
          <TableHead className="w-28 pr-4 text-right whitespace-normal">Упреждение, медиана</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {groups.map((g) => (
          <Fragment key={g.key}>
            <TableRow className="hover:bg-transparent">
              <TableCell colSpan={7} className="px-4 pt-4 whitespace-normal">
                <div className="font-medium">{g.title}</div>
                {g.note && <div className="text-xs text-muted-foreground">{g.note}</div>}
              </TableCell>
            </TableRow>
            {g.error ? (
              <TableRow className="hover:bg-transparent">
                <TableCell colSpan={7} className="px-4 whitespace-normal">
                  <QueryError error={g.error.error} onRetry={g.error.retry} what="Живые метрики версий" compact />
                </TableCell>
              </TableRow>
            ) : (
              g.rows.map((r) => <VersionRow key={r.version} r={r} />)
            )}
            {g.diffs?.map((d) => <DiffRow key={`${d.a}-${d.b}`} diff={d} />)}
          </Fragment>
        ))}
      </TableBody>
    </Table>
  )
}

function Section({ title, children }: { title: ReactNode; children: ReactNode }) {
  return (
    <section className="space-y-2">
      <h3 className="px-4 text-sm font-medium">{title}</h3>
      {children}
    </section>
  )
}

function offlineGroup(c: ModelCompare, versions: string[]): Group {
  return {
    key: 'offline',
    title: `Офлайн · ${c.period.label}`,
    note: (
      <>
        Решений: {fmtInt(c.decisions)}, неисправностей: {fmtInt(c.positives)}.
        {c.clusters && (
          <>
            {' '}
            Тревоги одного объекта за сутки зависимы, поэтому интервалы — по объекто-суткам: {fmtInt(c.clusters.object_days)}, из них с
            неисправностью {fmtInt(c.clusters.with_faults)}.
          </>
        )}
      </>
    ),
    rows: versions.map((v) => {
      const o = c.versions[v]
      return { version: v, precision: o.precision, recall: o.recall, f1: o.f1, ap: o.ap, alerts: o.alerts, tp: o.tp, fp: o.fp, fn: o.fn, leadP50: o.lead_time?.p50_s, ci: c.ci?.[v] }
    }),
    diffs: c.diff ? [c.diff] : undefined,
  }
}

/**
 * The owner's chain «v3 where it decided, else v0» offline (lct-25, #241): on its own decisions — the half year with the
 * week v3 is silent after a day without journal data (7 563), where v3's silence is a miss and v0 decides for it.
 */
function fallbackGroup(f: CompareFallback, chain: string): Group {
  const h = f.half_year
  const row = (version: string, b: FallbackBlock, note?: string): Row => ({
    version,
    precision: b.precision,
    recall: b.recall,
    f1: b.precision !== null && b.recall !== null && b.precision + b.recall > 0 ? (2 * b.precision * b.recall) / (b.precision + b.recall) : null,
    ap: null,
    alerts: b.alerts,
    tp: b.tp,
    fp: b.fp,
    fn: b.fn,
    leadP50: b.lead_minutes ? b.lead_minutes.p50 * 60 : undefined,
    ci: b.ci90 ? { precision: b.ci90.P, recall: b.ci90.R } : undefined,
    note,
  })
  const diff = (d: FallbackDiff | null | undefined, b: string): Diff[] => (d ? [{ a: chain, b, precision: d.P, recall: d.R }] : [])
  return {
    key: 'fallback',
    title: `Офлайн · связка ${versionLabel(chain)} · I полугодие 2026 с неделей после пропуска журнала`,
    note: (
      <>
        Решений: {fmtInt(h.combo.decisions)}, неисправностей: {fmtInt(h.combo.positives)}. Здесь v3 молчит на {fmtInt(h.v3.silent)}{' '}
        {plural(h.v3.silent, ['решении', 'решениях', 'решениях'])} — это её пропуски, а у связки там решает v0. Поэтому цифры v0 и v3 — не
        те, что на общих решениях выше.
        {h.clusters && ` Интервалы — по объекто-суткам: ${fmtInt(h.clusters.object_days)}, из них с неисправностью ${fmtInt(h.clusters.with_faults)}.`}
      </>
    ),
    rows: [
      row('baseline-v0', h.v0),
      row('v3', h.v3, `${versionAuthor('v3')} · молчит на ${fmtInt(h.v3.silent)}`),
      row(chain, h.combo),
    ],
    diffs: [...diff(h.combo_minus_v0, 'baseline-v0'), ...diff(h.combo_minus_v3, 'v3')],
  }
}

/** One version's live row: the run's closed decisions scored by every version (`paired`). */
function useLiveRow(version: string, run: Run | null, enabled: boolean): Row & { error: Error | null; refetch: () => void } {
  const m = useMetrics(enabled ? 'smoke_alarm' : null, { model_version: version, paired: 'true' })
  const b = m.data?.all
  const live = run?.live?.versions?.[version]
  return {
    version,
    primary: version === run?.primary_model,
    precision: b?.precision ?? null,
    recall: b?.recall ?? null,
    f1: b?.f1 ?? null,
    alerts: b?.alerts ?? 0,
    tp: b?.tp ?? 0,
    fp: b?.fp ?? 0,
    fn: b?.fn ?? 0,
    leadP50: b?.lead_time?.p50_s,
    pending: b && b.decisions === 0 && live ? live : undefined,
    error: m.error,
    refetch: () => void m.refetch(),
  }
}

/** Live block of the run where both versions scored; `null` — nothing to compare in it. */
function useLiveGroup(run: Run | null, versions: [string, string] | null): Group | null {
  // Two versions per run (v0 and v3) — a fixed number of hooks, idle without a pair.
  const on = run !== null && versions !== null
  const [a, b] = versions ?? ['', '']
  const rows = [useLiveRow(a, run, on), useLiveRow(b, run, on)]
  const skipped = [
    usePredictions({ model: 'smoke_alarm', model_version: a, status: 'skipped', size: 1 }, on).data?.total,
    usePredictions({ model: 'smoke_alarm', model_version: b, status: 'skipped', size: 1 }, on).data?.total,
  ]
  if (!on) return null
  const first = run.live?.versions?.[a]
  const failed = rows.find((r) => r.error)
  return {
    key: 'live',
    title: `Живые · прогон №${run.id} · ${run.scenario_title}`,
    note: (
      <>
        {first && first.decisions > 0
          ? `Общих закрытых решений: ${fmtInt(first.decisions)}, неисправностей: ${fmtInt(first.positives)}`
          : 'Закрытых общих решений пока нет: исход известен после конца горизонта (24 ч), до него — только нижняя граница Precision'}
        {first && first.history > 0 && ` · ${historyNote(first.history, first.decisions)}`}.
        {skipped.every((n) => n !== undefined) && (
          <>
            {' '}
            Без оценки: {versionLabel(a)} — {fmtInt(skipped[0])}, {versionLabel(b)} — {fmtInt(skipped[1])} (в сравнение не входят).
          </>
        )}
      </>
    ),
    rows,
    error: failed?.error ? { error: failed.error, retry: failed.refetch } : undefined,
  }
}

function KindTable({ c, versions: [a, b] }: { c: ModelCompare; versions: [string, string] }) {
  const byKind = c.by_kind
  const only = c.disagree.by_kind
  if (!byKind) return null
  const pr = (x: { precision: number | null; recall: number | null } | undefined) => (x ? `${fmtPercent(x.precision)} / ${fmtPercent(x.recall)}` : '—')
  const onlyText = (kind: AlarmKind, v: string) => {
    const o = only?.[kind]?.[v]
    if (!o) return '—'
    if (!o.fault && !o.no_fault) return '0'
    return `${fmtInt(o.fault)} · ${fmtInt(o.no_fault)}`
  }
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead className="pl-4">Вид эпизода тревоги</TableHead>
          <TableHead className="text-right">Решений · неиспр.</TableHead>
          <TableHead className="text-right">{versionLabel(a)} P / R</TableHead>
          <TableHead className="text-right">{versionLabel(b)} P / R</TableHead>
          {only && (
            <>
              <TableHead className="text-right" title="Неисправность после тревоги · ложная тревога">
                Тревога только у {versionLabel(a)}: верных · ложных
              </TableHead>
              <TableHead className="pr-4 text-right" title="Неисправность после тревоги · ложная тревога">
                Только у {versionLabel(b)}: верных · ложных
              </TableHead>
            </>
          )}
        </TableRow>
      </TableHeader>
      <TableBody>
        {KINDS.filter((k) => byKind[k]).map((k) => {
          const row = byKind[k]!
          const n = row[a] ?? row[b]
          return (
            <TableRow key={k}>
              <TableCell className="pl-4">{ALARM_KIND_TITLE[k]}</TableCell>
              <TableCell className="text-right tabular-nums">
                {fmtInt(n?.decisions)} · {fmtInt(n?.positives)}
              </TableCell>
              <TableCell className="text-right tabular-nums">{pr(row[a])}</TableCell>
              <TableCell className="text-right tabular-nums">{pr(row[b])}</TableCell>
              {only && (
                <>
                  <TableCell className="text-right tabular-nums">{onlyText(k, a)}</TableCell>
                  <TableCell className="pr-4 text-right tabular-nums">{onlyText(k, b)}</TableCell>
                </>
              )}
            </TableRow>
          )
        })}
      </TableBody>
    </Table>
  )
}

/** Offline disagreements: who alerts alone, how often it is right, by episode kind, and the silent week of a version. */
function OfflineDisagree({ c, versions }: { c: ModelCompare; versions: [string, string] }) {
  const d = c.disagree
  const cov = c.coverage
  return (
    <div className="space-y-3">
      <p className="px-4 text-sm">
        Офлайн расходятся на <span className="font-medium">{fmtInt(d.decisions)}</span> из {fmtInt(c.decisions)} решений (
        {fmtPercent(c.decisions ? d.decisions / c.decisions : null)}): тревога у одной версии, у другой — нет. В расхождениях права{' '}
        {versions.map((v, i) => (
          <span key={v}>
            {i > 0 && ', '}
            {versionLabel(v)} — {fmtInt(d.right[v] ?? 0)} {plural(d.right[v] ?? 0, ['раз', 'раза', 'раз'])}
          </span>
        ))}
        .
      </p>
      <div className="grid gap-3 px-4 sm:grid-cols-2">
        {versions.map((v) => {
          const alone = d.alert_only[v] ?? 0
          const fault = d.alert_only_fault?.[v]
          return (
            <div key={v} className="rounded-lg border p-3 text-sm">
              <div className="font-medium">
                Тревога только у {versionLabel(v)} — {fmtInt(alone)}
              </div>
              {fault !== undefined && fault !== null && (
                <div className="tabular-nums text-muted-foreground">
                  верных {fmtInt(fault)} · ложных {fmtInt(alone - fault)}
                </div>
              )}
            </div>
          )
        })}
      </div>
      {d.alert_only_fault && (
        <p className="px-4 text-xs text-muted-foreground">
          Верная тревога в расхождении — неисправность, которую другая версия пропустила; ложная — лишняя тревога, которой другая
          версия избежала.
        </p>
      )}
      <KindTable c={c} versions={versions} />
      {cov && (
        <div className="mx-4 flex items-start gap-2 rounded-lg bg-status-risk/5 p-3 text-sm">
          <CircleAlert className="mt-0.5 size-4 shrink-0 text-status-risk" />
          <div>
            <div className="font-medium">
              {versionLabel(cov.version)} молчит {fmtInt(cov.silent_days)} {plural(cov.silent_days, ['сутки', 'суток', 'суток'])} после дня без данных журнала
            </div>
            <div className="text-muted-foreground">
              После пропуска журнала {cov.gap_days.map(fmtDate).join(', ')} с {fmtDate(cov.from)} по {fmtDate(cov.to)} —{' '}
              {fmtInt(cov.decisions)} {plural(cov.decisions, ['тревога осталась', 'тревоги остались', 'тревог остались'])} без оценки{' '}
              {versionLabel(cov.version)}, среди них {fmtInt(cov.positives)} {plural(cov.positives, ['неисправность', 'неисправности', 'неисправностей'])}.
              {Object.entries(cov.others).map(([v, o]) => (
                <span key={v}>
                  {' '}
                  {versionLabel(v)} нашла из них {fmtInt(o.tp)} (P {fmtPercent(o.precision)}, R {fmtPercent(o.recall)}).
                </span>
              ))}{' '}
              В сравнение выше эти решения не входят. В прогоне такие тревоги у {versionLabel(cov.version)} — «Без оценки»: за {fmtInt(cov.silent_days)}{' '}
              {plural(cov.silent_days, ['сутки', 'суток', 'суток'])} до тревоги был день без данных журнала.
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

const outcomeText = (p: Prediction) => (p.outcome === 'fault' ? 'неисправность' : p.outcome === 'no_fault' ? 'без неисправности' : 'ожидается')

/** Live disagreements of the run: decisions of the primary version where the other one alerts differently. */
function LiveDisagree({ run, versions }: { run: Run; versions: [string, string] }) {
  const primary = run.primary_model ?? versions[0]
  const q = usePredictions({ model: 'smoke_alarm', model_version: primary, disagree: 'true', size: 100 })
  const items = q.data?.items ?? []
  const total = q.data?.total ?? 0
  const scoreOf = (p: Prediction, v: string) =>
    v === p.model_version ? { score: p.score, alert: p.alert } : (p.paired.find((o) => o.model_version === v) ?? { score: null, alert: null })
  const aloneOf = (p: Prediction) => versions.find((v) => scoreOf(p, v).alert === true) ?? null
  const rightOf = (p: Prediction) => (p.outcome === null ? null : (versions.find((v) => scoreOf(p, v).alert === (p.outcome === 'fault')) ?? null))
  const count = (f: (p: Prediction) => boolean) => items.filter(f).length
  if (q.isError) return <QueryError error={q.error} onRetry={() => void q.refetch()} what="Расхождения прогона" compact className="mx-4" />
  if (q.isPending) return null
  return (
    <div className="space-y-2">
      <p className="px-4 text-sm">
        {total === 0 ? (
          <>В прогоне №{run.id} версии пока ни разу не разошлись.</>
        ) : (
          <>
            В прогоне №{run.id} расходятся на <span className="font-medium">{fmtInt(total)}</span>{' '}
            {plural(total, ['решении', 'решениях', 'решениях'])}
            {total <= items.length && (
              <>
                : тревога только у {versions.map((v) => `${versionLabel(v)} — ${fmtInt(count((p) => aloneOf(p) === v))}`).join(', только у ')}; права{' '}
                {versions.map((v) => `${versionLabel(v)} — ${fmtInt(count((p) => rightOf(p) === v))}`).join(', ')}
                {count((p) => p.outcome === null) > 0 && `, ждут исхода — ${fmtInt(count((p) => p.outcome === null))}`}
              </>
            )}
            .{' '}
            <RunLink to="/predictions?compare=disagree" className="underline underline-offset-4">
              Все в журнале
            </RunLink>
          </>
        )}
      </p>
      {items.length > 0 && (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="pl-4">Решение</TableHead>
              <TableHead>Канал</TableHead>
              {versions.map((v) => (
                <TableHead key={v} className="text-right">
                  {versionLabel(v)}
                </TableHead>
              ))}
              <TableHead>Исход</TableHead>
              <TableHead className="pr-4">Права</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {items.slice(0, DISAGREE_SHOWN).map((p) => {
              const right = rightOf(p)
              return (
                <TableRow key={p.id}>
                  <TableCell className="pl-4 tabular-nums">{fmtDateTimeShort(p.decision_time)}</TableCell>
                  <TableCell className="max-w-64">
                    <ChannelLink id={p.channel_id} name={p.channel_name} sensorType={p.sensor_type} />
                    <div className="truncate text-xs text-muted-foreground">{p.object_name}</div>
                  </TableCell>
                  {versions.map((v) => {
                    const s = scoreOf(p, v)
                    return (
                      <TableCell key={v} className="text-right tabular-nums">
                        <span className={cn(s.alert ? 'font-medium text-status-risk' : 'text-muted-foreground')}>{fmtScore(s.score)}</span>
                        <div className="text-xs text-muted-foreground">{s.alert ? 'тревога' : s.alert === false ? 'нет' : 'пропуск'}</div>
                      </TableCell>
                    )
                  })}
                  <TableCell>{outcomeText(p)}</TableCell>
                  <TableCell className="pr-4 font-medium">{right ? versionLabel(right) : <span className="font-normal text-muted-foreground">—</span>}</TableCell>
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
      )}
      {items.length > DISAGREE_SHOWN && (
        <p className="px-4 text-xs text-muted-foreground">
          Показаны последние {DISAGREE_SHOWN} из {fmtInt(total)} — остальные в журнале, фильтр «Модели расходятся».
        </p>
      )}
    </div>
  )
}

/** A recent run where both versions scored — to offer when the chosen run has one version only. */
function useComparableRun(): Run | null {
  const { runs } = useRun()
  return [...runs].sort((a, b) => b.id - a.id).find((r) => !r.dataset && r.primary_model && r.shadow_models.length > 0) ?? null
}

export function VersionsCompare() {
  const compare = useModelCompare('smoke_alarm')
  const models = useModels().data?.items
  const { run } = useRun()
  const other = useComparableRun()
  const c = compare.data
  // A chain primary (lct-95): its parts score every alarm, live on the paired decisions — the same as two versions.
  const runVersions = run && !run.dataset && run.primary_model ? (chainOf(run.primary_model) ?? [run.primary_model, ...run.shadow_models]).sort(byVersion) : []
  const chain = models?.find((m) => m.model === 'smoke_alarm' && chainOf(m.version))?.version ?? 'v3>baseline-v0'
  const offlineVersions = c ? Object.keys(c.versions).sort(byVersion) : []
  const pair = (vs: string[]) => (vs.length === 2 ? (vs as [string, string]) : null)
  const livePair = pair(runVersions)
  const offlinePair = pair(offlineVersions)
  const shown = [...new Set([...offlineVersions, ...runVersions])].sort(byVersion)
  const threshold = (v: string) => models?.find((m) => m.model === 'smoke_alarm' && m.version === v)?.threshold
  const live = useLiveGroup(livePair ? run : null, livePair)
  if (!c && !livePair) {
    if (compare.isError) return <QueryError error={compare.error} onRetry={() => void compare.refetch()} what="Сравнение v0 и v3" compact />
    return null
  }
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <GitCompareArrows className="size-4" /> {shown.map(versionLabel).join(' и ')} на одних решениях
        </CardTitle>
        <CardDescription>
          Обе модели оценивают одни и те же дымовые тревоги, каждая на своём пороге:{' '}
          {shown.map((v, i) => (
            <span key={v}>
              {i > 0 && ', '}
              {versionLabel(v)} — {versionAuthor(v) ?? v}
              {threshold(v) !== undefined && threshold(v) !== null && ` (${fixed3(threshold(v))})`}
            </span>
          ))}
          . Здесь только решения, оценённые обеими: наборы проверки у версий разные, их собственные цифры ниже напрямую не сравниваются.
          «24 ч» — горизонт исхода, а не упреждение: верная тревога опережает отказ на минуты. Отметка на полосе — требование ТЗ.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-6 overflow-x-auto px-0">
        {(c || live) && (
          <VersionsTable
            groups={[...(c ? [offlineGroup(c, offlineVersions)] : []), ...(c?.fallback ? [fallbackGroup(c.fallback, chain)] : []), ...(live ? [live] : [])]}
          />
        )}
        {!c && <p className="px-4 text-sm text-muted-foreground">Офлайн-сравнения в ML-сервисе нет — только живое.</p>}
        {!live && (
          <p className="px-4 text-sm text-muted-foreground">
            {run?.dataset
              ? 'Открыт прогон внешнего набора — в нём событийной модели нет.'
              : run
                ? `В прогоне №${run.id} тревоги оценивала одна версия — живого сравнения нет.`
                : 'Прогонов ещё не было.'}
            {other && (
              <>
                {' '}
                Обе версии работали в прогоне{' '}
                <Link to={{ pathname: '/models', search: `?run=${other.id}` }} className="text-foreground underline underline-offset-4">
                  №{other.id} · {other.scenario_title}
                </Link>
                .
              </>
            )}
          </p>
        )}
        {(offlinePair || livePair) && (
          <Section title="Где расходятся">
            <div className="space-y-5">
              {c && offlinePair && <OfflineDisagree c={c} versions={offlinePair} />}
              {livePair && run && <LiveDisagree run={run} versions={livePair} />}
            </div>
          </Section>
        )}
      </CardContent>
    </Card>
  )
}
