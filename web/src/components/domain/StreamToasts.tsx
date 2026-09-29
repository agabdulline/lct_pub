import { useRef } from 'react'
import { useNavigate } from 'react-router'
import { toast } from 'sonner'

import type { WorkOrder } from '@/lib/api/types'
import { channelsText, endText } from '@/components/access/access-view'
import { equipmentLine } from '@/components/equipment/equipment-view'
import { ACCESS_NOTE, ACCESS_TITLE, EQUIPMENT_KIND_LABEL, PRIORITY_LABEL } from '@/lib/labels'
import { useRunTo } from '@/lib/run'
import { useStreamEvent } from '@/lib/sse/bus'

// Toasts for new work orders from the live stream. The daily forecast creates a dozen at once — they are grouped.
export function StreamToasts() {
  const navigate = useNavigate()
  const runTo = useRunTo()
  const batch = useRef<WorkOrder[]>([])
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)

  useStreamEvent('work_order.created', (wo) => {
    batch.current.push(wo)
    if (timer.current) return
    timer.current = setTimeout(() => {
      timer.current = null
      const items = batch.current
      batch.current = []
      if (items.length === 1) {
        const [w] = items
        toast(`Заявка №${w.id} · ${PRIORITY_LABEL[w.priority].toLowerCase()} приоритет`, {
          description: w.title,
          action: { label: 'Открыть', onClick: () => navigate(runTo(`/work-orders/${w.id}`)) },
        })
      } else {
        toast(`Создано заявок: ${items.length}`, {
          description: items
            .slice(0, 3)
            .map((w) => w.channel_name)
            .join(', ') + (items.length > 3 ? '…' : ''),
          action: { label: 'К заявкам', onClick: () => navigate(runTo('/work-orders')) },
        })
      }
    }, 1500)
  })

  // An outage is news for the dispatcher wherever they are: one toast when the episode reaches the threshold.
  useStreamEvent('equipment_episode.created', (e) => {
    toast.warning(`${EQUIPMENT_KIND_LABEL[e.kind]} · ${e.object_name}`, {
      description: equipmentLine(e),
      action: { label: 'В ленте', onClick: () => navigate(runTo({ pathname: '/', search: `?equipment=${e.id}` })) },
    })
  })

  // Opened under guard: the rule waited 5 minutes for the guard to be taken off, it was not — news for the dispatcher.
  useStreamEvent('access_incident.created', (inc) => {
    toast.warning(`${ACCESS_TITLE} · ${inc.sub_object_name}`, {
      description: `${channelsText(inc, 1)} · ${inc.priority ? `${PRIORITY_LABEL[inc.priority].toLowerCase()} приоритет · ` : ''}${endText(inc)}. ${ACCESS_NOTE}.`,
      action: { label: 'В ленте', onClick: () => navigate(runTo({ pathname: '/', search: `?access=${inc.id}` })) },
    })
  })

  return null
}
