import { TriangleAlert } from 'lucide-react'

import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { eventsWord, lossWindowText } from '@/lib/event-loss'
import { fmtInt, plural } from '@/lib/format'
import { useRun } from '@/lib/run'

const SHOWN = 4

/**
 * While the chosen run lost events — the source sent them, the gateway did not take them (#229, emulator.md §7) —
 * every screen says so, with the windows by the run's clock: alarms, forecasts and metrics there are incomplete.
 */
export function EventLossBanner() {
  const { run } = useRun()
  const lost = run?.events_lost ?? 0
  if (!run || lost <= 0) return null
  const windows = run.loss_windows ?? []
  const shown = windows.slice(0, SHOWN)
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-status-risk/30 bg-status-risk/10 px-4 py-1.5 text-sm">
      <Tooltip>
        <TooltipTrigger render={<span className="flex cursor-default items-center gap-1.5 font-medium text-status-risk" />}>
          <TriangleAlert className="size-4" /> Потеряно {fmtInt(lost)} {eventsWord(lost)} прогона
        </TooltipTrigger>
        <TooltipContent className="max-w-80">
          Эмулятор отправил эти события, а шлюз их не принял — сверка со счётчиком источника. Прогон доигран как обычно, но в
          этих окнах тревоги, прогнозы, заявки и метрики могут быть неполными.
        </TooltipContent>
      </Tooltip>
      <span>данные за эти окна неполные</span>
      {shown.length > 0 && (
        <span className="text-muted-foreground tabular-nums">
          {shown.map(lossWindowText).join('; ')}
          {windows.length > shown.length && `; ещё ${fmtInt(windows.length - shown.length)} ${plural(windows.length - shown.length, ['окно', 'окна', 'окон'])}`}
        </span>
      )}
    </div>
  )
}
