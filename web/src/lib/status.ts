import type { Status } from '@/lib/api/types'

// Colors of statuses — design-system tokens only (docs/architecture/frontend.md §2).
export const STATUS_BADGE_CLASS: Record<Status, string> = {
  ok: 'bg-status-ok/15 text-status-ok',
  risk: 'bg-status-risk/15 text-status-risk',
  fault: 'bg-status-fault/15 text-status-fault',
  alarm: 'bg-status-alarm/15 text-status-alarm',
  ppr: 'bg-status-ppr/15 text-status-ppr',
  unknown: 'bg-status-unknown/15 text-status-unknown',
}

export const STATUS_DOT_CLASS: Record<Status, string> = {
  ok: 'bg-status-ok',
  risk: 'bg-status-risk',
  fault: 'bg-status-fault',
  alarm: 'bg-status-alarm',
  ppr: 'bg-status-ppr',
  unknown: 'bg-status-unknown',
}
