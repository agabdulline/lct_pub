import { Activity, BellRing, ClipboardList, ScrollText } from 'lucide-react'

import { MetricCard } from '@/components/domain/MetricCard'
import { StatusBadge } from '@/components/domain/StatusBadge'
import { StreamLog } from '@/components/domain/StreamLog'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { useHealth } from '@/lib/api/queries'
import { fmtDateTime, fmtInt } from '@/lib/format'
import { RUN_STATUS_LABEL } from '@/lib/labels'
import { useRun } from '@/lib/run'

const titles: Record<string, string> = {
  db: 'PostgreSQL + TimescaleDB',
  redis: 'Redis',
  worker: 'Worker и расписание (Celery)',
  gateway: 'Шлюз OPC UA',
  ml_service: 'ML-сервис',
}

// Service screen: health of all parts (GET /api/v1/health), the selected run and the live stream.
export function SystemPage() {
  const { data, isError, isPending } = useHealth()
  const { run } = useRun()

  return (
    <div className="mx-auto w-full max-w-5xl space-y-4">
      {isError && <StatusBadge status="alarm">API недоступен</StatusBadge>}
      {isPending && <Skeleton className="h-40" />}
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {data &&
          Object.entries(data.components).map(([key, component]) => {
            const { status, ...details } = component
            return (
              <Card key={key} size="sm">
                <CardHeader className="flex flex-row items-center justify-between">
                  <CardTitle>{titles[key] ?? key}</CardTitle>
                  <StatusBadge status={status === 'ok' ? 'ok' : 'alarm'}>{status === 'ok' ? 'работает' : 'недоступен'}</StatusBadge>
                </CardHeader>
                <CardContent>
                  <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
                    {Object.entries(details).map(([k, v]) => (
                      <div key={k} className="contents">
                        <dt className="text-muted-foreground">{k}</dt>
                        <dd className="break-all font-mono">{String(v)}</dd>
                      </div>
                    ))}
                  </dl>
                </CardContent>
              </Card>
            )
          })}
      </div>

      <h2 className="pt-2 text-base font-semibold">
        Прогон {run ? `№${run.id} · ${run.scenario_title}` : '— базовая история'}
        {run && <span className="ml-2 text-sm font-normal text-muted-foreground">{RUN_STATUS_LABEL[run.status]}</span>}
      </h2>
      {run && (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <MetricCard title="События" icon={Activity} value={fmtInt(run.events)} hint={`с ${fmtDateTime(run.sim_start)}`} />
          <MetricCard title="Тревоги" icon={BellRing} value={fmtInt(run.alarms)} hint="дымовые и тепловые, все вердикты" />
          <MetricCard title="Прогнозы" icon={ScrollText} value={fmtInt(run.predictions)} hint="обе модели, включая пропущенные" />
          <MetricCard title="Заявки" icon={ClipboardList} value={fmtInt(run.work_orders)} hint="автоматические и ручные" />
        </div>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Живой поток</CardTitle>
          <CardDescription>События SSE выбранного прогона — /api/v1/stream</CardDescription>
        </CardHeader>
        <CardContent>
          <StreamLog />
        </CardContent>
      </Card>
    </div>
  )
}
