"""Own-channel, strictly past-only features and exact fault-onset labels."""
from pathlib import Path
import json, time
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from .baseline_core import channel_frame, past_sum, FEATURES as BASE_FEATURES, POLICY as BASE_POLICY

VERSION='own-channel-exact-onsets-v2'
HORIZONS=(2,7,14,30)
POLICY={**BASE_POLICY,'cooldown_days':0}
BIN_EDGES=(0,1,2,3,7,14,30,90,180,365)
CAT_FEATURES=['sensor_type','last_state','channel_key']
EXTRA=(['channel_key','days_since_last_onset','days_since_last_fault_end','last_completed_fault_log_seconds',
        'onset_gap_median_days','onset_gap_cv','age_over_median_onset_gap','onsets_lifetime_log1p',
        'fault_days_lifetime_rate','events_vs_own_365d','alarm_vs_own_365d','fault_ewm_7d','fault_ewm_30d','fault_ewm_180d']
       +[f'{c}_bin_{lo}_{hi}d' for c in ('events','alarm','fault','unknown','smoke','trigger','open') for lo,hi in zip(BIN_EDGES[:-1],BIN_EDGES[1:])]
       +[f'onsets_{w}d' for w in (1,7,30,90,365)]
       +[f'completed_fault_{name}_{w}d' for w in (7,30,90,365) for name in ('count','hours')])
FEATURES=BASE_FEATURES+EXTRA


def make_channel(g,episodes,dates,observed_dates,label_end_exclusive=None):
    """All feature windows exclude T. Future endpoints never enter features.

    y_h=1 when the FIRST next observed fault onset is in [T+1d,T+h days).
    Lead-day onsets remain negatives, not retrospectively excluded examples.
    Global missing days invalidate labels; silence of one channel does not.
    """
    dates=pd.DatetimeIndex(dates)
    b=channel_frame(g,dates,observed_dates,include_target=False,policy=POLICY)
    z=g.set_index('date').reindex(dates)
    n=len(dates)
    ext={'channel_key':str(int(g.sensor_id.iloc[0]))}
    for c in ('events','alarm','fault','unknown','smoke','trigger','open'):
        values=z[f'n_{c}'].fillna(0).to_numpy(float)
        for lo,hi in zip(BIN_EDGES[:-1],BIN_EDGES[1:]):
            ext[f'{c}_bin_{lo}_{hi}d']=np.log1p(past_sum(values,hi)-past_sum(values,lo)).astype('float32')
    fd=z.n_fault.fillna(0).gt(0).astype(float)
    for span in (7,30,180):
        ext[f'fault_ewm_{span}d']=fd.shift(1,fill_value=0).ewm(span=span,adjust=False).mean().to_numpy('float32')
    ext['fault_days_lifetime_rate']=(past_sum(fd,n)/np.maximum(b.history_days,1)).astype('float32')
    for c in ('events','alarm'):
        x=z[f'n_{c}'].fillna(0).to_numpy()
        ext[f'{c}_vs_own_365d']=(np.log1p(past_sum(x,7)/7)-np.log1p(past_sum(x,365)/np.minimum(np.maximum(b.history_days,1),365))).astype('float32')
    ep=episodes.loc[episodes.start_is_observed].sort_values('start')
    starts=ep.start.to_numpy('datetime64[ns]')
    now=dates.to_numpy('datetime64[ns]')
    idx=np.searchsorted(starts,now,side='left')
    ext['onsets_lifetime_log1p']=np.log1p(idx).astype('float32')
    for w in (1,7,30,90,365):
        ext[f'onsets_{w}d']=(idx-np.searchsorted(starts,now-np.timedelta64(w,'D'),side='left')).astype('float32')
    for key in ('days_since_last_onset','onset_gap_median_days','onset_gap_cv','age_over_median_onset_gap'):
        ext[key]=np.full(n,np.nan,dtype='float32')
    if len(starts):
        last=starts[np.maximum(idx-1,0)]
        age=(now-last)/np.timedelta64(1,'D')
        ext['days_since_last_onset']=np.where(idx>0,age,np.nan).astype('float32')
        gaps=pd.Series(np.r_[np.nan,np.diff(starts)/np.timedelta64(1,'D')])
        med=gaps.rolling(8,min_periods=2).median().to_numpy()
        cv=(gaps.rolling(8,min_periods=2).std()/gaps.rolling(8,min_periods=2).mean().clip(lower=.001)).to_numpy()
        ext['onset_gap_median_days']=np.where(idx>0,med[np.maximum(idx-1,0)],np.nan).astype('float32')
        ext['onset_gap_cv']=np.where(idx>0,cv[np.maximum(idx-1,0)],np.nan).astype('float32')
        ext['age_over_median_onset_gap']=(age/np.maximum(ext['onset_gap_median_days'],.01)).astype('float32')
    # A duration becomes known only when the fault state has ended.
    completed=ep.loc[ep.end.notna()&~ep.coverage_uncertain].sort_values('end')
    ends=completed.end.to_numpy('datetime64[ns]')
    dur=completed.duration_seconds.to_numpy(float)
    endidx=np.searchsorted(ends,now,side='left')
    ext['days_since_last_fault_end']=np.full(n,np.nan,dtype='float32')
    ext['last_completed_fault_log_seconds']=np.full(n,np.nan,dtype='float32')
    if len(ends):
        ext['days_since_last_fault_end']=np.where(endidx>0,(now-ends[np.maximum(endidx-1,0)])/np.timedelta64(1,'D'),np.nan).astype('float32')
        ext['last_completed_fault_log_seconds']=np.where(endidx>0,np.log1p(dur[np.maximum(endidx-1,0)]),np.nan).astype('float32')
    cum=np.r_[0,np.cumsum(dur)]
    for w in (7,30,90,365):
        left=np.searchsorted(ends,now-np.timedelta64(w,'D'),side='left')
        ext[f'completed_fault_count_{w}d']=(endidx-left).astype('float32')
        ext[f'completed_fault_hours_{w}d']=np.log1p((cum[endidx]-cum[left])/3600).astype('float32')
    b=pd.concat([b,pd.DataFrame(ext,index=b.index)],axis=1)
    known=dates.isin(pd.DatetimeIndex(observed_dates))
    missing_past=past_sum(~known,7)>0
    b.loc[b.eligible&missing_past,'status']='missing_recent_global_data'
    b['eligible']=b.status.eq('eligible')
    if label_end_exclusive is not None:
        next_time=np.full(n,np.datetime64('NaT'),dtype='datetime64[ns]')
        valid=idx<len(starts)
        next_time[valid]=starts[idx[valid]]
        b['next_onset']=next_time
        b['early_fault']=(next_time<now+np.timedelta64(1,'D')).astype('int8')
        kc=np.r_[0,np.cumsum(known)]
        pos=np.arange(n)
        for h in HORIZONS:
            covered=(kc[np.minimum(pos+h,n)]-kc[pos])==h
            within=dates+pd.Timedelta(days=h)<=pd.Timestamp(label_end_exclusive)
            y=((next_time>=now+np.timedelta64(1,'D'))&(next_time<now+np.timedelta64(h,'D'))).astype('float32')
            y[~(covered&within)]=np.nan
            b[f'y{h}']=y
    return b


def build_cache(daily,episodes,observed_dates,out_dir,label_end_exclusive='2026-06-30',start_year=2022):
    out=Path(out_dir);out.mkdir(parents=True,exist_ok=True)
    if (out/'metadata.json').exists():
        m=json.loads((out/'metadata.json').read_text())
        assert m['version']==VERSION and m['features']==FEATURES
        return m
    if list(out.glob('features_*.parquet')):raise ValueError('Incomplete cache; use a new directory')
    dates=pd.date_range(daily.date.min(),daily.date.max())
    egs={int(s):e for s,e in episodes.groupby('sensor_id')}
    empty=episodes.iloc[:0]
    writers={};frames=[];totals=[];tick=time.perf_counter()
    def flush():
        if not frames:return
        f=pd.concat(frames,ignore_index=True)
        for year,q in f.groupby(f.date.dt.year):
            t=pa.Table.from_pandas(q,preserve_index=False)
            if year not in writers:writers[year]=pq.ParquetWriter(out/f'features_{year}.parquet',t.schema,compression='zstd')
            writers[year].write_table(t)
        frames.clear()
    try:
        for i,(sid,g) in enumerate(daily.groupby('sensor_id',sort=True),1):
            f=make_channel(g,egs.get(int(sid),empty),dates,observed_dates,label_end_exclusive)
            f=f.loc[f.eligible&f.date.dt.year.ge(start_year),['date','sensor_id',*FEATURES,'next_onset','early_fault',*[f'y{h}' for h in HORIZONS]]]
            frames.append(f)
            if i%40==0:flush()
            if i%1000==0:print('Feature channels',i,'seconds',round(time.perf_counter()-tick),flush=True)
        flush()
    finally:
        for w in writers.values():w.close()
    m={'version':VERSION,'features':FEATURES,'categorical_features':CAT_FEATURES,'years':sorted(map(int,writers)),
       'seconds':time.perf_counter()-tick,'policy':POLICY,'label':'first next observed onset in [T+1d,T+h days)'}
    (out/'metadata.json').write_text(json.dumps(m,indent=2),encoding='utf-8')
    return m
