import { Database, ExternalLink } from 'lucide-react'

import { useDataset } from '@/lib/api/queries'
import { DATASET_DIRECTION_LABEL } from '@/lib/labels'
import { useRun } from '@/lib/run'

/**
 * While a run of an external dataset is chosen, every screen says so (external-data.md §7): what dataset, that it
 * is not Moskollektor data, real or synthetic, the task direction and the license with its link.
 */
export function ExternalRunBanner() {
  const { run } = useRun()
  const key = run?.dataset ?? null
  const d = useDataset(key)
  if (!key) return null
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-source-external/30 bg-source-external/10 px-4 py-1.5 text-sm">
      <span className="flex items-center gap-1.5 font-medium text-source-external">
        <Database className="size-4" /> Внешние данные: {d?.short ?? key}
      </span>
      <span className="font-medium">не данные Москоллектора</span>
      {d && (
        <span className="text-muted-foreground">
          {DATASET_DIRECTION_LABEL[d.direction]} · {d.synthetic ? 'синтетические данные' : 'реальные измерения'} ·{' '}
          {d.license_url ? (
            <a href={d.license_url} target="_blank" rel="noreferrer" className="underline underline-offset-4 hover:text-foreground">
              {d.license}
            </a>
          ) : (
            d.license
          )}
        </span>
      )}
      {d?.url && (
        <a href={d.url} target="_blank" rel="noreferrer" className="ml-auto flex items-center gap-1 text-muted-foreground hover:text-foreground">
          источник <ExternalLink className="size-3.5" />
        </a>
      )}
    </div>
  )
}
