import { Select, SelectContent, SelectGroup, SelectItem, SelectLabel, SelectSeparator, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useObjects } from '@/lib/api/queries'

const ALL = 'all'

type Props = {
  value: number | null
  onChange: (id: number | null) => void
  className?: string
}

// Object or sub-object filter: objects with their sub-objects under them.
export function ObjectSelect({ value, onChange, className }: Props) {
  const { data } = useObjects()
  const objects = data?.[0]?.children ?? []
  const items = [
    { value: ALL, label: 'Все объекты' },
    ...objects.flatMap((o) => [{ value: String(o.id), label: o.name }, ...o.children.map((s) => ({ value: String(s.id), label: s.name }))]),
  ]
  return (
    <Select items={items} value={value === null ? ALL : String(value)} onValueChange={(v) => onChange(!v || v === ALL ? null : Number(v))}>
      <SelectTrigger size="sm" className={className} aria-label="Объект">
        <SelectValue />
      </SelectTrigger>
      <SelectContent className="max-h-96">
        <SelectItem value={ALL}>Все объекты</SelectItem>
        {objects.map((o) => (
          <SelectGroup key={o.id}>
            <SelectSeparator />
            <SelectLabel>{o.name}</SelectLabel>
            <SelectItem value={String(o.id)}>Весь объект · {o.channels} кан.</SelectItem>
            {o.children.map((s) => (
              <SelectItem key={s.id} value={String(s.id)}>
                {s.name}
              </SelectItem>
            ))}
          </SelectGroup>
        ))}
      </SelectContent>
    </Select>
  )
}
