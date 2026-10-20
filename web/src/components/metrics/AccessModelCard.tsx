import { CircleAlert, ShieldQuestion } from 'lucide-react'

import { StatusBadge } from '@/components/domain/StatusBadge'
import { TargetMeter } from '@/components/metrics/TargetMeter'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import type { ModelInfo, OfflineMetrics } from '@/lib/api/types'
import { fmtInt, fmtPercent } from '@/lib/format'
import { REQUIREMENT } from '@/lib/offline-metrics'

const ci = (range: number[] | undefined) => (range ? `${fmtPercent(range[0])}–${fmtPercent(range[1])}` : null)

function QualityRow({ name, note, m, range }: { name: string; note: string; m: OfflineMetrics; range?: Record<string, number[]> | null }) {
  return (
    <TableRow>
      <TableCell className="pl-4">
        <div className="font-medium">{name}</div>
        <div className="text-xs text-muted-foreground">{note}</div>
      </TableCell>
      <TableCell>
        <TargetMeter value={m.precision} target={REQUIREMENT.precision} />
        {range?.P && <div className="text-xs text-muted-foreground">90%: {ci(range.P)}</div>}
      </TableCell>
      <TableCell>
        <TargetMeter value={m.recall} target={REQUIREMENT.recall} />
        {range?.R && <div className="text-xs text-muted-foreground">90%: {ci(range.R)}</div>}
      </TableCell>
      <TableCell className="text-right tabular-nums">
        {fmtInt(m.tp)} / {fmtInt(m.fp)}
      </TableCell>
      <TableCell className="pr-4 text-right tabular-nums">
        {fmtInt(m.decisions)} · {fmtInt(m.positives)}
      </TableCell>
    </TableRow>
  )
}

/**
 * The access model (lct-8f) on «Модели»: a proxy «own or not» by whether the guard is taken off within 5 min, not
 * intrusion detection — its quality with 90% intervals against the guard system's own rule on the same decisions, how
 * it works in the service (the model speeds up, the rule backs it up) and its limitations as the ML report states them.
 */
export function AccessModelCard({ m }: { m: ModelInfo }) {
  const off = m.offline_metrics
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-2">
          <ShieldQuestion className="size-4 text-muted-foreground" />
          {m.title}
          <StatusBadge status="unknown">прокси, не обнаружение проникновения</StatusBadge>
          {m.active && <StatusBadge status="ok">в ленте прогонов</StatusBadge>}
        </CardTitle>
        <CardDescription>
          {m.version} · {m.author ?? '—'} · порог {m.threshold?.toFixed(3) ?? '—'} · признаков {fmtInt(m.features)} · обучение{' '}
          {m.trained_on || '—'}. Решение — при первом открытии двери или люка подобъекта на охране; исход — сняли ли охрану за 5 мин.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4 px-0">
        {off ? (
          <div className="overflow-x-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="pl-4">На тех же решениях · {off.period}</TableHead>
                  <TableHead>Precision</TableHead>
                  <TableHead>Recall</TableHead>
                  <TableHead className="text-right">TP / FP</TableHead>
                  <TableHead className="pr-4 text-right">Решений · «нужна реакция»</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                <QualityRow name="Модель" note="положительный класс — охрану не сняли за 5 мин" m={off} range={m.offline_ci90} />
                {m.offline_rule && (
                  <QualityRow name="Ориентир — правило" note={m.offline_rule_label ?? 'правило охранной системы'} m={m.offline_rule} />
                )}
              </TableBody>
            </Table>
          </div>
        ) : (
          <p className="px-4 text-sm text-muted-foreground">Офлайн-итога у модели нет.</p>
        )}
        <div className="space-y-2 px-4 text-sm">
          <p>
            <span className="font-medium">В сервисе модель ускоряет, правило страхует.</span> «Нужна реакция» — инцидент и заявка
            сразу при первом открытии, а не через 5 мин. «Вероятно свой» — строка ленты без заявки; если охрану не сняли за 5 мин,
            инцидент заводит правило — промах модели до диспетчера доходит. В ленте и панели инцидента видно, кто решил.
          </p>
          {m.limitations && m.limitations.length > 0 && (
            <div className="rounded-lg p-3 ring-1 ring-foreground/10">
              <div className="mb-1.5 flex items-center gap-2 font-medium">
                <CircleAlert className="size-4 text-status-risk" /> Ограничения
              </div>
              <ul className="list-disc space-y-1 pl-4 text-muted-foreground">
                {m.limitations.map((l) => (
                  <li key={l}>{l}</li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  )
}
