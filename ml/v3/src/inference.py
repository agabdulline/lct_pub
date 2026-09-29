"""Vectorized v2 inference; observed states only, predictions never feed history."""
import time
import numpy as np
import pandas as pd
from .base_snapshot import snapshot_features as base_snapshot
from .features import BIN_EDGES,FEATURES,CAT_FEATURES


def snapshot_features(daily,episodes,channels,observed_dates,as_of):
    T=pd.Timestamp(as_of)
    b=base_snapshot(daily,channels,observed_dates,T).set_index('sensor_id')
    h=daily.loc[daily.date<T]
    idx=b.index
    ext={'channel_key':idx.astype(str)}
    def count(x):return x.groupby('sensor_id',sort=False).size().reindex(idx,fill_value=0)
    sums={0:pd.DataFrame(0.,index=idx,columns=['n_'+c for c in ('events','alarm','fault','unknown','smoke','trigger','open')])}
    for w in BIN_EDGES[1:]:
        q=h.loc[h.date>=T-pd.Timedelta(days=w)]
        sums[w]=q.groupby('sensor_id',sort=False)[sums[0].columns].sum().reindex(idx,fill_value=0)
    for c in ('events','alarm','fault','unknown','smoke','trigger','open'):
        for lo,hi in zip(BIN_EDGES[:-1],BIN_EDGES[1:]):
            ext[f'{c}_bin_{lo}_{hi}d']=np.log1p(sums[hi]['n_'+c]-sums[lo]['n_'+c]).astype('float32')
    f=h.loc[h.n_fault>0,['sensor_id','date']].copy()
    ages=(T-f.date).dt.days.to_numpy()
    for span in (7,30,180):
        alpha=2/(span+1)
        f['ewm']=alpha*(1-alpha)**(ages-1)
        ext[f'fault_ewm_{span}d']=f.groupby('sensor_id')['ewm'].sum().reindex(idx,fill_value=0).astype('float32')
    ext['fault_days_lifetime_rate']=(count(f)/np.maximum(b.history_days,1)).astype('float32')
    for c in ('events','alarm'):
        ext[f'{c}_vs_own_365d']=(np.log1p(sums[7]['n_'+c]/7)-np.log1p(sums[365]['n_'+c]/np.minimum(np.maximum(b.history_days,1),365))).astype('float32')
    ep=episodes.loc[(episodes.start<T)&episodes.start_is_observed].sort_values(['sensor_id','start'])
    ext['onsets_lifetime_log1p']=np.log1p(count(ep)).astype('float32')
    last=ep.groupby('sensor_id',sort=False).start.max().reindex(idx)
    ext['days_since_last_onset']=((T-last).dt.total_seconds()/86400).astype('float32')
    for w in (1,7,30,90,365):
        ext[f'onsets_{w}d']=count(ep.loc[ep.start>=T-pd.Timedelta(days=w)]).astype('float32')
    recent=ep.groupby('sensor_id',sort=False).tail(9).copy()
    recent['gap']=recent.groupby('sensor_id').start.diff().dt.total_seconds()/86400
    gaps=recent.groupby('sensor_id').gap.agg(['median','std','mean','count']).reindex(idx)
    median=gaps['median'].where(gaps['count']>=2)
    ext['onset_gap_median_days']=median.astype('float32')
    ext['onset_gap_cv']=(gaps['std']/gaps['mean'].clip(lower=.001)).where(gaps['count']>=2).astype('float32')
    ext['age_over_median_onset_gap']=(ext['days_since_last_onset']/median.clip(lower=.01)).astype('float32')
    completed=ep.loc[ep.end.notna()&(ep.end<T)&~ep.coverage_uncertain].sort_values(['sensor_id','end'])
    last=completed.groupby('sensor_id',sort=False).tail(1).set_index('sensor_id').reindex(idx)
    ext['days_since_last_fault_end']=((T-last.end).dt.total_seconds()/86400).astype('float32')
    ext['last_completed_fault_log_seconds']=np.log1p(last.duration_seconds).astype('float32')
    for w in (7,30,90,365):
        q=completed.loc[completed.end>=T-pd.Timedelta(days=w)]
        ext[f'completed_fault_count_{w}d']=count(q).astype('float32')
        ext[f'completed_fault_hours_{w}d']=np.log1p(q.groupby('sensor_id').duration_seconds.sum().reindex(idx,fill_value=0)/3600).astype('float32')
    return pd.concat([b,pd.DataFrame(ext,index=idx)],axis=1).reset_index()


def predict_snapshot(model,daily,episodes,channels,observed_dates,as_of,threshold,horizon):
    tick=time.perf_counter()
    f=snapshot_features(daily,episodes,channels,observed_dates,as_of)
    for c in CAT_FEATURES:f[c]=f[c].astype(str)
    f['risk_score']=np.nan;f['alert']=pd.array([pd.NA]*len(f),dtype='Int8')
    mask=f.eligible
    if mask.any():
        f.loc[mask,'risk_score']=model.predict_proba(f.loc[mask,model.feature_names_])[:,1]
        f.loc[mask,'alert']=(f.loc[mask,'risk_score']>=threshold).astype('int8')
    f['target_start']=pd.Timestamp(as_of)+pd.Timedelta(days=0 if horizon==1 else 1)
    f['target_end']=pd.Timestamp(as_of)+pd.Timedelta(days=horizon)
    cols=['sensor_id','object_id','sensor_name','sensor_type','date','target_start','target_end','status','risk_score','alert']
    return f[cols],{'seconds':time.perf_counter()-tick,'channels':len(f),'eligible':int(mask.sum()),'includes_feature_computation':True}
