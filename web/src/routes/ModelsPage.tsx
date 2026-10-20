import { Bot, ChevronDown, CircleAlert, Database, GitCompareArrows, ShieldQuestion } from 'lucide-react'
import { useState } from 'react'

import { DatasetCard } from '@/components/emulator/DatasetCard'
import { PageInfo } from '@/components/layout/PageInfo'
import { DatasetBadge } from '@/components/domain/DatasetBadge'
import { QueryError } from '@/components/domain/QueryError'
import { SensorTypeIcon } from '@/components/domain/SensorTypeIcon'
import { StatusBadge } from '@/components/domain/StatusBadge'
import { AccessModelCard } from '@/components/metrics/AccessModelCard'
import { DailyRoutingCard } from '@/components/metrics/DailyRouting'
import { MetricsPanel } from '@/components/metrics/MetricsPanel'
import { experimentStats } from '@/components/metrics/experiments'
import { OfflineReportCard } from '@/components/metrics/OfflineReportCard'
import { TargetMeter } from '@/components/metrics/TargetMeter'
import { VersionsCompare } from '@/components/metrics/VersionsCompare'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { useDataset, useDatasets, useMetrics, useModelCompare, useModels } from '@/lib/api/queries'
import type { DailyRouting, ModelInfo, ModelName, OfflineMetrics } from '@/lib/api/types'
import { fmtInt, fmtPercent, plural } from '@/lib/format'
import {
  byVersion,
  chainOf,
  DATASET_DIRECTION_LABEL,
  EVAL_SCHEME_LABEL,
  historyNote,
  horizonLabel,
  MODEL_DESCRIPTION,
  MODEL_LABEL,
  triggerLabel,
  versionAuthor,
  versionLabel,
} from '@/lib/labels'
import { OFFLINE, REQUIREMENT, routingF1 } from '@/lib/offline-metrics'
import { useSearchState } from '@/lib/search-state'
import { cn } from '@/lib/utils'

const f1 = (v: number | null | undefined) => (v === null || v === undefined ? '—' : v.toFixed(3).replace('.', ','))

const schemeLabel = (s: string | null | undefined) => (s ? (EVAL_SCHEME_LABEL[s] ?? s) : '—')

/** The owner's chain (lct-95): its parts with their thresholds, in the order they decide. */
function ChainCard({ m, parts }: { m: ModelInfo; parts: ModelInfo[] }) {
  const chain = m.chain ?? chainOf(m.version) ?? []
  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-2">
          <GitCompareArrows className="size-4 text-muted-foreground" />
          {versionLabel(m.version)}
          <StatusBadge status="unknown">связка</StatusBadge>
          {m.active && <StatusBadge status="ok">основная новых прогонов</StatusBadge>}
          {!m.scoring && <StatusBadge status="alarm">части не готовы</StatusBadge>}
        </CardTitle>
        <CardDescription>{m.title || versionAuthor(m.version)}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-1 text-sm">
        {chain.map((v, i) => {
          const part = parts.find((p) => p.version === v)
          return (
            <div key={v} className="flex justify-between gap-3">
              <span className="text-muted-foreground">
                {i === 0 ? 'Решает' : 'Иначе'} {versionLabel(v)}
                {i === 0 ? ', где оценила' : ''}
              </span>
              <span className="font-mono">{part?.threshold != null ? `порог ${part.threshold.toFixed(3)}` : '—'}</span>
            </div>
          )
        })}
        <p className="pt-1 text-xs text-muted-foreground">
          Решение каждой части — на её пороге. В ленте и журнале видно, чьё решение дало вердикт и почему не первой.
        </p>
      </CardContent>
    </Card>
  )
}

function VersionCard({ m }: { m: ModelInfo }) {
  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-2">
          <Bot className="size-4 text-muted-foreground" />
          {m.version}
          {m.family === 'baseline' ? (
            <StatusBadge status="ppr">базовая модель команды</StatusBadge>
          ) : m.family === 'each_channel' ? (
            <StatusBadge status="unknown">отдельная модель типа</StatusBadge>
          ) : (
            <StatusBadge status="unknown">ML v3</StatusBadge>
          )}
          {/* A daily model works by the routing table (task C), not by being the active one. */}
          {m.routes ? (
            m.routes.length ? (
              <StatusBadge status="ok">в таблице маршрутизации</StatusBadge>
            ) : (
              <StatusBadge status="unknown">не в таблице</StatusBadge>
            )
          ) : (
            m.active && <StatusBadge status="ok">активна</StatusBadge>
          )}
          {!m.loaded && <StatusBadge status="alarm">не загружена</StatusBadge>}
          {m.loaded && !m.scoring && <StatusBadge status="alarm">не готова скорить</StatusBadge>}
        </CardTitle>
        <CardDescription>{m.title}</CardDescription>
      </CardHeader>
      <CardContent className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
        {m.author && (
          <>
            <span className="text-muted-foreground">Чья</span>
            <span className="text-right">{m.author}</span>
          </>
        )}
        {m.routes && (
          <>
            <span className="text-muted-foreground">Ведёт по таблице</span>
            <span className="text-right">{m.routes.length ? m.routes.join(', ') : 'ни одного типа'}</span>
          </>
        )}
        <span className="text-muted-foreground">Порог</span>
        <span className="text-right font-mono">{m.threshold === null ? 'не задан' : m.threshold.toFixed(3)}</span>
        <span className="text-muted-foreground">Признаков</span>
        <span className="text-right">{fmtInt(m.features)}</span>
        <span className="text-muted-foreground">Обучение</span>
        <span className="text-right">{m.trained_on}</span>
        <span className="text-muted-foreground">Горизонт</span>
        <span className="text-right font-mono text-xs">{m.horizon}</span>
        {m.sensor_types.length > 0 && (
          <>
            <span className="text-muted-foreground">Типы каналов</span>
            <span className="flex flex-wrap justify-end gap-1">
              {m.sensor_types.map((t) => (
                <SensorTypeIcon key={t} type={t} />
              ))}
            </span>
          </>
        )}
        {m.artifact_sha256 && (
          <>
            <span className="text-muted-foreground">sha256</span>
            <span className="truncate text-right font-mono text-xs" title={m.artifact_sha256}>
              {m.artifact_sha256.slice(0, 16)}…
            </span>
          </>
        )}
      </CardContent>
    </Card>
  )
}

/** A model on an external data set: which set, how it is called and checked — never mixed with the hackathon models. */
function ExternalModelCard({ m }: { m: ModelInfo }) {
  const d = useDataset(m.dataset)
  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-2">
          <Bot className="size-4 text-muted-foreground" />
          {m.model} · {m.version}
          {m.dataset && <DatasetBadge dataset={m.dataset} />}
          {m.active && <StatusBadge status="ok">активна</StatusBadge>}
          {!m.loaded && <StatusBadge status="alarm">не загружена</StatusBadge>}
        </CardTitle>
        <CardDescription>{m.title}</CardDescription>
      </CardHeader>
      <CardContent className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
        <span className="text-muted-foreground">Направление ТЗ</span>
        <span className="text-right">{d ? DATASET_DIRECTION_LABEL[d.direction] : '—'}</span>
        <span className="text-muted-foreground">Данные</span>
        <span className="text-right">{m.synthetic === true ? 'синтетические' : m.synthetic === false ? 'реальные измерения' : '—'}</span>
        <span className="text-muted-foreground">Вызов</span>
        <span className="text-right">{triggerLabel(m.trigger)}</span>
        <span className="text-muted-foreground">Горизонт</span>
        <span className="text-right">{horizonLabel(m.horizon)}</span>
        <span className="text-muted-foreground">Проверка</span>
        <span className="text-right">{schemeLabel(m.eval_scheme)}</span>
        <span className="text-muted-foreground">Порог</span>
        <span className="text-right font-mono">{m.threshold === null ? 'не задан' : m.threshold.toFixed(3)}</span>
        <span className="text-muted-foreground">Признаков</span>
        <span className="text-right">{fmtInt(m.features)}</span>
        <span className="text-muted-foreground">Обучение</span>
        <span className="text-right">{m.trained_on}</span>
        {m.artifact_sha256 && (
          <>
            <span className="text-muted-foreground">sha256</span>
            <span className="truncate text-right font-mono text-xs" title={m.artifact_sha256}>
              {m.artifact_sha256.slice(0, 16)}…
            </span>
          </>
        )}
      </CardContent>
    </Card>
  )
}

function ExternalModels({ models }: { models: ModelInfo[] }) {
  return (
    <div className="space-y-4">
      <p className="text-sm text-muted-foreground">
        Модели на открытых наборах других организаций — для направлений ТЗ, где у хакатона нет данных. Метрики не смешиваются с
        моделями на журнале СМВУ: другие данные и другая задача. Живые метрики появятся, когда модели начнут вызываться в
        прогонах наборов.
      </p>
      {models.map((m) => (
        <div key={`${m.model}-${m.version}`} className="space-y-4">
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            <ExternalModelCard m={m} />
          </div>
          {m.offline ? (
            <OfflineReportCard report={m.offline} external />
          ) : (
            <p className="text-sm text-muted-foreground">Офлайн-отчёта у модели нет.</p>
          )}
        </div>
      ))}
    </div>
  )
}

/** «Коротко о метриках» line of an external model: its main check, labelled as the data set's, not the hackathon's. */
function ExternalSummary({ m }: { m: ModelInfo }) {
  const d = useDataset(m.dataset)
  const o = m.offline
  if (!o) return null
  const main = o.fire_vs_nuisance ?? o.test
  const e = experimentStats(o)
  return (
    <li>
      Внешний набор {d?.short ?? m.dataset}, модель {m.model} {m.version}: Precision {fmtPercent(main.precision)}, Recall {fmtPercent(main.recall)} на{' '}
      {fmtInt(main.decisions)} решениях{o.fire_vs_nuisance ? ' (пожар против помехи)' : ''}; проверка — {schemeLabel(o.eval_scheme)}.
      {e && e.nuisances > 0 && (
        <>
          {' '}
          Ложные вызовы: хотя бы одна тревога «пожар» при {fmtInt(e.nuisancesAlerted)} из {fmtInt(e.nuisances)} помех.
        </>
      )}{' '}
      С моделями хакатона не сравнивается.
    </li>
  )
}

/** «Коротко о метриках» line of the event-model versions on the same decisions (15.3), when the ML service has it. */
function CompareSummary({ items }: { items: ModelInfo[] }) {
  const c = useModelCompare('smoke_alarm').data
  if (!c) return null
  const versions = Object.keys(c.versions).sort(byVersion)
  const pass = versions.every((v) => (c.versions[v].precision ?? 0) >= REQUIREMENT.precision && (c.versions[v].recall ?? 0) >= REQUIREMENT.recall)
  const primary = items.find((m) => m.model === 'smoke_alarm' && m.active)?.version
  const f = c.fallback?.half_year
  const chain = items.find((m) => m.model === 'smoke_alarm' && chainOf(m.version))?.version ?? 'v3>baseline-v0'
  const combo = f?.combo
  const comboPass = combo && (combo.precision ?? 0) >= REQUIREMENT.precision && (combo.recall ?? 0) >= REQUIREMENT.recall
  return (
    <>
      <li>
        Событийные {versions.map(versionLabel).join(' и ')} на одних решениях ({c.period.label}, {fmtInt(c.decisions)}):{' '}
        {versions.map((v) => `${versionLabel(v)} — P ${fmtPercent(c.versions[v].precision)} / R ${fmtPercent(c.versions[v].recall)}`).join(', ')}
        {pass ? ' — обе проходят пороги ТЗ' : ''}. Расходятся на {fmtInt(c.disagree.decisions)}{' '}
        {plural(c.disagree.decisions, ['решении', 'решениях', 'решениях'])}
        {c.coverage && `; ${versionLabel(c.coverage.version)} молчит ${c.coverage.silent_days} суток после дня без данных журнала`}.
        {primary && ` Основная по умолчанию — ${versionLabel(primary)}.`}
      </li>
      {combo && (
        <li>
          Связка {versionLabel(chain)} — v3, где она оценила, иначе v0 — на полугодии с неделей после пропуска журнала (
          {fmtInt(combo.decisions)} {plural(combo.decisions, ['решение', 'решения', 'решений'])}): P {fmtPercent(combo.precision)} / R {fmtPercent(combo.recall)}
          {comboPass ? ' — проходит пороги ТЗ' : ''}
          {f.combo_minus_v0 &&
            `; против v0 — P ${signedPp(f.combo_minus_v0.P.value)}, R ${signedPp(f.combo_minus_v0.R.value)}${f.combo_minus_v0.R.ci90[0] < 0 && f.combo_minus_v0.R.ci90[1] > 0 ? ' (разница по R не доказана)' : ''}`}
          {f.combo_minus_v3 && `; против v3 — R ${signedPp(f.combo_minus_v3.R.value)}: v0 закрывает неделю, когда v3 молчит`}.
        </li>
      )}
    </>
  )
}

/** «Коротко о метриках» line of the daily routing table (task C): who scores which types and the mix against v0 alone. */
function RoutingSummary({ routing, items }: { routing: DailyRouting | null | undefined; items: ModelInfo[] }) {
  const mix = routing?.offline?.routing
  if (!routing || !mix) return null
  const base = routing.offline?.v0_only
  // whose model leads each type — by the author in /models, without version names (owner's decision)
  const team = (m: string) => items.find((i) => i.model === 'daily' && i.version === m)?.author === 'команда'
  const teamTypes = routing.routes.filter((r) => team(r.model)).length
  const devTypes = routing.routes.filter((r) => !team(r.model)).map((r) => r.sensor_type)
  const none = routing.unrouted.length
  return (
    <li>
      Ежедневная модель — у каждого типа канала своя: у {teamTypes} {plural(teamTypes, ['типа', 'типов', 'типов'])} модель команды
      {devTypes.length > 0 && `, на типах ${devTypes.map((t) => `«${t}»`).join(' и ')} — модели ML-разработчика`}
      {none > 0 && `; у ${none} ${plural(none, ['типа', 'типов', 'типов'])} прогноза нет`}. Офлайн ({fmtInt(mix.decisions)} канал-дней,{' '}
      {routing.test?.label ?? 'офлайн'}): P {fmtPercent(mix.precision)} / R {fmtPercent(mix.recall)}
      {base && ` против ${fmtPercent(base.precision)} / ${fmtPercent(base.recall)} у одной модели команды`}
      {mix.caught_episodes != null && base?.caught_episodes != null && `; эпизодов «Неисправен» найдено ${fmtInt(mix.caught_episodes)} против ${fmtInt(base.caught_episodes)}`}
      ; горизонт ≥ 24 ч.
    </li>
  )
}

/** «Коротко о метриках» line of the event model: what the service decides, on its offline check. */
function EventSummary({ items }: { items: ModelInfo[] }) {
  const off = serviceOffline('smoke_alarm', items)
  if (!off) return null
  const pass = off.precision >= REQUIREMENT.precision && off.recall >= REQUIREMENT.recall
  return (
    <li>
      Событийная модель ({off.period}, {fmtInt(off.decisions)} {plural(off.decisions, ['решение', 'решения', 'решений'])}): P {fmtPercent(off.precision)} / R{' '}
      {fmtPercent(off.recall)}
      {pass ? ' — проходит пороги ТЗ' : ''}; упреждение — минуты: это триаж тревоги, а не предупреждение за сутки.
    </li>
  )
}

/** A difference of shares in percentage points with its sign: «+1,9 п.п.», «−3,5 п.п.». */
const signedPp = (v: number) => `${v > 0 ? '+' : v < 0 ? '−' : ''}${Math.abs(v * 100).toFixed(1).replace('.', ',')} п.п.`

/** Everything about the team's and the ML developer's models of a task — inside the collapsed comparison (owner's
 * decision: the interface shows what the service decides, the versions only here). */
function VersionsDetail({ model, versions, routing }: { model: ModelName; versions: ModelInfo[]; routing?: DailyRouting | null }) {
  const off = OFFLINE[model]
  const metrics = useMetrics(model)
  // Daily models the routing table does not pick (task C) — a line of names, not cards.
  const idle = versions.filter((v) => v.routes && !v.routes.length && !v.active)
  const cards = versions.filter((v) => !idle.includes(v))
  const mix = model === 'daily' ? routing?.offline?.routing : undefined
  // Versions the ML service serves with their own offline check (v0 and, since 15.1, v3); the static numbers of the
  // ML report — only for versions it does not serve (v2, daily v3).
  const served = versions.filter((v) => v.family !== 'external' && v.offline_metrics).sort((a, b) => byVersion(a.version, b.version))
  const staticRows = off.rows.filter((r) => !served.some((v) => v.version === r.version))
  const live = metrics.data?.all

  return (
    <div className="space-y-4">
      {model === 'smoke_alarm' && (
        <ul className="list-disc space-y-1.5 pl-4 text-sm">
          <CompareSummary items={versions} />
        </ul>
      )}
      {model === 'daily' && routing && (
        <DailyRoutingCard routing={routing} models={versions} live={metrics.data?.by_sensor_type} liveAll={live} />
      )}

      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        {cards.length ? (
          [...cards]
            .sort((a, b) => byVersion(a.version, b.version))
            .map((v) => (chainOf(v.version) ? <ChainCard key={`${v.model}-${v.version}`} m={v} parts={versions} /> : <VersionCard key={`${v.model}-${v.version}`} m={v} />))
        ) : (
          <p className="text-sm text-muted-foreground">Модель не загружена в ML-сервис</p>
        )}
      </div>
      {idle.length > 0 && (
        <p className="text-sm text-muted-foreground">
          Вне таблицы маршрутизации — ежедневный прогноз их не вызывает:{' '}
          {[...idle]
            .sort((a, b) => byVersion(a.version, b.version))
            .map((v) => `${v.title || v.version} (${v.version}${v.author ? `, ${v.author}` : ''}${v.scoring ? '' : ', не готова скорить'})`)
            .join('; ')}
          .
        </p>
      )}

      {model === 'smoke_alarm' && <VersionsCompare />}

      <Card>
        <CardHeader>
          <CardTitle>Качество: офлайн и живое</CardTitle>
          <CardDescription>
            Требование ТЗ — Precision &gt; {REQUIREMENT.precision * 100}%, Recall &gt; {REQUIREMENT.recall * 100}% (отметка на полосе). Каждая
            версия — на своём наборе решений проверки, наборы не совпадают{model === 'smoke_alarm' ? ': на одних решениях — в карточке выше' : ''}.
            {staticRows.length > 0 &&
              ` Офлайн ${staticRows
                .filter((r) => !chainOf(r.version))
                .map((r) => versionLabel(r.version))
                .join(' и ')} — ретроспективная проверка на 2026 из отчёта ML; 2026 использовался в исследовании, это не независимый финальный тест.`}
            {staticRows.some((r) => chainOf(r.version)) &&
              ' Связка — из отчёта сравнения (FALLBACK.md): на полугодии с неделей после пропуска журнала, где v3 молчит и решает v0.'}
            {mix && ' Смесь по таблице маршрутизации — на строках, общих с v0, из отчёта маршрутизации: так ежедневный прогноз работает сейчас.'}
          </CardDescription>
        </CardHeader>
        <CardContent className="overflow-x-auto px-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="pl-4">Версия</TableHead>
                <TableHead>Проверка</TableHead>
                <TableHead>Precision</TableHead>
                <TableHead>Recall</TableHead>
                <TableHead className="text-right">F1</TableHead>
                <TableHead className="text-right">AP</TableHead>
                <TableHead className="text-right">TP / FP</TableHead>
                <TableHead className="pr-4 text-right">Решений · положит.</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {served.map((b) => (
                <TableRow key={`served-${b.version}`}>
                  <TableCell className="pl-4 font-medium">
                    {b.version}
                    {b.family === 'baseline' && ' · базовая'}
                  </TableCell>
                  <TableCell className="text-muted-foreground">{b.offline_metrics!.period}</TableCell>
                  <TableCell>
                    <TargetMeter value={b.offline_metrics!.precision} target={REQUIREMENT.precision} />
                  </TableCell>
                  <TableCell>
                    <TargetMeter value={b.offline_metrics!.recall} target={REQUIREMENT.recall} />
                  </TableCell>
                  <TableCell className="text-right tabular-nums">{f1(b.offline_metrics!.f1)}</TableCell>
                  <TableCell className="text-right tabular-nums">{f1(b.offline_metrics!.ap)}</TableCell>
                  <TableCell className="text-right tabular-nums">
                    {fmtInt(b.offline_metrics!.tp)} / {fmtInt(b.offline_metrics!.fp)}
                  </TableCell>
                  <TableCell className="pr-4 text-right tabular-nums">
                    {fmtInt(b.offline_metrics!.decisions)} · {fmtInt(b.offline_metrics!.positives)}
                  </TableCell>
                </TableRow>
              ))}
              {mix && (
                <TableRow>
                  <TableCell className="pl-4 font-medium">смесь по таблице</TableCell>
                  <TableCell className="text-muted-foreground">{routing?.test?.label ?? '—'}</TableCell>
                  <TableCell>
                    <TargetMeter value={mix.precision} target={REQUIREMENT.precision} />
                  </TableCell>
                  <TableCell>
                    <TargetMeter value={mix.recall} target={REQUIREMENT.recall} />
                  </TableCell>
                  <TableCell className="text-right tabular-nums">{f1(routingF1(mix))}</TableCell>
                  <TableCell className="text-right text-muted-foreground">—</TableCell>
                  <TableCell className="text-right tabular-nums">
                    {fmtInt(mix.tp)} / {fmtInt(mix.fp)}
                  </TableCell>
                  <TableCell className="pr-4 text-right tabular-nums">
                    {fmtInt(mix.decisions)} · {fmtInt(mix.positives)}
                  </TableCell>
                </TableRow>
              )}
              {staticRows.map((r) => (
                <TableRow key={r.version}>
                  <TableCell className="pl-4 font-medium">{versionLabel(r.version)}</TableCell>
                  <TableCell className="text-muted-foreground">{r.period}</TableCell>
                  <TableCell>
                    <TargetMeter value={r.precision} target={REQUIREMENT.precision} />
                  </TableCell>
                  <TableCell>
                    <TargetMeter value={r.recall} target={REQUIREMENT.recall} />
                  </TableCell>
                  <TableCell className="text-right tabular-nums">{f1(r.f1)}</TableCell>
                  <TableCell className="text-right tabular-nums">{f1(r.ap)}</TableCell>
                  <TableCell className="text-right tabular-nums">
                    {fmtInt(r.tp)} / {fmtInt(r.fp)}
                  </TableCell>
                  <TableCell className="pr-4 text-right tabular-nums">
                    {fmtInt(r.decisions ?? off.decisions)} · {fmtInt(r.positives ?? off.positives)}
                  </TableCell>
                </TableRow>
              ))}
              <TableRow>
                <TableCell className="pl-4 font-medium">
                  {model === 'daily' && !metrics.data?.model_version ? 'смесь по таблице' : versionLabel(metrics.data?.model_version)} · живые
                </TableCell>
                <TableCell className="text-muted-foreground">
                  этот прогон, закрытые прогнозы
                  {live && metrics.data && metrics.data.history > 0 && <div className="text-xs">{historyNote(metrics.data.history, live.decisions)}</div>}
                </TableCell>
                <TableCell>
                  <TargetMeter value={live?.precision} target={REQUIREMENT.precision} />
                </TableCell>
                <TableCell>
                  <TargetMeter value={live?.recall} target={REQUIREMENT.recall} />
                </TableCell>
                <TableCell className="text-right tabular-nums">{f1(live?.f1)}</TableCell>
                <TableCell className="text-right text-muted-foreground">—</TableCell>
                <TableCell className="text-right tabular-nums">{live ? `${fmtInt(live.tp)} / ${fmtInt(live.fp)}` : '—'}</TableCell>
                <TableCell className="pr-4 text-right tabular-nums">{live ? `${fmtInt(live.decisions)} · ${fmtInt(live.positives)}` : '—'}</TableCell>
              </TableRow>
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      {versions
        .filter((v) => v.offline)
        .map((v) => (
          <OfflineReportCard key={`offline-${v.model}-${v.version}`} report={v.offline!} />
        ))}

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Упреждение и признаки v3</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2 text-sm">
            <p>
              <span className="text-muted-foreground">Упреждение офлайн: </span>
              {off.lead}.
            </p>
            <p>
              <span className="text-muted-foreground">Модель: </span>
              {off.features}.
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <CircleAlert className="size-4 text-status-risk" /> Ограничения v3
            </CardTitle>
          </CardHeader>
          <CardContent>
            <ul className="list-disc space-y-1.5 pl-4 text-sm">
              {off.notes.map((n) => (
                <li key={n}>{n}</li>
              ))}
            </ul>
          </CardContent>
        </Card>
      </div>

    </div>
  )
}

/** How the service decides a task — one line for the ⓘ of its card. */
const DEVICE: Record<ModelName, string> = {
  smoke_alarm:
    'Решает модель ML-разработчика, а где она тревогу не оценила (день без данных журнала, канал не в «Норме» и др.), — модель команды. В ленте и журнале — одна оценка и вердикт сервиса.',
  daily:
    'У каждого типа канала своя модель по таблице маршрутизации: модель ML-разработчика, где она проходит пороги и не хуже модели команды, иначе модель команды; у пяти типов прогноза нет.',
}

/** The task's final offline result — of what the service decides: the event model's chain, the daily routing mix. */
function serviceOffline(model: ModelName, items: ModelInfo[], routing?: DailyRouting | null): OfflineMetrics | null {
  if (model === 'daily') {
    const mix = routing?.offline?.routing
    if (!mix?.precision || mix.recall === null) return null
    const f = routingF1(mix) ?? 0
    return { period: routing?.test?.label ?? 'офлайн', precision: mix.precision, recall: mix.recall, f1: f, ap: null, tp: mix.tp, fp: mix.fp, decisions: mix.decisions, positives: mix.positives }
  }
  const chain = items.find((m) => m.model === 'smoke_alarm' && m.family === 'chain' && m.active)
  if (chain?.offline_metrics) return chain.offline_metrics
  const row = OFFLINE.smoke_alarm.rows.find((r) => chainOf(r.version))
  return row ? { ...row, decisions: row.decisions ?? OFFLINE.smoke_alarm.decisions, positives: row.positives ?? OFFLINE.smoke_alarm.positives } : null
}

/** A task as the service runs it: its final offline and live metrics; the versions — in the collapsed comparison. */
function ModelSection({ model, versions, items, routing }: { model: ModelName; versions: ModelInfo[]; items: ModelInfo[]; routing?: DailyRouting | null }) {
  const off = OFFLINE[model]
  const metrics = useMetrics(model)
  const live = metrics.data?.all
  const offline = serviceOffline(model, items, routing)
  const [open, setOpen] = useState(false)
  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-1">
            {MODEL_LABEL[model]} модель <PageInfo>{DEVICE[model]}</PageInfo>
          </CardTitle>
          <CardDescription>
            {MODEL_DESCRIPTION[model]}. Цель: {off.target.charAt(0).toLowerCase() + off.target.slice(1)}. Требование ТЗ — Precision
            &gt; {REQUIREMENT.precision * 100}%, Recall &gt; {REQUIREMENT.recall * 100}% (отметка на полосе).
          </CardDescription>
        </CardHeader>
        <CardContent className="overflow-x-auto px-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="pl-4">Проверка</TableHead>
                <TableHead>Precision</TableHead>
                <TableHead>Recall</TableHead>
                <TableHead className="text-right">F1</TableHead>
                <TableHead className="text-right">TP / FP</TableHead>
                <TableHead className="pr-4 text-right">Решений · положит.</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {offline && (
                <TableRow>
                  <TableCell className="pl-4">
                    <div className="font-medium">Офлайн</div>
                    <div className="text-xs text-muted-foreground">{offline.period}</div>
                  </TableCell>
                  <TableCell>
                    <TargetMeter value={offline.precision} target={REQUIREMENT.precision} />
                  </TableCell>
                  <TableCell>
                    <TargetMeter value={offline.recall} target={REQUIREMENT.recall} />
                  </TableCell>
                  <TableCell className="text-right tabular-nums">{f1(offline.f1)}</TableCell>
                  <TableCell className="text-right tabular-nums">
                    {fmtInt(offline.tp)} / {fmtInt(offline.fp)}
                  </TableCell>
                  <TableCell className="pr-4 text-right tabular-nums">
                    {fmtInt(offline.decisions)} · {fmtInt(offline.positives)}
                  </TableCell>
                </TableRow>
              )}
              <TableRow>
                <TableCell className="pl-4">
                  <div className="font-medium">Живые</div>
                  <div className="text-xs text-muted-foreground">
                    этот прогон, закрытые прогнозы
                    {live && metrics.data && metrics.data.history > 0 && <> · {historyNote(metrics.data.history, live.decisions)}</>}
                  </div>
                </TableCell>
                <TableCell>
                  <TargetMeter value={live?.precision} target={REQUIREMENT.precision} />
                </TableCell>
                <TableCell>
                  <TargetMeter value={live?.recall} target={REQUIREMENT.recall} />
                </TableCell>
                <TableCell className="text-right tabular-nums">{f1(live?.f1)}</TableCell>
                <TableCell className="text-right tabular-nums">{live ? `${fmtInt(live.tp)} / ${fmtInt(live.fp)}` : '—'}</TableCell>
                <TableCell className="pr-4 text-right tabular-nums">{live ? `${fmtInt(live.decisions)} · ${fmtInt(live.positives)}` : '—'}</TableCell>
              </TableRow>
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Живые метрики этого прогона</CardTitle>
          <CardDescription>По прогнозам с истёкшим горизонтом — те же, что в журнале прогнозов</CardDescription>
        </CardHeader>
        <CardContent>
          {metrics.isError ? (
            <QueryError error={metrics.error} onRetry={() => void metrics.refetch()} what="Живые метрики" compact />
          ) : (
            <MetricsPanel metrics={metrics.data} model={model} />
          )}
        </CardContent>
      </Card>

      {/* the team's and the ML developer's models — collapsed: the jury sees what the service decides */}
      <Card size="sm">
        <CardHeader>
          <Button variant="ghost" className="justify-start gap-2 px-0 font-semibold hover:bg-transparent" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
            <ChevronDown className={cn('transition-transform', !open && '-rotate-90')} />
            Сравнение моделей команды и ML-разработчика
          </Button>
          {!open && (
            <CardDescription>
              {model === 'smoke_alarm'
                ? 'Обе модели на одних решениях, расхождения, офлайн-отчёты и ограничения каждой.'
                : 'Какая модель ведёт какой тип канала, отчёты и ограничения каждой.'}
            </CardDescription>
          )}
        </CardHeader>
        {open && (
          <CardContent>
            <VersionsDetail model={model} versions={versions} routing={routing} />
          </CardContent>
        )}
      </Card>
    </div>
  )
}

// Models (docs/product/v1-scope.md, Should «страница модели»): versions, thresholds, offline v3, live metrics,
// and what we tell the jury about the metrics.
export function ModelsPage() {
  const s = useSearchState()
  const models = useModels()
  const datasets = useDatasets().data ?? []
  const items = models.data?.items ?? []
  const external = items.filter((v) => v.family === 'external')
  const access = items.filter((v) => v.family === 'access')
  const asked = s.get('model')
  const tab =
    asked === 'daily'
      ? 'daily'
      : asked === 'external' && external.length
        ? 'external'
        : asked === 'datasets' && datasets.length
          ? 'datasets'
          : asked === 'access' && access.length
            ? 'access'
            : 'smoke_alarm'

  return (
    <div className="mx-auto w-full max-w-7xl space-y-4">

      <Card>
        <CardHeader>
          <CardTitle>Коротко о метриках</CardTitle>
        </CardHeader>
        <CardContent>
          <ul className="list-disc space-y-1.5 pl-4 text-sm">
            <EventSummary items={items} />
            <RoutingSummary routing={models.data?.daily_routing} items={items} />
            {external.map((v) => (
              <ExternalSummary key={`${v.model}-${v.version}`} m={v} />
            ))}
            <li>Каналы без обучающей истории и нестабильность III квартала 2025 показываем открыто.</li>
            <li>Трактовку «горизонт ≥ 24 ч» уточняем у организаторов (вопрос Q4).</li>
          </ul>
        </CardContent>
      </Card>

      <Tabs value={tab} onValueChange={(v) => s.set({ model: v === 'daily' || v === 'external' || v === 'datasets' || v === 'access' ? v : null })}>
        <TabsList>
          <TabsTrigger value="smoke_alarm">{MODEL_LABEL.smoke_alarm}</TabsTrigger>
          <TabsTrigger value="daily">{MODEL_LABEL.daily}</TabsTrigger>
          {access.length > 0 && (
            <TabsTrigger value="access">
              <ShieldQuestion /> Доступ
            </TabsTrigger>
          )}
          {external.length > 0 && (
            <TabsTrigger value="external">
              <Database className="text-source-external" /> Внешние наборы
            </TabsTrigger>
          )}
          {datasets.length > 0 && (
            <TabsTrigger value="datasets">
              <Database className="text-source-external" /> Наборы данных
            </TabsTrigger>
          )}
        </TabsList>
        {models.isPending && <Skeleton className="h-64" />}
        {models.isError && (
          <QueryError error={models.error} onRetry={() => void models.refetch()} what="Модели из API" compact />
        )}
        {datasets.length > 0 && (
          <TabsContent value="datasets">
            <div className="space-y-3">
              <p className="text-sm text-muted-foreground">
                Открытые наборы других организаций для направлений ТЗ, где у хакатона нет данных. Это не данные Москоллектора: у
                прогона — своя плашка на всех экранах, свои объекты и модели. Сценарии наборов запускаются на экране «Эмулятор СМВУ».
              </p>
              <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
                {datasets.map((d) => (
                  <DatasetCard key={d.key} d={d} />
                ))}
              </div>
            </div>
          </TabsContent>
        )}
        {access.length > 0 && (
          <TabsContent value="access" className="space-y-3">
            {access.map((m) => (
              <AccessModelCard key={m.version} m={m} />
            ))}
          </TabsContent>
        )}
        {external.length > 0 && (
          <TabsContent value="external">
            <ExternalModels models={external} />
          </TabsContent>
        )}
        {(['smoke_alarm', 'daily'] as ModelName[]).map((m) => (
          <TabsContent key={m} value={m}>
            {/* Offline metrics are static: shown even when the API has no model list. */}
            {(models.data || models.isError) && (
              <ModelSection model={m} versions={items.filter((v) => v.model === m)} items={items} routing={models.data?.daily_routing} />
            )}
          </TabsContent>
        ))}
      </Tabs>
    </div>
  )
}
