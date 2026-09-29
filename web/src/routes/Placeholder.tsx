import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'

// Заглушка экрана до его реализации — этап указан в docs/PLAN.md.
export function Placeholder({ title, description, stage }: { title: string; description: string; stage: string }) {
  return (
    <div className="mx-auto max-w-4xl space-y-4">
      <h1 className="text-xl font-semibold">{title}</h1>
      <Card>
        <CardHeader>
          <CardTitle>Экран в разработке</CardTitle>
          <CardDescription>{description}</CardDescription>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">План: {stage}</CardContent>
      </Card>
    </div>
  )
}
