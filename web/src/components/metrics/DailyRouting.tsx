import { Split } from 'lucide-react'
import type { ReactNode } from 'react'

import { SensorTypeIcon } from '@/components/domain/SensorTypeIcon'
import { TargetMeter } from '@/components/metrics/TargetMeter'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Table, TableBody, TableCell, TableFooter, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import type { DailyRouting, MetricsBlock, ModelInfo, RoutingOffline, SensorType } from '@/lib/api/types'
import { fmtDays, fmtInt, fmtPercent } from '@/lib/format'
import { byVersion, VERSION_AUTHOR, versionLabel } from '@/lib/labels'
import { REQUIREMENT, ROUTE_NOTE } from '@/lib/offline-metrics'

/** Whose a daily model is: the API's `author`, else the static label of the version. */
const authorOf = (version: string, models: ModelInfo[]) => models.find((m) => m.version === version)?.author ?? VERSION_AUTHOR[version] ?? null

/** A live value under the offline one: closed decisions of the run, shown only when there are any. */
const LiveLine = ({ children }: { children: ReactNode }) => <span className="block text-xs text-muted-foreground">{children}</span>

/**
 * Offline cells of a route or of a total — P, R, TP / FP, decisions and positives, episodes found, lead time — with the
 * run's closed live decisions of the same types under P, R and decisions.
 */
function OfflineCells({ o, live }: { o: RoutingOffline | null | undefined; live?: MetricsBlock | null }) {
  const shown = live?.decisions ? live : null
  if (!o)
    return (
      <TableCell colSpan={6} className="pr-4 text-muted-foreground">
        офлайн-итога нет
      </TableCell>
    )
  return (
    <>
      <TableCell>
        <TargetMeter value={o.precision} target={REQUIREMENT.precision} />
        {shown && <LiveLine>живые {fmtPercent(shown.precision)}</LiveLine>}
      </TableCell>
      <TableCell>
        <TargetMeter value={o.recall} target={REQUIREMENT.recall} />
        {shown && <LiveLine>живые {fmtPercent(shown.recall)}</LiveLine>}
      </TableCell>
      <TableCell className="text-right tabular-nums">
        {fmtInt(o.tp)} / {fmtInt(o.fp)}
      </TableCell>
      <TableCell className="text-right tabular-nums">
        {fmtInt(o.decisions)} · {fmtInt(o.positives)}
        {shown && (
          <LiveLine>
            живых {fmtInt(shown.decisions)} · {fmtInt(shown.positives)}
          </LiveLine>
        )}
      </TableCell>
      <TableCell className="text-right tabular-nums">
        {o.episodes != null && o.caught_episodes != null ? `${fmtInt(o.caught_episodes)} из ${fmtInt(o.episodes)}` : '—'}
      </TableCell>
      <TableCell className="pr-4 text-right tabular-nums">{fmtDays(o.lead_days?.p50)}</TableCell>
    </>
  )
}

/**
 * The daily routing table (task C): which daily model scores which sensor type, whose it is and why, with its offline
 * result on the rows common with v0 and the live closed decisions of the run by type. Totals — the mix by the table
 * against v0 alone on the same rows.
 */
export function DailyRoutingCard({
  routing,
  models,
  live,
  liveAll,
}: {
  routing: DailyRouting
  models: ModelInfo[]
  live: Record<string, MetricsBlock> | null | undefined
  liveAll: MetricsBlock | undefined
}) {
  const routes = [...routing.routes].sort((a, b) => byVersion(a.model, b.model))
  const mix = routing.offline?.routing
  const v0 = routing.offline?.v0_only
  const notes = [...new Set(routes.map((r) => r.model))].filter((m) => ROUTE_NOTE[m]).map((m) => ROUTE_NOTE[m])

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Split className="size-4 text-muted-foreground" /> Кто ведёт какой тип канала
        </CardTitle>
        <CardDescription>
          Таблица маршрутизации от {routing.version}: у каждого типа канала своя ежедневная модель. Правило — {routing.rule ?? 'не указано'}.
          Офлайн — {routing.test?.label ?? 'период не указан'}, на строках, общих с v0; каждая строка — модель, выбранная для типа.
          {routing.report && <> Отчёт — {routing.report}.</>}
        </CardDescription>
      </CardHeader>
      <CardContent className="overflow-x-auto px-0">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="pl-4">Тип канала</TableHead>
              <TableHead>Модель · чья · почему</TableHead>
              <TableHead>Precision</TableHead>
              <TableHead>Recall</TableHead>
              <TableHead className="text-right">TP / FP</TableHead>
              <TableHead className="text-right whitespace-normal">Решений · положит.</TableHead>
              <TableHead className="text-right whitespace-normal">Эпизодов найдено</TableHead>
              <TableHead className="pr-4 text-right whitespace-normal">Упреждение, медиана</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {routes.map((r) => (
              <TableRow key={r.sensor_type} className="align-top">
                <TableCell className="pl-4">
                  <span className="flex items-center gap-2">
                    <SensorTypeIcon type={r.sensor_type as SensorType} withTooltip={false} />
                    {r.sensor_type}
                  </span>
                </TableCell>
                <TableCell className="max-w-60 min-w-48 whitespace-normal">
                  <span className="font-medium">{versionLabel(r.model)}</span>
                  {authorOf(r.model, models) && <span className="text-muted-foreground"> · {authorOf(r.model, models)}</span>}
                  {r.why && <span className="block text-xs text-muted-foreground">{r.why}</span>}
                </TableCell>
                <OfflineCells o={r.offline} live={live?.[r.sensor_type]} />
              </TableRow>
            ))}
            {routing.unrouted.map((u) => (
              <TableRow key={u.sensor_type} className="align-top text-muted-foreground">
                <TableCell className="pl-4">
                  <span className="flex items-center gap-2">
                    <SensorTypeIcon type={u.sensor_type as SensorType} withTooltip={false} />
                    {u.sensor_type}
                  </span>
                </TableCell>
                <TableCell colSpan={7} className="whitespace-normal pr-4">
                  Ежедневного прогноза нет{u.why ? ` — ${u.why}` : ''}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
          {(mix || v0) && (
            <TableFooter>
              {mix && (
                <TableRow>
                  <TableCell className="pl-4 font-medium">Смесь по таблице</TableCell>
                  <TableCell className="text-muted-foreground">все модели, каждая на своих типах</TableCell>
                  <OfflineCells o={mix} live={liveAll} />
                </TableRow>
              )}
              {v0 && (
                <TableRow>
                  <TableCell className="pl-4 font-medium">Только v0</TableCell>
                  <TableCell className="text-muted-foreground">v0 на всех типах тех же строк</TableCell>
                  <OfflineCells o={v0} />
                </TableRow>
              )}
            </TableFooter>
          )}
        </Table>
        <div className="space-y-1 px-4 pt-3 text-xs text-muted-foreground">
          <p>
            Эпизод найден — до начала «Неисправен» у канала была тревога модели. Под офлайн-цифрами — живые: закрытые прогнозы
            этого прогона тех же типов. Горизонт ежедневной модели 30 суток, поэтому в коротком прогоне они появляются только
            после досчёта по журналу.
          </p>
          {notes.map((n) => (
            <p key={n}>{n}</p>
          ))}
        </div>
      </CardContent>
    </Card>
  )
}
