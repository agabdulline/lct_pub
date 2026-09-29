// Node drawing: color — status token, shape — subsystem, ring — daily model risk, badge — active alarms
// (docs/architecture/frontend.md §4, «Кодирование»).

import type { Status, Subsystem } from '@/lib/api/types'

import type { Effects } from './effects'
import type { GNode } from './model'
import { rgba, type Palette } from './palette'

export type Shape = 'circle' | 'square' | 'diamond' | 'triangle' | 'hexagon' | 'triangleDown'

export const SUBSYSTEM_SHAPE: Record<Subsystem, Shape> = {
  'Пожарная охрана': 'circle',
  'Охранная подсистема': 'square',
  'Диспетчерский контроль': 'diamond',
  'Температурная подсистема': 'triangle',
  'Газовая охрана': 'hexagon',
  'Диагностическая подсистема': 'triangleDown',
}

export function shapePath(ctx: CanvasRenderingContext2D, shape: Shape, x: number, y: number, r: number) {
  ctx.beginPath()
  switch (shape) {
    case 'circle':
      ctx.arc(x, y, r, 0, 2 * Math.PI)
      break
    case 'square': {
      const s = r * 0.9
      ctx.rect(x - s, y - s, 2 * s, 2 * s)
      break
    }
    case 'diamond':
      ctx.moveTo(x, y - r * 1.2)
      ctx.lineTo(x + r * 1.2, y)
      ctx.lineTo(x, y + r * 1.2)
      ctx.lineTo(x - r * 1.2, y)
      ctx.closePath()
      break
    case 'triangle':
      ctx.moveTo(x, y - r * 1.25)
      ctx.lineTo(x + r * 1.15, y + r * 0.8)
      ctx.lineTo(x - r * 1.15, y + r * 0.8)
      ctx.closePath()
      break
    case 'triangleDown':
      ctx.moveTo(x, y + r * 1.25)
      ctx.lineTo(x + r * 1.15, y - r * 0.8)
      ctx.lineTo(x - r * 1.15, y - r * 0.8)
      ctx.closePath()
      break
    case 'hexagon':
      for (let i = 0; i < 6; i++) {
        const a = (Math.PI / 3) * i + Math.PI / 6
        const px = x + r * 1.08 * Math.cos(a)
        const py = y + r * 1.08 * Math.sin(a)
        if (i === 0) ctx.moveTo(px, py)
        else ctx.lineTo(px, py)
      }
      ctx.closePath()
      break
  }
}

export type DrawState = {
  palette: Palette
  effects: Effects
  now: number
  hovered: GNode | null
  selected: string | null
  expanded: string | null
  dimmed: (n: GNode) => boolean
  /** Visible area in graph coordinates — labels outside it are skipped. */
  view: { x0: number; x1: number; y0: number; y1: number } | null
  riskThreshold: number
}

function ringOf(n: GNode, threshold: number): { arc: number; above: boolean } | null {
  if (n.kind === 'channel') return n.risk !== null && n.risk >= 0.2 ? { arc: Math.min(1, n.risk), above: n.risk >= threshold } : null
  if (!n.at_risk || !n.channels) return null
  return { arc: Math.min(1, Math.sqrt(n.at_risk / n.channels)), above: true }
}

/** Healthy channels are quieter than problems, so problems read first. */
const FILL_ALPHA: Record<Status, number> = { ok: 0.55, risk: 1, fault: 1, alarm: 1, ppr: 1, unknown: 0.5 }

function labelVisible(n: GNode, scale: number, st: DrawState): boolean {
  if (st.hovered === n || st.selected === n.id || st.expanded === n.id) return true
  switch (n.kind) {
    case 'district':
    case 'object':
      return true
    case 'sub_object':
      // Real sub-object names are long and collide on the overview: only problems are labelled until zoomed in.
      return scale > 2.4 || n.status === 'alarm' || n.status === 'ppr'
    case 'tag_group':
      return scale > 2.6
    default:
      return scale > 7
  }
}

export function drawNode(n: GNode, ctx: CanvasRenderingContext2D, scale: number, st: DrawState) {
  if (n.x === undefined || n.y === undefined) return
  const { palette, effects, now } = st
  const x = n.x
  const y = n.y
  const r = n.r
  const dim = st.dimmed(n)
  const alpha = dim ? 0.12 : 1
  const aggregate = n.kind !== 'channel'
  const color = effects.color(n.id, palette.status[n.status], now)
  // A subsystem of an external dataset has no shape of its own: a circle.
  const shape: Shape = aggregate ? 'circle' : n.subsystem ? (SUBSYSTEM_SHAPE[n.subsystem as Subsystem] ?? 'circle') : 'circle'

  // Body.
  shapePath(ctx, shape, x, y, r)
  ctx.fillStyle = rgba(color, (aggregate ? 0.9 : FILL_ALPHA[n.status]) * alpha)
  ctx.fill()
  if (aggregate) {
    ctx.lineWidth = Math.max(0.4, 1.2 / scale)
    ctx.strokeStyle = rgba(palette.background, 0.9 * alpha)
    ctx.stroke()
  }

  // Risk ring. Channel: the arc is the daily model score, above the threshold — the risk token.
  // Aggregate: the arc is the share of channels at risk (square root, so 1–5% stay visible) — the maximum
  // over a hundred channels is above the threshold almost everywhere and tells nothing.
  const ring = ringOf(n, st.riskThreshold)
  if (!dim && ring) {
    const ringR = r + Math.max(0.9, 2.2 / scale)
    ctx.beginPath()
    ctx.arc(x, y, ringR, -Math.PI / 2, -Math.PI / 2 + 2 * Math.PI * ring.arc)
    ctx.strokeStyle = rgba(ring.above ? palette.status.risk : palette.muted, ring.above ? 0.95 : 0.55)
    ctx.lineWidth = Math.max(0.5, (ring.above ? 1.8 : 1.1) / scale)
    ctx.stroke()
  }

  // Hover and selection outline.
  if (st.hovered === n || st.selected === n.id || st.expanded === n.id) {
    shapePath(ctx, shape, x, y, r + Math.max(1.2, 3 / scale))
    ctx.strokeStyle = rgba(palette.foreground, st.expanded === n.id && st.hovered !== n ? 0.5 : 0.9)
    ctx.lineWidth = Math.max(0.5, 1.5 / scale)
    ctx.stroke()
  }

  // Active alarm badge on aggregates.
  if (aggregate && !dim && n.active_alarms > 0 && (n.kind !== 'tag_group' || scale > 1.5)) {
    const br = Math.max(r * 0.42, 5 / scale)
    const bx = x + r * 0.78
    const by = y - r * 0.78
    ctx.beginPath()
    ctx.arc(bx, by, br, 0, 2 * Math.PI)
    ctx.fillStyle = rgba(n.status === 'ppr' ? palette.status.ppr : palette.status.alarm)
    ctx.fill()
    ctx.fillStyle = rgba(palette.background)
    ctx.font = `600 ${br * 1.25}px ${palette.font}`
    ctx.textAlign = 'center'
    ctx.textBaseline = 'middle'
    ctx.fillText(n.active_alarms > 99 ? '99+' : String(n.active_alarms), bx, by + br * 0.05)
  }

  // Label.
  if (!dim && labelVisible(n, scale, st)) {
    const v = st.view
    if (v && (x < v.x0 - 50 || x > v.x1 + 50 || y < v.y0 - 20 || y > v.y1 + 20)) return
    const px = n.kind === 'district' || n.kind === 'object' ? 12 : n.kind === 'channel' ? 10 : 11
    const size = px / scale
    ctx.font = `${n.kind === 'object' || n.kind === 'district' ? 600 : 400} ${size}px ${palette.font}`
    ctx.textAlign = 'center'
    ctx.textBaseline = 'top'
    const ty = y + r + (ring ? 3 / scale : 0) + 2 / scale
    ctx.lineWidth = 3 / scale
    ctx.strokeStyle = rgba(palette.background, 0.85)
    ctx.strokeText(n.label, x, ty)
    ctx.fillStyle = rgba(n.kind === 'channel' || n.kind === 'tag_group' ? palette.muted : palette.foreground)
    ctx.fillText(n.label, x, ty)
  }
}

export function paintPointerArea(n: GNode, color: string, ctx: CanvasRenderingContext2D) {
  if (n.x === undefined || n.y === undefined) return
  ctx.fillStyle = color
  ctx.beginPath()
  ctx.arc(n.x, n.y, n.r + 1.5, 0, 2 * Math.PI)
  ctx.fill()
}
