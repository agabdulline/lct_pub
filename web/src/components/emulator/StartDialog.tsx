import { Play } from 'lucide-react'
import { useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router'
import { toast } from 'sonner'

import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { useEmulatorCommand, useEmulatorState } from '@/lib/api/queries'
import type { Scenario } from '@/lib/api/types'

const SPEEDS = [1, 10, 30, 60]

// Start of a scenario: the speed and a warning that the running run stops. Used on the console and the scenario page.
export function StartDialog({ scenario, onClose }: { scenario: Scenario | null; onClose: () => void }) {
  const command = useEmulatorCommand()
  const { data: em } = useEmulatorState()
  const navigate = useNavigate()
  const [params] = useSearchParams()
  const [speed, setSpeed] = useState<number | null>(null)
  const chosen = speed ?? scenario?.default_speed ?? 10
  // primary_model is not sent: the service starts the run with its own event model.
  const active = em && (em.state === 'running' || em.state === 'paused') ? em.run_id : null

  const start = () => {
    if (!scenario) return
    command.mutate(
      { action: 'start', scenario: scenario.id, speed: chosen },
      {
        onSuccess: (res) => {
          const runId = (res as { run_id?: number | null } | undefined)?.run_id ?? null
          toast.success(`Прогон ${runId ? `№${runId} ` : ''}запущен`, {
            description: `${scenario.title} · ×${chosen}`,
          })
          onClose()
          // The run is created by the api at start: pin it, so every screen opens the new run.
          const next = new URLSearchParams(params)
          if (runId) next.set('run', String(runId))
          else next.delete('run')
          navigate({ pathname: '/', search: next.toString() })
        },
        onError: (e) => toast.error('Не запустился', { description: e.message }),
      },
    )
  }

  return (
    <Dialog open={scenario !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-[calc(100%-2rem)] sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Запустить «{scenario?.title}»</DialogTitle>
          <DialogDescription>
            Запуск создаёт новый прогон.
            {active ? ` Текущий прогон №${active} остановится — его данные сохранятся, его можно открыть и сравнить.` : ''} Удалить
            прогон можно только командой на сервере.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-2">
          <div className="text-sm">Скорость</div>
          <ToggleGroup className="flex-wrap" value={[String(chosen)]} onValueChange={(v) => v[0] && setSpeed(Number(v[0]))} variant="outline">
            {SPEEDS.filter((s) => s <= (scenario?.max_speed ?? 60)).map((s) => (
              <ToggleGroupItem key={s} value={String(s)}>
                ×{s}
              </ToggleGroupItem>
            ))}
          </ToggleGroup>
          <p className="text-xs text-muted-foreground">
            ×60 — минута симуляции за секунду. Во всплесках плановой проверки время замедляется, события не теряются.
          </p>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            Отмена
          </Button>
          <Button onClick={start} disabled={command.isPending}>
            <Play /> {command.isPending ? 'Запускаем…' : 'Запустить'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
