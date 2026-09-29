// "Now" of the selected run: emulator time for an active run, the last simulated moment for a finished one.

import { useClock } from '@/lib/api/queries'
import { useRun } from '@/lib/run'

export function useSimNow(): string | null {
  const clock = useClock()
  const { run } = useRun()
  if (!run) return clock.data?.now ?? null
  if (run.status === 'running' || run.status === 'paused') return clock.data?.now ?? run.sim_now
  return run.sim_now ?? run.sim_end
}
