import { MetricCard } from '@/components/domain/MetricCard'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { useConfirmedAlerts, useOfflineRef } from '@/lib/api/queries'
import type { Metrics, MetricsBlock, ModelInfo, ModelName } from '@/lib/api/types'
import { fmtDateTime, fmtDuration, fmtInt, fmtPercent } from '@/lib/format'
import { externalHorizonHours, historyNote, triggerLabel } from '@/lib/labels'
import { REQUIREMENT } from '@/lib/offline-metrics'
import { cn } from '@/lib/utils'

const f1 = (v: number | null) => (v === null ? '—' : v.toFixed(3).replace('.', ','))

type MatrixWords = { yes: string; no: string; tp: string; fp: string; fn: string; tn: string }

const FAULT_WORDS: MatrixWords = {
  yes: 'Отказ был',
  no: 'Отказа не было',
  tp: 'Модель предупредила, отказ случился',
  fp: 'Модель предупредила, отказа не было',
  fn: 'Модель промолчала, отказ случился',
  tn: 'Модель промолчала, отказа не было',
}

// An external model: the outcome is the data set's label at the moment of decision.
const LABEL_WORDS: MatrixWords = {
  yes: 'По разметке было',
  no: 'По разметке не было',
  tp: 'Модель подала тревогу, по разметке событие шло',
  fp: 'Модель подала тревогу, по разметке события не было',
  fn: 'Модель промолчала, по разметке событие шло',
  tn: 'Модель промолчала, по разметке события не было',
}

// An external forecasting model (Azure PdM): the outcome is a failure label starting within the horizon.
const FORECAST_WORDS: MatrixWords = {
  yes: 'Отказ по разметке был',
  no: 'Отказа не было',
  tp: 'Модель подала тревогу, по разметке отказ начался в горизонте',
  fp: 'Модель подала тревогу, по разметке отказа в горизонте не было',
  fn: 'Модель промолчала, по разметке отказ начался в горизонте',
  tn: 'Модель промолчала, по разметке отказа в горизонте не было',
}

function Matrix({ m, words = FAULT_WORDS }: { m: MetricsBlock; words?: MatrixWords }) {
  const cell = (label: string, n: number, cls: string, hint: string) => (
    <div className={cn('rounded-lg p-2.5 ring-1 ring-foreground/10', cls)} title={hint}>
      <div className="text-lg font-semibold">{fmtInt(n)}</div>
      <div className="text-xs text-muted-foreground">{label}</div>
    </div>
  )
  return (
    <div className="grid grid-cols-[auto_1fr_1fr] items-center gap-1.5 text-sm">
      <span />
      <span className="text-center text-xs text-muted-foreground">{words.yes}</span>
      <span className="text-center text-xs text-muted-foreground">{words.no}</span>
      <span className="pr-2 text-right text-xs text-muted-foreground">Тревога</span>
      {cell('верно (TP)', m.tp, 'bg-status-ok/10', words.tp)}
      {cell('ложные (FP)', m.fp, 'bg-status-risk/10', words.fp)}
      <span className="pr-2 text-right text-xs text-muted-foreground">Нет тревоги</span>
      {cell('пропуски (FN)', m.fn, 'bg-status-alarm/10', words.fn)}
      {cell('верно (TN)', m.tn, 'bg-muted/40', words.tn)}
    </div>
  )
}

function BlockRow({ label, m }: { label: string; m: MetricsBlock }) {
  return (
    <TableRow>
      <TableCell>{label}</TableCell>
      <TableCell className="text-right tabular-nums">{fmtInt(m.decisions)}</TableCell>
      <TableCell className="text-right tabular-nums">{fmtInt(m.positives)}</TableCell>
      <TableCell className="text-right tabular-nums">{fmtInt(m.alerts)}</TableCell>
      <TableCell className="text-right tabular-nums">{fmtPercent(m.precision)}</TableCell>
      <TableCell className="text-right tabular-nums">{fmtPercent(m.recall)}</TableCell>
      <TableCell className="text-right tabular-nums">{f1(m.f1)}</TableCell>
    </TableRow>
  )
}

/** Live metrics of one model over closed forecasts, with the denominators and the offline reference. */
export function MetricsPanel({ metrics, model }: { metrics: Metrics | undefined; model: ModelName }) {
  const m = metrics?.all
  // The version the metrics are of (the run's primary one by default): the reference and the bound are of it too.
  const version = metrics?.model_version ?? null
  const off = useOfflineRef(model, version)
  const confirmed = useConfirmedAlerts(model, version)
  // Nothing closed yet, but alerts already confirmed by faults: show the lower bound instead of a dash.
  const bound = m && m.precision === null && confirmed && confirmed.confirmed > 0 ? confirmed.confirmed / confirmed.all : null
  const lowN = m && m.decisions > 0 && m.positives < 20
  const meets = (v: number | null | undefined, req: number) => (v === null || v === undefined ? undefined : v >= req ? 'ok' : 'risk')

  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6">
        <MetricCard
          title="Закрытых решений"
          value={fmtInt(m?.decisions)}
          loading={!metrics}
          hint={
            metrics &&
            [
              metrics.open ? `ещё открыто ${fmtInt(metrics.open)} — горизонт не истёк${metrics.open_fault ? `; у ${fmtInt(metrics.open_fault)} неисправность уже началась` : ''}` : null,
              m ? historyNote(metrics.history, m.decisions) : null,
            ]
              .filter(Boolean)
              .join('; ') || 'открытых нет'
          }
        />
        <MetricCard title="Положительных" value={fmtInt(m?.positives)} loading={!metrics} hint="отказ случился в горизонте" />
        {bound !== null && confirmed ? (
          <MetricCard
            title="Precision, не ниже"
            value={fmtPercent(bound)}
            tone={bound >= REQUIREMENT.precision ? 'ok' : undefined}
            hint={`${fmtInt(confirmed.confirmed)} из ${fmtInt(confirmed.all)} тревог уже подтвердились отказом; точное значение — после конца горизонта · требование > ${REQUIREMENT.precision * 100}%`}
          />
        ) : (
          <MetricCard
            title="Precision"
            value={fmtPercent(m?.precision)}
            tone={meets(m?.precision, REQUIREMENT.precision)}
            loading={!metrics}
            hint={`${m && m.decisions > 0 && m.alerts === 0 ? 'тревог модели не было · ' : ''}требование > ${REQUIREMENT.precision * 100}% · офлайн ${fmtPercent(off.precision)}`}
          />
        )}
        <MetricCard
          title="Recall"
          value={fmtPercent(m?.recall)}
          tone={meets(m?.recall, REQUIREMENT.recall)}
          loading={!metrics}
          hint={`требование > ${REQUIREMENT.recall * 100}% · офлайн ${fmtPercent(off.recall)}`}
        />
        <MetricCard title="F1" value={f1(m?.f1 ?? null)} loading={!metrics} hint={`офлайн ${f1(off.f1)}`} />
        <MetricCard
          title="Упреждение, медиана"
          value={fmtDuration(m?.lead_time?.p50_s)}
          loading={!metrics}
          hint={m?.lead_time ? `10–90%: ${fmtDuration(m.lead_time.p10_s)} — ${fmtDuration(m.lead_time.p90_s)}` : 'нет верных тревог'}
        />
      </div>
      {metrics?.note && <p className="text-xs text-muted-foreground">{metrics.note}</p>}
      {metrics && m && metrics.history > 0 && (
        <p className="text-xs text-muted-foreground">
          Досчитано по журналу после конца прогона: {fmtInt(metrics.history)} из {fmtInt(m.decisions)} закрытых решений. Горизонт
          коротких прогонов выходит за их часы — исход взят из фактических событий журнала
          {metrics.outcomes_until ? ` (данные до ${fmtDateTime(metrics.outcomes_until)})` : ''}. В журнале такие исходы помечены «по журналу».
        </p>
      )}
      {lowN && (
        <p className="text-sm text-status-risk">
          Положительных меньше 20 — живые Precision и Recall пока неустойчивы, сравнивайте с офлайн-отчётом осторожно.
        </p>
      )}
      {metrics && m && m.decisions === 0 && (
        <p className="rounded-lg bg-muted/40 p-3 text-sm text-muted-foreground">
          Закрытых прогнозов пока нет: исход известен только после конца горизонта
          {model === 'daily' ? ' — у ежедневной модели это до 30 суток, в коротком прогоне живых метрик не будет' : ''}. Ориентир —
          офлайн-отчёт: Precision {fmtPercent(off.precision)}, Recall {fmtPercent(off.recall)} на{' '}
          {fmtInt(off.decisions)} решениях ({off.decisionsLabel}).
        </p>
      )}
      {metrics && m && m.decisions > 0 && (
        <div className="grid gap-4 lg:grid-cols-[20rem_1fr]">
          <Matrix m={m} />
          <div className="overflow-x-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Каналы</TableHead>
                  <TableHead className="text-right">Решений</TableHead>
                  <TableHead className="text-right">Положит.</TableHead>
                  <TableHead className="text-right">Тревог</TableHead>
                  <TableHead className="text-right">Precision</TableHead>
                  <TableHead className="text-right">Recall</TableHead>
                  <TableHead className="text-right">F1</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                <BlockRow label="Канал был в обучении" m={metrics.seen} />
                <BlockRow label="Канал не был в обучении" m={metrics.unseen} />
                <BlockRow label="Все" m={metrics.all} />
                <TableRow className="text-muted-foreground hover:bg-transparent">
                  <TableCell>Офлайн, {off.period}</TableCell>
                  <TableCell className="text-right tabular-nums">{fmtInt(off.decisions)}</TableCell>
                  <TableCell className="text-right tabular-nums">{fmtInt(off.positives)}</TableCell>
                  <TableCell className="text-right tabular-nums">{fmtInt(off.alerts)}</TableCell>
                  <TableCell className="text-right tabular-nums">{fmtPercent(off.precision)}</TableCell>
                  <TableCell className="text-right tabular-nums">{fmtPercent(off.recall)}</TableCell>
                  <TableCell className="text-right tabular-nums">{f1(off.f1)}</TableCell>
                </TableRow>
              </TableBody>
            </Table>
          </div>
        </div>
      )}
    </div>
  )
}

/**
 * Live metrics of an external model in a data set run, the outcome by the data set's labels. A detector (Magdeburg,
 * VAST) judges the moment of decision: no lead time, a decision without a label has no outcome. A forecaster (Azure
 * PdM, «failure within 24 h») has a horizon: the outcome is a failure label starting within it, the lead time is to
 * that start, and a decision stays open until the horizon ends. No ТЗ requirement either way (another data set and
 * task); the reference is the model's own offline check on all decisions.
 */
export function ExternalMetricsPanel({ metrics, model }: { metrics: Metrics | undefined; model: ModelInfo | undefined }) {
  const m = metrics?.all
  const off = model?.offline
  const ref = off?.test
  const lowN = m && m.decisions > 0 && m.positives < 20
  const horizon = externalHorizonHours(model, metrics)
  const forecast = horizon > 0
  const openHint = metrics?.open
    ? forecast
      ? `ещё ${fmtInt(metrics.open)} — горизонт ${fmtInt(horizon)} ч не истёк к концу прогона${metrics.open_fault ? `, у ${fmtInt(metrics.open_fault)} отказ уже начался` : ''}; в метрики не входят`
      : `ещё ${fmtInt(metrics.open)} — в интервалах без разметки, в метрики не входят`
    : `решение ${triggerLabel(model?.trigger)}`
  return (
    <div className="space-y-4">
      <div className={cn('grid gap-3 sm:grid-cols-2 lg:grid-cols-3', forecast ? 'xl:grid-cols-6' : 'xl:grid-cols-5')}>
        <MetricCard title={forecast ? 'Закрытых решений' : 'Решений с разметкой'} value={fmtInt(m?.decisions)} loading={!metrics} hint={openHint} />
        <MetricCard
          title={forecast ? 'Отказ по разметке' : 'Событие по разметке'}
          value={fmtInt(m?.positives)}
          loading={!metrics}
          hint={forecast ? `отказ по разметке набора начался в ближайшие ${fmtInt(horizon)} ч после решения` : 'положительный класс модели шёл в момент решения'}
        />
        <MetricCard
          title="Precision"
          value={fmtPercent(m?.precision)}
          loading={!metrics}
          hint={`${m && m.decisions > 0 && m.alerts === 0 ? 'тревог модели не было · ' : ''}офлайн ${fmtPercent(ref?.precision)}`}
        />
        <MetricCard title="Recall" value={fmtPercent(m?.recall)} loading={!metrics} hint={`офлайн ${fmtPercent(ref?.recall)}`} />
        <MetricCard title="F1" value={f1(m?.f1 ?? null)} loading={!metrics} hint={`офлайн ${f1(ref?.f1 ?? null)}`} />
        {forecast && (
          <MetricCard
            title="Упреждение, медиана"
            value={fmtDuration(m?.lead_time?.p50_s)}
            loading={!metrics}
            hint={m?.lead_time ? `до начала отказа по разметке · 10–90%: ${fmtDuration(m.lead_time.p10_s)} — ${fmtDuration(m.lead_time.p90_s)}` : 'нет верных тревог'}
          />
        )}
      </div>
      <p className="text-xs text-muted-foreground">
        {forecast
          ? `Требования ТЗ не отмечены: они про каналы Москоллектора, а это модель другого набора. Исход — начался ли отказ по разметке набора в [T, T + ${fmtInt(horizon)} ч); решение закрывается, когда горизонт истёк.`
          : 'Требования ТЗ не отмечены: они про прогноз неисправности канала, а здесь обнаружение в момент решения. Упреждения нет по той же причине.'}
      </p>
      {lowN && (
        <p className="text-sm text-status-risk">
          Событий по разметке меньше 20 — живые Precision и Recall пока неустойчивы, сравнивайте с офлайн-проверкой осторожно.
        </p>
      )}
      {metrics && m && m.decisions === 0 && (
        <p className="rounded-lg bg-muted/40 p-3 text-sm text-muted-foreground">
          {forecast ? `Закрытых решений пока нет: исход — после конца горизонта ${fmtInt(horizon)} ч.` : 'Решений с разметкой в этом прогоне пока нет.'}
          {ref && ` Ориентир — офлайн-проверка ${off.version}: Precision ${fmtPercent(ref.precision)}, Recall ${fmtPercent(ref.recall)} на ${fmtInt(ref.decisions)} решениях (${off.period.label}).`}
        </p>
      )}
      {metrics && m && m.decisions > 0 && (
        <div className="grid gap-4 lg:grid-cols-[20rem_1fr]">
          <Matrix m={m} words={forecast ? FORECAST_WORDS : LABEL_WORDS} />
          <div className="overflow-x-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Решения</TableHead>
                  <TableHead className="text-right">Решений</TableHead>
                  <TableHead className="text-right">Положит.</TableHead>
                  <TableHead className="text-right">Тревог</TableHead>
                  <TableHead className="text-right">Precision</TableHead>
                  <TableHead className="text-right">Recall</TableHead>
                  <TableHead className="text-right">F1</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                <BlockRow label="Этот прогон" m={m} />
                {off && ref && (
                  <TableRow className="text-muted-foreground hover:bg-transparent">
                    <TableCell>
                      Офлайн {off.version}, все решения · {off.period.label}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">{fmtInt(ref.decisions)}</TableCell>
                    <TableCell className="text-right tabular-nums">{fmtInt(ref.positives)}</TableCell>
                    <TableCell className="text-right tabular-nums">{fmtInt(ref.alerts)}</TableCell>
                    <TableCell className="text-right tabular-nums">{fmtPercent(ref.precision)}</TableCell>
                    <TableCell className="text-right tabular-nums">{fmtPercent(ref.recall)}</TableCell>
                    <TableCell className="text-right tabular-nums">{f1(ref.f1)}</TableCell>
                  </TableRow>
                )}
              </TableBody>
            </Table>
          </div>
        </div>
      )}
    </div>
  )
}
