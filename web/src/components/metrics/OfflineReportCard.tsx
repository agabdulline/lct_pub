import { CircleCheck, CircleX } from 'lucide-react'

import { SensorTypeIcon } from '@/components/domain/SensorTypeIcon'
import { StatusBadge } from '@/components/domain/StatusBadge'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import type { AlarmKind, OfflineBlock, OfflineReport } from '@/lib/api/types'
import { fmtDateTimeShort, fmtDuration, fmtInt, fmtPercent, plural } from '@/lib/format'
import { EVAL_SCHEME_LABEL, EXPERIMENT_KIND_LABEL, EXTERNAL_CLASS_LABEL } from '@/lib/labels'
import { REQUIREMENT } from '@/lib/offline-metrics'

import { experimentStatsOf, readExperiments, type Experiment } from './experiments'
import { PrCurveChart } from './PrCurveChart'
import { TargetMeter } from './TargetMeter'

const ROLE: Record<NonNullable<OfflineReport['by_quarter']>[number]['role'], { label: string; independent: boolean }> = {
  early_stop: { label: 'ранняя остановка', independent: false },
  threshold: { label: 'подбор порога', independent: false },
  test: { label: 'проверка', independent: true },
}

const num = (v: number | null) => (v === null ? '—' : v.toFixed(3).replace('.', ','))

/** The ТЗ requirement marks the bars of hackathon models only: an external model solves another task. */
type Targets = { precision?: number; recall?: number }
const TZ: Targets = REQUIREMENT
const NONE: Targets = {}

function BlockCells({ b, targets = TZ }: { b: OfflineBlock; targets?: Targets }) {
  return (
    <>
      <TableCell className="text-right tabular-nums">{fmtInt(b.decisions)}</TableCell>
      <TableCell className="text-right tabular-nums">{fmtInt(b.positives)}</TableCell>
      <TableCell>
        <TargetMeter value={b.precision} target={targets.precision} />
      </TableCell>
      <TableCell>
        <TargetMeter value={b.recall} target={targets.recall} />
      </TableCell>
      <TableCell className="text-right tabular-nums">{num(b.f1)}</TableCell>
      <TableCell className="pr-4 text-right tabular-nums">{num(b.ap)}</TableCell>
    </>
  )
}

function Head({ first }: { first: string }) {
  return (
    <TableHeader>
      <TableRow>
        <TableHead className="pl-4">{first}</TableHead>
        <TableHead className="text-right">Решений</TableHead>
        <TableHead className="text-right">Положит.</TableHead>
        <TableHead>Precision</TableHead>
        <TableHead>Recall</TableHead>
        <TableHead className="text-right">F1</TableHead>
        <TableHead className="pr-4 text-right">AP</TableHead>
      </TableRow>
    </TableHeader>
  )
}

const KIND_LABEL: Record<AlarmKind, string> = { ppr: 'плановая проверка', group: 'группа', solitary: 'одиночная' }

/** Sum of blocks: counts add up, rates are recomputed from the sums. */
function combine(blocks: OfflineBlock[]): Pick<OfflineBlock, 'decisions' | 'positives' | 'precision' | 'recall' | 'f1'> {
  const s = blocks.reduce((a, b) => ({ d: a.d + b.decisions, pos: a.pos + b.positives, tp: a.tp + b.tp, fp: a.fp + b.fp, fn: a.fn + b.fn }), { d: 0, pos: 0, tp: 0, fp: 0, fn: 0 })
  const precision = s.tp + s.fp ? s.tp / (s.tp + s.fp) : null
  const recall = s.tp + s.fn ? s.tp / (s.tp + s.fn) : null
  const f1 = precision !== null && recall !== null && precision + recall > 0 ? (2 * precision * recall) / (precision + recall) : null
  return { decisions: s.d, positives: s.pos, precision, recall, f1 }
}

// Test decisions by the alarm episode kind: planned checks dominate the event model's decisions (A12),
// the alarms outside them are what a dispatcher triages.
function EpisodeKinds({ rows }: { rows: NonNullable<OfflineReport['by_episode_kind']> }) {
  const outside = combine(rows.filter((r) => r.kind !== 'ppr'))
  return (
    <div>
      <div className="mb-1 px-4 text-sm font-medium">По виду эпизода тревоги</div>
      <Table>
        <Head first="Эпизод" />
        <TableBody>
          {rows.map((r) => (
            <TableRow key={r.kind}>
              <TableCell className="pl-4">{KIND_LABEL[r.kind]}</TableCell>
              <BlockCells b={r} />
            </TableRow>
          ))}
          <TableRow className="font-medium">
            <TableCell className="pl-4">Вне плановых проверок</TableCell>
            <TableCell className="text-right tabular-nums">{fmtInt(outside.decisions)}</TableCell>
            <TableCell className="text-right tabular-nums">{fmtInt(outside.positives)}</TableCell>
            <TableCell>
              <TargetMeter value={outside.precision} target={REQUIREMENT.precision} />
            </TableCell>
            <TableCell>
              <TargetMeter value={outside.recall} target={REQUIREMENT.recall} />
            </TableCell>
            <TableCell className="text-right tabular-nums">{num(outside.f1)}</TableCell>
            <TableCell className="pr-4 text-right text-muted-foreground">—</TableCell>
          </TableRow>
        </TableBody>
      </Table>
      <p className="px-4 pt-1.5 text-xs text-muted-foreground">
        Большая часть решений событийной модели — тревоги плановых проверок; строка «вне плановых проверок» — одиночные и
        групповые тревоги, которые диспетчер разбирает в первую очередь.
      </p>
    </div>
  )
}

const ofN = (n: number, total: number) => `${fmtInt(n)} из ${fmtInt(total)}`

const classLabel = (c: string) => EXTERNAL_CLASS_LABEL[c] ?? c
const kindsLabel = (kinds: string[]) => kinds.map((k) => EXPERIMENT_KIND_LABEL[k] ?? k).join(', ')

// External model (Magdeburg): what happened in each experiment and what the model concluded — the honest view of a
// detector checked leave-one-experiment-out.
function Experiments({ rows }: { rows: Experiment[] }) {
  const s = experimentStatsOf(rows)
  return (
    <div>
      <div className="mb-1 px-4 text-sm font-medium">По опытам</div>
      <ul className="space-y-0.5 px-4 pb-2 text-sm">
        {s.nuisances > 0 && (
          <li>
            <span className="font-medium">Ложные вызовы:</span> помехи с хотя бы одной тревогой «пожар» —{' '}
            <span className="font-medium">{ofN(s.nuisancesAlerted, s.nuisances)}</span>
          </li>
        )}
        {s.nuisances > 0 && <li>Помехи с итогом «пожар» по опыту — {ofN(s.nuisancesPredictedFire, s.nuisances)}</li>}
        {s.fires > 0 && (
          <li>
            Пожары обнаружены (хотя бы одна тревога) — {ofN(s.firesAlerted, s.fires)}; итог «пожар» по опыту —{' '}
            {ofN(s.firesPredicted, s.fires)}
          </li>
        )}
        <li>
          Итог по опыту совпал с классом опыта — {fmtInt(s.matched)} из {fmtInt(s.total)} {plural(s.total, ['опыта', 'опытов', 'опытов'])}
        </li>
      </ul>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead className="pl-4">Опыт</TableHead>
            <TableHead>Что было</TableHead>
            <TableHead className="text-right">Мин</TableHead>
            <TableHead className="text-right">Решений</TableHead>
            <TableHead className="text-right">Тревог «пожар»</TableHead>
            <TableHead className="text-right">Узлов</TableHead>
            <TableHead className="text-right">Первая тревога</TableHead>
            <TableHead className="pr-4">Итог модели</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((r) => {
            const ok = r.predicted === r.label
            return (
              <TableRow key={r.n}>
                <TableCell className="pl-4 tabular-nums">№{r.n}</TableCell>
                <TableCell>
                  <span className="font-medium">{classLabel(r.label)}</span>
                  {r.kinds.length > 0 && <span className="text-muted-foreground"> · {kindsLabel(r.kinds)}</span>}
                </TableCell>
                <TableCell className="text-right tabular-nums">{r.minutes === null ? '—' : r.minutes.toFixed(1).replace('.', ',')}</TableCell>
                <TableCell className="text-right tabular-nums">{fmtInt(r.decisions)}</TableCell>
                <TableCell className="text-right tabular-nums">{fmtInt(r.fireAlerts)}</TableCell>
                <TableCell className="text-right tabular-nums">{fmtInt(r.nodes)}</TableCell>
                <TableCell className="text-right tabular-nums">{r.firstAlert === null ? 'не было' : fmtDuration(r.firstAlert)}</TableCell>
                <TableCell className="pr-4">
                  {r.predicted && (
                    <span className="inline-flex items-center gap-1.5">
                      {ok ? <CircleCheck className="size-4 text-status-ok" /> : <CircleX className="size-4 text-status-alarm" />}
                      {classLabel(r.predicted)}
                      <span className="text-xs text-muted-foreground">{ok ? 'верно' : 'мимо'}</span>
                    </span>
                  )}
                </TableCell>
              </TableRow>
            )
          })}
        </TableBody>
      </Table>
      <p className="px-4 pt-1.5 text-xs text-muted-foreground">
        Итог модели — класс с наибольшей средней вероятностью по всем решениям опыта (все узлы, все минуты). Первая тревога —
        от начала опыта до первого решения любого узла с вероятностью пожара не ниже порога; у помех это первая ложная
        тревога. Решения раз в минуту — точность до минуты.
      </p>
    </div>
  )
}

/**
 * Full offline check of a model: test, seen/unseen, the rule without a model, PR curve, quarters, types. An external
 * model (`external`) has neither the ТЗ requirement nor channel history: its check is by class and by experiment.
 */
export function OfflineReportCard({ report, external = false }: { report: OfflineReport; external?: boolean }) {
  const lt = report.lead_time
  const daily = report.model === 'daily'
  const targets = external ? NONE : TZ
  const experiments = readExperiments(report)
  const firesDetected = experiments.filter((e) => e.label === 'fire' && e.firstAlert !== null).length
  // Magdeburg wording (fire, nuisance) only where the model has these classes.
  const fireClass = !!report.by_class?.some((c) => c.class === 'fire')
  const scheme = report.eval_scheme ? (EVAL_SCHEME_LABEL[report.eval_scheme] ?? report.eval_scheme) : null
  return (
    <Card>
      <CardHeader>
        <CardTitle>
          Офлайн-проверка {report.version} · {report.period.label}
        </CardTitle>
        <CardDescription>
          {external ? (
            <>
              Порог {report.threshold.toFixed(3).replace('.', ',')}.{scheme && <> Проверка: {scheme}.</>} Требования ТЗ здесь не
              отмечены: они про прогноз неисправности канала, а у модели внешнего набора своя задача.
            </>
          ) : (
            <>Рабочий порог {report.threshold.toFixed(3).replace('.', ',')}, подобран до проверочного периода.</>
          )}{' '}
          Посчитано {fmtDateTimeShort(report.computed_at)}.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-6 px-0">
        <div className="grid gap-6 px-4 lg:grid-cols-[1fr_20rem]">
          <div>
            <div className="mb-1 text-sm font-medium">Precision — Recall на проверке</div>
            {report.pr_curve?.length ? (
              <PrCurveChart report={{ ...report, pr_curve: report.pr_curve }} requirement={!external} />
            ) : (
              <p className="text-sm text-muted-foreground">
                Кривой нет: модель решает правилом и нормами, без оценки, которую можно сдвигать порогом.
              </p>
            )}
          </div>
          <div className="space-y-3 text-sm">
            <div className="font-medium">{external ? 'Время до первой тревоги' : 'Упреждение верных тревог'}</div>
            {lt ? (
              <dl className="grid grid-cols-2 gap-x-3 gap-y-1">
                <dt className="text-muted-foreground">минимум</dt>
                <dd className="text-right">{fmtDuration(lt.min_s)}</dd>
                <dt className="text-muted-foreground">10%</dt>
                <dd className="text-right">{fmtDuration(lt.p10_s)}</dd>
                <dt className="text-muted-foreground">25%</dt>
                <dd className="text-right">{fmtDuration(lt.p25_s)}</dd>
                <dt className="font-medium">медиана</dt>
                <dd className="text-right font-medium">{fmtDuration(lt.p50_s)}</dd>
                <dt className="text-muted-foreground">75%</dt>
                <dd className="text-right">{fmtDuration(lt.p75_s)}</dd>
                <dt className="text-muted-foreground">90%</dt>
                <dd className="text-right">{fmtDuration(lt.p90_s)}</dd>
                <dt className="text-muted-foreground">максимум</dt>
                <dd className="text-right">{fmtDuration(lt.max_s)}</dd>
              </dl>
            ) : (
              <p className="text-muted-foreground">Верных тревог нет</p>
            )}
            <p className="text-xs text-muted-foreground">
              {external
                ? firesDetected
                  ? `От начала опыта с огнём до первой тревоги «пожар» любого узла, по ${fmtInt(firesDetected)} обнаруженным пожарам. Это скорость обнаружения, а не упреждение.`
                  : 'От начала события (опыта, поездки) до первой тревоги модели. Это скорость обнаружения, а не упреждение.'
                : daily
                  ? 'Горизонт — сутки 2–30: предупреждение заранее.'
                  : 'Минуты — это разбор тревоги, а не предупреждение за сутки.'}
            </p>
          </div>
        </div>

        <Table>
          <Head first="Выборка" />
          <TableBody>
            {report.fire_vs_nuisance && (
              <TableRow>
                <TableCell className="pl-4 font-medium">Пожар против помехи, решения во время опытов</TableCell>
                <BlockCells b={report.fire_vs_nuisance} targets={targets} />
              </TableRow>
            )}
            <TableRow>
              <TableCell className={external && report.fire_vs_nuisance ? 'pl-4' : 'pl-4 font-medium'}>
                {external ? (fireClass ? 'Пожар против фона и помех, все решения' : 'Все решения проверки') : 'Проверка, все каналы'}
              </TableCell>
              <BlockCells b={report.test} targets={targets} />
            </TableRow>
            {report.seen && (
              <TableRow>
                <TableCell className="pl-4">С историей <span className="text-xs text-muted-foreground">— {report.seen_rule}</span></TableCell>
                <BlockCells b={report.seen} />
              </TableRow>
            )}
            {report.unseen && (
              <TableRow>
                <TableCell className="pl-4">Без истории</TableCell>
                <BlockCells b={report.unseen} />
              </TableRow>
            )}
            {report.rule && (
              <TableRow className="text-muted-foreground">
                <TableCell className="pl-4">
                  {external ? (
                    <>
                      Правило разметки: {report.rule.name}
                      <div className="text-xs">его точность — определение метки, а не качество модели</div>
                    </>
                  ) : (
                    <>Правило без модели: {report.rule.name}</>
                  )}
                </TableCell>
                <BlockCells b={report.rule} targets={targets} />
              </TableRow>
            )}
          </TableBody>
        </Table>

        {report.by_episode_kind && report.by_episode_kind.length > 0 && <EpisodeKinds rows={report.by_episode_kind} />}

        {report.by_class && report.by_class.length > 0 && (
          <div>
            <div className="mb-1 px-4 text-sm font-medium">По классам</div>
            <Table>
              <Head first="Класс" />
              <TableBody>
                {report.by_class.map((c) => (
                  <TableRow key={c.class}>
                    <TableCell className="pl-4">{classLabel(c.class)}</TableCell>
                    <BlockCells b={c} targets={targets} />
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            <p className="px-4 pt-1.5 text-xs text-muted-foreground">
              Каждый класс — против остальных на всех решениях.
              {report.by_class.some((c) => c.class === 'nuisance') &&
                ' «Помеха» — дым и газы без огня (дым-машина, сварка, выхлоп и другие): модель учится не принимать их за пожар.'}
            </p>
          </div>
        )}

        {experiments.length > 0 && <Experiments rows={experiments} />}

        {report.by_quarter && (
        <div>
          <div className="mb-1 px-4 text-sm font-medium">По кварталам — стабильность во времени</div>
          <Table>
            <Head first="Квартал" />
            <TableBody>
              {report.by_quarter.map((q) => (
                <TableRow key={q.quarter}>
                  <TableCell className="pl-4">
                    <span className="font-medium">{q.quarter.replace('Q', ' · кв. ')}</span>{' '}
                    <StatusBadge status={ROLE[q.role].independent ? 'ok' : 'unknown'}>{ROLE[q.role].label}</StatusBadge>
                  </TableCell>
                  <BlockCells b={q} />
                </TableRow>
              ))}
            </TableBody>
          </Table>
          <p className="px-4 pt-1.5 text-xs text-muted-foreground">
            Кварталы «ранняя остановка» и «подбор порога» использовались при обучении — они не независимы от модели. Честная
            оценка — только «проверка».
          </p>
        </div>
        )}

        {report.by_type && (
          <div>
            <div className="mb-1 px-4 text-sm font-medium">По типам каналов</div>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="pl-4">Тип</TableHead>
                  <TableHead className="text-right">Доля отказов</TableHead>
                  <TableHead className="text-right">Решений</TableHead>
                  <TableHead className="text-right">Положит.</TableHead>
                  <TableHead>Precision</TableHead>
                  <TableHead>Recall</TableHead>
                  <TableHead className="pr-4 text-right">AP</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {report.by_type.map((t) => (
                  <TableRow key={t.sensor_type}>
                    <TableCell className="pl-4">
                      <span className="flex items-center gap-2">
                        <SensorTypeIcon type={t.sensor_type} withTooltip={false} />
                        {t.sensor_type}
                      </span>
                    </TableCell>
                    <TableCell className="text-right tabular-nums">{fmtPercent(t.base_rate, 2)}</TableCell>
                    <TableCell className="text-right tabular-nums">{fmtInt(t.decisions)}</TableCell>
                    <TableCell className="text-right tabular-nums">{fmtInt(t.positives)}</TableCell>
                    <TableCell>
                      {t.alerts ? <TargetMeter value={t.precision} target={REQUIREMENT.precision} /> : <span className="text-xs text-muted-foreground">тревог не было</span>}
                    </TableCell>
                    <TableCell>
                      <TargetMeter value={t.recall} target={REQUIREMENT.recall} />
                    </TableCell>
                    <TableCell className="pr-4 text-right tabular-nums">{num(t.ap)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </CardContent>
    </Card>
  )
}
