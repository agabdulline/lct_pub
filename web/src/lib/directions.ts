export type Direction = 'sensor' | 'fire' | 'access' | 'wear' | 'system'

/** Display order of the directions (the order of the task statement). */
export const DIRECTION_ORDER: Direction[] = ['sensor', 'fire', 'access', 'wear', 'system']

export const DIRECTION_TITLE: Record<Direction, string> = {
  sensor: 'Отказ датчика',
  fire: 'Пожарный риск',
  access: 'Несанкционированный доступ',
  wear: 'Износ инфраструктуры',
  system: 'Работа системы',
}

export const DIRECTION_SHORT: Record<Direction, string> = {
  sensor: 'Отказ датчика',
  fire: 'Пожар',
  access: 'Доступ',
  wear: 'Износ',
  system: 'Система',
}

const DATASET_DIRECTION: Record<string, Direction> = { magdeburg: 'fire', vast2017: 'access', azure_pdm: 'wear' }
const SENSOR_IDS = new Set(['ppr', 'ppr-failures', 'solitary-fault', 'subobject-week', 'new-object', 'sample-2026-08-01'])
const ACCESS_IDS = new Set(['guard-hatch-night', 'guard-late-disarm'])

/** The dataset key from a run's `dataset` string or a scenario's `dataset` object. */
function datasetKey(dataset: unknown): string | null {
  if (typeof dataset === 'string') return dataset || null
  if (dataset && typeof dataset === 'object' && 'key' in dataset && typeof dataset.key === 'string') return dataset.key
  return null
}

/** Task direction of a scenario or run; the dataset key (when known) wins over the id prefix. */
export function directionOf(scenarioId: string, dataset?: unknown): Direction {
  const key = datasetKey(dataset)
  if (key && DATASET_DIRECTION[key]) return DATASET_DIRECTION[key]
  if (scenarioId.startsWith('ext-magdeburg-')) return 'fire'
  if (scenarioId.startsWith('ext-vast2017-') || ACCESS_IDS.has(scenarioId)) return 'access'
  if (scenarioId.startsWith('ext-pdm-')) return 'wear'
  if (SENSOR_IDS.has(scenarioId) || scenarioId.startsWith('upload-')) return 'sensor'
  return 'system'
}

/** Text tone of a direction: group headers and filter buttons, same colors as DirectionBadge. */
export const DIRECTION_TEXT: Record<Direction, string> = {
  sensor: 'text-status-ppr',
  fire: 'text-status-alarm',
  access: 'text-status-fault',
  wear: 'text-status-risk',
  system: 'text-status-unknown',
}
