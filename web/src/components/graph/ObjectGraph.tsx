import { useQueryClient } from '@tanstack/react-query'
import { forwardRef, useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState } from 'react'
import ForceGraph2D from 'react-force-graph-2d'

import type { AlarmEpisode, ChannelStatusEvent, Graph, Alarm, Status, Subsystem } from '@/lib/api/types'
import { isPprAlert } from '@/lib/alarm'
import { useRun } from '@/lib/run'
import { useStreamEvent } from '@/lib/sse/bus'

import { drawNode, paintPointerArea, type DrawState } from './draw'
import { Effects } from './effects'
import { applyLayout, drawPicketAxis, type FG, type LayoutMode, type PicketAxis } from './layout'
import { GraphModel, type GLink, type GNode, type StatusChange } from './model'
import { rgba, usePalette } from './palette'
import { GraphTooltip } from './GraphTooltip'

export type ObjectGraphHandle = {
  /** Camera to the node; `zoom` — target scale, `offsetX` — screen pixels to keep free on the right. */
  focus: (id: string, zoom?: number, offsetX?: number) => boolean
  fit: (ids?: Set<string>) => void
  /** Runs `fn` when the physics settles (or after 4 s): the camera moves once, to the final layout. */
  whenSettled: (fn: () => void) => void
  zoomBy: (k: number) => void
  hasNode: (id: string) => boolean
  subtree: (id: string) => Set<string>
}

type Props = {
  overview: Graph | undefined
  sub: Graph | undefined
  expandedId: number | null
  layout: LayoutMode
  subsystems: Set<Subsystem> | null
  statuses: Set<Status> | null
  selected: string | null
  riskThreshold: number
  showFps?: boolean
  onNodeClick: (node: GNode) => void
  onBackgroundClick: () => void
}

type Queued =
  | { type: 'alarm'; data: Alarm }
  | { type: 'status'; data: ChannelStatusEvent }
  | { type: 'episode'; data: AlarmEpisode }

const STATUS_RANK: Record<Status, number> = { alarm: 5, ppr: 4, fault: 3, risk: 2, unknown: 1, ok: 0 }

export const ObjectGraph = forwardRef<ObjectGraphHandle, Props>(function ObjectGraph(props, ref) {
  const { overview, sub, expandedId, layout, subsystems, statuses, selected, riskThreshold, showFps } = props
  const fgRef = useRef<FG | undefined>(undefined)
  const model = useMemo(() => new GraphModel(), [])
  const effects = useMemo(() => new Effects(), [])
  const palette = usePalette()
  const qc = useQueryClient()
  const { runKey } = useRun()

  const containerRef = useRef<HTMLDivElement>(null)
  const [size, setSize] = useState({ width: 800, height: 600 })
  const [data, setData] = useState<{ nodes: GNode[]; links: GLink[] }>({ nodes: [], links: [] })
  const [hover, setHover] = useState<{ node: GNode; x: number; y: number } | null>(null)
  const [animating, setAnimating] = useState(false)

  // Mutable state read by canvas callbacks every frame.
  const st = useRef({
    palette,
    hovered: null as GNode | null,
    selected,
    expanded: expandedId !== null ? `sub_object:${expandedId}` : null,
    subsystems,
    statuses,
    view: null as DrawState['view'],
    axis: null as PicketAxis | null,
    queue: [] as Queued[],
    seenPpr: new Set<number>(),
    overviewDirty: false,
    fitted: false,
    frames: 0,
    fpsAt: 0,
    animating: false,
    settled: [] as (() => void)[],
    settleTimer: null as ReturnType<typeof setTimeout> | null,
  })
  const fpsRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    Object.assign(st.current, {
      palette,
      selected,
      subsystems,
      statuses,
      expanded: expandedId !== null ? `sub_object:${expandedId}` : null,
    })
  }, [palette, selected, subsystems, statuses, expandedId])

  const startAnimating = useCallback(() => {
    if (!st.current.animating) {
      st.current.animating = true
      setAnimating(true)
    }
  }, [])

  const applyChanges = useCallback(
    (changes: StatusChange[]) => {
      for (const c of changes) effects.transition(c.node.id, st.current.palette.status[c.from])
      if (changes.length) startAnimating()
    },
    [effects, startAnimating],
  )

  // Container size.
  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const ro = new ResizeObserver(([entry]) => {
      const { width, height } = entry.contentRect
      setSize({ width: Math.max(200, Math.floor(width)), height: Math.max(200, Math.floor(height)) })
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  // Overview data → model.
  useEffect(() => {
    if (!overview) return
    const { structural, changes } = model.setOverview(overview)
    applyChanges(changes)
    // The model is an external mutable store: only structural changes reach React state.
    // eslint-disable-next-line react/set-state-in-effect
    if (structural) setData(model.data())
  }, [overview, model, applyChanges])

  // Expanded sub-object → model.
  useEffect(() => {
    if (expandedId === null) {
      if (model.expanded !== null) {
        model.collapse()
        // eslint-disable-next-line react/set-state-in-effect
        setData(model.data())
      }
      return
    }
    if (!sub || !model.node(`sub_object:${expandedId}`)) return
    const { structural, changes } = model.expand(expandedId, sub)
    applyChanges(changes)
    if (structural) setData(model.data())
  }, [sub, expandedId, model, applyChanges, data.nodes.length])

  // Forces for the layout; re-applied when the node set changes.
  useEffect(() => {
    const fg = fgRef.current
    if (!fg || !data.nodes.length) return
    st.current.axis = applyLayout(fg, model, layout)
    fg.d3ReheatSimulation()
  }, [data, layout, model])

  // Frame-batched stream events: a planned check brings dozens of alarms within one frame.
  useStreamEvent('alarm.created', (a) => {
    st.current.queue.push({ type: 'alarm', data: a })
    startAnimating()
  })
  useStreamEvent('alarm.scored', (a) => {
    const node = model.channel(a.channel_id)
    if (node) node.pprAlert = isPprAlert(a)
  })
  useStreamEvent('channel.status', (e) => {
    st.current.queue.push({ type: 'status', data: e })
    startAnimating()
  })
  useStreamEvent('alarm_episode.updated', (e) => {
    if (e.kind === 'ppr' && !st.current.seenPpr.has(e.id)) {
      st.current.seenPpr.add(e.id)
      st.current.queue.push({ type: 'episode', data: e })
      startAnimating()
    }
  })

  // Aggregates of the overview come from the server: refetch at most every 4 s after live changes.
  useEffect(() => {
    const t = setInterval(() => {
      if (!st.current.overviewDirty) return
      st.current.overviewDirty = false
      void qc.invalidateQueries({ queryKey: ['graph', runKey, '/graph'] })
    }, 4000)
    return () => clearInterval(t)
  }, [qc, runKey])

  const emitChain = useCallback(
    (from: GNode, color: string) => {
      const fg = fgRef.current
      if (!fg) return
      model.chainUp(from.id).forEach((link, i) => {
        setTimeout(() => {
          link.particleColor = color
          fg.emitParticle(link)
        }, i * 260)
      })
    },
    [model],
  )

  const processQueue = useCallback(() => {
    const s = st.current
    if (!s.queue.length) return
    const items = s.queue.splice(0, s.queue.length)
    const p = s.palette
    const particles = new Set<string>()
    const changes: StatusChange[] = []
    const groups = new Set<string>()
    for (const item of items) {
      if (item.type === 'alarm') {
        const a = item.data
        const node = model.channel(a.channel_id) ?? model.node(`sub_object:${a.sub_object_id}`)
        if (!node) continue
        const ppr = a.episode?.kind === 'ppr'
        effects.pulse(node, ppr ? p.status.ppr : p.status.alarm)
        if (!particles.has(node.id)) {
          particles.add(node.id)
          emitChain(node, rgba(ppr ? p.status.ppr : p.status.alarm))
        }
      } else if (item.type === 'status') {
        const e = item.data
        const node = model.channel(e.channel_id)
        if (node && node.status !== e.status) {
          changes.push({ node, from: node.status })
          node.status = e.status
          if (e.risk !== null) node.risk = e.risk
          if (node.parent) groups.add(node.parent)
        }
        // Optimistic aggregates: an alarm lights the sub-object and the object at once.
        for (const id of [`sub_object:${e.sub_object_id}`, `object:${e.object_id}`]) {
          const agg = model.node(id)
          if (agg && STATUS_RANK[e.status] >= 4 && STATUS_RANK[e.status] > STATUS_RANK[agg.status]) {
            changes.push({ node: agg, from: agg.status })
            agg.status = e.status
          }
        }
        s.overviewDirty = true
      } else {
        const e = item.data
        const obj = model.node(`object:${e.object_id}`)
        if (obj) effects.wave(obj, p.status.ppr, 110)
        e.channel_ids.forEach((cid, i) => {
          const node = model.channel(cid)
          if (node) effects.pulse(node, p.status.ppr, { delay: i * 45, rings: 1, duration: 1600 })
        })
      }
    }
    for (const g of groups) {
      const c = model.refreshGroup(g)
      if (c) changes.push(c)
    }
    applyChanges(changes)
  }, [model, effects, emitChain, applyChanges])

  const dimmed = useCallback((n: GNode) => {
    const s = st.current
    if (s.subsystems && n.kind === 'channel' && n.subsystem && !s.subsystems.has(n.subsystem)) return true
    if (s.statuses && (n.kind === 'channel' || n.kind === 'sub_object') && !s.statuses.has(n.status)) return true
    return false
  }, [])

  const nodeCanvasObject = useCallback(
    (node: object, ctx: CanvasRenderingContext2D, scale: number) => {
      const s = st.current
      drawNode(node as GNode, ctx, scale, {
        palette: s.palette,
        effects,
        now: performance.now(),
        hovered: s.hovered,
        selected: s.selected,
        expanded: s.expanded,
        dimmed,
        view: s.view,
        riskThreshold,
      })
    },
    [effects, dimmed, riskThreshold],
  )

  const onRenderFramePre = useCallback(
    (ctx: CanvasRenderingContext2D, scale: number) => {
      processQueue()
      const fg = fgRef.current
      if (fg) {
        const a = fg.screen2GraphCoords(0, 0)
        const b = fg.screen2GraphCoords(size.width, size.height)
        st.current.view = { x0: a.x, y0: a.y, x1: b.x, y1: b.y }
      }
      const axis = st.current.axis
      if (layout === 'pickets' && axis) {
        const p = st.current.palette
        drawPicketAxis(ctx, axis, scale, { text: rgba(p.muted, 0.9), line: rgba(p.muted, 0.35), font: p.font })
      }
    },
    [processQueue, size, layout],
  )

  const onRenderFramePost = useCallback(
    (ctx: CanvasRenderingContext2D, scale: number) => {
      const now = performance.now()
      effects.draw(ctx, scale, now)
      const s = st.current
      if (s.animating && !effects.active && !s.queue.length) {
        s.animating = false
        setAnimating(false)
      }
      if (showFps) {
        s.frames++
        if (now - s.fpsAt >= 1000) {
          if (fpsRef.current) fpsRef.current.textContent = `${Math.round((s.frames * 1000) / (now - s.fpsAt))} к/с · ${model.nodes.size} узлов`
          s.frames = 0
          s.fpsAt = now
        }
      }
    },
    [effects, showFps, model],
  )

  const flushSettled = useCallback(() => {
    const s = st.current
    if (s.settleTimer) clearTimeout(s.settleTimer)
    s.settleTimer = null
    s.settled.splice(0).forEach((fn) => fn())
  }, [])

  const onEngineStop = useCallback(() => {
    flushSettled()
    if (st.current.fitted) return
    st.current.fitted = true
    fgRef.current?.zoomToFit(600, 40)
  }, [flushSettled])

  const onNodeHover = useCallback((node: object | null) => {
    const n = node as GNode | null
    st.current.hovered = n
    const fg = fgRef.current
    if (n && fg && n.x !== undefined && n.y !== undefined) {
      const p = fg.graph2ScreenCoords(n.x, n.y)
      setHover({ node: n, x: p.x, y: p.y })
    } else setHover(null)
    if (containerRef.current) containerRef.current.style.cursor = n ? 'pointer' : 'grab'
  }, [])

  useImperativeHandle(
    ref,
    () => ({
      focus: (id, zoom, offsetX = 0) => {
        const n = model.node(id)
        const fg = fgRef.current
        if (!n || !fg || n.x === undefined || n.y === undefined) return false
        const k = zoom ?? fg.zoom()
        fg.centerAt(n.x + offsetX / 2 / k, n.y, 800)
        if (zoom) fg.zoom(zoom, 800)
        effects.pulse(n, st.current.palette.foreground, { rings: 3, duration: 1800, spread: 22 })
        startAnimating()
        return true
      },
      fit: (ids) => fgRef.current?.zoomToFit(800, 50, ids ? (n: object) => ids.has((n as GNode).id) : undefined),
      whenSettled: (fn) => {
        const s = st.current
        s.fitted = true
        s.settled.push(fn)
        if (s.settleTimer) clearTimeout(s.settleTimer)
        // A 900-channel sub-object needs ~3 s to spread; zooming in earlier shows a heap.
        s.settleTimer = setTimeout(flushSettled, 4000)
      },
      zoomBy: (k) => {
        const fg = fgRef.current
        if (fg) fg.zoom(fg.zoom() * k, 300)
      },
      hasNode: (id) => model.nodes.has(id),
      subtree: (id) => model.subtree(id),
    }),
    [model, effects, startAnimating, flushSettled],
  )

  const p = palette
  return (
    <div ref={containerRef} className="relative size-full overflow-hidden" style={{ cursor: 'grab' }}>
      <ForceGraph2D
        ref={fgRef}
        width={size.width}
        height={size.height}
        graphData={data}
        nodeId="id"
        backgroundColor={rgba(p.background)}
        nodeCanvasObject={nodeCanvasObject}
        nodePointerAreaPaint={(n, color, ctx) => paintPointerArea(n as GNode, color, ctx)}
        nodeVisibility={(n) => {
          const node = n as GNode
          if (layout !== 'pickets' || expandedId === null) return true
          return node.depth >= 3 || node.id === `sub_object:${expandedId}`
        }}
        linkVisibility={(l) => {
          const link = l as GLink
          if (layout !== 'pickets' || expandedId === null) return true
          return link.depth >= 4
        }}
        linkColor={(l) => rgba(p.muted, (l as GLink).depth >= 4 ? (layout === 'pickets' ? 0.07 : 0.14) : (l as GLink).depth === 3 ? 0.3 : 0.35)}
        linkWidth={(l) => ((l as GLink).depth >= 4 ? 0.5 : 1)}
        linkDirectionalParticleWidth={3.2}
        linkDirectionalParticleSpeed={0.035}
        linkDirectionalParticleColor={(l) => (l as GLink).particleColor ?? rgba(p.status.alarm)}
        autoPauseRedraw={!animating}
        d3AlphaDecay={0.035}
        d3VelocityDecay={0.38}
        cooldownTime={6000}
        minZoom={0.2}
        maxZoom={40}
        onRenderFramePre={onRenderFramePre}
        onRenderFramePost={onRenderFramePost}
        onEngineStop={onEngineStop}
        onNodeHover={onNodeHover}
        onNodeClick={(n) => props.onNodeClick(n as GNode)}
        onBackgroundClick={props.onBackgroundClick}
        onZoom={() => hover && setHover(null)}
        showPointerCursor={false}
      />
      {hover && <GraphTooltip node={hover.node} x={hover.x} y={hover.y} riskThreshold={riskThreshold} />}
      {showFps && (
        <div ref={fpsRef} className="absolute top-2 left-2 rounded-md bg-card/90 px-2 py-1 font-mono text-xs text-muted-foreground ring-1 ring-foreground/10">
          …
        </div>
      )}
    </div>
  )
})
