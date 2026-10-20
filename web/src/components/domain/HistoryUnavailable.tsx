import { History } from 'lucide-react'
import { useNavigate } from 'react-router'

import { Button } from '@/components/ui/button'
import { Empty, EmptyContent, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from '@/components/ui/empty'
import { useRun } from '@/lib/run'

/** A run-only screen in «Вся история» (`?run=0`): the base has no feed, no clock and no work orders of its own. */
export function HistoryUnavailable({ what }: { what: string }) {
  const navigate = useNavigate()
  const { all } = useRun()
  const mode = all ? '«Все данные»' : '«Вся история»'
  return (
    <Empty className="mx-auto max-w-xl">
      <EmptyHeader>
        <EmptyMedia variant="icon">
          <History />
        </EmptyMedia>
        <EmptyTitle>
          {what}: недоступно в режиме {mode}
        </EmptyTitle>
        <EmptyDescription>
          {all ? 'Все данные — хакатон и наборы без прогона' : 'Вся история — журнал хакатона без прогона'}: ленты, ползунка времени и
          заявок здесь нет. Итоги — на дашборде, состояние сети — на карте, решения моделей — в журнале прогнозов. Чтобы увидеть
          ленту, выберите прогон в шапке.
        </EmptyDescription>
      </EmptyHeader>
      <EmptyContent>
        <Button variant="outline" onClick={() => navigate(all ? '/dashboard?run=all' : '/dashboard?run=0')}>
          {all ? 'Дашборд всех данных' : 'Дашборд всей истории'}
        </Button>
      </EmptyContent>
    </Empty>
  )
}
