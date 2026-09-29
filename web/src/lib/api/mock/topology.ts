// Synthetic topology: 1 district → 16 objects → 78 sub-objects → ~10k channels.
// Shapes follow docs/architecture/data.md §4 (median 68 channels per sub-object, one with 994),
// names and ids are invented — no hackathon data here.

import type { SensorType, Status, Subsystem } from '@/lib/api/types'

import { createRng, lognormal, pick, randInt, score, weighted, type Rng } from './random'

export type MockObject = {
  id: number
  parent_id: number | null
  level: 'district' | 'object' | 'sub_object'
  kind: 'district' | 'controlHouse' | 'guardObject'
  name: string
}

export type MockChannel = {
  id: number
  name: string
  tag: string
  tag_group: string | null
  subsystem: Subsystem
  sensor_type: SensorType
  object_id: number
  sub_object_id: number
  picket_number: number | null
  picket_offset: number | null
  status: Status
  value: string
  is_alarm: boolean
  risk: number | null
  seen_channel: boolean
  /** Hidden mock property: how noisy the channel is — drives solitary alarms and faults. */
  noise: number
}

const GREEK = [
  'Альфа', 'Бета', 'Гамма', 'Дельта', 'Эпсилон', 'Дзета', 'Эта', 'Тета',
  'Йота', 'Каппа', 'Лямбда', 'Мю', 'Ню', 'Кси', 'Омикрон', 'Сигма',
]

// Proportions of the channel reference (docs/data-format.md §5–6).
const TYPE_WEIGHTS: (readonly [SensorType, number])[] = [
  ['Датчик дыма', 4337],
  ['Состояние фазы', 957],
  ['КД Дверь', 917],
  ['Датчик движения', 804],
  ['Переключатель', 801],
  ['Состояние УИР-Р', 762],
  ['Датчик температуры', 600],
  ['Газовый датчик', 529],
  ['Тепловой датчик', 472],
  ['КД АВ', 397],
  ['Состояние вентилятора', 354],
  ['Состояние насоса', 180],
  ['Ручной извещатель', 130],
  ['ИБП', 80],
  ['КД Люк', 55],
  ['Состояние охраны', 47],
  ['Стекло', 29],
  ['Датчик затопления', 25],
  ['9-секционный люк', 9],
]

export const SUBSYSTEM_OF: Record<SensorType, Subsystem> = {
  'Датчик дыма': 'Пожарная охрана',
  'Тепловой датчик': 'Пожарная охрана',
  'Ручной извещатель': 'Пожарная охрана',
  'Состояние УИР-Р': 'Пожарная охрана',
  'КД Дверь': 'Охранная подсистема',
  'Датчик движения': 'Охранная подсистема',
  'КД АВ': 'Охранная подсистема',
  'КД Люк': 'Охранная подсистема',
  Стекло: 'Охранная подсистема',
  '9-секционный люк': 'Охранная подсистема',
  'Состояние фазы': 'Диспетчерский контроль',
  Переключатель: 'Диспетчерский контроль',
  'Состояние вентилятора': 'Диспетчерский контроль',
  'Состояние насоса': 'Диспетчерский контроль',
  'Состояние охраны': 'Диспетчерский контроль',
  'Датчик затопления': 'Диспетчерский контроль',
  'Датчик температуры': 'Температурная подсистема',
  'Газовый датчик': 'Газовая охрана',
  ИБП: 'Диагностическая подсистема',
}

const NAME_PREFIX: Record<SensorType, readonly string[]> = {
  'Датчик дыма': ['Дым', 'ДД'],
  'Тепловой датчик': ['ТД'],
  'Ручной извещатель': ['ИПР'],
  'Состояние УИР-Р': ['УИР-Р', 'УСРСБ'],
  'КД Дверь': ['КД дверь отс.', 'КД отс.дв.'],
  'Датчик движения': ['ОД АВ', 'ОД ВШ', 'ОД'],
  'КД АВ': ['КД АВ', 'МРСБ АВ'],
  'КД Люк': ['КД люк'],
  Стекло: ['Стекло пульт'],
  '9-секционный люк': ['МРСБ 9 секц.'],
  'Состояние фазы': ['АВР Ввод1', 'АВР Ввод2', 'ЩАП Ввод1'],
  Переключатель: ['Управление В', 'Управление Ф'],
  'Состояние вентилятора': ['Вентилятор В1', 'Вентилятор В2'],
  'Состояние насоса': ['АНС Н1', 'АНС Н2', 'АНС Н3'],
  'Состояние охраны': ['Охранная зона'],
  'Датчик затопления': ['Затопление'],
  'Датчик температуры': ['Темп.', 'ТЕМП'],
  'Газовый датчик': ['ГАЗ Д'],
  ИБП: ['ИБП', 'АКБ'],
}

/** The daily routing table (task C, `ml/models/daily_routing.json`): sensor type → the model that scores it. */
export const DAILY_ROUTES: Partial<Record<SensorType, string>> = {
  'Датчик дыма': 'baseline-v0',
  'Состояние насоса': 'baseline-v0',
  'Состояние вентилятора': 'baseline-v0',
  'Датчик температуры': 'baseline-v0',
  'Газовый датчик': 'baseline-v0',
  'Состояние фазы': 'baseline-v0',
  'Ручной извещатель': 'manual_alarm',
  ИБП: 'ups_ensemble',
}

/** Types the daily forecast scores: the routed ones; the rest have no daily model. */
export const DAILY_TYPES = Object.keys(DAILY_ROUTES) as SensorType[]

export const ALARM_TYPES: SensorType[] = ['Датчик дыма']

export const DAILY_THRESHOLD = 0.42
export const ALARM_THRESHOLD = 0.31

export function normalValue(type: SensorType, rng: Rng): string {
  switch (type) {
    case 'Датчик дыма':
      return 'Дыма нет'
    case 'Газовый датчик':
      return (0.01 * randInt(rng, 0, 3)).toFixed(2)
    case 'Датчик температуры':
      return String(randInt(rng, 12, 24))
    case 'Датчик движения':
      return 'Движения нет'
    case 'Состояние фазы':
      return 'Есть питание'
    case 'Состояние насоса':
    case 'Состояние вентилятора':
      return rng() < 0.3 ? 'Включен' : 'Выключен'
    case 'Переключатель':
      return 'Включен'
    case 'Состояние УИР-Р':
      return 'Рычаг норма'
    case 'Состояние охраны':
      return 'На охране'
    case 'ИБП':
      return 'Питание от сети'
    default:
      return 'Норма'
  }
}

export const ALARM_VALUE: Partial<Record<SensorType, string>> = {
  'Датчик дыма': 'Обнаружен дым',
  'Тепловой датчик': 'Не замкнут',
  'Ручной извещатель': 'Рычаг сдернут',
}

export type Topology = {
  district: MockObject
  objects: MockObject[]
  subObjects: MockObject[]
  channels: MockChannel[]
  byId: Map<number, MockObject>
  channelById: Map<number, MockChannel>
  channelsBySub: Map<number, MockChannel[]>
  channelsByObject: Map<number, MockChannel[]>
}

export function buildTopology(seed = 20260926): Topology {
  const rng = createRng(seed)
  const district: MockObject = { id: 1, parent_id: null, level: 'district', kind: 'district', name: 'Северный округ' }
  const objects: MockObject[] = GREEK.map((g, i) => ({
    id: 100 + i,
    parent_id: 1,
    level: 'object',
    kind: i % 4 === 3 ? 'guardObject' : 'controlHouse',
    name: `Объект ${g}`,
  }))

  // 78 sub-objects spread over 16 objects.
  const perObject = objects.map(() => 2)
  for (let left = 78 - perObject.length * 2; left > 0; left--) perObject[randInt(rng, 0, objects.length - 1)]++
  const subObjects: MockObject[] = []
  objects.forEach((o, i) => {
    for (let k = 1; k <= perObject[i]; k++) {
      const guard = rng() < 0.3
      subObjects.push({
        id: 1000 + subObjects.length,
        parent_id: o.id,
        level: 'sub_object',
        kind: guard ? 'guardObject' : 'controlHouse',
        name: `${guard ? 'Пост охраны' : 'ДУ'} ${GREEK[i]}-${k}`,
      })
    }
  })

  const channels: MockChannel[] = []
  const channelsBySub = new Map<number, MockChannel[]>()
  const channelsByObject = new Map<number, MockChannel[]>()
  // The largest sub-object reproduces the 994-channel worst case for the graph (frontend.md §4).
  const largest = 1000 + 11
  subObjects.forEach((sub, subIndex) => {
    const n = sub.id === largest ? 994 : Math.round(Math.min(640, Math.max(8, lognormal(rng, 68, 0.85))))
    const objIndex = objects.findIndex((o) => o.id === sub.parent_id)
    const tagPrefix = `${10 + objIndex}-${(subIndex % 9) + 1}`
    const pickets = Math.max(12, Math.round(n / 3.2))
    const groups = Math.max(1, Math.round(n / 19))
    // Troubled tag groups concentrate faults and risk — they make the map readable.
    const troubled = new Set<number>()
    for (let g = 1; g <= groups; g++) if (rng() < 0.06) troubled.add(g)

    const list: MockChannel[] = []
    for (let k = 0; k < n; k++) {
      const type = weighted(rng, TYPE_WEIGHTS)
      const hasPicket = rng() > 0.075
      const picket = hasPicket ? randInt(rng, 1, pickets) : null
      // Groups follow picket segments: a tag group is a stretch of the collector.
      const group = picket ? Math.min(groups, 1 + Math.floor(((picket - 1) / pickets) * groups)) : randInt(rng, 1, groups)
      const isTroubled = troubled.has(group)
      const tagGroup = `${tagPrefix}.${group}`
      const prefix = pick(rng, NAME_PREFIX[type])
      const numbered = type === 'Газовый датчик' ? `${prefix}${randInt(rng, 1, 60)}` : prefix
      const offset = picket && rng() < 0.08 ? randInt(rng, 1, 9) : null
      const noise = Math.min(1, lognormal(rng, isTroubled ? 0.25 : 0.04, 1.1))
      const covered = DAILY_TYPES.includes(type)
      const risk = covered ? score(rng, Math.min(0.9, 0.04 + noise * 0.8), 8) : null
      const r = rng()
      let status: Status = 'ok'
      if (r < 0.012 + noise * 0.08) status = 'fault'
      else if (r < 0.03 + noise * 0.08) status = 'unknown'
      else if (risk !== null && risk >= DAILY_THRESHOLD) status = 'risk'
      const value = status === 'fault' ? 'Неисправен' : status === 'unknown' ? 'Неопределен' : normalValue(type, rng)
      const channel: MockChannel = {
        id: 200_000 + channels.length * 7 + randInt(rng, 0, 6),
        name: picket ? `${numbered} ПК${picket}${offset ? `+${offset}` : ''}` : `${numbered} ${sub.kind === 'guardObject' ? 'пост' : 'камера'} ${k + 1}`,
        tag: `${tagGroup}.${randInt(rng, 100, 199)}.${randInt(rng, 1, 9)}.`,
        tag_group: rng() < 0.015 ? null : tagGroup,
        subsystem: SUBSYSTEM_OF[type],
        sensor_type: type,
        object_id: sub.parent_id!,
        sub_object_id: sub.id,
        picket_number: picket,
        picket_offset: offset,
        status,
        value,
        is_alarm: false,
        risk,
        seen_channel: rng() > 0.1,
        noise,
      }
      list.push(channel)
      channels.push(channel)
    }
    channelsBySub.set(sub.id, list)
    const byObj = channelsByObject.get(sub.parent_id!) ?? []
    byObj.push(...list)
    channelsByObject.set(sub.parent_id!, byObj)
  })

  const byId = new Map<number, MockObject>([district, ...objects, ...subObjects].map((o) => [o.id, o]))
  const channelById = new Map(channels.map((c) => [c.id, c]))
  return { district, objects, subObjects, channels, byId, channelById, channelsBySub, channelsByObject }
}
