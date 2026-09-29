import { ChevronDown, ChevronRight, PlugZap, ShieldAlert } from 'lucide-react'
import { Fragment, useState } from 'react'

import { AccessMarks } from '@/components/access/AccessMarks'
import { accessHeadline, accessStateLabel, accessStatus, armedText, channelsText as accessChannelsText, endText, openingsText } from '@/components/access/access-view'
import { AlarmDecision } from '@/components/domain/ChainDecision'
import { ChannelLink } from '@/components/domain/ChannelLink'
import { PprAlertMark } from '@/components/domain/PprAlertMark'
import { RiskMeter } from '@/components/domain/RiskMeter'
import { VersionScores } from '@/components/domain/VersionScores'
import { RunLink } from '@/components/domain/RunLink'
import { StatusBadge } from '@/components/domain/StatusBadge'
import { VerdictBadge } from '@/components/domain/VerdictBadge'
import { byTypeText, channelsText } from '@/components/equipment/equipment-view'
import { Spinner } from '@/components/ui/spinner'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { isPprAlert } from '@/lib/alarm'
import type { AccessIncident, Alarm, EquipmentEpisode, Verdict } from '@/lib/api/types'
import { fmtDateTimeShort, fmtTime, mskDay } from '@/lib/format'
import {
  ACCESS_NOTE,
  ACCESS_RESOLUTION_LABEL,
  ALARM_KIND_LABEL,
  chainOf,
  EQUIPMENT_ACTION,
  EQUIPMENT_KIND_LABEL,
  OBJECT_HISTORY_NOTE,
  PRIORITY_LABEL,
  skipReasonLabel,
  VERDICT_ACTION,
} from '@/lib/labels'
import { useRun } from '@/lib/run'
import { cn } from '@/lib/utils'

import { alarmContext, feedRows, withEvents } from './alarm-view'

// Left edge of a row — the verdict token, so «требует реакции» reads first.
const EDGE: Record<Verdict, string> = {
  reaction: 'border-l-status-alarm',
  sensor_fault: 'border-l-status-risk',
  ppr: 'border-l-status-ppr',
  no_score: 'border-l-status-unknown',
  pending: 'border-l-transparent',
}

type Props = {
  items: Alarm[]
  selectedId: number | null
  onSelect: (id: number) => void
  collapsePpr: boolean
  /** Alarms that arrived live — highlighted for a few seconds. */
  fresh: Set<number>
  /** Moscow day of the run's "now": alarms of other days show their date. */
  today: string | null
  /** Equipment episodes of the same page window — information rows without a verdict. */
  equipment?: EquipmentEpisode[]
  selectedEquipmentId?: number | null
  onSelectEquipment?: (id: number) => void
  freshEquipment?: Set<number>
  /** Access incidents (opened under guard) of the same page window — rows of the guard rules, no model verdict. */
  access?: AccessIncident[]
  selectedAccessId?: number | null
  onSelectAccess?: (id: number) => void
  freshAccess?: Set<number>
}

// Left edge of an access row: an open incident by its priority, a finished one is neutral.
const ACCESS_EDGE = { alarm: 'border-l-status-alarm', risk: 'border-l-status-risk' } as const

function ScoreCell({ a }: { a: Alarm }) {
  const chain = chainOf(useRun().run?.primary_model)
  const p = a.prediction
  if (a.verdict === 'pending')
    return (
      <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
        <Spinner /> оценка идёт
      </span>
    )
  if (!p) return <span className="text-muted-foreground">—</span>
  // Stage 15: the other versions' scores under the primary one, and whether they disagree; under a chain — whose
  // decision it is and why not the first version's.
  const others = a.scores && a.scores.length > 1 && (
    <div>
      <VersionScores scores={a.scores} disagree={a.disagree} />
      {chain && <AlarmDecision alarm={a} chain={chain} short />}
    </div>
  )
  if (p.score === null)
    return (
      <div className="space-y-0.5">
        <div className="text-xs text-muted-foreground" title={p.skip_reason ? skipReasonLabel(p.skip_reason) : undefined}>
          пропуск
        </div>
        {others}
      </div>
    )
  const meter = <RiskMeter value={p.score} threshold={p.threshold} label={p.inherited ? 'Оценка по более ранней тревоге' : 'Оценка событийной модели'} />
  return (
    <div className="space-y-0.5">
      <div>{meter}</div>
      {others}
      {p.object_history === false && (
        <div className="text-xs text-muted-foreground" title={OBJECT_HISTORY_NOTE}>
          нет истории объекта
        </div>
      )}
    </div>
  )
}

function ActionCell({ a }: { a: Alarm }) {
  if (a.work_order_id)
    return (
      <RunLink to={`/work-orders/${a.work_order_id}`} className="text-xs font-medium underline-offset-4 hover:underline" onClick={(e) => e.stopPropagation()}>
        → заявка №{a.work_order_id}
      </RunLink>
    )
  return <span className="text-xs text-muted-foreground">{VERDICT_ACTION[a.verdict]}</span>
}

export function AlarmTable({
  items,
  selectedId,
  onSelect,
  collapsePpr,
  fresh,
  today,
  equipment = [],
  selectedEquipmentId,
  onSelectEquipment,
  freshEquipment,
  access = [],
  selectedAccessId,
  onSelectAccess,
  freshAccess,
}: Props) {
  const [open, setOpen] = useState<Set<number>>(new Set())
  const rows = withEvents(feedRows(items, collapsePpr), equipment, access)
  const fmt = (ts: string) => (today && mskDay(ts) === today ? fmtTime(ts) : fmtDateTimeShort(ts))

  const alarmRow = (a: Alarm, nested = false) => (
    <TableRow
      key={a.id}
      data-state={selectedId === a.id ? 'selected' : undefined}
      onClick={() => onSelect(a.id)}
      className={cn(
        'cursor-pointer border-l-2',
        EDGE[a.verdict],
        fresh.has(a.id) && 'animate-in bg-status-alarm/5 duration-500 fade-in slide-in-from-top-1',
        nested && 'bg-muted/30',
      )}
    >
      <TableCell className={cn('font-mono text-xs tabular-nums', nested && 'pl-8')}>{fmt(a.ts)}</TableCell>
      <TableCell className="max-w-56">
        <div className="truncate font-medium">{a.object_name}</div>
        <div className="truncate text-xs text-muted-foreground">
          {a.sub_object_name} · {a.episode ? ALARM_KIND_LABEL[a.episode.kind].toLowerCase() : '—'}
        </div>
      </TableCell>
      <TableCell className="max-w-64">
        <ChannelLink id={a.channel_id} name={a.channel_name} sensorType={a.sensor_type} className="max-w-full" />
        <div className="truncate text-xs text-muted-foreground">{alarmContext(a)}</div>
      </TableCell>
      <TableCell>
        <ScoreCell a={a} />
      </TableCell>
      <TableCell>
        <div className="flex flex-col items-start gap-1">
          <VerdictBadge verdict={a.verdict} refined={a.verdict_refined} inherited={a.prediction?.inherited} skipReason={a.prediction?.skip_reason} />
          {isPprAlert(a) ? <PprAlertMark workOrderId={a.work_order_id} /> : <ActionCell a={a} />}
        </div>
      </TableCell>
    </TableRow>
  )

  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead className="w-28">Время</TableHead>
          <TableHead>Объект</TableHead>
          <TableHead>Канал и контекст</TableHead>
          <TableHead className="w-32">Оценка</TableHead>
          <TableHead className="w-72">Вердикт и действие</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((row) => {
          if (row.type === 'alarm') return alarmRow(row.alarm)
          if (row.type === 'access') {
            const inc = row.incident
            const status = accessStatus(inc)
            return (
              <TableRow
                key={`acc-${inc.id}`}
                data-state={selectedAccessId === inc.id ? 'selected' : undefined}
                onClick={() => onSelectAccess?.(inc.id)}
                className={cn(
                  'cursor-pointer border-l-2',
                  status === 'alarm' || status === 'risk' ? ACCESS_EDGE[status] : 'border-l-status-unknown',
                  freshAccess?.has(inc.id) && 'animate-in bg-status-alarm/5 duration-500 fade-in slide-in-from-top-1',
                )}
              >
                <TableCell className="font-mono text-xs tabular-nums">
                  {fmt(inc.start_ts)}
                  <div className="text-muted-foreground">{endText(inc)}</div>
                </TableCell>
                <TableCell className="max-w-56">
                  <div className="truncate font-medium">{inc.sub_object_name}</div>
                  <div className="truncate text-xs text-muted-foreground">{inc.object_name} · доступ</div>
                </TableCell>
                <TableCell className="max-w-64">
                  <div className="font-medium">
                    {accessHeadline(inc)} · {openingsText(inc.openings)}
                  </div>
                  <div className="truncate text-xs text-muted-foreground">
                    {accessChannelsText(inc)}
                    {armedText(inc) ? ` · ${armedText(inc)}` : ''}
                  </div>
                </TableCell>
                <TableCell>
                  <span className="text-xs text-muted-foreground" title={ACCESS_NOTE}>
                    по правилам охраны
                  </span>
                </TableCell>
                <TableCell>
                  <div className="flex flex-col items-start gap-1">
                    <StatusBadge status={status}>
                      <ShieldAlert /> {accessStateLabel(inc)}
                      {inc.status === 'open' && inc.priority && ` · ${PRIORITY_LABEL[inc.priority].toLowerCase()} приоритет`}
                    </StatusBadge>
                    <AccessMarks incident={inc} />
                    {inc.work_order_id && (
                      <RunLink
                        to={`/work-orders/${inc.work_order_id}`}
                        className="text-xs font-medium underline-offset-4 hover:underline"
                        onClick={(e) => e.stopPropagation()}
                      >
                        → заявка №{inc.work_order_id}
                      </RunLink>
                    )}
                    <span className="text-xs text-muted-foreground">
                      {inc.resolution ? `разбор: ${ACCESS_RESOLUTION_LABEL[inc.resolution].toLowerCase()}` : 'не разобран'}
                    </span>
                  </div>
                </TableCell>
              </TableRow>
            )
          }
          if (row.type === 'equipment') {
            const e = row.episode
            const active = e.end_ts === null
            return (
              <TableRow
                key={`eq-${e.id}`}
                data-state={selectedEquipmentId === e.id ? 'selected' : undefined}
                onClick={() => onSelectEquipment?.(e.id)}
                className={cn(
                  'cursor-pointer border-l-2',
                  active ? 'border-l-status-risk' : 'border-l-status-unknown',
                  freshEquipment?.has(e.id) && 'animate-in bg-status-risk/5 duration-500 fade-in slide-in-from-top-1',
                )}
              >
                <TableCell className="font-mono text-xs tabular-nums">
                  {fmt(e.start_ts)}
                  <div className="text-muted-foreground">{e.end_ts ? `до ${fmtTime(e.end_ts)}` : 'идёт'}</div>
                </TableCell>
                <TableCell className="max-w-56">
                  <div className="truncate font-medium">{e.object_name}</div>
                  <div className="truncate text-xs text-muted-foreground">
                    {e.sub_object_name ?? 'подобъект не определён'}
                    {e.sub_objects.length > 1 ? ` и ещё ${e.sub_objects.length - 1}` : ''} · оборудование
                  </div>
                </TableCell>
                <TableCell className="max-w-64">
                  <div className="font-medium">
                    {EQUIPMENT_KIND_LABEL[e.kind]} · {channelsText(e.channels)}
                  </div>
                  <div className="truncate text-xs text-muted-foreground">{byTypeText(e.by_type)}</div>
                </TableCell>
                <TableCell>
                  <span className="text-xs text-muted-foreground">без модели</span>
                </TableCell>
                <TableCell>
                  <div className="flex flex-col items-start gap-1">
                    <StatusBadge status={active ? 'risk' : 'unknown'}>
                      <PlugZap /> {EQUIPMENT_KIND_LABEL[e.kind]}
                    </StatusBadge>
                    <span className="text-xs text-muted-foreground">
                      {active
                        ? EQUIPMENT_ACTION[e.kind]
                        : `вернулись ${e.returned_channels} из ${e.channels}${e.active_channels ? ` · не в норме ${e.active_channels}` : ''}`}
                    </span>
                  </div>
                </TableCell>
              </TableRow>
            )
          }
          const expanded = open.has(row.episodeId)
          const first = row.alarms[row.alarms.length - 1]
          const last = row.alarms[0]
          const total = last.episode?.n_channels ?? row.alarms.length
          const toggle = () =>
            setOpen((prev) => {
              const next = new Set(prev)
              if (next.has(row.episodeId)) next.delete(row.episodeId)
              else next.add(row.episodeId)
              return next
            })
          return (
            <Fragment key={`ppr-${row.episodeId}`}>
              <TableRow
                onClick={toggle}
                className={cn('cursor-pointer border-l-2 border-l-status-ppr', row.alarms.some((a) => fresh.has(a.id)) && 'bg-status-ppr/5')}
              >
                <TableCell className="font-mono text-xs tabular-nums">
                  <span className="inline-flex items-center gap-1">
                    {expanded ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />}
                    {fmt(last.ts)}
                  </span>
                  {row.alarms.length > 1 && <div className="pl-4.5 text-muted-foreground">с {fmtTime(first.ts)}</div>}
                </TableCell>
                <TableCell className="max-w-56">
                  <div className="truncate font-medium">{last.object_name}</div>
                  <div className="text-xs text-muted-foreground">эпизод ППР</div>
                </TableCell>
                <TableCell>
                  <div className="font-medium">{total} датчиков</div>
                  <div className="text-xs text-muted-foreground">
                    {expanded ? 'свернуть' : `на странице ${row.alarms.length} — развернуть`}
                  </div>
                </TableCell>
                <TableCell>
                  <span className="text-muted-foreground">—</span>
                </TableCell>
                <TableCell>
                  <div className="flex flex-col items-start gap-1">
                    <VerdictBadge verdict="ppr" refined={row.alarms.some((a) => a.verdict_refined)} />
                    {row.alarms.some(isPprAlert) ? (
                      <PprAlertMark count={row.alarms.filter(isPprAlert).length} />
                    ) : (
                      <span className="text-xs text-muted-foreground">{VERDICT_ACTION.ppr}</span>
                    )}
                  </div>
                </TableCell>
              </TableRow>
              {expanded && row.alarms.map((a) => alarmRow(a, true))}
            </Fragment>
          )
        })}
      </TableBody>
    </Table>
  )
}
