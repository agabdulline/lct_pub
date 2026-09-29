import {
  Activity,
  BellRing,
  Bot,
  ClipboardList,
  LayoutDashboard,
  Network,
  Radio,
  ScrollText,
  type LucideIcon,
} from 'lucide-react'

// Экраны первой версии — docs/product/v1-scope.md, маршруты — docs/architecture/frontend.md §3.
export const navigation: { to: string; title: string; icon: LucideIcon }[] = [
  { to: '/', title: 'Лента тревог', icon: BellRing },
  { to: '/dashboard', title: 'Дашборд', icon: LayoutDashboard },
  { to: '/objects', title: 'Карта объектов', icon: Network },
  { to: '/predictions', title: 'Журнал прогнозов', icon: ScrollText },
  { to: '/work-orders', title: 'Заявки на ТО', icon: ClipboardList },
  { to: '/models', title: 'Модели', icon: Bot },
  { to: '/emulator', title: 'Эмулятор СМВУ', icon: Radio },
  { to: '/system', title: 'Состояние системы', icon: Activity },
]
