import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import type { ChannelTimeline, StateEpisode } from '@/lib/api/types'
import { fmtDate, fmtDateTimeShort, fmtDuration } from '@/lib/format'
import { STATUS_DOT_CLASS } from '@/lib/status'
import { cn } from '@/lib/utils'

const LANES: { kind: StateEpisode['kind']; label: string; cls: string }[] = [
  { kind: 'fault', label: 'Неисправен', cls: STATUS_DOT_CLASS.fault },
  { kind: 'alarm', label: 'Тревога', cls: STATUS_DOT_CLASS.alarm },
  { kind: 'off', label: 'Отключён', cls: STATUS_DOT_CLASS.unknown },
]

const DAY = 86_400_000

// State tape of a channel (v1-scope, «Карточка канала»): episodes as segments on a time axis, one lane per state.
export function StateTape({ timeline }: { timeline: ChannelTimeline }) {
  const from = new Date(timeline.from).getTime()
  const to = new Date(timeline.to).getTime()
  const span = Math.max(1, to - from)
  const x = (t: number) => `${Math.min(100, Math.max(0, ((t - from) / span) * 100))}%`
  const lanes = LANES.filter((l) => l.kind !== 'off' || timeline.episodes.some((e) => e.kind === 'off'))

  // Day ticks, thinned to at most ~8 labels.
  const days = Math.ceil(span / DAY)
  const step = Math.max(1, Math.ceil(days / 8))
  const ticks: number[] = []
  const firstMidnight = Math.ceil((from + 3 * 3_600_000) / DAY) * DAY - 3 * 3_600_000
  for (let t = firstMidnight, i = 0; t < to; t += DAY, i++) if (i % step === 0) ticks.push(t)

  return (
    <div className="space-y-1.5">
      {lanes.map((lane) => {
        const episodes = timeline.episodes.filter((e) => e.kind === lane.kind)
        return (
          <div key={lane.kind} className="grid grid-cols-[6rem_1fr] items-center gap-3">
            <span className="text-xs text-muted-foreground">{lane.label}</span>
            <div className="relative h-5 rounded-sm bg-muted/50">
              {episodes.map((e, i) => {
                const s = new Date(e.start_ts).getTime()
                const end = e.end_ts ? new Date(e.end_ts).getTime() : to
                return (
                  <Tooltip key={i}>
                    <TooltipTrigger
                      render={
                        <span
                          className={cn('absolute inset-y-0.5 rounded-[2px] hover:outline hover:outline-foreground/50', lane.cls)}
                          style={{ left: x(s), width: `max(3px, calc(${x(end)} - ${x(s)}))` }}
                        />
                      }
                    />
                    <TooltipContent>
                      <div className="font-medium">
                        {lane.label} · {e.end_ts ? fmtDuration((end - s) / 1000) : 'продолжается'}
                      </div>
                      <div>
                        {fmtDateTimeShort(e.start_ts)} — {e.end_ts ? fmtDateTimeShort(e.end_ts) : 'сейчас'}
                      </div>
                    </TooltipContent>
                  </Tooltip>
                )
              })}
            </div>
          </div>
        )
      })}
      <div className="grid grid-cols-[6rem_1fr] gap-3">
        <span />
        <div className="relative h-4 text-[10px] text-muted-foreground">
          {ticks.map((t) => (
            <span key={t} className="absolute -translate-x-1/2 tabular-nums" style={{ left: x(t) }}>
              {fmtDate(new Date(t).toISOString()).slice(0, 5)}
            </span>
          ))}
        </div>
      </div>
    </div>
  )
}
