import { useState } from 'react'
import { toast } from 'sonner'

import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Field, FieldError, FieldGroup, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'
import { useCreateWorkOrder } from '@/lib/api/queries'
import type { WorkOrderPriority } from '@/lib/api/types'
import { PRIORITY_LABEL } from '@/lib/labels'

const PRIORITIES: WorkOrderPriority[] = ['high', 'medium', 'low']
const DUE_DAYS: Record<WorkOrderPriority, number> = { high: 1, medium: 7, low: 14 }

function addDays(base: string | null, days: number): string {
  const d = base ? new Date(base) : new Date()
  d.setUTCDate(d.getUTCDate() + days)
  return d.toISOString().slice(0, 10)
}

type Props = {
  open: boolean
  onOpenChange: (open: boolean) => void
  channel: { id: number; name: string }
  /** "Now" of the run — the default due date counts from it. */
  now: string | null
  onCreated?: (id: number) => void
}

// Manual work order (docs/api.md, POST /work-orders); automatic ones come from the rules.
export function CreateWorkOrderDialog({ open, onOpenChange, channel, now, onCreated }: Props) {
  const create = useCreateWorkOrder()
  const [priority, setPriority] = useState<WorkOrderPriority>('medium')
  const [title, setTitle] = useState(`Проверить ${channel.name}`)
  const [due, setDue] = useState(addDays(now, DUE_DAYS.medium))
  const [comment, setComment] = useState('')
  const [assignee, setAssignee] = useState('')

  const titleError = title.trim().length < 5 ? 'Опишите, что сделать — не короче 5 символов' : null

  const submit = (e: React.FormEvent) => {
    e.preventDefault()
    if (titleError) return
    create.mutate(
      { channel_id: channel.id, priority, title: title.trim(), due_date: due, comment: comment.trim() || null, assignee: assignee.trim() || null },
      {
        onSuccess: (wo) => {
          toast.success(`Заявка №${wo.id} создана`, { description: wo.title })
          onOpenChange(false)
          onCreated?.(wo.id)
        },
      },
    )
  }

  const items = PRIORITIES.map((p) => ({ value: p, label: PRIORITY_LABEL[p] }))

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <form onSubmit={submit} className="space-y-4">
          <DialogHeader>
            <DialogTitle>Заявка на ТО</DialogTitle>
            <DialogDescription>Ручная заявка по каналу {channel.name}. Автоматические создаются правилами.</DialogDescription>
          </DialogHeader>
          <FieldGroup>
            <Field data-invalid={!!titleError}>
              <FieldLabel htmlFor="wo-title">Что сделать</FieldLabel>
              <Input id="wo-title" value={title} onChange={(e) => setTitle(e.target.value)} aria-invalid={!!titleError} />
              {titleError && <FieldError>{titleError}</FieldError>}
            </Field>
            <div className="grid grid-cols-2 gap-3">
              <Field>
                <FieldLabel>Приоритет</FieldLabel>
                <Select
                  items={items}
                  value={priority}
                  onValueChange={(v) => {
                    const p = (v ?? 'medium') as WorkOrderPriority
                    setPriority(p)
                    setDue(addDays(now, DUE_DAYS[p]))
                  }}
                >
                  <SelectTrigger className="w-full" aria-label="Приоритет">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {items.map((i) => (
                      <SelectItem key={i.value} value={i.value}>
                        {i.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </Field>
              <Field>
                <FieldLabel htmlFor="wo-due">Срок</FieldLabel>
                <Input id="wo-due" type="date" value={due} onChange={(e) => setDue(e.target.value)} required />
              </Field>
            </div>
            <Field>
              <FieldLabel htmlFor="wo-assignee">Исполнитель</FieldLabel>
              <Input id="wo-assignee" value={assignee} onChange={(e) => setAssignee(e.target.value)} placeholder="необязательно" />
            </Field>
            <Field>
              <FieldLabel htmlFor="wo-comment">Комментарий</FieldLabel>
              <Textarea id="wo-comment" value={comment} onChange={(e) => setComment(e.target.value)} rows={3} placeholder="необязательно" />
            </Field>
          </FieldGroup>
          {create.isError && <p className="text-sm text-status-alarm">Не создана: {create.error.message}</p>}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              Отмена
            </Button>
            <Button type="submit" disabled={create.isPending || !!titleError}>
              {create.isPending ? 'Создаём…' : 'Создать заявку'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
