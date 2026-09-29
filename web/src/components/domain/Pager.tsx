import { ChevronLeft, ChevronRight } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { fmtInt } from '@/lib/format'

type Props = { page: number; size: number; total: number; onPage: (page: number) => void; noun?: string }

// Server pagination: `{items, total, page, size}` (docs/api.md).
export function Pager({ page, size, total, onPage, noun = 'записей' }: Props) {
  const pages = Math.max(1, Math.ceil(total / size))
  const from = total ? (page - 1) * size + 1 : 0
  const to = Math.min(total, page * size)
  return (
    <div className="flex items-center justify-between gap-3 text-sm text-muted-foreground">
      <span>
        {fmtInt(from)}–{fmtInt(to)} из {fmtInt(total)} {noun}
      </span>
      <div className="flex items-center gap-1">
        <Button variant="outline" size="icon-sm" aria-label="Предыдущая страница" disabled={page <= 1} onClick={() => onPage(page - 1)}>
          <ChevronLeft />
        </Button>
        <span className="min-w-20 text-center tabular-nums">
          {page} / {pages}
        </span>
        <Button variant="outline" size="icon-sm" aria-label="Следующая страница" disabled={page >= pages} onClick={() => onPage(page + 1)}>
          <ChevronRight />
        </Button>
      </div>
    </div>
  )
}
