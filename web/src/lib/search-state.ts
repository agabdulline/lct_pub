// Screen filters live in the URL: a filtered view can be shared as a link and survives reload.

import { useCallback } from 'react'
import { useSearchParams } from 'react-router'

export function useSearchState() {
  const [params, setParams] = useSearchParams()
  const set = useCallback(
    (patch: Record<string, string | number | null | undefined>, keepPage = false) => {
      setParams(
        (prev) => {
          const next = new URLSearchParams(prev)
          for (const [k, v] of Object.entries(patch)) {
            if (v === null || v === undefined || v === '') next.delete(k)
            else next.set(k, String(v))
          }
          // Any filter change starts from the first page.
          if (!keepPage && !('page' in patch)) next.delete('page')
          return next
        },
        { replace: true },
      )
    },
    [setParams],
  )
  const num = (key: string): number | null => {
    const v = params.get(key)
    return v && /^\d+$/.test(v) ? Number(v) : null
  }
  const list = (key: string): string[] => params.get(key)?.split(',').filter(Boolean) ?? []
  return { params, set, num, list, get: (key: string) => params.get(key) }
}
