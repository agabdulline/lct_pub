import { ChevronRight, Crosshair, Filter, Maximize, Minus, Plus, Search, X } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router'

import { QueryError } from '@/components/domain/QueryError'
import { ChannelPanel } from '@/components/graph/ChannelPanel'
import { GraphLegend } from '@/components/graph/GraphLegend'
import { GraphSearch, type SearchPick } from '@/components/graph/GraphSearch'
import type { LayoutMode } from '@/components/graph/layout'
import type { GNode } from '@/components/graph/model'
import { ObjectGraph, type ObjectGraphHandle } from '@/components/graph/ObjectGraph'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Kbd } from '@/components/ui/kbd'
import { Spinner } from '@/components/ui/spinner'
import { Toggle } from '@/components/ui/toggle'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { useGraph, useModels, useSubGraph } from '@/lib/api/queries'
import { SUBSYSTEMS, type Status, type Subsystem } from '@/lib/api/types'
import { STATUS_LABEL } from '@/lib/labels'
import { useRunTo } from '@/lib/run'
import { useStreamEvent } from '@/lib/sse/bus'
import { STATUS_DOT_CLASS } from '@/lib/status'
import { cn } from '@/lib/utils'

const STATUSES: Status[] = ['alarm', 'ppr', 'fault', 'risk', 'ok', 'unknown']
const DEFAULT_DAILY_THRESHOLD = 0.5
/** Width of the channel side panel — the camera keeps the focused node out from under it. */
const PANEL_WIDTH = 384
/** Close enough to read the channel and its neighbours, far enough to see the group around it. */
const CHANNEL_ZOOM = 4.5

function toggle<T>(set: Set<T> | null, all: readonly T[], value: T): Set<T> | null {
  const next = new Set(set ?? all)
  if (next.has(value)) next.delete(value)
  else next.add(value)
  return next.size === all.length ? null : next
}

// Map of objects — an interactive graph of the hierarchy and tag groups (docs/architecture/frontend.md §4).
// URL: /objects — overview; /objects/<sub-object> — expanded; /objects/<object> — camera on the object;
// ?channel=<id> — side panel; ?layout=pickets — the picket axis.
export function ObjectsPage() {
  const params = useParams()
  const [search, setSearch] = useSearchParams()
  const navigate = useNavigate()
  const runTo = useRunTo()
  const graphRef = useRef<ObjectGraphHandle>(null)

  const overview = useGraph()
  const models = useModels()
  const id = params.id && /^\d+$/.test(params.id) ? Number(params.id) : null
  const nodes = overview.data?.nodes
  const target = useMemo(() => (id === null ? null : nodes?.find((n) => n.id === `sub_object:${id}` || n.id === `object:${id}`)), [nodes, id])
  const expandedId = target?.kind === 'sub_object' ? id : null
  const sub = useSubGraph(expandedId)

  const layout: LayoutMode = search.get('layout') === 'pickets' && expandedId !== null ? 'pickets' : 'groups'
  const channelParam = search.get('channel')
  const selectedChannel = channelParam && /^\d+$/.test(channelParam) ? Number(channelParam) : null

  const follow = search.get('follow') === '1'
  const lastFollow = useRef(0)
  const [subsystems, setSubsystems] = useState<Set<Subsystem> | null>(null)
  const [statuses, setStatuses] = useState<Set<Status> | null>(null)
  const [searchOpen, setSearchOpen] = useState(false)

  const dailyThreshold =
    models.data?.items.find((m) => m.model === 'daily' && m.active)?.threshold ?? DEFAULT_DAILY_THRESHOLD

  // ⌘K / Ctrl+K opens the search.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        setSearchOpen((o) => !o)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const setParam = useCallback(
    (key: string, value: string | null) => {
      const next = new URLSearchParams(search)
      if (value === null) next.delete(key)
      else next.set(key, value)
      setSearch(next)
    },
    [search, setSearch],
  )

  // Camera: object → its subtree; expanded sub-object → its channels; channel from the URL → the node.
  const focusKey = `${target?.id ?? ''}|${sub.data ? expandedId : ''}|${layout}`
  useEffect(() => {
    const g = graphRef.current
    if (!g || !target || (target.kind === 'sub_object' && !sub.data)) return
    // The camera moves once the physics settles, to the final layout.
    g.whenSettled(() => {
      if (selectedChannel !== null && g.focus(`channel:${selectedChannel}`, CHANNEL_ZOOM, PANEL_WIDTH)) return
      g.fit(g.subtree(target.id))
    })
    // selectedChannel is read once per focus target: clicking a channel must not move the camera.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusKey])

  const onNodeClick = useCallback(
    (n: GNode) => {
      switch (n.kind) {
        case 'district':
          navigate(runTo('/objects'))
          graphRef.current?.fit()
          break
        case 'object':
          navigate(runTo(`/objects/${n.num}`))
          break
        case 'sub_object': {
          // A second click on the expanded sub-object collapses it back to its object.
          const parent = n.parent ? Number(n.parent.split(':')[1]) : null
          navigate(runTo(expandedId === n.num && parent !== null ? `/objects/${parent}` : `/objects/${n.num}`))
          break
        }
        case 'tag_group':
          graphRef.current?.fit(graphRef.current.subtree(n.id))
          break
        case 'channel':
          setParam('channel', String(n.num))
          break
      }
    },
    [navigate, runTo, expandedId, setParam],
  )

  const onPick = useCallback(
    (p: SearchPick) => {
      if (p.kind === 'channel') {
        const to = runTo({ pathname: `/objects/${p.channel.sub_object_id}`, search: `?channel=${p.channel.id}` })
        if (expandedId === p.channel.sub_object_id) {
          setParam('channel', String(p.channel.id))
          graphRef.current?.focus(`channel:${p.channel.id}`, CHANNEL_ZOOM, PANEL_WIDTH)
        } else navigate(to)
      } else navigate(runTo(`/objects/${p.id}`))
    },
    [navigate, runTo, expandedId, setParam],
  )

  // «Следить за тревогами»: a new alarm outside planned checks opens its sub-object, the camera flies to the channel
  // and its panel opens — at most once in 8 s, so a burst does not throw the camera around.
  useStreamEvent('alarm.created', (a) => {
    if (!follow || a.episode?.kind === 'ppr') return
    const now = Date.now()
    if (now - lastFollow.current < 8000) return
    lastFollow.current = now
    if (expandedId === a.sub_object_id) {
      setParam('channel', String(a.channel_id))
      graphRef.current?.focus(`channel:${a.channel_id}`, CHANNEL_ZOOM, PANEL_WIDTH)
      return
    }
    const next = new URLSearchParams(search)
    next.set('channel', String(a.channel_id))
    navigate(runTo({ pathname: `/objects/${a.sub_object_id}`, search: `?${next.toString()}` }))
  })

  // Equipment going down is followed too: its sub-object opens and fits the screen (same 8 s guard as alarms).
  useStreamEvent('equipment_episode.created', (e) => {
    if (!follow) return
    const now = Date.now()
    if (now - lastFollow.current < 8000) return
    lastFollow.current = now
    const next = new URLSearchParams(search)
    next.delete('channel')
    if (e.sub_object_id !== null && expandedId === e.sub_object_id) {
      setParam('channel', null)
      graphRef.current?.whenSettled(() => graphRef.current?.fit(graphRef.current.subtree(`sub_object:${e.sub_object_id}`)))
      return
    }
    navigate(runTo({ pathname: `/objects/${e.sub_object_id ?? e.object_id}`, search: next.toString() ? `?${next.toString()}` : '' }))
  })

  const crumbs = useMemo(() => {
    if (!target || !nodes) return []
    const out: { id: number; label: string }[] = []
    for (let n: typeof target | undefined = target; n && n.kind !== 'district'; n = nodes.find((x) => x.id === n!.parent)) {
      out.unshift({ id: Number(n.id.split(':')[1]), label: n.label })
    }
    return out
  }, [target, nodes])

  const loading = overview.isPending || (expandedId !== null && sub.isPending)

  return (
    <div className="relative -m-4 h-[calc(100svh_-_3rem_-_var(--banners-h,0px))] min-h-0 overflow-hidden">
      <ObjectGraph
        ref={graphRef}
        overview={overview.data}
        sub={expandedId !== null ? sub.data : undefined}
        expandedId={expandedId}
        layout={layout}
        subsystems={subsystems}
        statuses={statuses}
        selected={selectedChannel !== null ? `channel:${selectedChannel}` : null}
        riskThreshold={dailyThreshold}
        showFps={search.get('fps') === '1'}
        onNodeClick={onNodeClick}
        onBackgroundClick={() => selectedChannel !== null && setParam('channel', null)}
      />

      {/* Toolbar */}
      <div className="pointer-events-none absolute inset-x-3 top-3 flex flex-wrap items-start gap-2">
        <div className="pointer-events-auto flex min-w-0 items-center gap-1 rounded-xl bg-card/95 px-2 py-1.5 text-sm shadow-sm ring-1 ring-foreground/10 backdrop-blur">
          <Button variant="ghost" size="sm" onClick={() => navigate(runTo('/objects'))} className="font-semibold">
            Карта объектов
          </Button>
          {crumbs.map((c) => (
            <span key={c.id} className="flex min-w-0 items-center gap-1">
              <ChevronRight className="size-3.5 shrink-0 text-muted-foreground" />
              <Button variant="ghost" size="sm" className="max-w-48 truncate" onClick={() => navigate(runTo(`/objects/${c.id}`))}>
                {c.label}
              </Button>
            </span>
          ))}
          {expandedId !== null && (
            <Tooltip>
              <TooltipTrigger
                render={
                  <Button
                    variant="ghost"
                    size="icon-xs"
                    aria-label="Свернуть подобъект"
                    onClick={() => navigate(runTo(target?.parent ? `/objects/${target.parent.split(':')[1]}` : '/objects'))}
                  />
                }
              >
                <X />
              </TooltipTrigger>
              <TooltipContent>Свернуть подобъект</TooltipContent>
            </Tooltip>
          )}
          {loading && <Spinner className="ml-1" />}
        </div>

        <div className="pointer-events-auto flex items-center gap-1 rounded-xl bg-card/95 p-1 shadow-sm ring-1 ring-foreground/10 backdrop-blur">
          <ToggleGroup
            value={[layout]}
            onValueChange={(v) => v[0] && setParam('layout', v[0] === 'pickets' ? 'pickets' : null)}
            size="sm"
            disabled={expandedId === null}
          >
            <ToggleGroupItem value="groups">По группам</ToggleGroupItem>
            <ToggleGroupItem value="pickets">По пикетам</ToggleGroupItem>
          </ToggleGroup>
        </div>

        <div className="pointer-events-auto flex items-center gap-1 rounded-xl bg-card/95 p-1 shadow-sm ring-1 ring-foreground/10 backdrop-blur">
          <DropdownMenu>
            <DropdownMenuTrigger render={<Button variant="ghost" size="sm" />}>
              <Filter />
              Фильтры
              {(subsystems || statuses) && <span className="size-1.5 rounded-full bg-primary" />}
            </DropdownMenuTrigger>
            <DropdownMenuContent className="w-64" align="start">
              <DropdownMenuGroup>
                <DropdownMenuLabel>Подсистемы — каналы</DropdownMenuLabel>
                {SUBSYSTEMS.map((s) => (
                  <DropdownMenuCheckboxItem
                    key={s}
                    checked={!subsystems || subsystems.has(s)}
                    onCheckedChange={() => setSubsystems((cur) => toggle(cur, SUBSYSTEMS, s))}
                    closeOnClick={false}
                  >
                    {s}
                  </DropdownMenuCheckboxItem>
                ))}
              </DropdownMenuGroup>
              <DropdownMenuSeparator />
              <DropdownMenuGroup>
                <DropdownMenuLabel>Статусы — подобъекты и каналы</DropdownMenuLabel>
                {STATUSES.map((s) => (
                  <DropdownMenuCheckboxItem
                    key={s}
                    checked={!statuses || statuses.has(s)}
                    onCheckedChange={() => setStatuses((cur) => toggle(cur, STATUSES, s))}
                    closeOnClick={false}
                  >
                    <span className={cn('size-2 rounded-full', STATUS_DOT_CLASS[s])} />
                    {STATUS_LABEL[s]}
                  </DropdownMenuCheckboxItem>
                ))}
              </DropdownMenuGroup>
              <DropdownMenuSeparator />
              <DropdownMenuItem
                onClick={() => {
                  setSubsystems(null)
                  setStatuses(null)
                }}
              >
                Сбросить фильтры
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
          <Button variant="ghost" size="sm" onClick={() => setSearchOpen(true)}>
            <Search />
            Поиск
            <Kbd>⌘K</Kbd>
          </Button>
          <Tooltip>
            <TooltipTrigger
              render={<Toggle size="sm" pressed={follow} onPressedChange={(on) => setParam('follow', on ? '1' : null)} aria-label="Следить за тревогами" />}
            >
              <Crosshair />
              Следить
            </TooltipTrigger>
            <TooltipContent>Новая тревога вне плановой проверки — камера на канал, панель открывается сама</TooltipContent>
          </Tooltip>
        </div>
      </div>

      {/* Zoom */}
      <div className="absolute right-3 bottom-3 flex flex-col gap-1 rounded-xl bg-card/95 p-1 shadow-sm ring-1 ring-foreground/10 backdrop-blur">
        <Button variant="ghost" size="icon-sm" aria-label="Приблизить" onClick={() => graphRef.current?.zoomBy(1.5)}>
          <Plus />
        </Button>
        <Button variant="ghost" size="icon-sm" aria-label="Отдалить" onClick={() => graphRef.current?.zoomBy(1 / 1.5)}>
          <Minus />
        </Button>
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label="Показать всё"
          onClick={() => graphRef.current?.fit(target ? graphRef.current.subtree(target.id) : undefined)}
        >
          <Maximize />
        </Button>
      </div>

      <div className="absolute bottom-3 left-3">
        <GraphLegend />
      </div>

      {overview.isError && (
        <div className="absolute inset-0 flex items-center justify-center">
          <QueryError error={overview.error} onRetry={() => void overview.refetch()} what="Карта объектов" />
        </div>
      )}
      {sub.isError && expandedId !== null && (
        <div className="absolute top-16 left-3 w-96">
          <QueryError error={sub.error} onRetry={() => void sub.refetch()} what="Каналы подобъекта" compact className="bg-card ring-1 ring-foreground/10" />
        </div>
      )}

      <GraphSearch open={searchOpen} onOpenChange={setSearchOpen} objects={nodes ?? []} onPick={onPick} />
      <ChannelPanel channelId={selectedChannel} onClose={() => setParam('channel', null)} />
    </div>
  )
}
