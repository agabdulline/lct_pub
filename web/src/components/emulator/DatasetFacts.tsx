import { useDataset } from '@/lib/api/queries'
import { DATASET_DIRECTION_LABEL } from '@/lib/labels'
import { cn } from '@/lib/utils'

/** What an external data set is: direction of the task, real or synthetic, licence and source (external-data.md §7). */
export function DatasetFacts({ dataset, className }: { dataset: string; className?: string }) {
  const d = useDataset(dataset)
  if (!d) return null
  return (
    <div className={cn('space-y-0.5 rounded-lg bg-source-external/10 px-2 py-1.5 text-xs', className)}>
      <div className="font-medium">{DATASET_DIRECTION_LABEL[d.direction]}</div>
      <div className="text-muted-foreground">{d.synthetic ? 'Синтетические данные' : 'Реальные измерения'}</div>
      <div className="text-muted-foreground">
        Лицензия:{' '}
        {d.license_url ? (
          <a href={d.license_url} target="_blank" rel="noreferrer" className="underline underline-offset-4 hover:text-foreground">
            {d.license}
          </a>
        ) : (
          d.license
        )}{' '}
        ·{' '}
        <a href={d.url} target="_blank" rel="noreferrer" className="underline underline-offset-4 hover:text-foreground">
          источник
        </a>
      </div>
    </div>
  )
}
