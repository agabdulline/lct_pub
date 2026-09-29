import { Building2, Network } from 'lucide-react'
import { useEffect, useState } from 'react'

import { SensorTypeIcon } from '@/components/domain/SensorTypeIcon'
import { StatusDot } from '@/components/domain/StatusBadge'
import { Command, CommandDialog, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from '@/components/ui/command'
import { Spinner } from '@/components/ui/spinner'
import { useChannels } from '@/lib/api/queries'
import type { Channel, GraphNode } from '@/lib/api/types'

export type SearchPick = { kind: 'object' | 'sub_object'; id: number } | { kind: 'channel'; channel: Channel }

type Props = {
  open: boolean
  onOpenChange: (open: boolean) => void
  objects: GraphNode[]
  onPick: (pick: SearchPick) => void
}

function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value)
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms)
    return () => clearTimeout(t)
  }, [value, ms])
  return v
}

// ⌘K search over objects, sub-objects and channels; the pick moves the camera (frontend.md §4).
export function GraphSearch({ open, onOpenChange, objects, onPick }: Props) {
  const [q, setQ] = useState('')
  const query = useDebounced(q.trim(), 200)
  const channels = useChannels({ q: query, size: 20 }, open && query.length >= 2)
  const lower = q.trim().toLowerCase()
  const places = objects
    .filter((n) => (n.kind === 'object' || n.kind === 'sub_object') && (!lower || n.label.toLowerCase().includes(lower)))
    .slice(0, lower ? 12 : 8)

  const pick = (p: SearchPick) => {
    onPick(p)
    onOpenChange(false)
    setQ('')
  }

  return (
    <CommandDialog open={open} onOpenChange={onOpenChange} title="Поиск на карте" description="Объект, подобъект или канал">
      <Command shouldFilter={false}>
        <CommandInput placeholder="Объект, подобъект, канал: название, тег или id…" value={q} onValueChange={setQ} />
        <CommandList>
          <CommandEmpty>{channels.isFetching ? 'Ищем…' : 'Ничего не найдено'}</CommandEmpty>
          {places.length > 0 && (
            <CommandGroup heading="Объекты">
              {places.map((n) => (
                <CommandItem
                  key={n.id}
                  value={n.id}
                  onSelect={() => pick({ kind: n.kind as 'object' | 'sub_object', id: Number(n.id.split(':')[1]) })}
                >
                  {n.kind === 'object' ? <Building2 /> : <Network />}
                  <span className="truncate">{n.label}</span>
                  <span className="ml-auto flex items-center gap-2 text-xs text-muted-foreground">
                    {n.channels} кан.
                    <StatusDot status={n.status} />
                  </span>
                </CommandItem>
              ))}
            </CommandGroup>
          )}
          {query.length >= 2 && (
            <CommandGroup heading={channels.isFetching ? undefined : `Каналы${channels.data ? ` · найдено ${channels.data.total}` : ''}`}>
              {channels.isFetching && !channels.data && (
                <div className="flex items-center gap-2 px-2 py-1.5 text-sm text-muted-foreground">
                  <Spinner /> Ищем каналы…
                </div>
              )}
              {channels.data?.items.map((c) => (
                <CommandItem key={c.id} value={`channel:${c.id}`} onSelect={() => pick({ kind: 'channel', channel: c })}>
                  <SensorTypeIcon type={c.sensor_type} withTooltip={false} />
                  <span className="truncate">{c.name}</span>
                  <span className="ml-auto flex items-center gap-2 truncate text-xs text-muted-foreground">
                    {c.sub_object_name}
                    <StatusDot status={c.status} />
                  </span>
                </CommandItem>
              ))}
            </CommandGroup>
          )}
        </CommandList>
      </Command>
    </CommandDialog>
  )
}
