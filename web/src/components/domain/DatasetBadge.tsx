import { Database } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { useDataset } from '@/lib/api/queries'
import { DATASET_DIRECTION_LABEL } from '@/lib/labels'
import { cn } from '@/lib/utils'

/**
 * An external dataset — never mistaken for the Moskollektor journal (external-data.md §7): the source token, the
 * short name, and in the tooltip the full title, the task direction, real or synthetic, the license.
 */
export function DatasetBadge({ dataset, className }: { dataset: string; className?: string }) {
  const d = useDataset(dataset)
  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <Badge
            variant="outline"
            className={cn('cursor-default border-source-external/40 bg-source-external/10 font-medium text-source-external', className)}
          />
        }
      >
        <Database /> {d?.short ?? dataset}
      </TooltipTrigger>
      <TooltipContent className="max-w-80">
        <div className="font-medium">{d?.title ?? dataset}</div>
        {d && (
          <div>
            {DATASET_DIRECTION_LABEL[d.direction]} · {d.synthetic ? 'синтетические данные' : 'реальные измерения'} · {d.license}
          </div>
        )}
        <div>Внешний открытый набор — не данные Москоллектора</div>
      </TooltipContent>
    </Tooltip>
  )
}
