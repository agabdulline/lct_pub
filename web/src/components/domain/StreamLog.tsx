import { useState } from 'react'

import { ChannelLink } from '@/components/domain/ChannelLink'
import { RiskMeter } from '@/components/domain/RiskMeter'
import { StatusBadge } from '@/components/domain/StatusBadge'
import { VerdictBadge } from '@/components/domain/VerdictBadge'
import { accessLine } from '@/components/access/access-view'
import { equipmentLine } from '@/components/equipment/equipment-view'
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from '@/components/ui/empty'
import type { StreamEventMap, StreamEventType } from '@/lib/api/types'
import { fmtTime } from '@/lib/format'
import { ALARM_KIND_LABEL, PRIORITY_LABEL, STATUS_LABEL } from '@/lib/labels'
import { useStreamEvent } from '@/lib/sse/bus'

type Entry = { key: number; type: StreamEventType; at: string; body: React.ReactNode }

const LIMIT = 40
let seq = 0

// Diagnostic log of the live stream: the latest events as they arrive (clock ticks are skipped).
export function StreamLog() {
  const [entries, setEntries] = useState<Entry[]>([])
  const push = <T extends StreamEventType>(type: T, at: string, body: React.ReactNode) =>
    setEntries((prev) => [{ key: seq++, type, at, body }, ...prev].slice(0, LIMIT))

  const alarm = (type: 'alarm.created' | 'alarm.scored') => (a: StreamEventMap['alarm.created']) =>
    push(
      type,
      a.ts,
      <>
        <ChannelLink id={a.channel_id} name={a.channel_name} sensorType={a.sensor_type} />
        <span className="truncate text-muted-foreground">
          {a.object_name} · {a.episode ? ALARM_KIND_LABEL[a.episode.kind].toLowerCase() : '—'}
        </span>
        <RiskMeter value={a.prediction?.score} threshold={a.prediction?.threshold} />
        <VerdictBadge verdict={a.verdict} refined={a.verdict_refined} inherited={a.prediction?.inherited} skipReason={a.prediction?.skip_reason} short />
      </>,
    )
  useStreamEvent('alarm.created', alarm('alarm.created'))
  useStreamEvent('alarm.scored', alarm('alarm.scored'))
  useStreamEvent('channel.status', (e) =>
    push(
      'channel.status',
      e.ts,
      <>
        <span className="font-mono text-xs">канал {e.channel_id}</span>
        <span className="text-muted-foreground">
          {STATUS_LABEL[e.prev_status]} →
        </span>
        <StatusBadge status={e.status} />
      </>,
    ),
  )
  useStreamEvent('work_order.created', (w) =>
    push(
      'work_order.created',
      w.created_at,
      <>
        <span className="truncate">Заявка №{w.id}: {w.title}</span>
        <StatusBadge status={w.priority === 'high' ? 'alarm' : 'risk'}>{PRIORITY_LABEL[w.priority]}</StatusBadge>
      </>,
    ),
  )
  useStreamEvent('alarm_episode.updated', (e) =>
    push(
      'alarm_episode.updated',
      e.start_ts,
      <>
        <span>
          Эпизод {e.object_name}: {ALARM_KIND_LABEL[e.kind].toLowerCase()}, датчиков {e.n_channels}
        </span>
      </>,
    ),
  )
  const equipment = (type: 'equipment_episode.created' | 'equipment_episode.updated') => (e: StreamEventMap['equipment_episode.created']) =>
    push(type, e.end_ts ?? e.start_ts, <span className="truncate">{equipmentLine(e)}</span>)
  useStreamEvent('equipment_episode.created', equipment('equipment_episode.created'))
  useStreamEvent('equipment_episode.updated', equipment('equipment_episode.updated'))
  const access = (type: 'access_incident.created' | 'access_incident.updated') => (e: StreamEventMap['access_incident.created']) =>
    push(type, e.end_ts ?? e.start_ts, <span className="truncate">{accessLine(e)}</span>)
  useStreamEvent('access_incident.created', access('access_incident.created'))
  useStreamEvent('access_incident.updated', access('access_incident.updated'))
  useStreamEvent('daily_forecast.completed', (e) =>
    push('daily_forecast.completed', e.decision_time, <span>Ежедневный прогноз на {e.date}: тревог {e.alerts}</span>),
  )

  if (!entries.length)
    return (
      <Empty>
        <EmptyHeader>
          <EmptyTitle>Событий пока нет</EmptyTitle>
          <EmptyDescription>Здесь появятся тревоги, смены состояний и заявки из живого потока выбранного прогона.</EmptyDescription>
        </EmptyHeader>
      </Empty>
    )

  return (
    <ul className="divide-y text-sm">
      {entries.map((e) => (
        <li key={e.key} className="flex min-w-0 items-center gap-3 py-1.5">
          <span className="w-16 shrink-0 font-mono text-xs tabular-nums text-muted-foreground">{fmtTime(e.at)}</span>
          <span className="w-40 shrink-0 font-mono text-xs text-muted-foreground">{e.type}</span>
          <span className="flex min-w-0 flex-1 items-center gap-3">{e.body}</span>
        </li>
      ))}
    </ul>
  )
}
