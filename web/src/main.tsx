import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { RouterProvider } from 'react-router'

import { TooltipProvider } from '@/components/ui/tooltip'
import { ApiError } from '@/lib/api/client'
import { applyTheme } from '@/lib/theme'
import { router } from '@/router'
import './index.css'

// 4xx will not get better on retry; 5xx, timeouts and network errors get two more attempts.
const retry = (count: number, error: Error) =>
  !(error instanceof ApiError && error.status >= 400 && error.status < 500) && count < 2

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry, staleTime: 5000, refetchOnWindowFocus: false } },
})

applyTheme()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <RouterProvider router={router} />
      </TooltipProvider>
    </QueryClientProvider>
  </StrictMode>,
)
