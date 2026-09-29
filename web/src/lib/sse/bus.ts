// In-app bus for stream events: screens subscribe for animations and toasts,
// the provider feeds it from the single EventSource (docs/architecture/frontend.md §5).

import { createContext, useContext, useEffect, useRef } from 'react'

import type { StreamEventMap, StreamEventType } from '@/lib/api/types'

type Handler<T extends StreamEventType> = (data: StreamEventMap[T]) => void

export class StreamBus {
  private handlers = new Map<StreamEventType, Set<Handler<StreamEventType>>>()

  on<T extends StreamEventType>(type: T, handler: Handler<T>): () => void {
    const set = this.handlers.get(type) ?? new Set()
    set.add(handler as Handler<StreamEventType>)
    this.handlers.set(type, set)
    return () => set.delete(handler as Handler<StreamEventType>)
  }

  emit<T extends StreamEventType>(type: T, data: StreamEventMap[T]) {
    this.handlers.get(type)?.forEach((h) => h(data))
  }
}

export type StreamStatus = 'connecting' | 'open' | 'error'

export type StreamStatusValue = { status: StreamStatus; lastEventAt: number | null }

// Two contexts: the bus is stable, the status changes every second and must not re-render subscribers.
export const StreamBusContext = createContext<StreamBus | null>(null)
export const StreamStatusContext = createContext<StreamStatusValue>({ status: 'connecting', lastEventAt: null })

export const useStreamStatus = () => useContext(StreamStatusContext)

/** Subscribes to one event type; the latest handler is always used. */
export function useStreamEvent<T extends StreamEventType>(type: T, handler: Handler<T>) {
  const bus = useContext(StreamBusContext)
  if (!bus) throw new Error('StreamProvider is missing')
  const ref = useRef(handler)
  useEffect(() => {
    ref.current = handler
  })
  useEffect(() => bus.on(type, (data) => ref.current(data)), [bus, type])
}
