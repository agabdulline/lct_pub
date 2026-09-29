// Graph model: node objects live across API refetches and SSE updates, so the physics keeps positions
// and only structural changes (expand, collapse) reheat the simulation (docs/architecture/frontend.md §4).

import type { Graph, GraphNode, GraphNodeKind, Status } from '@/lib/api/types'

export type GNode = GraphNode & {
  x?: number
  y?: number
  vx?: number
  vy?: number
  fx?: number
  fy?: number
  /** Radius in graph units. */
  r: number
  depth: number
  /** Numeric id of the channel or the object. */
  num: number
  /** Sub-object the node belongs to (for channels and tag groups — the parent sub-object). */
  subObjectId: number | null
  /** A12: the live stream reported an alarm inside a planned check with the model above the threshold. */
  pprAlert?: boolean
}

/** Edges go child → parent: particles of a new alarm travel up to the object. */
export type GLink = { source: string | GNode; target: string | GNode; depth: number; particleColor?: string }

const DEPTH: Record<GraphNodeKind, number> = { district: 0, object: 1, sub_object: 2, tag_group: 3, channel: 4 }

export function radius(kind: GraphNodeKind, channels: number | null): number {
  const n = channels ?? 0
  switch (kind) {
    case 'district':
      return 11
    case 'object':
      return 5 + Math.sqrt(n) / 7
    case 'sub_object':
      return 3 + Math.sqrt(n) / 5.5
    case 'tag_group':
      return 2.4 + Math.sqrt(n) / 3.2
    default:
      return 2.1
  }
}

export function numericId(id: string): number {
  const parts = id.split(':')
  return Number(parts[1])
}

/** Aggregate status rule — the same as the backend (docs/api.md, «Статус агрегата»). */
export function aggregateStatus(children: { status: Status }[]): { status: Status; active: number } {
  let alarm = 0
  let ppr = 0
  let fault = 0
  let risk = 0
  for (const c of children) {
    if (c.status === 'alarm') alarm++
    else if (c.status === 'ppr') ppr++
    else if (c.status === 'fault') fault++
    else if (c.status === 'risk') risk++
  }
  const n = children.length || 1
  const status: Status = alarm ? 'alarm' : ppr ? 'ppr' : fault >= n * 0.1 ? 'fault' : risk >= n * 0.05 ? 'risk' : 'ok'
  return { status, active: alarm + ppr }
}

export type StatusChange = { node: GNode; from: Status }

export class GraphModel {
  readonly nodes = new Map<string, GNode>()
  readonly links = new Map<string, GLink>()
  readonly children = new Map<string, Set<string>>()
  expanded: number | null = null

  private addChild(parent: string, child: string) {
    const set = this.children.get(parent) ?? new Set()
    set.add(child)
    this.children.set(parent, set)
  }

  private upsert(n: GraphNode, changes: StatusChange[]): boolean {
    const existing = this.nodes.get(n.id)
    if (existing) {
      if (existing.status !== n.status) changes.push({ node: existing, from: existing.status })
      Object.assign(existing, n, { r: radius(n.kind, n.channels) })
      return false
    }
    const parent = n.parent ? this.nodes.get(n.parent) : undefined
    const jitter = () => (Math.random() - 0.5) * 4
    const node: GNode = {
      ...n,
      r: radius(n.kind, n.channels),
      depth: DEPTH[n.kind],
      num: numericId(n.id),
      subObjectId: null,
      // New nodes fly out of their parent.
      x: parent?.x !== undefined ? parent.x + jitter() : undefined,
      y: parent?.y !== undefined ? parent.y + jitter() : undefined,
    }
    this.nodes.set(n.id, node)
    return true
  }

  private setEdges(g: Graph) {
    for (const e of g.edges) {
      const child = this.nodes.get(e.target)
      if (!child) continue
      if (!this.links.has(e.target)) this.links.set(e.target, { source: e.target, target: e.source, depth: child.depth })
      this.addChild(e.source, e.target)
    }
  }

  /** Overview: district, objects, sub-objects. Returns whether the node set changed. */
  setOverview(g: Graph): { structural: boolean; changes: StatusChange[] } {
    const changes: StatusChange[] = []
    let structural = false
    // Parents first, so children start next to them.
    const ordered = [...g.nodes].sort((a, b) => DEPTH[a.kind] - DEPTH[b.kind])
    for (const n of ordered) if (this.upsert(n, changes)) structural = true
    for (const n of this.nodes.values()) if (n.kind === 'sub_object') n.subObjectId = n.num
    this.setEdges(g)
    return { structural, changes }
  }

  /** Expanded sub-object: tag groups and channels. Only one sub-object is expanded at a time. */
  expand(subObjectId: number, g: Graph): { structural: boolean; changes: StatusChange[] } {
    let structural = false
    if (this.expanded !== subObjectId) {
      this.collapse()
      structural = true
    }
    this.expanded = subObjectId
    const changes: StatusChange[] = []
    const ordered = [...g.nodes].sort((a, b) => DEPTH[a.kind] - DEPTH[b.kind])
    for (const n of ordered) {
      if (n.kind !== 'tag_group' && n.kind !== 'channel') continue
      if (this.upsert(n, changes)) structural = true
      this.nodes.get(n.id)!.subObjectId = subObjectId
    }
    this.setEdges(g)
    return { structural, changes }
  }

  collapse() {
    if (this.expanded === null) return
    for (const [id, n] of this.nodes) {
      if (n.depth >= 3) {
        this.nodes.delete(id)
        this.links.delete(id)
        this.children.delete(id)
      }
    }
    this.children.get(`sub_object:${this.expanded}`)?.clear()
    this.expanded = null
  }

  data() {
    return { nodes: [...this.nodes.values()], links: [...this.links.values()] }
  }

  node(id: string) {
    return this.nodes.get(id)
  }

  channel(channelId: number) {
    return this.nodes.get(`channel:${channelId}`)
  }

  /** Links from the node up to the district. */
  chainUp(id: string): GLink[] {
    const out: GLink[] = []
    for (let link = this.links.get(id); link; ) {
      out.push(link)
      const parent = typeof link.target === 'string' ? link.target : String(link.target.id)
      link = this.links.get(parent)
    }
    return out
  }

  /** The node and everything under it. */
  subtree(id: string): Set<string> {
    const out = new Set<string>([id])
    const stack = [id]
    while (stack.length) {
      for (const c of this.children.get(stack.pop()!) ?? []) {
        out.add(c)
        stack.push(c)
      }
    }
    return out
  }

  /** Recompute tag-group aggregates of the expanded sub-object from its channels (after SSE changes). */
  refreshGroup(groupId: string): StatusChange | null {
    const group = this.nodes.get(groupId)
    if (!group) return null
    const channels = [...(this.children.get(groupId) ?? [])].map((c) => this.nodes.get(c)!).filter(Boolean)
    const { status, active } = aggregateStatus(channels)
    group.active_alarms = active
    group.at_risk = channels.filter((c) => c.status === 'risk').length
    group.in_fault = channels.filter((c) => c.status === 'fault').length
    if (status === group.status) return null
    const change = { node: group, from: group.status }
    group.status = status
    return change
  }
}
