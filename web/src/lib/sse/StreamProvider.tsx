import { useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useState, type ReactNode } from 'react'

import { openEventSource } from '@/lib/api/client'
import { STREAM_EVENT_TYPES, type StreamEventMap, type StreamEventType } from '@/lib/api/types'
import { useRun } from '@/lib/run'
import { StreamBus, StreamBusContext, StreamStatusContext, type StreamStatus } from '@/lib/sse/bus'
import { StreamCache } from '@/lib/sse/cache'

// One EventSource per app on /stream?run= (D5). Reconnects with backoff when the server closes the stream.
export function StreamProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient()
  const { runId, runKey, ready } = useRun()
  const bus = useMemo(() => new StreamBus(), [])
  const [status, setStatus] = useState<StreamStatus>('connecting')
  const [lastEventAt, setLastEventAt] = useState<number | null>(null)

  useEffect(() => {
    if (!ready) return
    let source: EventSource | null = null
    let retry: ReturnType<typeof setTimeout> | null = null
    let attempt = 0
    let opened = false
    let disposed = false
    let lastMark = 0
    const cache = new StreamCache(qc, runKey)

    const connect = async () => {
      setStatus('connecting')
      const es = await openEventSource({ run: runId })
      if (disposed) return es.close()
      source = es
      es.onopen = () => {
        attempt = 0
        setStatus('open')
        // Events sent while the stream was down are lost: refetch everything the screens show.
        if (opened) void qc.invalidateQueries()
        opened = true
      }
      es.onerror = () => {
        // The browser reconnects by itself unless the stream is closed (404, 5xx on connect).
        if (es.readyState === EventSource.CLOSED || es.readyState === 2) {
          setStatus('error')
          es.close()
          const delay = Math.min(30_000, 1000 * 2 ** attempt++)
          retry = setTimeout(connect, delay)
        } else setStatus('connecting')
      }
      for (const type of STREAM_EVENT_TYPES) {
        es.addEventListener(type, (event) => {
          let data: StreamEventMap[StreamEventType]
          try {
            data = JSON.parse((event as MessageEvent<string>).data)
          } catch {
            return
          }
          cache.apply(type, data)
          bus.emit(type, data)
          const now = Date.now()
          if (now - lastMark > 1000) {
            lastMark = now
            setLastEventAt(now)
          }
        })
      }
    }
    void connect()

    return () => {
      disposed = true
      if (retry) clearTimeout(retry)
      source?.close()
      cache.dispose()
    }
  }, [qc, bus, runId, runKey, ready])

  const statusValue = useMemo(() => ({ status, lastEventAt }), [status, lastEventAt])
  return (
    <StreamBusContext.Provider value={bus}>
      <StreamStatusContext.Provider value={statusValue}>{children}</StreamStatusContext.Provider>
    </StreamBusContext.Provider>
  )
}
