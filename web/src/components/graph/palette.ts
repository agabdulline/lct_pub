// Canvas colors come from the same CSS tokens as the rest of the UI (docs/architecture/frontend.md §2):
// a token is resolved to RGB once per theme, so the graph can blend and animate colors.

import { useMemo } from 'react'

import type { Status } from '@/lib/api/types'
import { useTheme } from '@/lib/theme'

export type Rgb = readonly [number, number, number]

export type Palette = {
  status: Record<Status, Rgb>
  background: Rgb
  foreground: Rgb
  muted: Rgb
  border: Rgb
  card: Rgb
  font: string
}

const STATUSES: Status[] = ['ok', 'risk', 'fault', 'alarm', 'ppr', 'unknown']

let probe: CanvasRenderingContext2D | null = null

function resolve(value: string, fallback: Rgb): Rgb {
  if (!value) return fallback
  probe ??= document.createElement('canvas').getContext('2d', { willReadFrequently: true })
  if (!probe) return fallback
  probe.clearRect(0, 0, 1, 1)
  probe.fillStyle = '#000'
  probe.fillStyle = value
  probe.fillRect(0, 0, 1, 1)
  const [r, g, b] = probe.getImageData(0, 0, 1, 1).data
  return [r, g, b]
}

export function readPalette(): Palette {
  const cs = getComputedStyle(document.documentElement)
  const token = (name: string, fallback: Rgb) => resolve(cs.getPropertyValue(name).trim(), fallback)
  const status = Object.fromEntries(STATUSES.map((s) => [s, token(`--status-${s}`, [128, 128, 128])])) as Record<Status, Rgb>
  return {
    status,
    background: token('--background', [0, 0, 0]),
    foreground: token('--foreground', [255, 255, 255]),
    muted: token('--muted-foreground', [160, 160, 160]),
    border: token('--border', [60, 60, 60]),
    card: token('--card', [30, 30, 30]),
    font: cs.getPropertyValue('--font-sans').trim() || 'sans-serif',
  }
}

export const rgba = (c: Rgb, a = 1) => `rgba(${c[0]},${c[1]},${c[2]},${a})`

export const mix = (a: Rgb, b: Rgb, t: number): Rgb => [
  Math.round(a[0] + (b[0] - a[0]) * t),
  Math.round(a[1] + (b[1] - a[1]) * t),
  Math.round(a[2] + (b[2] - a[2]) * t),
]

export function usePalette(): Palette {
  const theme = useTheme()
  // eslint-disable-next-line react-hooks/exhaustive-deps -- the palette depends on the theme class on <html>
  return useMemo(() => readPalette(), [theme])
}
