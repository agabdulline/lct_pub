import { PprAlertMark } from '@/components/domain/PprAlertMark'
import { RiskMeter } from '@/components/domain/RiskMeter'
import { SensorTypeIcon } from '@/components/domain/SensorTypeIcon'
import { StatusBadge } from '@/components/domain/StatusBadge'
import { fmtInt, fmtScore } from '@/lib/format'

import type { GNode } from './model'

const KIND_LABEL: Record<GNode['kind'], string> = {
  district: 'Район',
  object: 'Объект',
  sub_object: 'Подобъект',
  tag_group: 'Группа по тегу',
  channel: 'Канал',
}

const HINT: Partial<Record<GNode['kind'], string>> = {
  sub_object: 'Клик — раскрыть каналы',
  object: 'Клик — приблизить',
  tag_group: 'Гипотеза электрической группы по двум частям тега',
  channel: 'Клик — краткая карточка',
}

// Hover card over the canvas: design-system tokens only, positioned at the node.
export function GraphTooltip({ node, x, y, riskThreshold }: { node: GNode; x: number; y: number; riskThreshold: number }) {
  const channel = node.kind === 'channel'
  return (
    <div
      className="pointer-events-none absolute z-10 w-60 -translate-x-1/2 rounded-lg bg-popover p-2.5 text-xs text-popover-foreground shadow-md ring-1 ring-foreground/10"
      style={{ left: x, top: y + node.r + 14 }}
    >
      <div className="mb-1 flex items-center gap-1.5">
        {channel && <SensorTypeIcon type={node.sensor_type} withTooltip={false} className="size-3.5" />}
        <span className="truncate font-medium">{node.label}</span>
      </div>
      <div className="mb-1.5 text-muted-foreground">
        {channel ? `${node.sensor_type} · ${node.subsystem}` : KIND_LABEL[node.kind]}
        {node.picket_number !== null && ` · ПК${node.picket_number}`}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <StatusBadge status={node.status} />
        {!channel && <span className="text-muted-foreground">каналов {fmtInt(node.channels)}</span>}
        {!channel && node.active_alarms > 0 && <span className="text-status-alarm">в тревоге {node.active_alarms}</span>}
      </div>
      {channel && node.risk !== null && (
        <div className="mt-1.5 flex items-center gap-2 text-muted-foreground">
          Риск 1–30 сут
          <RiskMeter value={node.risk} threshold={riskThreshold} />
        </div>
      )}
      {!channel && (
        <div className="mt-1.5 text-muted-foreground">
          в риске {fmtInt(node.at_risk)} · неисправно {fmtInt(node.in_fault)}
          {node.risk !== null && <> · макс. риск {fmtScore(node.risk)}</>}
        </div>
      )}
      {channel && node.pprAlert && <PprAlertMark className="mt-1.5" />}
      {HINT[node.kind] && <div className="mt-1.5 text-muted-foreground/80">{HINT[node.kind]}</div>}
    </div>
  )
}
