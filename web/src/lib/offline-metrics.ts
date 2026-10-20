// Offline metrics of ML v3 — static, from ml/reports/v3/REPORT_RU.md (§4, §2). The retrospective test on 2026
// is not an independent final test: 2026 was used during research (report §4). Shown next to live metrics.

import type { DailyRouting, ModelInfo, ModelName, RoutingOffline } from '@/lib/api/types'
import { fmtDays, fmtDuration } from '@/lib/format'
import { versionLabel } from '@/lib/labels'

export type OfflineRow = {
  version: string
  period: string
  precision: number
  recall: number
  f1: number
  ap: number | null
  tp: number
  fp: number
  /** A row on its own decisions (the chain's half year); without — the model's (`OfflineModel.decisions`). */
  decisions?: number
  positives?: number
}

export type OfflineModel = {
  model: ModelName
  target: string
  decisions: number
  decisionsLabel: string
  positives: number
  rows: OfflineRow[]
  lead: string
  features: string
  notes: string[]
}

/** Requirement of the task: Precision > 0.7 and Recall > 0.5 (docs/product/v1-scope.md). */
export const REQUIREMENT = { precision: 0.7, recall: 0.5 }

export const OFFLINE: Record<ModelName, OfflineModel> = {
  smoke_alarm: {
    model: 'smoke_alarm',
    target: 'Первое начало «Неисправен» в [T, T+24ч) после допустимой дымовой тревоги; решение через 1 с',
    decisions: 6512,
    decisionsLabel: 'допустимых дымовых тревог 2026',
    positives: 434,
    rows: [
      { version: 'v3', period: '2026, ретроспективно', precision: 0.848, recall: 0.747, f1: 0.794, ap: 0.788, tp: 324, fp: 58 },
      { version: 'v2', period: '2026, ретроспективно', precision: 0.88, recall: 0.691, f1: 0.774, ap: 0.776, tp: 300, fp: 41 },
      // The chain «v3 where it decided, else v0» (ml/reports/compare-v0-v3/FALLBACK.md, #241): the half year with the
      // week v3 is silent after a day without journal data. Shown while the ML service does not report it itself.
      {
        version: 'v3>baseline-v0',
        period: 'I полугодие 2026 с неделей после пропуска журнала',
        precision: 0.869,
        recall: 0.788,
        f1: 0.826,
        ap: null,
        tp: 423,
        fp: 64,
        decisions: 7563,
        positives: 537,
      },
    ],
    lead: 'медиана 2.32 мин, максимум 5.37 ч — это триаж, а не предупреждение за сутки',
    features: '227 признаков, 18 деревьев глубины 7, обучение 2023–2024',
    notes: [
      'Каналы без обучающих примеров: 587 каналов, 993 решения, 11 положительных — ни одной тревоги, Recall 0. С примерами — Recall 76.6%.',
      'III квартал 2025 заметно хуже: при отборе признаков P 50.3% / R 35.8% — результат нестабилен во времени.',
      'Метрики — только по допустимым дымовым тревогам, не по всем отказам всех датчиков.',
    ],
  },
  daily: {
    model: 'daily',
    target: 'Первое начало «Неисправен» в [T+24ч, T+30д) для восьми типов каналов',
    decisions: 967_488,
    decisionsLabel: 'канал-дней 2026',
    positives: 28_191,
    rows: [
      { version: 'v3', period: '2026, ретроспективно', precision: 0.737, recall: 0.292, f1: 0.418, ap: 0.41, tp: 8227, fp: 2931 },
      { version: 'v2', period: '2026, ретроспективно', precision: 0.773, recall: 0.228, f1: 0.352, ap: 0.41, tp: 6435, fp: 1892 },
    ],
    lead: 'медиана 12.5 суток, минимум 32 ч — горизонт ≥ 24 ч выполнен',
    features: '164 признака, 24 дерева глубины 6, обучение 2022–2024',
    notes: [
      'Recall 29.2% — требование Recall > 50% не выполнено; AP не вырос, изменён только порог.',
      'Полный 30-дневный горизонт ограничивает доступные даты теста, включая пропуск 1 июня.',
    ],
  },
}

/** The offline reference shown next to live metrics: always the offline check of the model that is live. */
export type OfflineRef = {
  version: string
  period: string
  precision: number | null
  recall: number | null
  f1: number | null
  decisions: number
  positives: number
  alerts: number
  decisionsLabel: string
  lead: string
}

const DECISIONS_NOUN: Record<ModelName, string> = { smoke_alarm: 'допустимых дымовых тревог', daily: 'канал-дней' }

/** F1 of a routing result: it carries only P and R. */
export const routingF1 = (o: RoutingOffline): number | null =>
  o.precision !== null && o.recall !== null && o.precision + o.recall > 0 ? (2 * o.precision * o.recall) / (o.precision + o.recall) : null

/** Lead time of a routing result in days: the median and the p10–p90 range. */
export const routingLead = (o: RoutingOffline): string => {
  const d = o.lead_days
  if (!d?.p50) return 'нет верных тревог'
  return `медиана ${fmtDays(d.p50)}, 10–90% — от ${fmtDays(d.p10)} до ${fmtDays(d.p90)}`
}

/** Caveats of a daily model the routing table picked, shown next to its route (`docs/QUESTIONS.md`). */
export const ROUTE_NOTE: Record<string, string> = {
  manual_alarm:
    'Модель извещателя проходит пороги впритык: нижние границы 90% интервалов — 64,4% и 45,0%, порог выбран на том же периоде; включение — вопрос владельцу (Q25, A20).',
}

/**
 * The shown version's own offline check (`offline` in /models: v0 and, since 15.1, the event v3); without a version —
 * the active one. The static numbers of the ML report are used only for a version the service has no report of:
 * they are never another version's numbers. The daily forecast without a version is the mix of models by the routing
 * table (task C): its reference is the mix on the rows common with v0, not v0 alone.
 */
export function offlineRef(model: ModelName, models: ModelInfo[] | undefined, version?: string | null, routing?: DailyRouting | null): OfflineRef {
  const mix = routing?.offline?.routing
  if (model === 'daily' && !version && mix) {
    const label = routing.test?.label ?? 'строки проверки'
    return {
      version: 'смесь по таблице',
      period: label,
      precision: mix.precision,
      recall: mix.recall,
      f1: routingF1(mix),
      decisions: mix.decisions,
      positives: mix.positives,
      alerts: mix.alerts,
      decisionsLabel: `канал-дней, ${label}`,
      lead: routingLead(mix),
    }
  }
  const own = models?.filter((m) => m.model === model)
  const active = version ? own?.find((m) => m.version === version) : own?.find((m) => m.active)
  // Without a report the static numbers of the same version (the chain's, v3's), not another's.
  version = version ?? active?.version ?? null
  const report = active?.offline
  if (report) {
    const t = report.test
    const lt = report.lead_time
    const lead = lt
      ? `медиана ${fmtDuration(lt.p50_s)}, максимум ${fmtDuration(lt.max_s)}${model === 'smoke_alarm' ? ' — это триаж, а не предупреждение за сутки' : ''}`
      : 'нет верных тревог'
    return {
      // The report names itself «v0»; the live model is «baseline-v0» — the name the screens show everywhere.
      version: active.version,
      period: report.period.label,
      precision: t.precision,
      recall: t.recall,
      f1: t.f1,
      decisions: t.decisions,
      positives: t.positives,
      alerts: t.alerts,
      decisionsLabel: `${DECISIONS_NOUN[model]}, ${report.period.label}`,
      lead,
    }
  }
  const off = OFFLINE[model]
  const row = off.rows.find((r) => r.version === version) ?? off.rows[0]
  return {
    version: versionLabel(row.version),
    period: row.period,
    precision: row.precision,
    recall: row.recall,
    f1: row.f1,
    decisions: row.decisions ?? off.decisions,
    positives: row.positives ?? off.positives,
    alerts: row.tp + row.fp,
    decisionsLabel: row.decisions ? `решений, ${row.period}` : off.decisionsLabel,
    lead: off.lead,
  }
}
