import { createBrowserRouter } from 'react-router'

import { AppLayout } from '@/components/layout/AppLayout'
import { AlarmFeedPage } from '@/routes/AlarmFeedPage'
import { Placeholder } from '@/routes/Placeholder'
import { RouteFallback } from '@/routes/RouteFallback'
import { SystemPage } from '@/routes/SystemPage'

// Маршруты — docs/architecture/frontend.md §3; содержимое экранов — docs/product/v1-scope.md.
// Heavy screens are loaded lazily: the graph and the charts do not weigh on the first screen.
const objectsPage = () => import('@/routes/ObjectsPage').then((m) => ({ Component: m.ObjectsPage }))

export const router = createBrowserRouter([
  {
    element: <AppLayout />,
    hydrateFallbackElement: <RouteFallback />,
    children: [
      { index: true, element: <AlarmFeedPage /> },
      { path: 'dashboard', lazy: () => import('@/routes/DashboardPage').then((m) => ({ Component: m.DashboardPage })) },
      { path: 'objects', lazy: objectsPage },
      { path: 'objects/:id', lazy: objectsPage },
      { path: 'channels/:id', lazy: () => import('@/routes/ChannelPage').then((m) => ({ Component: m.ChannelPage })) },
      { path: 'predictions', lazy: () => import('@/routes/PredictionsPage').then((m) => ({ Component: m.PredictionsPage })) },
      { path: 'work-orders', lazy: () => import('@/routes/WorkOrdersPage').then((m) => ({ Component: m.WorkOrdersPage })) },
      { path: 'work-orders/:id', lazy: () => import('@/routes/WorkOrderPage').then((m) => ({ Component: m.WorkOrderPage })) },
      { path: 'models', lazy: () => import('@/routes/ModelsPage').then((m) => ({ Component: m.ModelsPage })) },
      { path: 'emulator', lazy: () => import('@/routes/EmulatorPage').then((m) => ({ Component: m.EmulatorPage })) },
      { path: 'emulator/scenarios/:id', lazy: () => import('@/routes/ScenarioPage').then((m) => ({ Component: m.ScenarioPage })) },
      { path: 'emulator/upload', lazy: () => import('@/routes/UploadPage').then((m) => ({ Component: m.UploadPage })) },
      { path: 'emulator/upload/:id', lazy: () => import('@/routes/UploadPage').then((m) => ({ Component: m.UploadPage })) },
      { path: 'system', element: <SystemPage /> },
      { path: '*', element: <Placeholder title="Страница не найдена" description="Такого экрана нет." stage="—" /> },
    ],
  },
])
