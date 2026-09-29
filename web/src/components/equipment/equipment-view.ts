// Presentation helpers of equipment episodes (docs/architecture/frontend.md, «Оборудование в ленте»).

import type { EquipmentEpisode } from '@/lib/api/types'
import { fmtTime, plural } from '@/lib/format'
import { EQUIPMENT_KIND_LABEL, SENSOR_COUNT_FORMS } from '@/lib/labels'

export const channelsText = (n: number) => `${n.toLocaleString('ru-RU')} ${plural(n, ['канал', 'канала', 'каналов'])}`

/** «71 фаза, 30 вентиляторов, 21 насос» — the largest types first, the rest folded into «и ещё N». */
export function byTypeText(byType: EquipmentEpisode['by_type'], limit = 3): string {
  // A channel type of an external dataset has no word forms here: its name as is.
  const forms = (type: string): [string, string, string] => SENSOR_COUNT_FORMS[type as keyof typeof SENSOR_COUNT_FORMS] ?? [type, type, type]
  const shown = byType.slice(0, limit).map((t) => `${t.count.toLocaleString('ru-RU')} ${plural(t.count, forms(t.sensor_type))}`)
  const rest = byType.slice(limit).reduce((n, t) => n + t.count, 0)
  return rest ? `${shown.join(', ')} и ещё ${rest.toLocaleString('ru-RU')}` : shown.join(', ')
}

/** «12:53 — 13:14» or «с 12:53, идёт». */
export const periodText = (ep: EquipmentEpisode) => (ep.end_ts ? `${fmtTime(ep.start_ts)} — ${fmtTime(ep.end_ts)}` : `с ${fmtTime(ep.start_ts)}, идёт`)

/** One line: «Обесточивание · ПС объект Омега · 122 канала: 71 фаза, 30 вентиляторов, 21 насос · 12:53 — 13:14». */
export const equipmentLine = (ep: EquipmentEpisode) =>
  `${EQUIPMENT_KIND_LABEL[ep.kind]} · ${ep.sub_object_name ?? ep.object_name} · ${channelsText(ep.channels)}: ${byTypeText(ep.by_type)} · ${periodText(ep)}`

/** Where the map opens: the sub-object with the most channels, or the object when none is known. */
export const equipmentMapPath = (ep: EquipmentEpisode) => `/objects/${ep.sub_object_id ?? ep.object_id}`
