import { ChevronDown, ChevronUp } from 'lucide-react'
import { useState } from 'react'

import { StatusDot } from '@/components/domain/StatusBadge'
import { Button } from '@/components/ui/button'
import { SUBSYSTEMS, type Status } from '@/lib/api/types'
import { STATUS_LABEL } from '@/lib/labels'

import { SUBSYSTEM_SHAPE, type Shape } from './draw'

const SHAPE_SVG: Record<Shape, string> = {
  circle: 'M6 1a5 5 0 1 0 0.001 0Z',
  square: 'M1.5 1.5h9v9h-9Z',
  diamond: 'M6 0.5 11.5 6 6 11.5 0.5 6Z',
  triangle: 'M6 0.8 11.3 10.5H0.7Z',
  triangleDown: 'M6 11.2 11.3 1.5H0.7Z',
  hexagon: 'M6 0.6 10.7 3.3v5.4L6 11.4 1.3 8.7V3.3Z',
}

const STATUSES: Status[] = ['alarm', 'ppr', 'fault', 'risk', 'ok', 'unknown']

// Legend with the caveats the graph must state (frontend.md §4): no coordinates, tag groups are a hypothesis.
export function GraphLegend() {
  const [open, setOpen] = useState(() => {
    try {
      return localStorage.getItem('lct-graph-legend') === 'open'
    } catch {
      return false
    }
  })
  const toggle = () => {
    try {
      localStorage.setItem('lct-graph-legend', open ? 'closed' : 'open')
    } catch {
      // storage may be unavailable
    }
    setOpen(!open)
  }
  return (
    <div className="w-64 rounded-xl bg-card/95 text-xs text-card-foreground shadow-sm ring-1 ring-foreground/10 backdrop-blur">
      <div className="flex items-center justify-between px-3 py-2">
        <span className="font-medium">Легенда</span>
        <Button variant="ghost" size="icon-xs" aria-label={open ? 'Свернуть легенду' : 'Развернуть легенду'} onClick={toggle}>
          {open ? <ChevronDown /> : <ChevronUp />}
        </Button>
      </div>
      {open && (
        <div className="space-y-3 px-3 pb-3">
          <div className="grid grid-cols-2 gap-x-2 gap-y-1">
            {STATUSES.map((s) => (
              <span key={s} className="flex items-center gap-1.5">
                <StatusDot status={s} />
                {STATUS_LABEL[s]}
              </span>
            ))}
          </div>
          <div className="space-y-1">
            {SUBSYSTEMS.map((s) => (
              <span key={s} className="flex items-center gap-1.5">
                <svg viewBox="0 0 12 12" className="size-3 fill-muted-foreground" aria-hidden>
                  <path d={SHAPE_SVG[SUBSYSTEM_SHAPE[s]]} />
                </svg>
                {s}
              </span>
            ))}
          </div>
          <div className="space-y-1">
            <span className="flex items-center gap-1.5">
              <svg viewBox="0 0 16 16" className="size-4" aria-hidden>
                <circle cx="8" cy="8" r="4" className="fill-muted-foreground/60" />
                <path d="M8 1.5a6.5 6.5 0 0 1 6.2 8.4" className="fill-none stroke-status-risk" strokeWidth="1.6" />
              </svg>
              Кольцо — риск ежедневной модели; у агрегата — доля каналов в риске
            </span>
            <span className="flex items-center gap-1.5">
              <svg viewBox="0 0 16 16" className="size-4" aria-hidden>
                <circle cx="7" cy="9" r="5" className="fill-muted-foreground/60" />
                <circle cx="12" cy="4" r="3.5" className="fill-status-alarm" />
              </svg>
              Бейдж — каналы в тревоге сейчас
            </span>
            <span className="text-muted-foreground">Размер узла — число каналов</span>
          </div>
          <p className="text-muted-foreground">
            Координат в данных нет: граф показывает иерархию, а не расположение. Группа по тегу — гипотеза
            электрической группы (две части тега), не подтверждённая схема. Номер пикета — не метры.
          </p>
        </div>
      )}
    </div>
  )
}
