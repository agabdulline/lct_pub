import { useEffect, useState } from 'react'

import { SensorTypeIcon } from '@/components/domain/SensorTypeIcon'
import { StatusDot } from '@/components/domain/StatusBadge'
import { Command, CommandDialog, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from '@/components/ui/command'
import { useChannels } from '@/lib/api/queries'
import type { Channel } from '@/lib/api/types'

type Props = { open: boolean; onOpenChange: (open: boolean) => void; onPick: (c: Channel) => void }

// Channel search for a manual work order from the list screen.
export function ChannelPickerDialog({ open, onOpenChange, onPick }: Props) {
  const [q, setQ] = useState('')
  const [query, setQuery] = useState('')
  useEffect(() => {
    const t = setTimeout(() => setQuery(q.trim()), 200)
    return () => clearTimeout(t)
  }, [q])
  const channels = useChannels({ q: query, size: 20 }, open && query.length >= 2)

  return (
    <CommandDialog open={open} onOpenChange={onOpenChange} title="Канал для заявки" description="Название, тег или id канала">
      <Command shouldFilter={false}>
        <CommandInput placeholder="Канал: название, тег или id…" value={q} onValueChange={setQ} />
        <CommandList>
          <CommandEmpty>{query.length < 2 ? 'Введите хотя бы два символа' : channels.isFetching ? 'Ищем…' : 'Ничего не найдено'}</CommandEmpty>
          {!!channels.data?.items.length && (
            <CommandGroup heading={`Каналы · найдено ${channels.data.total}`}>
              {channels.data.items.map((c) => (
                <CommandItem
                  key={c.id}
                  value={String(c.id)}
                  onSelect={() => {
                    onPick(c)
                    onOpenChange(false)
                    setQ('')
                  }}
                >
                  <SensorTypeIcon type={c.sensor_type} withTooltip={false} />
                  <span className="truncate">{c.name}</span>
                  <span className="ml-auto flex items-center gap-2 truncate text-xs text-muted-foreground">
                    {c.object_name} · {c.sub_object_name}
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
