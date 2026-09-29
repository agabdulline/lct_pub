// Presentation helpers of the alarm feed (docs/product/v1-scope.md, «Лента тревог»).

import type { AccessIncident, Alarm, EquipmentEpisode } from '@/lib/api/types'

/** Context under the channel: why the verdict looks the way it does. */
export function alarmContext(a: Alarm): string {
  const ep = a.episode
  if (ep?.kind === 'ppr') return `плановая проверка · ${ep.n_channels} датч.`
  if (ep?.kind === 'group') return `группа · ${ep.n_channels} датч. на объекте`
  if (a.neighbors_alarmed_1h === 0) return 'соседи молчат 1 ч'
  if (a.neighbors_alarmed_1h === null) return 'соседей не определить'
  return `соседей с тревогой за 1 ч: ${a.neighbors_alarmed_1h}`
}

export type FeedRow =
  | { type: 'alarm'; alarm: Alarm }
  | { type: 'ppr'; episodeId: number; alarms: Alarm[] }
  | { type: 'equipment'; episode: EquipmentEpisode }
  | { type: 'access'; incident: AccessIncident }

const rowTime = (r: FeedRow) =>
  Date.parse(r.type === 'alarm' ? r.alarm.ts : r.type === 'ppr' ? r.alarms[0].ts : r.type === 'access' ? r.incident.start_ts : r.episode.start_ts)

/** Merges two lists sorted newest first into one, keeping that order; on a tie the first list goes first. */
function merge(a: FeedRow[], b: FeedRow[]): FeedRow[] {
  if (!b.length) return a
  const out: FeedRow[] = []
  let i = 0
  let j = 0
  while (i < a.length || j < b.length) {
    if (j >= b.length || (i < a.length && rowTime(a[i]) >= rowTime(b[j]))) out.push(a[i++])
    else out.push(b[j++])
  }
  return out
}

/**
 * Equipment episodes and access incidents stand among the alarm rows by their start, newest first. All lists come
 * sorted that way; a PPR row stands where its newest alarm is.
 */
export function withEvents(rows: FeedRow[], episodes: EquipmentEpisode[], incidents: AccessIncident[] = []): FeedRow[] {
  const withEq = merge(rows, episodes.map((episode) => ({ type: 'equipment' as const, episode })))
  return merge(withEq, incidents.map((incident) => ({ type: 'access' as const, incident })))
}

/**
 * A planned check brings dozens of alarms: on the feed it is one row (v1-scope), expandable.
 * Alarms of one PPR episode are collected where the first of them stands on the page. Only alarms whose verdict is
 * the planned check (or still pending) are folded: an alarm of the episode left with «требует реакции» — e.g. the
 * run stopped before the verdicts were refined — stays a row of its own, it must not hide in the episode.
 */
export function feedRows(items: Alarm[], collapsePpr: boolean): FeedRow[] {
  if (!collapsePpr) return items.map((alarm) => ({ type: 'alarm', alarm }))
  const rows: FeedRow[] = []
  const groups = new Map<number, Extract<FeedRow, { type: 'ppr' }>>()
  for (const alarm of items) {
    const ep = alarm.episode
    if (ep?.kind !== 'ppr' || (alarm.verdict !== 'ppr' && alarm.verdict !== 'pending')) {
      rows.push({ type: 'alarm', alarm })
      continue
    }
    const group = groups.get(ep.id)
    if (group) group.alarms.push(alarm)
    else {
      const row = { type: 'ppr' as const, episodeId: ep.id, alarms: [alarm] }
      groups.set(ep.id, row)
      rows.push(row)
    }
  }
  return rows
}
