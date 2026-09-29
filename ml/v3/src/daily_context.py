"""Past-only context for scheduled daily forecasts, including 24h advance warning."""
import numpy as np
import pandas as pd
from .neighbors import metadata, attach_metadata, META_CATS, META_NUM


class DailyContext:
    def __init__(self, daily, channels, alarms, episodes):
        self.registry=metadata(channels)
        d=daily[['sensor_id','date','n_alarm','n_events','n_fault','n_unknown']].copy()
        d['kind']=d.sensor_id.map(self.registry['тип_датчика']).fillna('__unknown__')
        d['fault_day']=d.n_fault.gt(0).astype('int64');d['unknown_day']=d.n_unknown.gt(0).astype('int64');d['active_day']=1
        self.cols=['n_alarm','n_events','fault_day','unknown_day','active_day']
        self.groups={}
        for scope,key in [('object','object_key'),('loop','loop_key')]:
            d['key']=d.sensor_id.map(self.registry[key]).fillna('__missing__')+'|'+d.kind
            a=d.groupby(['key','date'],sort=True)[self.cols].sum().reset_index()
            groups={}
            for k,g in a.groupby('key',sort=False):
                ts=g.date.to_numpy('datetime64[D]').astype('int64')
                cum=np.vstack([np.zeros(len(self.cols)),np.cumsum(g[self.cols].to_numpy(),axis=0)])
                groups[k]=(ts,cum)
            self.groups[scope]=groups
        self.alarms={int(s):g.sort_values('decision_time') for s,g in alarms.groupby('sensor_id',sort=False)}
        ep=episodes.loc[episodes.start_is_observed & episodes.end.notna() & ~episodes.coverage_uncertain].copy()
        self.episodes={int(s):g.sort_values('end') for s,g in ep.groupby('sensor_id',sort=False)}

    def transform(self,rows):
        f=attach_metadata(rows,self.registry).reset_index(drop=True)
        t=f.date.to_numpy('datetime64[D]').astype('int64')
        additions={}
        # Other channels of the SAME TYPE; remove the queried sensor from group totals.
        own={'n_alarm':'alarm_log1p','n_events':'events_log1p','fault_day':'fault_days','unknown_day':'unknown_days','active_day':'active_days'}
        for scope,key in [('object','object_key'),('loop','loop_key')]:
            for c in self.cols:
                for w in (1,7,30,90):additions[f'daily_{scope}_{c}_{w}d']=np.zeros(len(f),dtype='float32')
            keys=f[key]+'|'+f.sensor_type.astype(str)
            for k,idx in keys.groupby(keys,sort=False).groups.items():
                ix=np.asarray(idx);entry=self.groups[scope].get(k)
                if entry is None:continue
                ts,cum=entry;tt=t[ix];right=np.searchsorted(ts,tt)
                for w in (1,7,30,90):
                    val=cum[right]-cum[np.searchsorted(ts,tt-w)]
                    for j,c in enumerate(self.cols):
                        col=f'{own[c]}_{w}d';mine=f[col].to_numpy()[ix]
                        if c in ('n_alarm','n_events'):mine=np.expm1(mine)
                        # Existing counts use float32 logs; tolerate round-off after subtraction.
                        additions[f'daily_{scope}_{c}_{w}d'][ix]=np.log1p(np.maximum(val[:,j]-mine,0))
            for w in (7,30,90):
                faults=np.expm1(additions[f'daily_{scope}_fault_day_{w}d'])
                active=np.expm1(additions[f'daily_{scope}_active_day_{w}d'])
                rate=(faults+.5)/(active+5)
                additions[f'daily_{scope}_fault_rate_{w}d']=rate
                additions[f'daily_own_vs_{scope}_fault_{w}d']=f[f'fault_day_rate_{w}d'].to_numpy()-rate
        for w in (7,30,90,365):
            for name in ('alarm_decisions','isolated_object','isolated_near','matured_alarm_fault_rate'):
                additions[f'daily_{name}_{w}d']=np.zeros(len(f),dtype='float32')
        for w in (7,30,90,365):
            for name in ('short_fault_ends','long_fault_ends'):
                additions[f'daily_{name}_{w}d']=np.zeros(len(f),dtype='float32')
        for sid,q in f.groupby('sensor_id',sort=False):
            ix=q.index.to_numpy();tt=t[ix]*86400;g=self.alarms.get(int(sid))
            if g is not None:
                ts=g.decision_time.to_numpy('datetime64[s]').astype('int64');right=np.searchsorted(ts,tt)
                good=g.target.notna().to_numpy();mt=ts[good]+86400;yc=np.r_[0.,g.target.to_numpy()[good].cumsum()]
                mr=np.searchsorted(mt,tt,side='right')
                for w in (7,30,90,365):
                    left=np.searchsorted(ts,tt-w*86400)
                    additions[f'daily_alarm_decisions_{w}d'][ix]=np.log1p(right-left)
                    for scope in ('object','near'):
                        vals=g[f'{scope}_isolated_3600s'].fillna(0).to_numpy();cum=np.r_[0.,vals.cumsum()]
                        additions[f'daily_isolated_{scope}_{w}d'][ix]=np.log1p(cum[right]-cum[left])
                    ml=np.searchsorted(mt,tt-w*86400)
                    additions[f'daily_matured_alarm_fault_rate_{w}d'][ix]=(yc[mr]-yc[ml]+.5)/(mr-ml+5)
            ep=self.episodes.get(int(sid))
            if ep is not None:
                ts=ep.end.to_numpy('datetime64[s]').astype('int64');du=ep.duration_seconds.to_numpy()
                for name,mask in [('short_fault_ends',du<=60),('long_fault_ends',du>=3600)]:
                    ends=ts[mask];right=np.searchsorted(ends,tt)
                    for w in (7,30,90,365):
                        additions[f'daily_{name}_{w}d'][ix]=np.log1p(right-np.searchsorted(ends,tt-w*86400))
        return pd.concat([f,pd.DataFrame(additions)],axis=1)


def daily_features(base,frame,mode):
    if mode=='base':return list(base)
    if mode=='metadata':return list(base)+META_CATS+META_NUM
    return list(base)+META_CATS+META_NUM+[c for c in frame if c.startswith('daily_')]
