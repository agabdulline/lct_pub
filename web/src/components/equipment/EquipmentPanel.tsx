import { Map as MapIcon, PlugZap } from 'lucide-react'

import { RunLink } from '@/components/domain/RunLink'
import { SensorTypeIcon } from '@/components/domain/SensorTypeIcon'
import { StatusBadge, StatusDot } from '@/components/domain/StatusBadge'
import { Button } from '@/components/ui/button'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { Skeleton } from '@/components/ui/skeleton'
import { useEquipmentEpisode } from '@/lib/api/queries'
import { fmtDateTime, fmtInt, fmtTime } from '@/lib/format'
import { EQUIPMENT_ACTION, EQUIPMENT_KIND_LABEL } from '@/lib/labels'

import { byTypeText, channelsText, equipmentMapPath, periodText } from './equipment-view'

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-3 py-0.5">
      <span className="text-muted-foreground">{label}</span>
      <span className="text-right">{children}</span>
    </div>
  )
}

/**
 * Equipment episode beside the feed: which channels went down, which are back, where on the map. Non-modal, like the
 * alarm card. No model verdict — the event model scores smoke alarms, this is an equipment accident.
 */
export function EquipmentPanel({ episodeId, onClose }: { episodeId: number | null; onClose: () => void }) {
  const { data: ep, isPending } = useEquipmentEpisode(episodeId)
  const open = ep?.end_ts === null
  return (
    <Sheet open={episodeId !== null} onOpenChange={(o) => !o && onClose()} modal={false} disablePointerDismissal>
      <SheetContent
        showOverlay={false}
        className="w-[26rem] gap-0 overflow-y-auto data-[side=right]:top-12 data-[side=right]:h-[calc(100svh-3rem)] sm:max-w-md"
      >
        <SheetHeader className="pr-10">
          <SheetTitle className="flex items-center gap-2">
            <PlugZap className="size-4" />
            {ep ? EQUIPMENT_KIND_LABEL[ep.kind] : 'Оборудование'}
          </SheetTitle>
          <SheetDescription>{ep ? `${ep.object_name} · ${fmtDateTime(ep.start_ts)}` : ' '}</SheetDescription>
        </SheetHeader>
        {isPending && episodeId !== null && (
          <div className="space-y-2 px-4">
            <Skeleton className="h-6 w-40" />
            <Skeleton className="h-40" />
          </div>
        )}
        {ep && (
          <div className="space-y-4 px-4 pb-4 text-sm">
            <div className="space-y-2">
              <div className="flex flex-wrap items-center gap-2">
                <StatusBadge status={open ? 'risk' : 'unknown'}>{open ? 'идёт' : 'завершён'}</StatusBadge>
                <span className="text-muted-foreground">{periodText(ep)}</span>
              </div>
              <p>
                {channelsText(ep.channels)} объекта ушли в «{ep.value}»: {byTypeText(ep.by_type, 5)}. {EQUIPMENT_ACTION[ep.kind]}.
              </p>
              <p className="text-xs text-muted-foreground">
                Эпизод — не меньше 5 каналов оборудования одного объекта за 5 минут в «Обесточен», «Неисправен» или «Отключено
                устройство»; завершён, когда в норму вернулась половина. Модель это не оценивает: авария оборудования, а не
                неисправность датчика.
              </p>
            </div>

            <div className="rounded-lg p-3 ring-1 ring-foreground/10">
              <Row label="Каналов">{fmtInt(ep.channels)}</Row>
              <Row label="Вернулись в норму">{fmtInt(ep.returned_channels)}</Row>
              <Row label="Ещё не в норме">
                <span className={ep.active_channels ? 'font-medium' : undefined}>{fmtInt(ep.active_channels)}</span>
              </Row>
              <Row label="Подобъекты">
                <span className="flex flex-col items-end">
                  {ep.sub_objects.map((s) => (
                    <RunLink key={s.id} to={`/objects/${s.id}`} className="underline-offset-4 hover:underline">
                      {s.name} · {fmtInt(s.channels)}
                    </RunLink>
                  ))}
                </span>
              </Row>
            </div>

            <div>
              <div className="mb-1.5 font-medium">Каналы · {fmtInt(ep.members.length)}</div>
              <ul className="space-y-1">
                {ep.members.map((m) => (
                  <li key={m.channel_id} className="flex items-center gap-2">
                    <StatusDot status={m.end_ts ? m.status : 'fault'} />
                    <SensorTypeIcon type={m.sensor_type} className="size-3.5 shrink-0 text-muted-foreground" />
                    <RunLink to={`/channels/${m.channel_id}`} className="truncate underline-offset-4 hover:underline">
                      {m.channel_name}
                    </RunLink>
                    <span className="ml-auto shrink-0 text-xs text-muted-foreground tabular-nums">
                      {fmtTime(m.start_ts)}
                      {m.end_ts ? ` — ${fmtTime(m.end_ts)}` : ` · ${m.value.toLowerCase()}`}
                    </span>
                  </li>
                ))}
              </ul>
            </div>

            <Button className="w-full" variant="outline" nativeButton={false} render={<RunLink to={equipmentMapPath(ep)} />}>
              <MapIcon /> На карте — {ep.sub_object_name ?? ep.object_name}
            </Button>
          </div>
        )}
      </SheetContent>
    </Sheet>
  )
}
