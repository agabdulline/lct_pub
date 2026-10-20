import { Info } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'

/** Info icon next to a screen title: what the screen shows, in two or three sentences. */
export function PageInfo({ children }: { children: React.ReactNode }) {
  return (
    <Tooltip>
      <TooltipTrigger
        render={<Button variant="ghost" size="icon-xs" aria-label="Об этом экране" className="text-muted-foreground" />}
      >
        <Info />
      </TooltipTrigger>
      <TooltipContent className="max-w-sm text-left font-normal">{children}</TooltipContent>
    </Tooltip>
  )
}
