// What re-verdicting the past alarms of a run changed (15.2, `apply_to_past`), in words.

import type { ReverdictSummary, Verdict } from '@/lib/api/types'
import { fmtInt, plural } from '@/lib/format'
import { VERDICT_LABEL, versionLabel } from '@/lib/labels'

const verdictLabel = (v: string) => VERDICT_LABEL[v as Verdict] ?? v
const alarmsWord = (n: number) => plural(n, ['тревога', 'тревоги', 'тревог'])

/** Verdict moves of a re-verdict, «было→стало»: n — only real changes, the largest first. */
function verdictMoves(verdicts: Record<string, number> | undefined): [string, string, number][] {
  return Object.entries(verdicts ?? {})
    .map(([k, n]): [string, string, number] => {
      const [from, to] = k.split('→')
      return [from, to ?? '', n]
    })
    .filter(([from, to, n]) => from !== to && n > 0)
    .sort((a, b) => b[2] - a[2])
}

/**
 * What re-verdicting the past alarms did, in words (the toast after a switch and the audit): how many alarms, how
 * many now follow the new version, whose verdict actually changed, how many it had no decision on, new work orders.
 */
export function reverdictText(r: Pick<ReverdictSummary, 'version' | 'alarms' | 'changed' | 'verdicts' | 'no_decision' | 'orders_created'>): string {
  const moves = verdictMoves(r.verdicts)
  const moved = moves.reduce((n, [, , c]) => n + c, 0)
  const v = versionLabel(r.version)
  return [
    `${fmtInt(r.alarms)} ${alarmsWord(r.alarms)} прогона`,
    `решение ${v} взято для ${fmtInt(r.changed)}`,
    moved
      ? `вердикт сменился у ${fmtInt(moved)}: ${moves.map(([from, to, n]) => `${verdictLabel(from)} → ${verdictLabel(to)} — ${fmtInt(n)}`).join(', ')}`
      : 'вердикты не сменились',
    r.no_decision ? `без решения ${v} — ${fmtInt(r.no_decision)}, их вердикт прежний` : null,
    r.orders_created ? `новых заявок — ${fmtInt(r.orders_created)}` : 'новых заявок нет',
  ]
    .filter(Boolean)
    .join('; ')
}
