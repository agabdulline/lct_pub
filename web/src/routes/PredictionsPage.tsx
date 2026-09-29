import { ScrollText } from 'lucide-react'

import { ChannelLink } from '@/components/domain/ChannelLink'
import { ObjectSelect } from '@/components/domain/ObjectSelect'
import { OutcomeBadge } from '@/components/domain/OutcomeBadge'
import { Pager } from '@/components/domain/Pager'
import { QueryError } from '@/components/domain/QueryError'
import { RiskMeter } from '@/components/domain/RiskMeter'
import { VersionScores } from '@/components/domain/VersionScores'
import { PredictionDecision } from '@/components/domain/ChainDecision'
import { ClassProbabilities, ClassWithoutAlert, TopReason } from '@/components/metrics/ClassProbabilities'
import { ExternalMetricsPanel, MetricsPanel } from '@/components/metrics/MetricsPanel'
import { PredictionPanel } from '@/components/metrics/PredictionPanel'
import { classesOf } from '@/components/metrics/prediction-context'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from '@/components/ui/empty'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { useExternalModels, useMetrics, useModels, useOfflineRef, usePredictions } from '@/lib/api/queries'
import type { ModelInfo, ModelName, ModelScore, Prediction } from '@/lib/api/types'
import { fmtDateTimeShort, fmtSince } from '@/lib/format'
import { byVersion, chainOf, externalHorizonHours, MODEL_DESCRIPTION, MODEL_LABEL, skipReasonLabel, triggerLabel, versionAuthor, versionLabel } from '@/lib/labels'
import { closingNow, inMetrics } from '@/lib/outcome'
import { useRun } from '@/lib/run'
import { useSearchState } from '@/lib/search-state'
import { cn } from '@/lib/utils'

const PAGE_SIZE = 50

type Option = { value: string; label: string }
const ALERTS: Option[] = [
  { value: 'all', label: 'Все решения' },
  { value: 'true', label: 'С тревогой' },
  { value: 'false', label: 'Без тревоги' },
]
const OUTCOMES: Option[] = [
  { value: 'all', label: 'Любой исход' },
  { value: 'fault', label: 'Отказ был' },
  { value: 'no_fault', label: 'Отказа не было' },
  { value: 'open', label: 'Ожидается' },
]
// An external model's outcome is the data set's label at the moment of decision.
const OUTCOMES_EXTERNAL: Option[] = [
  { value: 'all', label: 'Любой исход' },
  { value: 'fault', label: 'По разметке было' },
  { value: 'no_fault', label: 'По разметке не было' },
  { value: 'open', label: 'Без разметки' },
]
// An external forecaster (Azure PdM): the outcome is a failure label within the horizon, open until it ends.
const OUTCOMES_FORECAST: Option[] = [
  { value: 'all', label: 'Любой исход' },
  { value: 'fault', label: 'Отказ по разметке был' },
  { value: 'no_fault', label: 'Отказа не было' },
  { value: 'open', label: 'Ожидается' },
]
// Stage 15: decisions where the event-model versions disagree.
const COMPARE: Option[] = [
  { value: 'all', label: 'Любые оценки моделей' },
  { value: 'disagree', label: 'Модели расходятся' },
]
const HISTORY: Option[] = [
  { value: 'all', label: 'Любая история' },
  { value: 'true', label: 'Есть история объекта' },
  { value: 'false', label: 'Нет истории объекта' },
]

function FilterSelect({ options, value, onChange, label, className }: { options: Option[]; value: string; onChange: (v: string) => void; label: string; className?: string }) {
  return (
    <Select items={options} value={value} onValueChange={(v) => onChange(v ?? 'all')}>
      <SelectTrigger size="sm" className={cn('w-40', className)} aria-label={label}>
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

const HACKATHON: ModelName[] = ['smoke_alarm', 'daily']
/** The whole daily forecast — every model of the routing table (task C). */
const MIX = 'mix'

/** The row's own decision as a score, to show it among the other versions' ones. */
const ownScore = (p: Prediction): ModelScore => ({
  model_version: p.model_version,
  prediction_id: p.id,
  status: p.status,
  score: p.score,
  threshold: p.threshold,
  alert: p.alert,
  primary: false,
  inherited: false,
  skip_reason: p.skip_reason,
})

/** Both versions scored and exactly one is above its threshold; `null` — one of them skipped. */
function disagrees(p: Prediction): boolean | null {
  if (!p.paired?.length) return null
  if (p.status !== 'scored' || p.paired.some((o) => o.status !== 'scored')) return null
  return p.paired.some((o) => o.alert !== p.alert)
}

/** Name of an external model in the switch: its task title from /models, else the task key. */
const externalName = (m: ModelInfo) => m.title || m.model

// Forecast journal and metrics (docs/product/v1-scope.md, экран 5). Metrics are per model and never mixed:
// the event model's numbers are not the numbers of all faults (AGENTS.md §5). A run of an external data set has only
// the data set's models: their decisions are detections checked against the data set's labels.
export function PredictionsPage() {
  const s = useSearchState()
  const { run } = useRun()
  const dataset = run?.dataset ?? null
  const externals = useExternalModels(dataset)
  const models = useModels()
  const modelsPending = models.isPending
  const asked = s.get('model')
  const externalModel = dataset ? (externals.find((m) => m.model === asked) ?? externals[0] ?? null) : null
  const model = dataset ? (externalModel?.model ?? null) : ((asked === 'daily' ? 'daily' : 'smoke_alarm') as ModelName)
  const hackathonModel: ModelName = model === 'daily' ? 'daily' : 'smoke_alarm'
  // Stage 15: the event model is scored by several versions; the journal shows one of them, the primary by default.
  // A chain primary (lct-95) is chosen as a whole — its decisions, one per alarm — or by each of its parts.
  const chain = chainOf(run?.primary_model)
  const versions =
    model === 'smoke_alarm' && run?.primary_model ? [...new Set([run.primary_model, ...(chain ?? []), ...(run.shadow_models ?? [])])].sort(byVersion) : []
  const askedVersion = s.get('version')
  const version = versions.length > 1 ? (askedVersion && versions.includes(askedVersion) ? askedVersion : (run?.primary_model ?? versions[0])) : null
  const compare = versions.length > 1 && s.get('compare') === 'disagree'
  // Task C: the daily forecast is a mix of models by the routing table — all of them by default, or one.
  const dailyModels = model === 'daily' ? (models.data?.items ?? []).filter((m) => m.model === 'daily' && m.routes?.length) : []
  const dailyVersions = dailyModels.map((m) => m.version).sort(byVersion)
  const dailyVersion = askedVersion && dailyVersions.includes(askedVersion) ? askedVersion : null
  const shownVersion = version ?? dailyVersion
  const alert = s.get('alert') ?? 'all'
  const outcome = s.get('outcome') ?? 'all'
  const history = s.get('history') ?? 'all'
  const objectId = s.num('object')
  const page = s.num('page') ?? 1
  const selected = s.num('prediction')

  const metrics = useMetrics(model, shownVersion ? { model_version: shownVersion } : {})
  // rows in the metrics — by the same «now» as the card: the run's clock or the end of the journal recount
  const closing = closingNow(run, metrics.data?.outcomes_until)
  // An external detector (Magdeburg, VAST) judges the moment of decision; a forecaster (Azure PdM) has a horizon.
  const horizonHours = externalModel ? externalHorizonHours(externalModel, metrics.data) : 0
  const forecast = !!externalModel && horizonHours > 0
  const detector = dataset !== null && !forecast
  const offline = useOfflineRef(hackathonModel, shownVersion ?? metrics.data?.model_version ?? null)
  const list = usePredictions(
    {
      model,
      page,
      size: PAGE_SIZE,
      alert: alert === 'all' ? null : alert,
      outcome: outcome === 'all' ? null : outcome,
      object_history: history === 'all' || dataset ? null : history,
      object_id: objectId,
      model_version: shownVersion,
      disagree: compare ? 'true' : null,
    },
    model !== null,
  )
  const data = list.data
  const external = dataset !== null
  // Magdeburg gives class probabilities, VAST gives reasons (factors): the column shows what the model has.
  const withClasses = !!data?.items.some((p) => classesOf(p.context).length > 0)

  return (
    <div className={cn('mx-auto w-full max-w-7xl space-y-4', selected !== null && '2xl:mr-[26rem] 2xl:w-[calc(100%-26rem)]')}>
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Журнал прогнозов</h1>
          <p className="text-sm text-muted-foreground">Все решения моделей с фактическим исходом — живая проверка Precision и Recall.</p>
        </div>
        {external ? (
          externals.length > 1 && (
            <ToggleGroup value={model ? [model] : []} onValueChange={(v) => v[0] && s.set({ model: v[0] })} variant="outline">
              {externals.map((m) => (
                <ToggleGroupItem key={m.model} value={m.model}>
                  {externalName(m)}
                </ToggleGroupItem>
              ))}
            </ToggleGroup>
          )
        ) : (
          <div className="flex flex-wrap items-center gap-2">
            {version && (
              <ToggleGroup
                value={[version]}
                onValueChange={(v) => v[0] && s.set({ version: v[0] === run?.primary_model ? null : v[0] })}
                variant="outline"
                aria-label="Версия событийной модели"
              >
                {versions.map((v) => (
                  <ToggleGroupItem key={v} value={v} title={versionAuthor(v)}>
                    {versionLabel(v)}
                    {v === run?.primary_model && <span className="text-xs text-muted-foreground">основная</span>}
                  </ToggleGroupItem>
                ))}
              </ToggleGroup>
            )}
            {dailyVersions.length > 1 && (
              <ToggleGroup
                value={[dailyVersion ?? MIX]}
                onValueChange={(v) => v[0] && s.set({ version: v[0] === MIX ? null : v[0] })}
                variant="outline"
                aria-label="Модель ежедневного прогноза"
              >
                <ToggleGroupItem value={MIX} title="Все модели таблицы маршрутизации, каждая — на своих типах каналов">
                  по таблице
                </ToggleGroupItem>
                {dailyVersions.map((v) => {
                  const m = dailyModels.find((d) => d.version === v)
                  return (
                    <ToggleGroupItem key={v} value={v} title={[m?.author, m?.routes?.join(', ')].filter(Boolean).join(': ')}>
                      {versionLabel(v)}
                    </ToggleGroupItem>
                  )
                })}
              </ToggleGroup>
            )}
            <ToggleGroup
              value={[hackathonModel]}
              onValueChange={(v) => v[0] && s.set({ model: v[0] === 'daily' ? 'daily' : null, version: null, compare: null })}
              variant="outline"
            >
              {HACKATHON.map((m) => (
                <ToggleGroupItem key={m} value={m}>
                  {MODEL_LABEL[m]}
                </ToggleGroupItem>
              ))}
            </ToggleGroup>
          </div>
        )}
      </div>

      {external && !externalModel ? (
        <Card>
          <CardContent>
            {modelsPending ? (
              <Skeleton className="h-24" />
            ) : (
              <p className="text-sm text-muted-foreground">
                Для этого набора в ML-сервисе нет модели — в прогоне только события набора, прогнозов нет.
              </p>
            )}
          </CardContent>
        </Card>
      ) : (
        <Card>
          <CardHeader>
            {externalModel ? (
              <>
                <CardTitle>
                  Метрики — {externalName(externalModel)} {metrics.data?.model_version ? `· ${metrics.data.model_version}` : ''}
                </CardTitle>
                <CardDescription>
                  Модель внешнего набора: решение {triggerLabel(externalModel.trigger)},{' '}
                  {forecast ? `исход — начался ли отказ по разметке набора в ближайшие ${horizonHours} ч.` : 'исход — разметка набора в момент решения.'}{' '}
                  Метрики не смешиваются с моделями на журнале СМВУ.
                </CardDescription>
              </>
            ) : (
              <>
                <CardTitle>
                  Метрики — {MODEL_LABEL[hackathonModel].toLowerCase()} модель{' '}
                  {metrics.data?.model_version ? `· ${versionLabel(metrics.data.model_version)}` : model === 'daily' && dailyVersions.length ? '· смесь по таблице' : ''}
                </CardTitle>
                <CardDescription>
                  {MODEL_DESCRIPTION[hackathonModel]}. Только прогнозы с истёкшим горизонтом. Упреждение офлайн {offline.version}:{' '}
                  {offline.lead}.
                </CardDescription>
              </>
            )}
          </CardHeader>
          <CardContent>
            {metrics.isError ? (
              <QueryError error={metrics.error} onRetry={() => void metrics.refetch()} what="Метрики" compact />
            ) : externalModel ? (
              <ExternalMetricsPanel metrics={metrics.data} model={externalModel} />
            ) : (
              <MetricsPanel metrics={metrics.data} model={hackathonModel} />
            )}
          </CardContent>
        </Card>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <ObjectSelect value={objectId} onChange={(id) => s.set({ object: id })} className="w-56" />
        <FilterSelect label="Тревога" options={ALERTS} value={alert} onChange={(v) => s.set({ alert: v === 'all' ? null : v })} />
        <FilterSelect
          label="Исход"
          options={forecast ? OUTCOMES_FORECAST : external ? OUTCOMES_EXTERNAL : OUTCOMES}
          value={outcome}
          onChange={(v) => s.set({ outcome: v === 'all' ? null : v })}
          className={external ? 'w-48' : undefined}
        />
        {versions.length > 1 && (
          <FilterSelect label="Сравнение моделей" options={COMPARE} value={compare ? 'disagree' : 'all'} onChange={(v) => s.set({ compare: v === 'all' ? null : v })} className="w-52" />
        )}
        {model === 'smoke_alarm' && (
          <FilterSelect label="История объекта" options={HISTORY} value={history} onChange={(v) => s.set({ history: v === 'all' ? null : v })} className="w-56" />
        )}
      </div>

      <Card className="py-0">
        <CardContent className="px-0">
          {list.isPending && model !== null && <Skeleton className="m-4 h-80" />}
          {list.isError && <QueryError error={list.error} onRetry={() => void list.refetch()} what="Журнал прогнозов" />}
          {data && data.items.length === 0 && (
            <Empty>
              <EmptyHeader>
                <EmptyMedia variant="icon">
                  <ScrollText />
                </EmptyMedia>
                <EmptyTitle>Прогнозов нет</EmptyTitle>
                <EmptyDescription>С такими фильтрами решений модели не было.</EmptyDescription>
              </EmptyHeader>
            </Empty>
          )}
          {data && data.items.length > 0 && (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="pl-4">Решение</TableHead>
                  <TableHead>Канал</TableHead>
                  <TableHead>Объект</TableHead>
                  <TableHead>Оценка</TableHead>
                  {version && <TableHead>Другая модель</TableHead>}
                  {detector ? <TableHead>{withClasses ? 'Классы' : 'Причина'}</TableHead> : <TableHead>Горизонт до</TableHead>}
                  <TableHead>Исход</TableHead>
                  {!detector && <TableHead className="text-right">Упреждение</TableHead>}
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.items.map((p) => (
                  <TableRow
                    key={p.id}
                    className="cursor-pointer"
                    data-state={selected === p.id ? 'selected' : undefined}
                    onClick={() => s.set({ prediction: p.id }, true)}
                  >
                    <TableCell className="pl-4 font-mono text-xs tabular-nums">{fmtDateTimeShort(p.decision_time)}</TableCell>
                    <TableCell className="max-w-56">
                      <ChannelLink id={p.channel_id} name={p.channel_name} sensorType={p.sensor_type} />
                      {p.object_history === false && <div className="text-xs text-muted-foreground">нет истории объекта</div>}
                    </TableCell>
                    <TableCell className="max-w-44 truncate">{p.object_name}</TableCell>
                    <TableCell>
                      {p.status === 'scored' ? (
                        <RiskMeter value={p.score} threshold={p.threshold} />
                      ) : (
                        <span className="text-xs text-muted-foreground">{p.skip_reason ? skipReasonLabel(p.skip_reason) : 'пропуск'}</span>
                      )}
                      {chain && chainOf(version) && <PredictionDecision prediction={p} chain={chain} short />}
                      {dailyVersions.length > 1 && !dailyVersion && (
                        <div className="text-xs text-muted-foreground" title={versionAuthor(p.model_version)}>
                          {versionLabel(p.model_version)}
                        </div>
                      )}
                    </TableCell>
                    {version && (
                      <TableCell>
                        <VersionScores scores={[ownScore(p), ...(p.paired ?? [])]} current={p.model_version} disagree={disagrees(p)} />
                      </TableCell>
                    )}
                    {detector ? (
                      <TableCell className="max-w-72">
                        {withClasses ? (
                          <>
                            <ClassProbabilities context={p.context} />
                            <ClassWithoutAlert prediction={p} />
                          </>
                        ) : (
                          <TopReason factors={p.factors} />
                        )}
                      </TableCell>
                    ) : (
                      <TableCell className="text-xs text-muted-foreground tabular-nums">{fmtDateTimeShort(p.horizon_end)}</TableCell>
                    )}
                    <TableCell>
                      <OutcomeBadge
                        outcome={p.outcome}
                        alert={p.alert}
                        outcomeAt={p.outcome_at}
                        horizonEnd={p.horizon_end}
                        source={p.outcome_source}
                        external={detector}
                        counted={inMetrics(p, closing)}
                      />
                    </TableCell>
                    {!detector && (
                      <TableCell className="text-right text-xs tabular-nums">
                        {p.outcome === 'fault' && p.alert && p.outcome_at ? fmtSince(p.decision_time, p.outcome_at) : '—'}
                      </TableCell>
                    )}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
      {data && data.total > PAGE_SIZE && (
        <Pager page={page} size={PAGE_SIZE} total={data.total} onPage={(p) => s.set({ page: p > 1 ? p : null }, true)} noun="прогнозов" />
      )}

      <PredictionPanel id={selected} onClose={() => s.set({ prediction: null }, true)} />
    </div>
  )
}
