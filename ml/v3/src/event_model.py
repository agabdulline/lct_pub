"""Optional smoke-alarm triage: next logged fault within 24h after an alarm.

This is a different decision population from scheduled daily forecasting.
The 24h outcome horizon does NOT imply at least 24h advance warning.
"""
import numpy as np
import pandas as pd

WINDOW_SECONDS=(60,300,900,3600,21600,86400,604800,2592000,31536000)
STATES={'smoke':'Обнаружен дым','fault':'Неисправен','unknown':'Неопределен',
        'normal':'Норма','no_smoke':'Дыма нет','disabled':'Отключен'}
EVENT_CATS=['channel_key','current_state','previous_state_1','previous_state_2','previous_state_3']
EVENT_FEATURES=(EVENT_CATS+['hour_sin','hour_cos','weekday','month_sin','month_cos','history_days',
                'event_gap_seconds','recent_gap_median_seconds','recent_gap_cv','hours_since_previous_smoke',
                'hours_since_previous_fault','smoke_rate_ratio_1h_24h','event_rate_ratio_1h_24h']
                +[f'{kind}_{w}s' for kind in ('events',*STATES) for w in WINDOW_SECONDS])


def build_alarm_rows(events,observed_dates,label_end_exclusive=None,fault_washout_hours=24):
    """One alarm decision per channel per 24h, using past-only eligibility.

    Decision time = alarm timestamp + 1 second: all events at that recorded
    second have arrived. If any says fault at that same second, no prediction.
    By default no fault message in the previous 24h: reduces repeated alerts
    about an existing unstable episode. Forecast only after >=7d history.
    Return all eligible decisions, including quiet-history / negative outcomes.
    """
    events=events.drop_duplicates(['sensor_id','event_id','ts','value']).sort_values(['sensor_id','ts','event_id'],kind='stable')
    observed=pd.DatetimeIndex(observed_dates)
    coverage=set(observed.to_numpy('datetime64[D]').astype('int64'))
    result=[]
    for sid,g in events.groupby('sensor_id',sort=True):
        ts=g.ts.to_numpy('datetime64[s]').astype('int64');values=g.value.to_numpy()
        if not len(ts):continue
        smoke=values=='Обнаружен дым';fault=values=='Неисправен'
        candidates=np.unique(ts[smoke])
        if not len(candidates):continue
        n=len(candidates);T=candidates+1
        right=np.searchsorted(ts,T,side='left')
        prior=np.maximum(right-1,0)
        current=values[prior]
        fault_ts=ts[fault];smoke_ts=ts[smoke]
        # Same-second conflicting state cannot be assumed repaired.
        same_fault=np.isin(candidates,fault_ts)
        last_fault_idx=np.searchsorted(fault_ts,T,side='left')
        lf=np.full(n,-1,dtype='int64')
        if len(fault_ts):lf=np.where(last_fault_idx>0,fault_ts[np.maximum(last_fault_idx-1,0)],-1)
        candidate_days=T//86400
        past_ok=np.fromiter((all(day-d in coverage for d in range(8)) for day in candidate_days),bool,count=n)
        eligible=(~same_fault)&np.isin(current,('Норма','Дыма нет','Обнаружен дым'))&(T-ts[0]>=7*86400)&past_ok
        if fault_washout_hours:eligible&=(lf<0)|(T-lf>fault_washout_hours*3600)
        keep=[];last_decision=-10**18
        for j in np.flatnonzero(eligible):
            if T[j]-last_decision>=86400:
                keep.append(j);last_decision=T[j]
        if not keep:continue
        keep=np.asarray(keep);T=T[keep];right=right[keep];prior=prior[keep];lf=lf[keep]
        dates=pd.to_datetime(T,unit='s');n=len(T)
        out={'sensor_id':int(sid),'decision_time':dates,'channel_key':str(int(sid)),
             'current_state':values[prior],'history_days':(T-ts[0])/86400,
             'weekday':dates.dayofweek.to_numpy(),'hour_sin':np.sin(2*np.pi*(dates.hour+dates.minute/60)/24),
             'hour_cos':np.cos(2*np.pi*(dates.hour+dates.minute/60)/24),
             'month_sin':np.sin(2*np.pi*dates.month/12),'month_cos':np.cos(2*np.pi*dates.month/12)}
        for lag in (1,2,3):out[f'previous_state_{lag}']=np.where(right>lag,values[np.maximum(prior-lag,0)],'__none__')
        gaps=pd.Series(np.r_[np.nan,np.diff(ts)])
        out['event_gap_seconds']=gaps.to_numpy()[prior]
        out['recent_gap_median_seconds']=gaps.rolling(10,min_periods=2).median().to_numpy()[prior]
        out['recent_gap_cv']=(gaps.rolling(10,min_periods=2).std()/gaps.rolling(10,min_periods=2).mean().clip(lower=1)).to_numpy()[prior]
        sp=np.searchsorted(smoke_ts,T-1,side='left')
        out['hours_since_previous_smoke']=np.where(sp>0,(T-smoke_ts[np.maximum(sp-1,0)])/3600,np.nan)
        out['hours_since_previous_fault']=np.where(lf>=0,(T-lf)/3600,np.nan)
        raw_counts={}
        for kind,state in [('events',None),*STATES.items()]:
            seq=ts if state is None else ts[values==state]
            stop=np.searchsorted(seq,T,side='left')
            for w in WINDOW_SECONDS:
                count=stop-np.searchsorted(seq,T-w,side='left')
                out[f'{kind}_{w}s']=np.log1p(count)
                if w in (3600,86400):raw_counts[kind,w]=count
        for kind in ('smoke','events'):
            out[f'{kind[:-1] if kind=="events" else kind}_rate_ratio_1h_24h']=np.log1p(raw_counts[kind,3600])-np.log1p(raw_counts[kind,86400]/24)
        if label_end_exclusive is not None:
            # Same-state duplicate fault messages cannot create a new onset.
            prev=np.concatenate([np.array(['__none__'],dtype=object),values[:-1]])
            onsets=ts[fault&(prev!='Неисправен')]
            fi=np.searchsorted(onsets,T,side='left')
            next_fault=np.full(n,-1,dtype='int64')
            if len(onsets):next_fault=np.where(fi<len(onsets),onsets[np.minimum(fi,len(onsets)-1)],-1)
            y=((next_fault>=T)&(next_fault<T+86400)).astype('float32')
            future_ok=np.fromiter((day in coverage and day+1 in coverage for day in T//86400),bool,count=n)
            y[~future_ok|(dates+pd.Timedelta(days=1)>pd.Timestamp(label_end_exclusive))]=np.nan
            out['target']=y
            out['next_onset']=pd.to_datetime(np.where(next_fault>=0,next_fault,np.iinfo('int64').min),unit='s',errors='coerce')
            out['lead_hours']=np.where(y==1,(next_fault-T)/3600,np.nan)
        result.append(pd.DataFrame(out))
    if not result:return pd.DataFrame(columns=['sensor_id','decision_time',*EVENT_FEATURES,'target','next_onset','lead_hours'])
    f=pd.concat(result,ignore_index=True).sort_values(['decision_time','sensor_id']).reset_index(drop=True)
    for col in EVENT_FEATURES:
        if col not in EVENT_CATS:f[col]=f[col].astype('float32')
    return f
