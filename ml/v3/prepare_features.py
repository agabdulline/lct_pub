"""Recompute v3 event features from full, factual smoke sequences."""
from pathlib import Path
import argparse,time,json
import pandas as pd
from src.event_model import build_alarm_rows
from src.neighbors import build_extended

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,default=Path(__file__).parent)
    p.add_argument('--output',type=Path,default=Path(__file__).parent)
    p.add_argument('--label-end',default='2026-06-30');a=p.parse_args();d=a.source/'data';tick=time.perf_counter()
    events=pd.read_parquet(d/'smoke_events_2022_2026.parquet')
    coverage=pd.read_csv(d/'global_coverage.csv',parse_dates=['date']).date
    rows=build_alarm_rows(events,coverage,a.label_end,24)
    f=build_extended(rows,events,pd.read_csv(d/'channels.csv'))
    out=a.output/'data';out.mkdir(parents=True,exist_ok=True)
    f.to_parquet(out/'alarm_features_v3.parquet',index=False,compression='zstd')
    print(json.dumps({'rows':len(f),'columns':len(f.columns),'seconds':time.perf_counter()-tick}),flush=True)

if __name__=='__main__':main()
