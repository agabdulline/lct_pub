"""Vectorized single-date equivalent of the notebook's dense feature builder.

No model labels are used. The notebook verifies this against historical feature
rows before reporting forecast latency. Numerical roundoff can differ slightly.
"""
import time
import numpy as np
import pandas as pd
from .baseline_core import (FEATURES, COUNT_COLUMNS, WINDOWS, POLICY, ACCEPTED_STATES)


def snapshot_features(daily, channels, observed_dates, as_of):
    T=pd.Timestamp(as_of)
    if T!=T.normalize():
        raise ValueError('Use a midnight snapshot')
    registry=channels.loc[channels['тип_датчика'].isin(POLICY['sensor_types']),
                          ['ид_канала_данных','ид_объект','название_датчика','тип_датчика']].rename(
        columns={'ид_канала_данных':'sensor_id','ид_объект':'object_id',
                 'название_датчика':'sensor_name','тип_датчика':'sensor_type'}).set_index('sensor_id')
    h=daily.loc[(daily.date<T)&daily.sensor_id.isin(registry.index)].sort_values(['sensor_id','date'])
    if h.duplicated(['sensor_id','date']).any():
        raise ValueError('Duplicate channel-day: aggregate the full day before scoring')
    result=registry.copy()
    result['date']=T
    last=h.groupby('sensor_id',sort=False).tail(1).set_index('sensor_id')
    first=h.groupby('sensor_id',sort=False).first_ts.min()
    last_ts=last.last_ts.reindex(result.index)
    age=(T-last_ts).dt.total_seconds()/3600
    observed=pd.DatetimeIndex(observed_dates)
    known_counts={w:int(pd.date_range(T-pd.Timedelta(days=w),T-pd.Timedelta(days=1)).isin(observed).sum()) for w in WINDOWS}
    sums={}
    for w in WINDOWS:
        part=h.loc[h.date>=T-pd.Timedelta(days=w)]
        sums[w]=part.groupby('sensor_id',sort=False)[list(COUNT_COLUMNS)+['n_numeric','numeric_sum','numeric_sumsq']].sum().reindex(result.index).fillna(0)
        for col in COUNT_COLUMNS:
            result[f'{col[2:]}_log1p_{w}d']=np.log1p(sums[w][col]).astype('float32')
        for name,col in [('active','n_events'),('fault','n_fault'),('unknown','n_unknown'),('smoke','n_smoke')]:
            result[f'{name}_days_{w}d']=part.loc[part[col]>0].groupby('sensor_id',sort=False).size().reindex(result.index).fillna(0).astype('float32')
        result[f'fault_day_rate_{w}d']=(result[f'fault_days_{w}d']/max(known_counts[w],1)).astype('float32')
        if w in (7,30):
            num=sums[w].n_numeric
            mu=sums[w].numeric_sum/np.maximum(num,1)
            var=np.maximum(0,sums[w].numeric_sumsq/np.maximum(num,1)-mu**2)
            result[f'numeric_count_log1p_{w}d']=np.log1p(num).astype('float32')
            result[f'numeric_mean_{w}d']=mu.where(num>0).astype('float32')
            result[f'numeric_std_{w}d']=np.sqrt(var).where(num>1).astype('float32')
            result[f'numeric_min_{w}d']=part.groupby('sensor_id',sort=False).numeric_min.min().reindex(result.index).astype('float32')
            result[f'numeric_max_{w}d']=part.groupby('sensor_id',sort=False).numeric_max.max().reindex(result.index).astype('float32')
    e7,e30=sums[7].n_events,sums[30].n_events
    result['events_growth_7_vs_previous23']=(np.log1p(e7/7)-np.log1p((e30-e7)/23)).astype('float32')
    for name in ('unknown','alarm','smoke'):
        result[f'{name}_share_7d']=(sums[7][f'n_{name}']/np.maximum(e7,1)).astype('float32')
    for name in ('fault','unknown','smoke'):
        last_day=h.loc[h[f'n_{name}']>0].groupby('sensor_id',sort=False).date.max().reindex(result.index)
        result[f'days_since_{name}_day']=(T-last_day).dt.days.astype('float32')
    result['hours_since_last_event']=age.astype('float32')
    result['history_days']=((T-first.reindex(result.index)).dt.total_seconds()/86400).fillna(0).astype('float32')
    result['global_observed_days_90d']=np.float32(known_counts[90])
    result['weekday']=np.float32(T.dayofweek)
    result['month_sin']=np.float32(np.sin(2*np.pi*T.month/12))
    result['month_cos']=np.float32(np.cos(2*np.pi*T.month/12))
    raw_state=last.last_value.reindex(result.index).fillna('__unknown__').astype(str)
    numeric=pd.to_numeric(raw_state,errors='coerce')
    result['last_numeric_value']=numeric.where(np.isfinite(numeric)).astype('float32')
    result['last_state']=raw_state.mask(np.isfinite(numeric),'__numeric__')
    accepted=pd.Series(False,index=result.index)
    for typ,states in ACCEPTED_STATES.items():
        accepted|=result.sensor_type.eq(typ)&result.last_state.isin(states)
    status=pd.Series('eligible',index=result.index)
    checks=[(last_ts.isna(),'no_history'),(result.last_state.eq('Неисправен'),'already_fault'),
            (~accepted,'unknown_or_unsupported_state'),(age>POLICY['max_last_event_age_days']*24,'stale_state'),
            (result.history_days<POLICY['min_history_days'],'short_history'),
            (pd.Series(known_counts[7]<7,index=result.index),'missing_recent_global_data')]
    for bad,reason in checks:
        status.loc[status.eq('eligible')&bad]=reason
    result['status']=status
    result['eligible']=status.eq('eligible')
    return result.reset_index()


def score_snapshot(model,daily,channels,observed_dates,as_of,threshold):
    tick=time.perf_counter()
    result=snapshot_features(daily,channels,observed_dates,as_of)
    mask=result.eligible
    result['risk_score']=np.nan
    if mask.any():
        result.loc[mask,'risk_score']=model.predict_proba(result.loc[mask,FEATURES])[:,1]
    result['alert']=pd.array([pd.NA]*len(result),dtype='Int8')
    result.loc[mask,'alert']=(result.loc[mask,'risk_score']>=threshold).astype('int8')
    result['target_start']=pd.Timestamp(as_of)+pd.Timedelta(days=1)
    result['target_end']=pd.Timestamp(as_of)+pd.Timedelta(days=2)
    result['threshold']=threshold
    cols=['sensor_id','object_id','sensor_name','sensor_type','date','target_start','target_end',
          'status','hours_since_last_event','risk_score','threshold','alert']
    return result[cols],{'feature_and_score_seconds':time.perf_counter()-tick,'rows':len(result),
                         'eligible':int(mask.sum()),'as_of':str(as_of)}
