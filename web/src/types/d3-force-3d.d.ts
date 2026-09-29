// Minimal typings for the forces used by the object graph (d3-force-3d ships no types).
declare module 'd3-force-3d' {
  type Accessor<N, T> = T | ((node: N, i: number, nodes: N[]) => T)
  interface Force<N> {
    (alpha: number): void
    initialize?: (nodes: N[], ...args: unknown[]) => void
  }
  interface PositionForce<N> extends Force<N> {
    strength(s: Accessor<N, number>): PositionForce<N>
  }
  interface XForce<N> extends PositionForce<N> {
    x(x: Accessor<N, number>): XForce<N>
  }
  interface YForce<N> extends PositionForce<N> {
    y(y: Accessor<N, number>): YForce<N>
  }
  interface CollideForce<N> extends Force<N> {
    radius(r: Accessor<N, number>): CollideForce<N>
    strength(s: number): CollideForce<N>
    iterations(n: number): CollideForce<N>
  }
  interface RadialForce<N> extends PositionForce<N> {
    radius(r: Accessor<N, number>): RadialForce<N>
    x(x: number): RadialForce<N>
    y(y: number): RadialForce<N>
  }
  export function forceCenter<N>(x?: number, y?: number): Force<N>
  export function forceX<N>(x?: Accessor<N, number>): XForce<N>
  export function forceY<N>(y?: Accessor<N, number>): YForce<N>
  export function forceCollide<N>(radius?: Accessor<N, number>): CollideForce<N>
  export function forceRadial<N>(radius: Accessor<N, number>, x?: number, y?: number): RadialForce<N>
}
