// Contract check (D15, 8.7): every hand-written type that has a generated counterpart in `schema.d.ts` must match it
// both ways. Checked by `tsc -b` (make test); nothing here runs. After `make api-types`, a backend change that the
// UI does not follow breaks the build on the line of the type.
//
// UI → backend: the UI type is assignable to the generated one — no required field of the API is missing.
// backend → UI: the generated type is assignable to the UI type with literal unions widened to `string` —
// the API has every field the UI reads, with the same nullability. Enums that the backend declares as plain
// `str` (sensor_type, subsystem, model, rule, skip_reason) stay narrow in the UI.

import type { components } from './schema'
import type * as T from './types'

type G = components['schemas']

type Widen<X> = X extends string
  ? string
  : X extends number
    ? number
    : X extends boolean
      ? boolean
      : X extends null
        ? null
        : X extends (infer U)[]
          ? Widen<U>[]
          : X extends object
            ? { [K in keyof X]: Widen<X[K]> }
            : X

// The `satisfies` form reports the exact field on mismatch.
export const contract = {
  alarm: (ui: T.Alarm, api: G['Alarm']) => [ui satisfies G['Alarm'], api satisfies Widen<T.Alarm>],
  alarmDetail: (ui: T.AlarmDetail, api: G['AlarmDetail']) => [ui satisfies G['AlarmDetail'], api satisfies Widen<T.AlarmDetail>],
  alarmPage: (ui: T.AlarmPage, api: G['AlarmPage']) => [ui satisfies G['AlarmPage'], api satisfies Widen<T.AlarmPage>],
  alarmCounts: (ui: T.AlarmCounts, api: G['AlarmCounts']) => [ui satisfies G['AlarmCounts'], api satisfies Widen<T.AlarmCounts>],
  channel: (ui: T.Channel, api: G['Channel']) => [ui satisfies G['Channel'], api satisfies Widen<T.Channel>],
  channelDetail: (ui: T.ChannelDetail, api: G['ChannelDetail']) => [ui satisfies G['ChannelDetail'], api satisfies Widen<T.ChannelDetail>],
  channelPage: (ui: T.Page<T.Channel>, api: G['ChannelPage']) => [ui satisfies G['ChannelPage'], api satisfies Widen<T.Page<T.Channel>>],
  timeline: (ui: T.ChannelTimeline, api: G['ChannelTimeline']) => [ui satisfies G['ChannelTimeline'], api satisfies Widen<T.ChannelTimeline>],
  equipmentEpisode: (ui: T.EquipmentEpisode, api: G['EquipmentEpisode']) => [ui satisfies G['EquipmentEpisode'], api satisfies Widen<T.EquipmentEpisode>],
  equipmentEpisodeDetail: (ui: T.EquipmentEpisodeDetail, api: G['EquipmentEpisodeDetail']) => [
    ui satisfies G['EquipmentEpisodeDetail'],
    api satisfies Widen<T.EquipmentEpisodeDetail>,
  ],
  equipmentPage: (ui: T.EquipmentPage, api: G['EquipmentEpisodePage']) => [ui satisfies G['EquipmentEpisodePage'], api satisfies Widen<T.EquipmentPage>],
  accessIncident: (ui: T.AccessIncident, api: G['AccessIncident']) => [ui satisfies G['AccessIncident'], api satisfies Widen<T.AccessIncident>],
  accessIncidentDetail: (ui: T.AccessIncidentDetail, api: G['AccessIncidentDetail']) => [
    ui satisfies G['AccessIncidentDetail'],
    api satisfies Widen<T.AccessIncidentDetail>,
  ],
  accessIncidentPage: (ui: T.AccessIncidentPage, api: G['AccessIncidentPage']) => [ui satisfies G['AccessIncidentPage'], api satisfies Widen<T.AccessIncidentPage>],
  accessIncidentPatch: (ui: T.AccessIncidentPatch, api: G['AccessIncidentPatch']) => [ui satisfies G['AccessIncidentPatch'], api satisfies Widen<T.AccessIncidentPatch>],
  clock: (ui: T.Clock, api: G['Clock']) => [ui satisfies G['Clock'], api satisfies Widen<T.Clock>],
  graph: (ui: T.Graph, api: G['Graph']) => [ui satisfies G['Graph'], api satisfies Widen<T.Graph>],
  objectNode: (ui: T.ObjectNode, api: G['ObjectNode']) => [ui satisfies G['ObjectNode'], api satisfies Widen<T.ObjectNode>],
  objectDetail: (ui: T.ObjectDetail, api: G['ObjectDetail']) => [ui satisfies G['ObjectDetail'], api satisfies Widen<T.ObjectDetail>],
  prediction: (ui: T.Prediction, api: G['Prediction']) => [ui satisfies G['Prediction'], api satisfies Widen<T.Prediction>],
  predictionPage: (ui: T.Page<T.Prediction>, api: G['PredictionPage']) => [ui satisfies G['PredictionPage'], api satisfies Widen<T.Page<T.Prediction>>],
  workOrder: (ui: T.WorkOrder, api: G['WorkOrder']) => [ui satisfies G['WorkOrder'], api satisfies Widen<T.WorkOrder>],
  run: (ui: T.Run, api: G['RunOut']) => [ui satisfies G['RunOut'], api satisfies Widen<T.Run>],
  dataset: (ui: T.Dataset, api: G['DatasetOut']) => [ui satisfies G['DatasetOut'], api satisfies Widen<T.Dataset>],
  metrics: (ui: T.Metrics, api: G['Metrics']) => [ui satisfies G['Metrics'], api satisfies Widen<T.Metrics>],
  metricsBlock: (ui: T.MetricsBlock, api: G['MetricsBlock']) => [ui satisfies G['MetricsBlock'], api satisfies Widen<T.MetricsBlock>],
  model: (ui: T.ModelInfo, api: G['ModelInfo']) => [ui satisfies G['ModelInfo'], api satisfies Widen<T.ModelInfo>],
  offlineReport: (ui: T.OfflineReport, api: G['OfflineReport']) => [ui satisfies G['OfflineReport'], api satisfies Widen<T.OfflineReport>],
  models: (ui: T.ModelsResponse, api: G['ModelsResponse']) => [ui satisfies G['ModelsResponse'], api satisfies Widen<T.ModelsResponse>],
  dashboardSummary: (ui: T.DashboardSummary, api: G['DashboardSummary']) => [ui satisfies G['DashboardSummary'], api satisfies Widen<T.DashboardSummary>],
  dashboardRisk: (ui: T.DashboardRisk, api: G['DashboardRisk']) => [ui satisfies G['DashboardRisk'], api satisfies Widen<T.DashboardRisk>],
  workOrderPage: (ui: T.Page<T.WorkOrder>, api: G['WorkOrderPage']) => [ui satisfies G['WorkOrderPage'], api satisfies Widen<T.Page<T.WorkOrder>>],
  workOrderDetail: (ui: T.WorkOrderDetail, api: G['WorkOrderDetail']) => [ui satisfies G['WorkOrderDetail'], api satisfies Widen<T.WorkOrderDetail>],
  workOrderCreate: (ui: T.WorkOrderCreate, api: G['WorkOrderCreate']) => [ui satisfies G['WorkOrderCreate'], api satisfies Widen<T.WorkOrderCreate>],
  workOrderPatch: (ui: T.WorkOrderPatch, api: G['WorkOrderPatch']) => [ui satisfies G['WorkOrderPatch'], api satisfies Widen<T.WorkOrderPatch>],
}
