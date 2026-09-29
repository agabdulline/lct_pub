// Animations of the graph: alarm pulse, planned-check wave, smooth color change (frontend.md §4, «Анимация»).
// Effects are drawn on the canvas every frame; they hold node references, so they follow the physics.

import type { GNode } from './model'
import { mix, rgba, type Rgb } from './palette'

type Pulse = { node: GNode; start: number; duration: number; color: Rgb; rings: number; spread: number }
type Wave = { node: GNode; start: number; duration: number; color: Rgb; radius: number }
type Transition = { from: Rgb; start: number }

const TRANSITION_MS = 700

export class Effects {
  private pulses: Pulse[] = []
  private waves: Wave[] = []
  private transitions = new Map<string, Transition>()

  get active(): boolean {
    return this.pulses.length > 0 || this.waves.length > 0 || this.transitions.size > 0
  }

  pulse(node: GNode, color: Rgb, opts: { delay?: number; rings?: number; duration?: number; spread?: number } = {}) {
    // A burst of alarms on one node gives one pulse, not a stack of them.
    const now = performance.now() + (opts.delay ?? 0)
    if (this.pulses.some((p) => p.node === node && Math.abs(p.start - now) < 400)) return
    this.pulses.push({ node, start: now, duration: opts.duration ?? 1400, color, rings: opts.rings ?? 2, spread: opts.spread ?? 14 })
  }

  wave(node: GNode, color: Rgb, radius: number, duration = 2200) {
    this.waves.push({ node, start: performance.now(), duration, color, radius })
  }

  transition(id: string, from: Rgb) {
    this.transitions.set(id, { from, start: performance.now() })
  }

  /** Fill color with a running transition applied. */
  color(id: string, target: Rgb, now: number): Rgb {
    const t = this.transitions.get(id)
    if (!t) return target
    const k = (now - t.start) / TRANSITION_MS
    if (k >= 1) {
      this.transitions.delete(id)
      return target
    }
    return mix(t.from, target, k * (2 - k))
  }

  draw(ctx: CanvasRenderingContext2D, scale: number, now: number) {
    this.waves = this.waves.filter((w) => now - w.start < w.duration)
    for (const w of this.waves) {
      if (w.node.x === undefined || w.node.y === undefined) continue
      const k = (now - w.start) / w.duration
      for (let i = 0; i < 3; i++) {
        const ki = k - i * 0.18
        if (ki <= 0 || ki >= 1) continue
        ctx.beginPath()
        ctx.arc(w.node.x, w.node.y, w.node.r + w.radius * ki, 0, 2 * Math.PI)
        ctx.strokeStyle = rgba(w.color, 0.55 * (1 - ki))
        ctx.lineWidth = (3 * (1 - ki) + 0.6) / Math.sqrt(scale)
        ctx.stroke()
      }
    }

    this.pulses = this.pulses.filter((p) => now - p.start < p.duration)
    for (const p of this.pulses) {
      if (p.node.x === undefined || p.node.y === undefined || now < p.start) continue
      const k = (now - p.start) / p.duration
      // The spread is in screen pixels, so a pulse is visible at any zoom.
      const spread = p.spread / scale
      for (let i = 0; i < p.rings; i++) {
        const ki = k * p.rings - i * 0.6
        if (ki <= 0 || ki >= 1) continue
        ctx.beginPath()
        ctx.arc(p.node.x, p.node.y, p.node.r + spread * ki, 0, 2 * Math.PI)
        ctx.strokeStyle = rgba(p.color, 0.9 * (1 - ki))
        ctx.lineWidth = 2.2 / scale
        ctx.stroke()
      }
      // Glow under the node while the pulse is young.
      if (k < 0.5) {
        ctx.beginPath()
        ctx.arc(p.node.x, p.node.y, p.node.r + spread * 0.25, 0, 2 * Math.PI)
        ctx.fillStyle = rgba(p.color, 0.25 * (1 - k * 2))
        ctx.fill()
      }
    }
  }
}
