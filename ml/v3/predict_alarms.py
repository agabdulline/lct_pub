"""Replay factual history and score eligible smoke alarms with causal peer context."""
from pathlib import Path
import argparse,json,time
import pandas as pd
from catboost import CatBoostClassifier
from src.event_model import build_alarm_rows
from src.neighbors import build_extended


def forecast(events,coverage,channels,model_dir,start,end,mode='balanced'):
    end=pd.Timestamp(end);tick=time.perf_counter()
    events=events.loc[events.ts<end]
    # Labels on earlier decisions are reconstructed from FACTS and are usable
    # only after their entire outcome window has closed (enforced by add_history).
    rows=build_alarm_rows(events,coverage,label_end_exclusive=end,fault_washout_hours=24)
    f=build_extended(rows,events,channels) if len(rows) else rows
    f=f.loc[f.decision_time.ge(start)&f.decision_time.lt(end)].copy()
    cfg=json.loads((model_dir/'smoke_alarm_config.json').read_text(encoding='utf-8'))
    threshold=cfg['threshold'] if mode=='balanced' else cfg['recall_mode_threshold']
    model=CatBoostClassifier();model.load_model(str(model_dir/'smoke_alarm_fault.cbm'))
    f['score']=model.predict_proba(f[cfg['features']])[:,1] if len(f) else pd.Series(dtype=float)
    f['alert']=f.score.ge(threshold);f['target_end']=f.decision_time+pd.Timedelta(days=1)
    columns=['sensor_id','decision_time','target_end','score','alert']
    return f[columns],dict(seconds=time.perf_counter()-tick,decisions=len(f),threshold=threshold,
        minimum_lead_hours=0,status='scored' if len(f) else 'no_eligible_alarm_decisions',history_source='observed events, never predictions')


def main():
    p=argparse.ArgumentParser(description=__doc__);here=Path(__file__).parent
    p.add_argument('--source',type=Path,default=here);p.add_argument('--model-dir',type=Path,default=here/'artifacts'/'models')
    p.add_argument('--from-date',required=True);p.add_argument('--until',required=True)
    p.add_argument('--new-events',type=Path,help='Original six-column event CSV appended to factual history')
    p.add_argument('--mode',choices=['balanced','recall'],default='balanced');p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();events=pd.read_parquet(a.source/'data'/'smoke_events_2022_2026.parquet')
    channels=pd.read_csv(a.source/'data'/'channels.csv');coverage=pd.read_csv(a.source/'data'/'global_coverage.csv',parse_dates=['date']).date
    if a.new_events:
        raw=pd.read_csv(a.new_events,dtype=str)
        raw=raw.loc[raw['ид_события'].ne('ид_события')].copy()
        raw['ts']=pd.to_datetime(raw['дата']+' '+raw['время'],format='%Y-%m-%d %H:%M:%S')
        raw=raw.loc[raw.ts.lt(a.until)]
        coverage=pd.concat([coverage,raw.ts.dt.normalize()],ignore_index=True).drop_duplicates()
        smoke=set(channels.loc[channels['тип_датчика'].eq('Датчик дыма'),'ид_канала_данных'].astype(str))
        raw=raw.loc[raw['ид_канала_данных'].isin(smoke)].rename(columns={'ид_события':'event_id','ид_канала_данных':'sensor_id','значение_датчика':'value'})
        raw[['sensor_id','event_id']]=raw[['sensor_id','event_id']].astype('int64')
        events=pd.concat([events,raw[['event_id','sensor_id','ts','value']]],ignore_index=True)
    f,report=forecast(events,coverage,channels,a.model_dir,a.from_date,a.until,a.mode)
    a.output.parent.mkdir(parents=True,exist_ok=True);f.to_csv(a.output,index=False)
    print(json.dumps(report),flush=True)


if __name__=='__main__':main()
