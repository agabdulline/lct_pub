"""Daily forecast with at least 24h warning; all input history must be factual."""
from pathlib import Path
import argparse,json,time
import pandas as pd
import numpy as np
from catboost import CatBoostClassifier
from src.inference import snapshot_features
from src.daily_context import DailyContext


def forecast(source,model_dir,as_of):
    tick=time.perf_counter();data=source/'data';T=pd.Timestamp(as_of)
    if T!=T.normalize():raise ValueError('Daily snapshot must be at midnight')
    daily=pd.read_parquet(data/'sensor_daily.parquet');ep=pd.read_parquet(data/'fault_episodes.parquet')
    channels=pd.read_csv(data/'channels.csv');coverage=pd.read_csv(data/'global_coverage.csv',parse_dates=['date']).date
    alarms=pd.read_parquet(data/'alarm_features_v3.parquet')
    f=snapshot_features(daily,ep,channels,coverage,T)
    f=DailyContext(daily,channels,alarms,ep).transform(f)
    cfg=json.loads((model_dir/'model_config.json').read_text(encoding='utf-8'))
    model=CatBoostClassifier();model.load_model(str(model_dir/cfg['model_file']))
    f['score']=np.nan;f['alert']=pd.array([pd.NA]*len(f),dtype='boolean')
    eligible=f.eligible
    if eligible.any():
        f.loc[eligible,'score']=model.predict_proba(f.loc[eligible,cfg['features']])[:,1]
        f.loc[eligible,'alert']=f.loc[eligible,'score'].ge(cfg['threshold'])
    f['target_start']=T+pd.Timedelta(days=1);f['target_end']=T+pd.Timedelta(days=cfg['horizon_days'])
    cols=['sensor_id','date','sensor_type','status','target_start','target_end','score','alert']
    return f[cols],dict(seconds=time.perf_counter()-tick,channels=len(f),eligible=int(eligible.sum()),minimum_lead_hours=24)


def main():
    p=argparse.ArgumentParser(description=__doc__);here=Path(__file__).parent
    p.add_argument('--source',type=Path,default=here);p.add_argument('--model-dir',type=Path,default=here/'artifacts'/'models')
    p.add_argument('--as-of',required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    f,r=forecast(a.source,a.model_dir,a.as_of);a.output.parent.mkdir(parents=True,exist_ok=True);f.to_csv(a.output,index=False)
    print(json.dumps(r),flush=True)


if __name__=='__main__':main()
