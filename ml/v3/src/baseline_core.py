"""Causal channel-only features and reproducible helpers for the notebook.

The target is a new *logged* fault episode, not a verified physical breakdown.
All feature windows are [T - window, T). A snapshot is made at midnight.
"""
from __future__ import annotations

from pathlib import Path
import hashlib
import json
import time
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from sklearn.metrics import (average_precision_score, precision_recall_curve,
                             roc_auc_score, confusion_matrix)

SEED = 42
VERSION = 'fault-onset-five-families-v1'
WINDOWS = (1, 7, 30, 90)
COUNT_COLUMNS = ('n_events', 'n_alarm', 'n_fault', 'n_unknown', 'n_smoke', 'n_disabled', 'n_normal')
SENSOR_FAMILIES = {
    'Датчик дыма':'Дымовые', 'Газовый датчик':'Газовые',
    'Датчик температуры':'Температурные', 'Тепловой датчик':'Температурные',
    'Датчик движения':'Объёмные', 'КД Дверь':'Контактные',
    'КД Люк':'Контактные', 'КД АВ':'Контактные',
}
ACCEPTED_STATES = {
    'Датчик дыма': ('Норма','Дыма нет','Обнаружен дым'),
    'Газовый датчик': ('Норма','Обнаружен газ','Газа нет','__numeric__'),
    'Датчик температуры': ('Норма','__numeric__'),
    'Тепловой датчик': ('Норма','В норме от +3 до +40','Температура ниже 3ºC','Температура выше 40ºC'),
    'Датчик движения': ('Норма','Движения нет','Обнаружено движение'),
    'КД Дверь': ('Норма','Не замкнут','Замкнут'),
    'КД Люк': ('Норма','Не замкнут','Замкнут'),
    'КД АВ': ('Норма','Не замкнут','Замкнут'),
}
CAT_FEATURES = ['last_state','sensor_type']
POLICY = dict(lead_hours=24, end_hours=48, cooldown_days=7, min_history_days=7,
              max_last_event_age_days=90, sensor_types=list(SENSOR_FAMILIES))


def dump_json(path, obj):
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str, allow_nan=False), encoding='utf-8')


def sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def past_sum(x, days):
    """At index i return sum(x[max(0,i-days):i]); x[i] is excluded."""
    x = np.asarray(x, dtype=np.float64)
    cs = np.r_[0., np.cumsum(x)]
    i = np.arange(len(x))
    return cs[i] - cs[np.maximum(i-days, 0)]


def days_since_day(mask):
    """Distance to the last observed event DAY strictly before the snapshot."""
    i = np.arange(len(mask))
    last = np.maximum.accumulate(np.where(mask, i, -1))
    last = np.r_[-1, last[:-1]]
    return np.where(last >= 0, i-last, np.nan).astype('float32')


def feature_columns():
    counts = [f'{col[2:]}_log1p_{w}d' for col in COUNT_COLUMNS for w in WINDOWS]
    days = [f'{name}_days_{w}d' for name in ('active','fault','unknown','smoke') for w in WINDOWS]
    rates = [f'fault_day_rate_{w}d' for w in WINDOWS]
    extra = ['events_growth_7_vs_previous23', 'unknown_share_7d', 'alarm_share_7d',
             'smoke_share_7d', 'days_since_fault_day', 'days_since_unknown_day',
             'days_since_smoke_day', 'hours_since_last_event', 'history_days',
             'global_observed_days_90d', 'weekday', 'month_sin', 'month_cos', 'last_state','sensor_type']
    numeric = ['last_numeric_value'] + [f'numeric_{stat}_{w}d' for w in (7,30) for stat in ('count_log1p','mean','std','min','max')]
    return counts + days + rates + extra + numeric


FEATURES = feature_columns()


def channel_frame(g, dates, observed_dates, *, include_target=True,
                  label_end_exclusive=None, policy=None):
    """Build one channel's dense calendar without using future data in features.

    Eligibility is known at T: >=7 days of history; accepted last state;
    no logged fault in the past 7 days; last event no older than 90 days;
    full global coverage in the past 7 days. Silence is not proof of health.

    y=1 iff a fault is logged in [T+24h,T+48h), AND none in [T,T+24h).
    An earlier fault is retained as y=0 with early_fault=1; it is never removed
    using future knowledge. This predicts the first registered fault after T.
    """
    policy = POLICY if policy is None else policy
    g = g.sort_values('date')
    if g.duplicated('date').any():
        raise ValueError('One row per channel/day is required')
    dates = pd.DatetimeIndex(dates)
    if len(dates) > 1 and not (np.diff(dates.values) == np.timedelta64(1, 'D')).all():
        raise ValueError('A complete daily calendar is required')
    z = g.set_index('date').reindex(dates)
    values = {c: z[c].fillna(0).to_numpy(dtype=float) for c in COUNT_COLUMNS}
    known = dates.isin(pd.DatetimeIndex(observed_dates))
    observed90 = past_sum(known, 90)
    output = {'date': dates}
    for col in COUNT_COLUMNS:
        for w in WINDOWS:
            output[f'{col[2:]}_log1p_{w}d'] = np.log1p(past_sum(values[col], w)).astype('float32')
    for name, col in [('active','n_events'),('fault','n_fault'),('unknown','n_unknown'),('smoke','n_smoke')]:
        for w in WINDOWS:
            output[f'{name}_days_{w}d'] = past_sum(values[col] > 0, w).astype('float32')
    for w in WINDOWS:
        denom = past_sum(known, w)
        output[f'fault_day_rate_{w}d'] = (output[f'fault_days_{w}d'] / np.maximum(denom, 1)).astype('float32')
    e7, e30 = past_sum(values['n_events'],7), past_sum(values['n_events'],30)
    output['events_growth_7_vs_previous23'] = (np.log1p(e7/7)-np.log1p((e30-e7)/23)).astype('float32')
    for name in ('unknown','alarm','smoke'):
        output[f'{name}_share_7d'] = (past_sum(values[f'n_{name}'],7)/np.maximum(e7,1)).astype('float32')
    for name in ('fault','unknown','smoke'):
        output[f'days_since_{name}_day'] = days_since_day(values[f'n_{name}'] > 0)
    last_ts = z.last_ts.ffill().shift(1)
    age = (pd.Series(dates,index=dates)-last_ts).dt.total_seconds().to_numpy()/3600
    output['hours_since_last_event'] = age.astype('float32')
    # first_ts is used only after at least one event has been seen, never to
    # infer a channel's pre-installation presence from later observations.
    first_ts = g.first_ts.min()
    history_days = np.maximum(0, (dates-first_ts).total_seconds().to_numpy()/86400)
    seen = last_ts.notna().to_numpy()
    output['history_days'] = np.where(seen,history_days,0).astype('float32')
    output['global_observed_days_90d'] = observed90.astype('float32')
    output['weekday'] = dates.dayofweek.to_numpy(dtype='float32')
    output['month_sin'] = np.sin(2*np.pi*dates.month/12).astype('float32')
    output['month_cos'] = np.cos(2*np.pi*dates.month/12).astype('float32')
    raw_state = z.last_value.ffill().shift(1).fillna('__unknown__').astype(str)
    last_numeric=pd.to_numeric(raw_state,errors='coerce').to_numpy(dtype=float)
    numeric_state=np.isfinite(last_numeric)
    state=np.where(numeric_state,'__numeric__',raw_state.to_numpy())
    output['last_numeric_value']=np.where(numeric_state,last_numeric,np.nan).astype('float32')
    for w in (7,30):
        numcount=past_sum(z.n_numeric.fillna(0).to_numpy(),w)
        numsum=past_sum(z.numeric_sum.fillna(0).to_numpy(),w)
        numsumsq=past_sum(z.numeric_sumsq.fillna(0).to_numpy(),w)
        mean=numsum/np.maximum(numcount,1)
        var=np.maximum(0,numsumsq/np.maximum(numcount,1)-mean**2)
        output[f'numeric_count_log1p_{w}d']=np.log1p(numcount).astype('float32')
        output[f'numeric_mean_{w}d']=np.where(numcount>0,mean,np.nan).astype('float32')
        output[f'numeric_std_{w}d']=np.where(numcount>1,np.sqrt(var),np.nan).astype('float32')
        output[f'numeric_min_{w}d']=z.numeric_min.shift(1).rolling(w,min_periods=1).min().to_numpy(dtype='float32')
        output[f'numeric_max_{w}d']=z.numeric_max.shift(1).rolling(w,min_periods=1).max().to_numpy(dtype='float32')
    sensor_type=str(g.sensor_type.iloc[0])
    output['sensor_type']=sensor_type
    output['last_state'] = state
    status = np.full(len(dates),'eligible',dtype=object)
    checks = [
        (~seen, 'no_history'),
        (state == 'Неисправен', 'already_fault'),
        (~np.isin(state, ACCEPTED_STATES[sensor_type]), 'unknown_or_unsupported_state'),
        (age > policy['max_last_event_age_days']*24, 'stale_state'),
        (history_days < policy['min_history_days'], 'short_history'),
        (past_sum(values['n_fault']>0,policy['cooldown_days'])>0, 'recent_fault_episode'),
        (past_sum(known,policy['cooldown_days']) < policy['cooldown_days'], 'missing_recent_global_data'),
    ]
    for bad, reason in checks:
        status[(status=='eligible') & bad] = reason
    output['status'] = status
    output['eligible'] = status == 'eligible'
    output['sensor_id'] = np.full(len(dates),int(g.sensor_id.iloc[0]),dtype='int64')
    if include_target:
        if label_end_exclusive is None:
            raise ValueError('Explicit label coverage boundary is required')
        fault = values['n_fault'] > 0
        next_fault = np.r_[fault[1:], False]
        next_known = np.r_[known[1:], False]
        valid = known & next_known & ((dates+pd.Timedelta(days=2)) <= pd.Timestamp(label_end_exclusive))
        y = (next_fault & ~fault).astype('float32')
        y[~valid] = np.nan
        output['target'] = y
        output['early_fault'] = fault.astype('int8')
        output['target_start'] = dates+pd.Timedelta(days=1)
        output['target_end'] = dates+pd.Timedelta(days=2)
    return pd.DataFrame(output)


def build_feature_cache(daily, observed_dates, out_dir, label_end_exclusive,
                        cache_identity, batch_channels=80):
    """Write small channel batches to year partitions; no huge dense RAM table."""
    out_dir=Path(out_dir)
    marker=out_dir/'metadata.json'
    if marker.exists():
        saved=json.loads(marker.read_text(encoding='utf-8'))
        if saved['cache_identity'] == cache_identity:
            if all((out_dir/f'features_{y}.parquet').is_file() for y in saved['years']):
                return saved
        raise ValueError('Cache differs from source/code; choose a fresh CACHE_DIR')
    out_dir.mkdir(parents=True,exist_ok=True)
    if list(out_dir.glob('features_*.parquet')):
        raise ValueError('Incomplete feature cache; choose a fresh CACHE_DIR')
    started=time.perf_counter()
    dates=pd.date_range(daily.date.min(),daily.date.max(),freq='D')
    writers, buffer, audits, counts = {}, [], [], []
    def flush():
        if not buffer:
            return
        b=pd.concat(buffer,ignore_index=True)
        for year,part in b.groupby(b.date.dt.year):
            tab=pa.Table.from_pandas(part,preserve_index=False)
            if int(year) not in writers:
                writers[int(year)]=pq.ParquetWriter(out_dir/f'features_{year}.parquet',tab.schema,compression='zstd')
            writers[int(year)].write_table(tab)
        buffer.clear()
    try:
        for i,(sid,g) in enumerate(daily.groupby('sensor_id',sort=True),start=1):
            frame=channel_frame(g,dates,observed_dates,label_end_exclusive=label_end_exclusive)
            present=frame.status.ne('no_history')
            audit=frame.loc[present].groupby([frame.loc[present,'date'].dt.year,'status']).size().rename('rows').reset_index()
            audits.append(audit)
            keep=frame.eligible & frame.target.notna()
            part=frame.loc[keep, ['date','sensor_id',*FEATURES,'target','early_fault','target_start','target_end']].copy()
            # A training label must be observable entirely before the next split.
            year_end=pd.to_datetime((part.date.dt.year+1).astype(str)+'-01-01')
            part=part.loc[part.target_end.to_numpy() <= year_end.to_numpy()].copy()
            part['target']=part.target.astype('int8')
            counts.append(part.groupby(part.date.dt.year).agg(rows=('target','size'),positives=('target','sum'),early_faults=('early_fault','sum')).reset_index())
            if len(part):
                buffer.append(part)
            if i % batch_channels == 0:
                flush()
            if i % 500 == 0:
                print(f'Признаки: {i} каналов; {time.perf_counter()-started:.1f} с',flush=True)
        flush()
    finally:
        for writer in writers.values():
            writer.close()
    totals=pd.concat(counts,ignore_index=True).groupby('date')[['rows','positives','early_faults']].sum().reset_index().rename(columns={'date':'year'})
    audit=pd.concat(audits,ignore_index=True).groupby(['date','status']).rows.sum().reset_index().rename(columns={'date':'year'})
    totals.to_csv(out_dir/'year_counts.csv',index=False)
    audit.to_csv(out_dir/'eligibility_audit.csv',index=False)
    metadata={'version':VERSION,'cache_identity':cache_identity,'features':FEATURES,
              'years':sorted(writers),'build_seconds':time.perf_counter()-started,
              'year_counts':totals.to_dict('records'),'policy':POLICY}
    dump_json(marker,metadata)
    return metadata


def load_years(cache_dir, years, max_negatives=None):
    """Sample ONLY training negatives, retaining inverse-probability weights."""
    cache_dir=Path(cache_dir)
    summary=pd.read_csv(cache_dir/'year_counts.csv').query('year in @years')
    negatives=int((summary.rows-summary.positives).sum())
    keep_probability=1. if max_negatives is None else min(1.,max_negatives/max(negatives,1))
    rng=np.random.default_rng(SEED)
    pieces=[]
    for year in years:
        for batch in pq.ParquetFile(cache_dir/f'features_{year}.parquet').iter_batches(batch_size=100_000):
            df=batch.to_pandas()
            keep=(df.target.to_numpy()==1)|(rng.random(len(df)) < keep_probability)
            df=df.loc[keep].copy()
            df['sample_weight']=np.where(df.target.eq(1),1.,1./keep_probability).astype('float32')
            pieces.append(df)
    result=pd.concat(pieces,ignore_index=True).sort_values(['date','sensor_id'],kind='stable').reset_index(drop=True)
    for c in CAT_FEATURES:
        result[c]=result[c].astype('category')
    return result, {'negative_keep_probability':keep_probability,'full_rows':int(summary.rows.sum()),
                    'sampled_rows':len(result),'positives':int(result.target.sum()),
                    'all_positive_rows_retained':True,'inverse_probability_weights':True}


def choose_threshold(y, scores, precision_floor=.70, min_alerts=20):
    """Choose on calibration ONLY. Fallback is explicitly reported if infeasible."""
    y=np.asarray(y,dtype=int)
    scores=np.asarray(scores,dtype=float)
    p,r,thresholds=precision_recall_curve(y,scores)
    p,r=p[:-1],r[:-1]
    n=len(scores)-np.searchsorted(np.sort(scores),thresholds,side='left')
    enough=n>=min_alerts
    feasible=enough & (p > precision_floor)
    if feasible.any():
        ids=np.flatnonzero(feasible)
        best=ids[np.argmax(r[ids])]
        rule='maximum_recall_given_precision_gt_0.70_and_at_least_20_alerts'
    else:
        f=.0+p*r*1.25/(.25*p+r+1e-12)
        candidates=np.flatnonzero(enough)
        if not len(candidates):
            raise ValueError('Too few calibration predictions')
        best=candidates[np.argmax(f[candidates])]
        rule='constraint_infeasible_fallback_max_F0.5_with_at_least_20_alerts'
    return {'threshold':float(thresholds[best]),'rule':rule,
            'calibration_precision':float(p[best]),'calibration_recall':float(r[best]),
            'calibration_alerts':int(n[best]),'feasible_precision_constraint':bool(feasible.any())}


def metrics(y, scores, threshold):
    y=np.asarray(y,dtype=int)
    scores=np.asarray(scores,dtype=float)
    pred=scores>=threshold
    tn,fp,fn,tp=confusion_matrix(y,pred,labels=[0,1]).ravel()
    precision=float(tp/(tp+fp)) if tp+fp else 0.
    recall=float(tp/(tp+fn)) if tp+fn else 0.
    two_classes=len(np.unique(y))==2
    return {'rows':len(y),'positives':int(y.sum()),'prevalence':float(y.mean()) if len(y) else 0.,
            'alerts':int(pred.sum()),'tp':int(tp),'fp':int(fp),'fn':int(fn),'tn':int(tn),
            'precision':precision,'recall':recall,'f1':float(2*precision*recall/(precision+recall)) if precision+recall else 0.,
            'average_precision':float(average_precision_score(y,scores)) if y.sum() else None,
            'roc_auc':float(roc_auc_score(y,scores)) if two_classes else None,
            'false_alerts_per_1000_channel_days':float(1000*fp/len(y)) if len(y) else 0.,
            'meets_precision_recall':precision>.7 and recall>.5}


def cluster_intervals(df, threshold, iterations=400):
    """Channel bootstrap: daily rows of a channel remain together."""
    y=df.target.to_numpy()==1
    p=df.score.to_numpy()>=threshold
    temp=pd.DataFrame({'sensor_id':df.sensor_id,'tp':y&p,'fp':~y&p,'fn':y&~p})
    a=temp.groupby('sensor_id')[['tp','fp','fn']].sum().to_numpy()
    rng=np.random.default_rng(SEED)
    draws=[]
    for _ in range(iterations):
        tp,fp,fn=a[rng.integers(0,len(a),len(a))].sum(axis=0)
        draws.append((tp/(tp+fp) if tp+fp else np.nan, tp/(tp+fn) if tp+fn else np.nan))
    draws=np.asarray(draws)
    return {'method':'percentile bootstrap by channel; spatial/shared outage dependence not modeled',
            'iterations':iterations,'precision_95':np.nanquantile(draws[:,0],[.025,.975]).tolist(),
            'recall_95':np.nanquantile(draws[:,1],[.025,.975]).tolist()}


def score_snapshot(model, daily, channels, observed_dates, as_of, threshold):
    """Score one midnight using factual daily aggregates strictly before it.

    This function returns explicit no-history/stale/unsupported statuses.
    Model predictions are never appended to the observed history.
    """
    started=time.perf_counter()
    as_of=pd.Timestamp(as_of)
    if as_of != as_of.normalize():
        raise ValueError('This baseline supports midnight snapshots only')
    history=daily.loc[daily.date < as_of].copy()
    if history.empty:
        raise ValueError('No past observations')
    dates=pd.date_range(history.date.min(),as_of,freq='D')
    rows=[]
    for sid,g in history.groupby('sensor_id',sort=True):
        frame=channel_frame(g,dates,observed_dates,include_target=False)
        rows.append(frame.iloc[[-1]])
    candidates=pd.concat(rows,ignore_index=True)
    all_ids=channels.loc[channels['тип_датчика'].isin(POLICY['sensor_types']), ['ид_канала_данных','ид_объект','название_датчика']].rename(columns={'ид_канала_данных':'sensor_id','ид_объект':'object_id','название_датчика':'sensor_name'})
    result=all_ids.merge(candidates,on='sensor_id',how='left',validate='one_to_one')
    result['date']=as_of
    result['status']=result.status.fillna('no_history')
    mask=result.status.eq('eligible')
    result['risk_score']=np.nan
    if mask.any():
        result.loc[mask,'risk_score']=model.predict_proba(result.loc[mask,FEATURES])[:,1]
    result['alert']=pd.array([pd.NA]*len(result),dtype='Int8')
    result.loc[mask,'alert']=(result.loc[mask,'risk_score']>=threshold).astype('int8')
    result['target_start']=as_of+pd.Timedelta(days=1)
    result['target_end']=as_of+pd.Timedelta(days=2)
    result['threshold']=threshold
    result['hours_since_last_event']=result.hours_since_last_event.astype(float)
    cols=['sensor_id','object_id','sensor_name','sensor_type','date','target_start','target_end','status',
          'hours_since_last_event','risk_score','threshold','alert']
    return result[cols], {'feature_and_score_seconds':time.perf_counter()-started,
                          'rows':len(result),'eligible':int(mask.sum()),'as_of':str(as_of)}


def run_causality_tests():
    """Boundary and future-perturbation tests, independent of model quality."""
    dates=pd.date_range('2024-01-01','2024-02-10')
    rows=[]
    for d in dates:
        rows.append({'sensor_id':1,'sensor_type':'Датчик дыма','date':d,**{c:0 for c in COUNT_COLUMNS},
                     'n_numeric':0,'numeric_sum':0.,'numeric_sumsq':0.,'numeric_min':np.nan,'numeric_max':np.nan,
                     'n_events':1,'first_ts':d+pd.Timedelta(hours=1),
                     'last_ts':d+pd.Timedelta(hours=1),'last_value':'Норма'})
    g=pd.DataFrame(rows)
    T=pd.Timestamp('2024-01-20')
    def get(x):
        f=channel_frame(x,dates,dates,label_end_exclusive='2024-02-10')
        return f.loc[f.date.eq(T)].iloc[0]
    clean=get(g)
    mutated=g.copy()
    mutated.loc[mutated.date >= T,'n_events']=999999
    mutated.loc[mutated.date >= T,'last_value']='Неисправен'
    pd.testing.assert_series_equal(clean[FEATURES],get(mutated)[FEATURES])
    boundary=g.copy()
    boundary.loc[boundary.date.eq(T+pd.Timedelta(days=1)),'n_fault']=1
    assert get(boundary).target == 1
    boundary.loc[boundary.date.eq(T),'n_fault']=1
    assert get(boundary).target == 0 and get(boundary).early_fault == 1
    assert get(boundary).eligible  # future early fault must not remove a row
    boundary=g.copy()
    boundary.loc[boundary.date.eq(T+pd.Timedelta(days=2)),'n_fault']=1
    assert get(boundary).target == 0  # exclusive 48h upper boundary
    fault_now=g.copy()
    fault_now.loc[fault_now.date.eq(T-pd.Timedelta(days=1)),'last_value']='Неисправен'
    assert get(fault_now).status == 'already_fault'
    missing=[d for d in dates if d != T+pd.Timedelta(days=1)]
    f=channel_frame(g,dates,missing,label_end_exclusive='2024-02-10')
    assert f.loc[f.date.eq(T),'target'].isna().all()
    # Exactly 7 prior calendar days are used for the cooldown.
    cooldown=g.copy()
    cooldown.loc[cooldown.date.eq(T-pd.Timedelta(days=7)),'n_fault']=1
    assert get(cooldown).status == 'recent_fault_episode'
    cooldown.loc[cooldown.date.eq(T-pd.Timedelta(days=7)),'n_fault']=0
    cooldown.loc[cooldown.date.eq(T-pd.Timedelta(days=8)),'n_fault']=1
    assert get(cooldown).eligible
    return {'future_feature_invariance':True,'24h_lower_and_48h_upper_bounds':True,
            'early_fault_not_filtered_by_future':True,'already_fault_excluded':True,
            'missing_future_not_negative':True,'cooldown_boundary':True}
