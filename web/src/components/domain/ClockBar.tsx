import { Gauge, Pause } from 'lucide-react'
import { Link } from 'react-router'

import { RunSelect } from '@/components/domain/RunSelect'
import { StatusBadge, StatusDot } from '@/components/domain/StatusBadge'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { MOCK } from '@/lib/api/client'
import { useClock, useEmulatorState, useHealth, useHistorySummary } from '@/lib/api/queries'
import { fmtDate, fmtDateTime } from '@/lib/format'
import { RUN_STATUS_LABEL } from '@/lib/labels'
import { useRun } from '@/lib/run'
import { useStreamStatus } from '@/lib/sse/bus'

// Header on every screen: run, simulated time, target and effective speed, pause, live stream and system status
// (docs/architecture/frontend.md §3).
export function ClockBar() {
  const clock = useClock()
  const health = useHealth()
  const emulator = useEmulatorState()
  const { run, ready, history, all } = useRun()
  const historyEnd = useHistorySummary(history).data?.to
  const stream = useStreamStatus()

  const streaming = clock.data?.mode === 'stream'
  const healthy = health.data?.status === 'ok'
  const em = emulator.data
  const active = run && em && em.run_id === run.id && (em.state === 'running' || em.state === 'paused')
  // A finished run shows its own last simulated moment, not the emulator clock.
  const now = active || !run ? clock.data?.now : (run.sim_now ?? run.sim_end)
  const slowed = active && em.state === 'running' && em.effective_speed < em.target_speed

  return (
    <div className="flex min-w-0 items-center gap-3 text-sm">
      {MOCK && (
        <StatusBadge status="unknown" className="hidden sm:inline-flex">
          моки
        </StatusBadge>
      )}
      <RunSelect />
      {/* until /runs answers the run is unknown: the emulator clock may belong to another run */}
      {/* «Вся история» has no clock: it is the journal up to the end of its data */}
      {ready && history && (
        <span className="hidden text-xs text-muted-foreground sm:inline">
          {all ? 'все данные · у источников своё время' : `весь журнал${historyEnd ? ` до ${fmtDate(historyEnd)}` : ''}`}
        </span>
      )}
      {ready && !history && <span className="hidden font-mono tabular-nums sm:inline">{fmtDateTime(now)}</span>}
      {active && (
        <Tooltip>
          <TooltipTrigger render={<span className="hidden items-center gap-1 text-xs text-muted-foreground md:inline-flex" />}>
            {em.state === 'paused' ? <Pause className="size-3.5" /> : <Gauge className="size-3.5" />}
            {em.state === 'paused' ? 'пауза' : <>×{em.target_speed}</>}
            {slowed && <span className="text-status-risk">→ ×{em.effective_speed}</span>}
          </TooltipTrigger>
          <TooltipContent>
            Целевая скорость ×{em.target_speed}, фактическая ×{em.effective_speed}
            {slowed && ' — всплеск событий, время замедлено, события не теряются'}
          </TooltipContent>
        </Tooltip>
      )}
      {/* Below `sm` the run's status gives way to the run itself: it is on the console and in the run list. */}
      {!active && run && (
        <StatusBadge status="unknown" className="hidden sm:inline-flex">
          {run.kind === 'dataset' ? 'журнал набора целиком' : `прогон ${RUN_STATUS_LABEL[run.status]}`}
        </StatusBadge>
      )}
      {ready && !run && (
        <StatusBadge status={streaming ? 'ppr' : 'unknown'} className="hidden sm:inline-flex">
          {streaming ? 'поток эмулятора' : 'реальное время'}
        </StatusBadge>
      )}
      <Tooltip>
        <TooltipTrigger render={<span className="inline-flex items-center" />}>
          <StatusDot status={stream.status === 'open' ? 'ok' : stream.status === 'error' ? 'alarm' : 'unknown'} pulse={stream.status === 'open'} />
        </TooltipTrigger>
        <TooltipContent>
          {stream.status === 'open' ? 'Живой поток подключён' : stream.status === 'error' ? 'Живой поток недоступен, переподключаемся' : 'Подключаемся к живому потоку'}
        </TooltipContent>
      </Tooltip>
      <Link to="/system" className="hidden lg:inline-flex">
        <StatusBadge status={health.isError ? 'alarm' : healthy ? 'ok' : 'risk'}>
          {health.isError ? 'API недоступен' : healthy ? 'система в норме' : 'система деградирована'}
        </StatusBadge>
      </Link>
    </div>
  )
}
