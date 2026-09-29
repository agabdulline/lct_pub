import { useSearchParams } from 'react-router'

import { Button } from '@/components/ui/button'
import { useRun } from '@/lib/run'

// A pinned run that the API does not know (a link from another stand, a deleted run): say so and offer the latest.
export function RunMissingBanner() {
  const { pinned, run, runs, runId, ready } = useRun()
  const [params, setParams] = useSearchParams()
  if (!pinned || !ready || !runs.length || run) return null
  const unpin = () => {
    const next = new URLSearchParams(params)
    next.delete('run')
    setParams(next)
  }
  return (
    <div className="flex items-center gap-3 border-b bg-status-risk/10 px-4 py-2 text-sm">
      <span>Прогона №{runId} нет — возможно, он удалён или ссылка с другого стенда. Экраны покажут пустые данные.</span>
      <Button size="xs" variant="outline" className="ml-auto" onClick={unpin}>
        Показать последний прогон
      </Button>
    </div>
  )
}
