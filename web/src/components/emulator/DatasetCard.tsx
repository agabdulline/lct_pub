import { CalendarRange, Database, Network } from 'lucide-react'
import { Link } from 'react-router'

import { DatasetFacts } from '@/components/emulator/DatasetFacts'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from '@/components/ui/card'
import type { Dataset } from '@/lib/api/types'
import { fmtDate, fmtInt, plural } from '@/lib/format'

/**
 * A loaded external dataset: what its journal covers and a link to its whole history — the dataset's service run,
 * which is not in the run list.
 */
export function DatasetCard({ d }: { d: Dataset }) {
  return (
    <Card className="flex flex-col">
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-source-external">
          <Database className="size-4 shrink-0" /> {d.short}
        </CardTitle>
        <CardDescription className="space-y-1">
          <span className="line-clamp-2 block text-xs font-medium text-foreground">{d.title}</span>
          <span className="line-clamp-4 block">{d.description}</span>
        </CardDescription>
      </CardHeader>
      <CardContent className="flex-1 space-y-1.5 text-sm">
        <div className="flex items-center gap-1.5 text-muted-foreground">
          <CalendarRange className="size-4" />
          журнал {fmtDate(d.period_start)} — {fmtDate(d.period_end)}
        </div>
        <div className="text-muted-foreground">
          {fmtInt(d.objects)} {plural(d.objects, ['объект', 'объекта', 'объектов'])} · {fmtInt(d.channels)}{' '}
          {plural(d.channels, ['канал', 'канала', 'каналов'])}
          {d.events !== null && ` · ${fmtInt(d.events)} ${plural(d.events, ['событие', 'события', 'событий'])}`}
        </div>
        <DatasetFacts dataset={d.key} />
      </CardContent>
      <CardFooter>
        {d.base_run_id !== null ? (
          <Button variant="outline" className="w-full" nativeButton={false} render={<Link to={{ pathname: '/objects', search: `?run=${d.base_run_id}` }} />}>
            <Network /> Вся история набора
          </Button>
        ) : (
          <span className="text-xs text-muted-foreground">Не загружен на этом стенде</span>
        )}
      </CardFooter>
    </Card>
  )
}
