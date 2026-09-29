import { DatasetBadge } from '@/components/domain/DatasetBadge'
import { Badge } from '@/components/ui/badge'
import type { ScenarioDataset, ScenarioSource } from '@/lib/api/types'
import { cn } from '@/lib/utils'

// Where the scenario data come from — on the card and the scenario page. External datasets get their own badge in
// the source token so they are never mistaken for the SMVU journal (external-data.md §7).
const LABEL: Record<ScenarioSource, string> = {
  builtin: 'журнал СМВУ',
  upload: 'из файла',
}

export function ScenarioSourceBadge({ source, dataset, className }: { source: ScenarioSource | undefined; dataset?: ScenarioDataset | null; className?: string }) {
  if (dataset) return <DatasetBadge dataset={dataset.key} className={className} />
  const s = source === 'upload' ? 'upload' : 'builtin'
  return (
    <Badge variant="outline" className={cn('font-normal', className)}>
      {LABEL[s]}
    </Badge>
  )
}
