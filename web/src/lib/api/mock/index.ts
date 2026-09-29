// Mock transport: replaces fetch and EventSource when VITE_API_MOCK=1 (docs/architecture/frontend.md §5).

import type { StreamEventType } from '@/lib/api/types'

import { MockHttpError, handle } from './handlers'
import { getWorld } from './world'

const latency = () => new Promise((r) => setTimeout(r, 80 + Math.random() * 180))

export async function mockRequest(method: string, path: string, query: URLSearchParams, body: unknown): Promise<unknown> {
  await latency()
  try {
    const result = handle(method, path, query, body)
    // A copy, as over the network: screens must not share objects with the simulation.
    return typeof result === 'string' ? result : structuredClone(result)
  } catch (e) {
    if (e instanceof MockHttpError) throw e
    console.error('[mock]', method, path, e)
    throw new MockHttpError(500, 'internal', String(e))
  }
}

export { MockHttpError }

/** POST /scenario-uploads: the file is read in the browser (lib/api/mock/upload.ts). */
export async function mockUpload(path: string, file: File, onProgress?: (share: number) => void): Promise<unknown> {
  if (path !== '/scenario-uploads') throw new MockHttpError(404, 'not_found', `Нет мок-загрузки для ${path}`)
  const { analyzeUpload } = await import('./upload')
  return structuredClone(await analyzeUpload(file, onProgress))
}

type Handler = (event: MessageEvent<string>) => void

/** Subset of EventSource used by the stream provider. */
export class MockEventSource {
  readyState = 0
  onopen: (() => void) | null = null
  onerror: (() => void) | null = null
  private handlers = new Map<string, Set<Handler>>()
  private unsubscribe: (() => void) | null = null

  constructor(url: string) {
    const run = new URL(url, 'http://mock').searchParams.get('run')
    setTimeout(() => {
      if (this.readyState === 2) return
      this.unsubscribe = getWorld().subscribe(run ? Number(run) : null, (type: StreamEventType, data) => {
        const event = new MessageEvent(type, { data: JSON.stringify(data) })
        this.handlers.get(type)?.forEach((h) => h(event))
      })
      this.readyState = 1
      this.onopen?.()
    }, 150)
  }

  addEventListener(type: string, handler: Handler) {
    const set = this.handlers.get(type) ?? new Set()
    set.add(handler)
    this.handlers.set(type, set)
  }

  removeEventListener(type: string, handler: Handler) {
    this.handlers.get(type)?.delete(handler)
  }

  close() {
    this.readyState = 2
    this.unsubscribe?.()
  }
}
