"""Ablations on 2025 H1/Q3, threshold on Q4, retrospective test on 2026."""
from pathlib import Path
import argparse, json, sys, time
import numpy as np
import pandas as pd
from catboost import CatBoostClassifier, Pool
from sklearn.metrics import precision_recall_curve, average_precision_score
from src.event_model import EVENT_FEATURES, EVENT_CATS
from src.neighbors import META_CATS, META_NUM
from src.training import threshold_balanced
from src.baseline_core import metrics, dump_json


def period(f, start, end):
    return f.loc[f.decision_time.ge(start) & (f.decision_time+pd.Timedelta(days=1)).le(end) & f.target.notna()].copy()


def recall_threshold(y, scores):
    p, r, t = precision_recall_curve(y, scores)
    ix = np.flatnonzero(r[:-1] >= .5)
    return float(t[ix[np.argmax(p[:-1][ix])]])


def score_metrics(f, scores, threshold):
    r = metrics(f.target, scores, threshold)
    r['false_alerts_per_1000_alarm_decisions'] = r.pop('false_alerts_per_1000_channel_days')
    return r


def specs(f):
    own = [c for c in f if c.startswith('own_') or c.startswith('history_self_')]
    peers = META_CATS + META_NUM + [c for c in f if c.startswith(('object_', 'loop_', 'near_', 'past_')) and c not in META_CATS]
    peers = list(dict.fromkeys(peers))
    history = [c for c in f if c.startswith(('history_object_', 'history_loop_'))]
    groups = {'base': EVENT_FEATURES, 'own': EVENT_FEATURES+own,
              'peers': EVENT_FEATURES+peers, 'all': EVENT_FEATURES+own+peers+history}
    result=[]
    for name, depth, l2 in [('base',5,10),('own',5,10),('peers',5,10),('all',5,10),('all',4,20),('all',7,20)]:
        feats = groups[name]
        result.append(dict(name=f'{name}_d{depth}', features=feats, depth=depth, l2=l2,
                           cats=[c for c in EVENT_CATS+META_CATS if c in feats]))
    return result


def parameters(spec, device, iterations):
    p=dict(iterations=iterations,depth=spec['depth'],learning_rate=.04,l2_leaf_reg=spec['l2'],
           loss_function='Logloss',eval_metric='PRAUC:type=Classic',random_seed=42,
           has_time=True,one_hot_max_size=32,allow_writing_files=False,thread_count=4,
           verbose=False,task_type=device)
    if device=='GPU':p.update(devices='0:1',data_partition='DocParallel',gpu_ram_part=.85,
                              boosting_type='Plain',max_ctr_complexity=1)
    return p


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,default=Path(__file__).parent)
    p.add_argument('--output',type=Path,default=Path(__file__).parent)
    p.add_argument('--device',choices=['CPU','GPU'],default='GPU')
    p.add_argument('--stage',choices=['develop','evaluate','all'],default='all')
    a=p.parse_args();root=a.output;reports=root/'artifacts'/'reports';models=root/'artifacts'/'models'
    reports.mkdir(parents=True,exist_ok=True);models.mkdir(parents=True,exist_ok=True)
    if a.device=='GPU':
        from catboost.utils import get_gpu_device_count
        assert get_gpu_device_count()>=2,'Select Kaggle GPU T4 x2'
    f=pd.read_parquet(a.source/'data'/'alarm_features_v3.parquet')
    if a.stage in ('develop','all'):
        tr=period(f,'2023-01-01','2025-01-01');va=period(f,'2025-01-01','2025-07-01')
        refit=period(f,'2023-01-01','2025-07-01');q3=period(f,'2025-07-01','2025-10-01')
        records=[]
        for spec in specs(f):
            tick=time.perf_counter();feats=spec['features'];cats=spec['cats']
            m=CatBoostClassifier(**parameters(spec,a.device,650))
            m.fit(Pool(tr[feats],tr.target,cat_features=cats),eval_set=Pool(va[feats],va.target,cat_features=cats),early_stopping_rounds=70)
            vs=m.predict_proba(va[feats])[:,1];h1=threshold_balanced(va.target,vs);trees=m.tree_count_
            model=CatBoostClassifier(**parameters(spec,a.device,trees))
            model.fit(Pool(refit[feats],refit.target,cat_features=cats))
            qs=model.predict_proba(q3[feats])[:,1];third=threshold_balanced(q3.target,qs)
            model.save_model(str(models/f'alarm_{spec["name"]}.cbm'))
            r=dict(spec=spec,trees=trees,h1=h1,q3=third,
                   h1_ap=float(average_precision_score(va.target,vs)),q3_ap=float(average_precision_score(q3.target,qs)),
                   robust_criterion=min(h1['criterion_fraction'],third['criterion_fraction']),seconds=time.perf_counter()-tick)
            records.append(r);dump_json(reports/'alarm_ablation_2025.json',records)
            print(json.dumps({k:v for k,v in r.items() if k!='spec'}|{'name':spec['name']},ensure_ascii=False),flush=True)
        selected=max(records,key=lambda r:(r['robust_criterion'],r['q3_ap']))
        dump_json(reports/'alarm_selected_2025.json',dict(selected=selected,device=a.device,
            rule='max min(H1 criterion fraction, Q3 criterion fraction); Q4 only thresholds; test not used'))
        print('SELECTED',selected['spec']['name'],flush=True)
    if a.stage in ('evaluate','all'):
        decision=json.loads((reports/'alarm_selected_2025.json').read_text(encoding='utf-8'))
        assert decision['device']==a.device
        best=decision['selected'];spec=best['spec'];feats=spec['features']
        model=CatBoostClassifier();model.load_model(str(models/f'alarm_{spec["name"]}.cbm'))
        model.save_model(str(models/'smoke_alarm_fault.cbm'))
        cal=period(f,'2025-10-01','2026-01-01');test=period(f,'2026-01-01','2026-06-30')
        cs=model.predict_proba(cal[feats])[:,1];ts=model.predict_proba(test[feats])[:,1]
        thresholds={'balanced':threshold_balanced(cal.target,cs)['threshold'],'recall':recall_threshold(cal.target,cs)}
        result=dict(selected=spec['name'],task_type=a.device,calibration_period='2025 Q4',test_is_pristine=False,
                    task='At eligible smoke alarm, first next logged fault in [T,T+24h); no minimum warning time.',modes={})
        for mode,threshold in thresholds.items():
            lead=test.loc[test.target.eq(1)&(ts>=threshold),'lead_hours']
            result['modes'][mode]=dict(threshold=threshold,calibration=score_metrics(cal,cs,threshold),
                test=score_metrics(test,ts,threshold),true_alert_lead_hours=lead.quantile([0,.1,.5,.9,1]).to_dict())
        config=dict(features=feats,categorical_features=spec['cats'],threshold=thresholds['balanced'],recall_mode_threshold=thresholds['recall'],
                    task_type=a.device,horizon_hours=24,minimum_lead_hours=0,selection=spec['name'],feature_version='v3-causal-neighbors-1')
        dump_json(models/'smoke_alarm_config.json',config);dump_json(reports/'alarm_evaluation.json',result)
        cols=['sensor_id','decision_time','target','next_onset','lead_hours']
        test[cols].assign(score=ts).to_parquet(reports/'alarm_test_predictions.parquet',index=False,compression='zstd')
        cal[cols].assign(score=cs).to_parquet(reports/'alarm_calibration_predictions.parquet',index=False,compression='zstd')
        pd.DataFrame({'feature':feats,'importance':model.get_feature_importance()}).sort_values('importance',ascending=False).to_csv(reports/'alarm_importance.csv',index=False)
        # Same validation and test rows for the frozen delivered v2 baseline.
        old=CatBoostClassifier();old.load_model(str(a.source/'reference_v2'/'smoke_alarm_fault.cbm'))
        oldcfg=json.loads((a.source/'reference_v2'/'smoke_alarm_config.json').read_text(encoding='utf-8'))
        oldts=old.predict_proba(test[oldcfg['features']])[:,1]
        result['frozen_v2_recall']=score_metrics(test,oldts,oldcfg['recall_mode_threshold'])
        seen=set(period(f,'2023-01-01','2025-07-01').sensor_id)
        cohorts=[]
        for is_seen,mask in [(True,test.sensor_id.isin(seen)),(False,~test.sensor_id.isin(seen))]:
            if mask.any():
                cohorts.append(dict(seen_in_training=is_seen,channels=int(test.loc[mask,'sensor_id'].nunique()),
                    **score_metrics(test.loc[mask],ts[mask],thresholds['balanced'])))
        pd.DataFrame(cohorts).to_csv(reports/'alarm_seen_unseen.csv',index=False)
        dump_json(reports/'alarm_evaluation.json',result)
        print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':main()
