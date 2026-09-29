// Layouts of the graph (docs/architecture/frontend.md §4, «Раскладки подобъекта»):
// - groups — free physics, channels cluster around their tag groups;
// - pickets — X is bound to the picket number, Y — a band per subsystem: a live linear scheme of the collector.

import { forceCenter, forceCollide, forceX, forceY } from 'd3-force-3d'
import type { ForceGraphMethods } from 'react-force-graph-2d'

import { SUBSYSTEMS } from '@/lib/api/types'

import type { GLink, GNode, GraphModel } from './model'

export type LayoutMode = 'groups' | 'pickets'

// The generic parameters of ForceGraphMethods differ between node and link accessors; one alias is enough here.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type FG = ForceGraphMethods<any, any>

const LINK_DISTANCE = [0, 120, 42, 30, 9]
const CHARGE = [-400, -260, -70, -30, -5]

// Hackathon objects stay in the middle; every external dataset gets its own cluster to the right, one above
// another, so our network and the datasets never mix. Only districts, objects and sub-objects are pulled —
// tag groups and channels follow them over the links.
const SOURCE_GAP = 900
const DATASET_STEP = 520
const SOURCE_PULL = 0.08

function sourceAnchors(model: GraphModel): Map<string, [number, number]> {
  const external = [...new Set([...model.nodes.values()].map((n) => n.source))]
    .filter((s) => s && s !== 'hackathon')
    .sort()
  const anchors = new Map<string, [number, number]>([['hackathon', [0, 0]]])
  external.forEach((s, i) => anchors.set(s, [SOURCE_GAP, (i - (external.length - 1) / 2) * DATASET_STEP]))
  return anchors
}

type ManyBody = { strength: (s: (n: GNode) => number) => ManyBody; distanceMax: (d: number) => ManyBody }
type LinkForce = { distance: (d: (l: GLink) => number) => LinkForce; strength: (s: (l: GLink) => number) => LinkForce }

/** Picket axis geometry of the expanded sub-object — for forces and for drawing the axis. */
export type PicketAxis = {
  cx: number
  cy: number
  minP: number
  maxP: number
  unit: number
  /** Bands top to bottom: the hackathon subsystems, then any other ones present (external datasets). */
  bands: string[]
  bandY: Record<string, number>
  bandH: Record<string, number>
  noPicketX: number
  groupY: number
}

export function picketAxis(model: GraphModel): PicketAxis | null {
  if (model.expanded === null) return null
  const sub = model.node(`sub_object:${model.expanded}`)
  if (!sub || sub.x === undefined || sub.y === undefined) return null
  let minP = Infinity
  let maxP = -Infinity
  for (const n of model.nodes.values()) {
    if (n.kind !== 'channel' || n.picket_number === null) continue
    minP = Math.min(minP, n.picket_number)
    maxP = Math.max(maxP, n.picket_number)
  }
  if (!Number.isFinite(minP)) {
    minP = 0
    maxP = 1
  }
  const span = Math.max(1, maxP - minP)
  // Wide enough to separate neighbours, not wider than ~900 units.
  const unit = Math.min(12, Math.max(1.6, 900 / span))
  // Band height follows the density: fire detectors are half of the channels and stack at one picket.
  const known = new Set<string>(SUBSYSTEMS)
  const extra = [...new Set([...model.nodes.values()].filter((n) => n.kind === 'channel' && n.subsystem && !known.has(n.subsystem)).map((n) => n.subsystem!))].sort()
  const bands: string[] = [...SUBSYSTEMS, ...extra]
  const counts: Record<string, number> = Object.fromEntries(bands.map((s) => [s, 0]))
  for (const n of model.nodes.values()) if (n.kind === 'channel' && n.subsystem) counts[n.subsystem] = (counts[n.subsystem] ?? 0) + 1
  const heights = bands.map((s) => {
    const perPicket = counts[s] / (span + 1)
    const rows = Math.ceil((perPicket * 4.6) / unit)
    return counts[s] ? Math.max(14, rows * 4.8 + 8) : 10
  })
  const total = heights.reduce((a, b) => a + b, 0)
  const bandY: Record<string, number> = {}
  const bandH: Record<string, number> = {}
  let top = sub.y - total / 2
  bands.forEach((s, i) => {
    bandY[s] = top + heights[i] / 2
    bandH[s] = heights[i]
    top += heights[i]
  })
  return { cx: sub.x, cy: sub.y, minP, maxP, unit, bands, bandY, bandH, noPicketX: 0, groupY: sub.y - total / 2 - 40 }
}

export const picketX = (axis: PicketAxis, p: number) => axis.cx + (p - (axis.minP + axis.maxP) / 2) * axis.unit

/** Radius of the expanded sub-object cluster: ~290 units for the 994-channel worst case. */
export const clusterRadius = (channels: number) => 24 + Math.sqrt(channels) * 8.5

/**
 * Keeps the expanded sub-object readable: the rest of the map moves out of its circle,
 * its groups and channels stay inside.
 */
function clearingForce(model: GraphModel, radius: number) {
  let inside: GNode[] = []
  let outside: GNode[] = []
  let center: GNode | undefined
  const force = (alpha: number) => {
    if (!center || center.x === undefined || center.y === undefined) return
    const cx = center.x
    const cy = center.y
    for (const n of outside) {
      if (n.x === undefined || n.y === undefined) continue
      const dx = n.x - cx
      const dy = n.y - cy
      const d = Math.hypot(dx, dy) || 1
      if (d < radius) {
        const k = ((radius - d) / d) * alpha * 0.6
        n.vx = (n.vx ?? 0) + dx * k
        n.vy = (n.vy ?? 0) + dy * k
      }
    }
    for (const n of inside) {
      if (n.x === undefined || n.y === undefined) continue
      const limit = radius * (n.kind === 'channel' ? 0.95 : 0.8)
      const dx = n.x - cx
      const dy = n.y - cy
      const d = Math.hypot(dx, dy) || 1
      if (d > limit) {
        const k = ((d - limit) / d) * alpha * 0.5
        n.vx = (n.vx ?? 0) - dx * k
        n.vy = (n.vy ?? 0) - dy * k
      }
    }
  }
  force.initialize = (nodes: GNode[]) => {
    center = model.expanded !== null ? model.node(`sub_object:${model.expanded}`) : undefined
    const sub = center ? model.subtree(center.id) : new Set<string>()
    inside = nodes.filter((n) => sub.has(n.id) && n !== center)
    outside = nodes.filter((n) => !sub.has(n.id))
  }
  return force
}

export function applyLayout(fg: FG, model: GraphModel, mode: LayoutMode): PicketAxis | null {
  const charge = fg.d3Force('charge') as unknown as ManyBody | undefined
  const link = fg.d3Force('link') as unknown as LinkForce | undefined
  const collide = forceCollide<GNode>((n) => n.r + (n.kind === 'channel' ? 0.8 : 3)).iterations(1)
  fg.d3Force('collide', collide)

  if (mode === 'groups' || model.expanded === null) {
    for (const n of model.nodes.values()) {
      if (n.depth < 3) {
        n.fx = undefined
        n.fy = undefined
      }
    }
    const expanded = model.expanded !== null ? model.node(`sub_object:${model.expanded}`) : undefined
    const radius = expanded ? clusterRadius(expanded.channels ?? 0) : 0
    charge?.strength((n) => CHARGE[n.depth]).distanceMax(320)
    link
      ?.distance((l) => (expanded && l.source === expanded ? radius * 0.9 : LINK_DISTANCE[l.depth]))
      .strength((l) => (l.depth >= 4 ? 0.8 : l.depth === 3 ? 0.5 : 0.35))
    const anchors = sourceAnchors(model)
    const pull = (n: GNode) => (anchors.size > 1 && n.depth <= 2 ? SOURCE_PULL : 0)
    fg.d3Force('x', forceX<GNode>((n) => anchors.get(n.source)?.[0] ?? 0).strength(pull))
    fg.d3Force('y', forceY<GNode>((n) => anchors.get(n.source)?.[1] ?? 0).strength(pull))
    fg.d3Force('center', forceCenter<GNode>())
    fg.d3Force('clear', expanded ? clearingForce(model, radius) : null)
    return null
  }
  // Centering would shift the free nodes off their picket targets.
  fg.d3Force('center', null)
  fg.d3Force('clear', null)

  // Pickets: the rest of the map stands still, the expanded sub-object unrolls along the axis.
  const axis = picketAxis(model)
  if (!axis) return null
  const sub = model.node(`sub_object:${model.expanded}`)!
  for (const n of model.nodes.values()) {
    if (n.depth < 3 && n !== sub) {
      n.fx = n.x
      n.fy = n.y
    }
  }
  const left = picketX(axis, axis.minP) - 60
  const right = picketX(axis, axis.maxP) + 50
  axis.noPicketX = right
  sub.fx = left
  sub.fy = axis.cy
  const target = (n: GNode): [number, number] | null => {
    if (n.kind === 'channel') {
      const y = n.subsystem ? axis.bandY[n.subsystem] : axis.cy
      return [n.picket_number === null ? right : picketX(axis, n.picket_number), y]
    }
    if (n.kind === 'tag_group') return [n.picket_number === null ? right : picketX(axis, n.picket_number), axis.groupY]
    return null
  }
  fg.d3Force(
    'x',
    forceX<GNode>((n) => target(n)?.[0] ?? n.x ?? 0).strength((n) => (target(n) ? (n.kind === 'channel' ? 0.7 : 0.5) : 0)),
  )
  fg.d3Force(
    'y',
    forceY<GNode>((n) => target(n)?.[1] ?? n.y ?? 0).strength((n) => (target(n) ? (n.kind === 'channel' ? 0.7 : 0.5) : 0)),
  )
  charge?.strength((n) => (n.depth >= 3 ? -1.5 : 0)).distanceMax(40)
  link?.distance((l) => (l.depth >= 3 ? 30 : LINK_DISTANCE[l.depth])).strength((l) => (l.depth >= 3 ? 0.01 : 0))
  return axis
}

/** Axis ticks and subsystem bands under the picket layout. */
export function drawPicketAxis(
  ctx: CanvasRenderingContext2D,
  axis: PicketAxis,
  scale: number,
  colors: { text: string; line: string; font: string },
) {
  const x0 = picketX(axis, axis.minP)
  const x1 = picketX(axis, axis.maxP)
  const first = axis.bands[0]
  const last = axis.bands[axis.bands.length - 1]
  const top = axis.bandY[first] - axis.bandH[first] / 2
  const bottom = axis.bandY[last] + axis.bandH[last] / 2 + 4
  ctx.save()
  ctx.strokeStyle = colors.line
  ctx.fillStyle = colors.text
  ctx.lineWidth = 1 / scale
  const px = 10 / scale
  ctx.font = `${px}px ${colors.font}`

  for (const s of axis.bands) {
    // Band boundary above, name at its left end.
    const y = axis.bandY[s] - axis.bandH[s] / 2
    ctx.beginPath()
    ctx.setLineDash([2 / scale, 4 / scale])
    ctx.moveTo(x0 - 20, y)
    ctx.lineTo(x1 + 20, y)
    ctx.stroke()
    ctx.setLineDash([])
    ctx.textAlign = 'left'
    ctx.textBaseline = 'top'
    ctx.fillText(s, x0 - 20, y + 2 / scale)
  }

  // Tick every N pickets so labels do not overlap on screen.
  const span = axis.maxP - axis.minP
  const pxPerPicket = axis.unit * scale
  const steps = [1, 2, 5, 10, 20, 50, 100]
  const step = steps.find((s) => s * pxPerPicket >= 42) ?? 100
  ctx.textAlign = 'center'
  ctx.textBaseline = 'top'
  for (let p = Math.ceil(axis.minP / step) * step; p <= axis.minP + span; p += step) {
    const x = picketX(axis, p)
    ctx.beginPath()
    ctx.moveTo(x, bottom)
    ctx.lineTo(x, bottom + 4 / scale)
    ctx.stroke()
    ctx.fillText(`ПК${p}`, x, bottom + 6 / scale)
  }
  ctx.beginPath()
  ctx.moveTo(x0, bottom)
  ctx.lineTo(x1, bottom)
  ctx.stroke()
  ctx.textAlign = 'center'
  ctx.textBaseline = 'bottom'
  ctx.fillText('без пикета', axis.noPicketX, top)
  ctx.fillText('группы по тегу', (x0 + x1) / 2, axis.groupY - 10)
  ctx.restore()
}
