"""Targeted leakage checks for the newly introduced feature paths."""
import time
import numpy as np
import pandas as pd
from .event_model import build_alarm_rows, EVENT_FEATURES
from .neighbors import build_extended, metadata, add_peer_features, add_history, META_CATS
from .daily_context import DailyContext
from .inference import snapshot_features


def check(source):
    tick=time.perf_counter();data=source/'data'
    events=pd.read_parquet(data/'smoke_events_2022_2026.parquet')
    channels=pd.read_csv(data/'channels.csv');coverage=pd.read_csv(data/'global_coverage.csv',parse_dates=['date']).date
    full=pd.read_parquet(data/'alarm_features_v3.parquet')
    feats=[c for c in full if c not in ('sensor_id','decision_time','target','next_onset','lead_hours')]
    reg=metadata(channels)
    # Real-stream replay: rebuild all past eligibility and outcomes from a truncated event stream.
    checks=[]
    for day in ('2025-03-13','2026-04-02'):
        q=full.loc[full.decision_time.ge(day)].iloc[0]
        obj=reg.loc[q.sensor_id,'object_key'];ids=set(reg.index[reg.object_key.eq(obj)])
        # Object histories and peer counts use every smoke channel in this object.
        ev=events.loc[events.sensor_id.isin(ids)&events.ts.lt(q.decision_time)]
        hist=build_alarm_rows(ev,coverage,q.decision_time,24)
        replay=build_extended(hist,ev,channels)
        actual=replay.loc[replay.sensor_id.eq(q.sensor_id)&replay.decision_time.eq(q.decision_time)]
        assert len(actual)==1,(day,'missing replay decision')
        pd.testing.assert_series_equal(q[feats],actual.iloc[0][feats],check_names=False,check_dtype=False,rtol=1e-5,atol=1e-5)
        checks.append({'at':str(q.decision_time),'channel':int(q.sensor_id),'object_channels':len(ids),'features':len(feats)})
    # Mutating all not-yet-available labels cannot affect the latest past decision.
    T=pd.Timestamp('2025-09-01');modified=full.copy()
    unknown=(modified.decision_time+pd.Timedelta(days=1)>T)
    modified.loc[unknown,'target']=1-modified.loc[unknown,'target'].fillna(0)
    keep=[c for c in modified if not c.startswith(('history_self_', 'history_object_', 'history_loop_', 'past_'))]
    changed=add_history(modified[keep]);ix=full.decision_time.le(T)
    hc=[c for c in full if c.startswith(('history_self_', 'history_object_', 'history_loop_'))]
    pd.testing.assert_frame_equal(full.loc[ix,hc],changed.loc[ix,hc],check_dtype=False)
    # Synthetic exclusion of self and simultaneous/future events at the right boundary.
    ch=channels.iloc[:3].copy();ch['ид_канала_данных']=[1,2,3];ch['ид_объект']=1
    ch['тип_датчика']='Датчик дыма';ch['название_датчика']=['ДД ПК1','ДД ПК2','ДД ПК100']
    ch['тег_инженерной_системы']=['a.1.1','a.1.2','a.2.1']
    ev=pd.DataFrame({'sensor_id':[1,2,3,1,2],'event_id':range(5),'ts':pd.to_datetime(['2025-01-01','2025-01-01','2025-01-01 23:59:50','2025-01-01 23:59:59','2025-01-02'],format='mixed'),
                     'value':['Норма','Норма','Обнаружен дым','Обнаружен дым','Обнаружен дым']})
    test=add_peer_features(pd.DataFrame({'sensor_id':[1],'decision_time':[pd.Timestamp('2025-01-02')]}),ev,metadata(ch))
    assert test.object_smoke_3600s_distinct.iloc[0]==1
    assert test.near_smoke_3600s_distinct.iloc[0]==0
    assert test.near_isolated_3600s.iloc[0]==1
    # Daily context also excludes future daily aggregates and unfinished episode endpoints.
    daily=pd.read_parquet(data/'sensor_daily.parquet');ep=pd.read_parquet(data/'fault_episodes.parquet');T=pd.Timestamp('2025-09-01')
    snap=snapshot_features(daily,ep,channels,coverage,T)
    snap=snap.loc[snap.eligible].sample(20,random_state=42).reset_index(drop=True)
    a=DailyContext(daily,channels,full,ep).transform(snap)
    truncated=ep.loc[ep.start<T].copy();truncated.loc[truncated.end>=T,['end','duration_seconds']]=[pd.NaT,np.nan]
    h=full.loc[full.decision_time<T].copy();h.loc[h.decision_time+pd.Timedelta(days=1)>T,'target']=np.nan
    b=DailyContext(daily.loc[daily.date<T],channels,h,truncated).transform(snap)
    dc=[c for c in a if c.startswith('daily_')]
    pd.testing.assert_frame_equal(a[dc],b[dc],check_dtype=False,rtol=1e-5,atol=1e-5)
    return {'real_stream_replays':checks,'future_label_mutation':True,'synthetic_peer_boundaries':True,
            'daily_future_truncation_channels':len(snap),'daily_new_features':len(dc),'seconds':time.perf_counter()-tick}
