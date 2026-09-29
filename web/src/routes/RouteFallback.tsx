import { Spinner } from '@/components/ui/spinner'

// Shown while a lazily loaded screen arrives.
export function RouteFallback() {
  return (
    <div className="flex h-svh items-center justify-center gap-2 text-sm text-muted-foreground">
      <Spinner /> Загрузка…
    </div>
  )
}
