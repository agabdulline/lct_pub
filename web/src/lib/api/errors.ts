import { ApiError } from '@/lib/api/client'

/** What went wrong, in words, by the API error (docs/api.md: `{error: {code, message}}`). */
export function describeError(error: unknown): { title: string; hint: string } {
  if (error instanceof ApiError) {
    if (error.code === 'timeout') return { title: 'API не ответил вовремя', hint: 'Сервис перегружен или завис — попробуйте ещё раз.' }
    if (error.code === 'network') return { title: 'API недоступен', hint: 'Нет соединения с сервисом — проверьте, что стенд запущен.' }
    if (error.code === 'run_not_found') return { title: 'Прогон не найден', hint: 'Выберите другой прогон в шапке.' }
    if (error.status === 404) return { title: 'Раздел ещё не подключён', hint: `API ответил 404 — эндпоинт пока не реализован. ${error.message}` }
    if (error.status === 503) return { title: 'Сервис временно недоступен', hint: error.message }
    if (error.status >= 500) return { title: 'Ошибка сервиса', hint: error.message }
    return { title: 'Запрос отклонён', hint: error.message }
  }
  return { title: 'Не удалось загрузить', hint: error instanceof Error ? error.message : String(error) }
}
