// TanStack Query hooks. The run is part of every key and every request (docs/api.md, «Прогон»).
// Key shape: [resource, runKey, ...args] — the stream provider relies on it to update the cache.

import { keepPreviousData, useInfiniteQuery, useMutation, useQuery, useQueryClient, type UseQueryOptions } from '@tanstack/react-query'
import { useMemo } from 'react'

import { apiGet, apiPatch, apiPost, type ApiError, type QueryParams } from '@/lib/api/client'
import type {
  AccessIncident,
  AccessIncidentDetail,
  AccessIncidentPage,
  AccessIncidentPatch,
  AlarmDetail,
  AlarmPage,
  AnyModelName,
  Channel,
  ChannelDetail,
  ChannelTimeline,
  ClockLive,
  DashboardRisk,
  DashboardSummary,
  Dataset,
  EmulatorSpeedBody,
  EmulatorStartBody,
  EmulatorState,
  EquipmentEpisodeDetail,
  EquipmentPage,
  Graph,
  Health,
  Metrics,
  ModelCompare,
  ModelInfo,
  ModelName,
  ModelsResponse,
  ObjectDetail,
  ObjectNode,
  Page,
  Prediction,
  Run,
  RunAuditItem,
  RunPatchResult,
  Scenario,
  ScenarioEventsPage,
  ScenarioSummary,
  ScenarioUpload,
  WorkOrder,
  WorkOrderCreate,
  WorkOrderDetail,
  WorkOrderPatch,
} from '@/lib/api/types'
import { offlineRef } from '@/lib/offline-metrics'
import { useRun } from '@/lib/run'

type Extra<T> = Omit<UseQueryOptions<T, Error, T, readonly unknown[]>, 'queryKey' | 'queryFn'>

/** Query of a run-scoped resource. */
function useRunQuery<T>(resource: string, path: string, params: QueryParams = {}, extra: Extra<T> = {}) {
  const { runId, runKey, ready } = useRun()
  return useQuery({
    queryKey: [resource, runKey, path, params] as const,
    queryFn: () => apiGet<T>(path, { ...params, run: runId }),
    ...extra,
    enabled: ready && (extra.enabled ?? true),
  })
}

// ---------- system

export const useClock = () =>
  useQuery({ queryKey: ['clock'], queryFn: () => apiGet<ClockLive>('/clock'), refetchInterval: 5000 })

export const useHealth = () =>
  useQuery({ queryKey: ['health'], queryFn: () => apiGet<Health>('/health'), refetchInterval: 5000 })

// ---------- objects, channels, graph

export const useObjects = () => useRunQuery<ObjectNode[]>('objects', '/objects', {}, { staleTime: 30_000 })

export const useObject = (id: number | null) =>
  useRunQuery<ObjectDetail>('object', `/objects/${id}`, {}, { enabled: id !== null })

export const useChannels = (params: QueryParams, enabled = true) =>
  useRunQuery<Page<Channel>>('channels', '/channels', params, { enabled, placeholderData: keepPreviousData })

export const useChannel = (id: number | null) =>
  useRunQuery<ChannelDetail>('channel', `/channels/${id}`, {}, { enabled: id !== null })

export const useChannelTimeline = (id: number | null, params: { from?: string; to?: string } = {}) =>
  useRunQuery<ChannelTimeline>('channel-timeline', `/channels/${id}/timeline`, params, { enabled: id !== null })

export const useChannelPredictions = (id: number | null, params: QueryParams = {}) =>
  useRunQuery<Page<Prediction>>('channel-predictions', `/channels/${id}/predictions`, params, {
    enabled: id !== null,
    placeholderData: keepPreviousData,
  })

export const useGraph = () => useRunQuery<Graph>('graph', '/graph', {}, { staleTime: 60_000 })

export const useSubGraph = (subObjectId: number | null) =>
  useRunQuery<Graph>('graph-sub', `/graph/${subObjectId}`, {}, { enabled: subObjectId !== null, staleTime: 60_000 })

// ---------- alarms

export const useAlarms = (params: QueryParams) =>
  useRunQuery<AlarmPage>('alarms', '/alarms', params, { placeholderData: keepPreviousData })

/** Equipment episodes of the run: outages and equipment faults, an information row of the feed (no model verdict). */
export const useEquipmentEpisodes = (params: QueryParams, enabled = true) =>
  useRunQuery<EquipmentPage>('equipment-episodes', '/equipment-episodes', params, { placeholderData: keepPreviousData, enabled })

export const useEquipmentEpisode = (id: number | null) =>
  useRunQuery<EquipmentEpisodeDetail>('equipment-episode', `/equipment-episodes/${id}`, {}, { enabled: id !== null })

/**
 * Opened under guard (stage 14.3): incidents of the run by the guard rules, an information row of the feed with no
 * model verdict. Candidates are never returned; `dismissed` only when asked for.
 */
export const useAccessIncidents = (params: QueryParams, enabled = true) =>
  useRunQuery<AccessIncidentPage>('access-incidents', '/access-incidents', params, { placeholderData: keepPreviousData, enabled })

export const useAccessIncident = (id: number | null) =>
  useRunQuery<AccessIncidentDetail>('access-incident', `/access-incidents/${id}`, {}, { enabled: id !== null })

/** The dispatcher's review of an incident; the work order stays as it is — it is closed on the orders screen. */
export function usePatchAccessIncident(id: number) {
  const qc = useQueryClient()
  const { runId } = useRun()
  return useMutation({
    mutationFn: (patch: AccessIncidentPatch) => apiPatch<AccessIncident>(`/access-incidents/${id}`, patch, { run: runId }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['access-incidents'] })
      qc.invalidateQueries({ queryKey: ['access-incident'] })
    },
  })
}

export const useAlarm = (id: number | null) =>
  useRunQuery<AlarmDetail>('alarm', `/alarms/${id}`, {}, { enabled: id !== null })

// ---------- predictions, metrics, models

// Outcomes are closed by the backend tick and announced by `prediction.updated` (lib/sse/cache.ts).
export const usePredictions = (params: QueryParams, enabled = true) =>
  useRunQuery<Page<Prediction>>('predictions', '/predictions', params, { placeholderData: keepPreviousData, enabled })

/**
 * Alerts whose fault has already started are final true positives; the rest may still go either way. So before
 * the horizon ends Precision is bounded from below by confirmed / all alerts — it cannot overstate, unlike
 * counting early-closed outcomes only (AGENTS.md §5).
 */
export function useConfirmedAlerts(model: AnyModelName, version: string | null = null) {
  // One version only: the event model is scored by several (stage 15), their alerts are never counted together.
  const all = usePredictions({ model, model_version: version, alert: 'true', size: 1 }).data?.total
  const confirmed = usePredictions({ model, model_version: version, alert: 'true', outcome: 'fault', size: 1 }).data?.total
  return all === undefined || confirmed === undefined ? null : { all, confirmed }
}

/** The channel's last scored decision (of one model or either) — skipped ones have no score and no factors. */
export const useLastScoredPrediction = (channelId: number | null, model: ModelName | null = null) =>
  useRunQuery<Page<Prediction>>('predictions', '/predictions', { channel_id: channelId, status: 'scored', model, size: 1 }, { enabled: channelId !== null })

/**
 * The event model's decision that holds for the channel now. A repeat alarm within 24 h is skipped
 * (`decided_within_24h`) and the earlier scored decision still stands — as the feed shows it («ранее»).
 * Other skips are the decision itself.
 */
export function useAlarmDecision(channel: ChannelDetail | undefined): { decision: Prediction | null; inherited: boolean } {
  const last = channel?.last_alarm_prediction ?? null
  const repeat = last?.status === 'skipped' && last.skip_reason === 'decided_within_24h'
  const scored = useLastScoredPrediction(repeat && channel ? channel.id : null, 'smoke_alarm').data?.items[0] ?? null
  return repeat && scored ? { decision: scored, inherited: true } : { decision: last, inherited: false }
}

export const usePrediction = (id: number | null) =>
  useRunQuery<Prediction>('prediction', `/predictions/${id}`, {}, { enabled: id !== null })

/** `model: null` — not known yet (a data set run before /models): no request. */
export const useMetrics = (model: AnyModelName | null, params: QueryParams = {}) =>
  useRunQuery<Metrics>('metrics', '/metrics', { model, ...params }, { enabled: model !== null })

/** Offline reference of the shown version (lib/offline-metrics.ts): its own report, never another version's numbers. */
export const useOfflineRef = (model: ModelName, version: string | null = null) => {
  const models = useModels().data
  return offlineRef(model, models?.items, version, models?.daily_routing)
}

export const useModels = () =>
  useQuery({ queryKey: ['models'], queryFn: () => apiGet<ModelsResponse>('/models'), staleTime: 60_000 })

/** External models of a data set (stage 14): what the journal offers in a run of that data set. */
export function useExternalModels(dataset: string | null | undefined): ModelInfo[] {
  const items = useModels().data?.items
  return dataset && items ? items.filter((m) => m.family === 'external' && m.dataset === dataset) : []
}

// ---------- dashboard

export const useDashboardSummary = () =>
  useRunQuery<DashboardSummary>('dashboard-summary', '/dashboard/summary', {}, { refetchInterval: 30_000 })

export const useDashboardRisk = () =>
  useRunQuery<DashboardRisk>('dashboard-risk', '/dashboard/risk', {}, { refetchInterval: 60_000 })

// ---------- work orders

export const useWorkOrders = (params: QueryParams) =>
  useRunQuery<Page<WorkOrder>>('work-orders', '/work-orders', params, { placeholderData: keepPreviousData })

export const useWorkOrder = (id: number | null) =>
  useRunQuery<WorkOrderDetail>('work-order', `/work-orders/${id}`, {}, { enabled: id !== null })

export function useCreateWorkOrder() {
  const qc = useQueryClient()
  const { runId } = useRun()
  return useMutation({
    mutationFn: (body: WorkOrderCreate) => apiPost<WorkOrder>('/work-orders', body, { run: runId }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['work-orders'] })
      qc.invalidateQueries({ queryKey: ['channel'] })
    },
  })
}

export function usePatchWorkOrder(id: number) {
  const qc = useQueryClient()
  const { runId } = useRun()
  return useMutation({
    mutationFn: (patch: WorkOrderPatch) => apiPatch<WorkOrderDetail>(`/work-orders/${id}`, patch, { run: runId }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['work-orders'] })
      qc.invalidateQueries({ queryKey: ['work-order'] })
      qc.invalidateQueries({ queryKey: ['dashboard-summary'] })
    },
  })
}

// ---------- emulator and runs

/** What a scenario will transmit (13.2): the data do not change, one read per visit is enough. */
export const useScenarioSummary = (id: string) =>
  useQuery({
    queryKey: ['scenario-summary', id],
    queryFn: () => apiGet<ScenarioSummary>(`/emulator/scenarios/${id}/summary`),
    staleTime: 10 * 60_000,
    // A scenario just made from a file reaches the emulator a few seconds after the upload answers: wait for it.
    retry: (count, e) => ((e as ApiError).status === 404 ? count < 8 : count < 2),
    retryDelay: (count, e) => ((e as ApiError).status === 404 ? 1000 : Math.min(8000, 1000 * 2 ** count)),
  })

/** All signals of a scenario in the order of transmission, page by page with the server's cursor. */
export const useScenarioEvents = (id: string, filters: QueryParams) =>
  useInfiniteQuery({
    queryKey: ['scenario-events', id, filters],
    queryFn: ({ pageParam }) => apiGet<ScenarioEventsPage>(`/emulator/scenarios/${id}/events`, { ...filters, cursor: pageParam, limit: 200 }),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor,
    staleTime: 10 * 60_000,
    placeholderData: keepPreviousData,
  })

/** The analysis of an uploaded journal (13.1); the page keeps the latest dry run itself. */
export const useScenarioUpload = (id: string | null) =>
  useQuery({
    queryKey: ['scenario-upload', id],
    queryFn: () => apiGet<ScenarioUpload>(`/scenario-uploads/${id}`),
    enabled: id !== null,
    staleTime: Infinity,
    retry: false,
  })

/**
 * External open datasets (external-data.md §2). Until the backend has the registry (404), there are none —
 * the screens show hackathon data only.
 */
export const useDatasets = () =>
  useQuery({
    queryKey: ['datasets'],
    queryFn: async () => {
      try {
        return (await apiGet<{ items: Dataset[] }>('/datasets')).items
      } catch (e) {
        if ((e as ApiError).status === 404) return [] as Dataset[]
        throw e
      }
    },
    staleTime: Infinity,
  })

export const useDataset = (key: string | null | undefined) => {
  const { data } = useDatasets()
  return key ? (data?.find((d) => d.key === key) ?? null) : null
}

export const useScenarios = () =>
  useQuery({
    queryKey: ['emulator-scenarios'],
    queryFn: () => apiGet<{ items: Scenario[] }>('/emulator/scenarios'),
    staleTime: 5 * 60_000,
  })

/**
 * Emulator state is polled every 3 s. Between polls the simulated time and the actual speed of the running run
 * come from the stream's `clock.tick`, kept in ['clock'] — never written into this query (lib/sse/cache.ts).
 */
export function useEmulatorState() {
  const query = useQuery({ queryKey: ['emulator-state'], queryFn: () => apiGet<EmulatorState>('/emulator/state'), refetchInterval: 3000 })
  const live = useClock().data
  const polled = query.data
  const data = useMemo(
    () =>
      polled && polled.state === 'running' && live?.run_id != null && live.run_id === polled.run_id
        ? { ...polled, sim_time: live.now, effective_speed: live.effective_speed ?? polled.effective_speed }
        : polled,
    [polled, live],
  )
  return { ...query, data }
}

export const useRunDetail = (id: number | null) =>
  useQuery({ queryKey: ['run', id], queryFn: () => apiGet<Run>(`/runs/${id}`), enabled: id !== null })

/**
 * Stage 15: switch the run's primary event-model version; alarms scored from now on follow it. `applyToPast` — also
 * re-verdict the past alarms by it (missing work orders are opened, none withdrawn): verdicts, orders and the run's
 * audit change too.
 */
export function useSetPrimaryModel() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ runId, version, applyToPast = false }: { runId: number; version: string; applyToPast?: boolean }) =>
      apiPatch<RunPatchResult>(`/runs/${runId}`, { primary_model: version, apply_to_past: applyToPast }),
    onSuccess: (_, { runId, applyToPast }) => {
      void qc.invalidateQueries({ queryKey: ['runs'] })
      void qc.invalidateQueries({ queryKey: ['run-audit', runId] })
      if (applyToPast) for (const key of ['alarms', 'alarm', 'work-orders', 'dashboard-summary']) void qc.invalidateQueries({ queryKey: [key] })
    },
  })
}

/** Stage 15: the run's audit — primary switches and re-verdicts, newest first. */
export const useRunAudit = (runId: number | null) =>
  useQuery({
    queryKey: ['run-audit', runId],
    queryFn: () => apiGet<{ items: RunAuditItem[] }>(`/runs/${runId}/audit`),
    enabled: runId !== null,
  })

/** Stage 15: offline check of the event-model versions on the same decisions (lct-50); 404 — not published yet. */
export const useModelCompare = (model: ModelName) =>
  useQuery({
    queryKey: ['models-compare', model],
    queryFn: async () => {
      try {
        return await apiGet<ModelCompare>('/models/compare', { model })
      } catch (e) {
        if ((e as ApiError).status === 404) return null
        throw e
      }
    },
    staleTime: 5 * 60_000,
  })

export function useEmulatorCommand() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (
      cmd:
        | ({ action: 'start' } & EmulatorStartBody & { primary_model?: string })
        | { action: 'pause' | 'resume' | 'stop' }
        | ({ action: 'speed' } & EmulatorSpeedBody),
    ) => {
      const { action, ...body } = cmd
      return apiPost<unknown>(`/emulator/${action}`, body)
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['emulator-state'] })
      qc.invalidateQueries({ queryKey: ['runs'] })
      qc.invalidateQueries({ queryKey: ['clock'] })
    },
  })
}
