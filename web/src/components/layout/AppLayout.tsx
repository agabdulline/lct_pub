import { Moon, Sun } from 'lucide-react'
import { useLayoutEffect, useRef, type ReactNode } from 'react'
import { NavLink, Outlet, useLocation } from 'react-router'

import { ClockBar } from '@/components/domain/ClockBar'
import { EventLossBanner } from '@/components/layout/EventLossBanner'
import { ExternalRunBanner } from '@/components/layout/ExternalRunBanner'
import { RunMissingBanner } from '@/components/domain/RunMissingBanner'
import { StreamToasts } from '@/components/domain/StreamToasts'
import { navigation } from '@/components/layout/navigation'
import { PageTitle } from '@/components/layout/PageTitle'
import { Button } from '@/components/ui/button'
import { Separator } from '@/components/ui/separator'
import { Skeleton } from '@/components/ui/skeleton'
import {
  Sidebar,
  SidebarContent,
  SidebarGroup,
  SidebarGroupContent,
  SidebarHeader,
  SidebarInset,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarProvider,
  SidebarTrigger,
} from '@/components/ui/sidebar'
import { Toaster } from '@/components/ui/sonner'
import { useRun, useRunSearch } from '@/lib/run'
import { RunProvider } from '@/lib/RunProvider'
import { StreamProvider } from '@/lib/sse/StreamProvider'
import { setTheme, useTheme } from '@/lib/theme'

function ThemeToggle() {
  const dark = useTheme() === 'dark'
  return (
    <Button variant="ghost" size="icon-sm" aria-label="Сменить тему" onClick={() => setTheme(dark ? 'light' : 'dark')}>
      {dark ? <Sun /> : <Moon />}
    </Button>
  )
}

function Navigation() {
  const { pathname } = useLocation()
  const search = useRunSearch()
  const isActive = (to: string) => (to === '/' ? pathname === '/' : pathname.startsWith(to))
  return (
    <SidebarMenu>
      {navigation.map(({ to, title, icon: Icon }) => (
        <SidebarMenuItem key={to}>
          <SidebarMenuButton isActive={isActive(to)} tooltip={title} render={<NavLink to={{ pathname: to, search }} />}>
            <Icon />
            <span>{title}</span>
          </SidebarMenuButton>
        </SidebarMenuItem>
      ))}
    </SidebarMenu>
  )
}

/**
 * Banners under the header push the content down; full-height screens (the object map) subtract their height,
 * kept in `--banners-h`, or the page would scroll and hide the banner under the sticky header.
 */
function Banners() {
  const ref = useRef<HTMLDivElement>(null)
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    const root = document.documentElement
    const update = () => root.style.setProperty('--banners-h', `${el.offsetHeight}px`)
    const observer = new ResizeObserver(update)
    observer.observe(el)
    update()
    return () => {
      observer.disconnect()
      root.style.removeProperty('--banners-h')
    }
  }, [])
  return (
    <div ref={ref}>
      <RunMissingBanner />
      <ExternalRunBanner />
      <EventLossBanner />
    </div>
  )
}

/** The screen once the run is known: which screen to draw — the hackathon's or a dataset's — depends on it. */
function RunGate({ children }: { children: ReactNode }) {
  const { ready } = useRun()
  if (ready) return children
  return (
    <div className="space-y-4" aria-busy="true" aria-label="Загружаем прогон">
      <Skeleton className="h-8 w-64" />
      <Skeleton className="h-24 w-full" />
      <Skeleton className="h-80 w-full" />
    </div>
  )
}

export function AppLayout() {
  const theme = useTheme()
  return (
    <RunProvider>
      <StreamProvider>
        <SidebarProvider>
          <Sidebar collapsible="icon">
            <SidebarHeader>
              <div className="px-2 py-1.5 text-sm font-semibold leading-tight">
                Диспетчер ОДС
                <div className="text-xs font-normal text-muted-foreground">Москоллектор · СМВУ</div>
              </div>
            </SidebarHeader>
            <SidebarContent>
              <SidebarGroup>
                <SidebarGroupContent>
                  <Navigation />
                </SidebarGroupContent>
              </SidebarGroup>
            </SidebarContent>
          </Sidebar>
          <SidebarInset className="min-w-0">
            <header className="sticky top-0 z-20 flex h-12 shrink-0 items-center gap-2 border-b bg-background/95 px-3 backdrop-blur">
              <SidebarTrigger />
              <Separator orientation="vertical" className="data-vertical:h-5 data-vertical:self-center" />
              <PageTitle />
              <div className="ml-auto flex min-w-0 items-center gap-2">
                <ClockBar />
                <ThemeToggle />
              </div>
            </header>
            <Banners />
            <main className="flex min-h-0 flex-1 flex-col p-4">
              <RunGate>
                <Outlet />
              </RunGate>
            </main>
          </SidebarInset>
        </SidebarProvider>
        <StreamToasts />
        <Toaster position="bottom-right" theme={theme} />
      </StreamProvider>
    </RunProvider>
  )
}
