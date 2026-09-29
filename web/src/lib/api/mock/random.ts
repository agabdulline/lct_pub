// Seeded random helpers: the mock world is synthetic and the same on every load.

export type Rng = () => number

export function createRng(seed: number): Rng {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6d2b79f5) >>> 0
    let t = a
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

export const randInt = (rng: Rng, min: number, max: number) => min + Math.floor(rng() * (max - min + 1))

export const pick = <T>(rng: Rng, items: readonly T[]): T => items[Math.floor(rng() * items.length)]

export function weighted<T>(rng: Rng, items: readonly (readonly [T, number])[]): T {
  const total = items.reduce((s, [, w]) => s + w, 0)
  let r = rng() * total
  for (const [item, w] of items) {
    r -= w
    if (r <= 0) return item
  }
  return items[items.length - 1][0]
}

export function normal(rng: Rng): number {
  const u = Math.max(rng(), 1e-12)
  return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * rng())
}

export const lognormal = (rng: Rng, median: number, sigma: number) => median * Math.exp(sigma * normal(rng))

/** Exponential interval for a Poisson process with `ratePerHour`, in milliseconds. */
export const expIntervalMs = (rng: Rng, ratePerHour: number) =>
  (-Math.log(Math.max(rng(), 1e-12)) / ratePerHour) * 3_600_000

/** Beta-like score in (0, 1) skewed to `mean`. */
export function score(rng: Rng, mean: number, spread = 6): number {
  const a = Math.max(mean * spread, 0.2)
  const b = Math.max((1 - mean) * spread, 0.2)
  // Approximation: average of gamma-free uniforms around the mean, good enough for a mock.
  let x = 0
  for (let i = 0; i < 3; i++) x += rng()
  const noise = (x / 3 - 0.5) * (1 / Math.sqrt(a + b)) * 2.4
  return Math.min(0.995, Math.max(0.005, mean + noise))
}

export function shuffle<T>(rng: Rng, items: T[]): T[] {
  for (let i = items.length - 1; i > 0; i--) {
    const j = Math.floor(rng() * (i + 1))
    ;[items[i], items[j]] = [items[j], items[i]]
  }
  return items
}
