"""Daily long-lead context ablation. Same cohort and 24h-30d target as v2."""
from pathlib import Path
import argparse,json,sys,time,gc
import numpy as np
import pandas as pd
from catboost import CatBoostClassifier,Pool
from sklearn.metrics import average_precision_score
from src.features import FEATURES,CAT_FEATURES
from src.training import load_period,threshold_balanced,episode_metrics
from src.baseline_core import dump_json,metrics
from src.neighbors import META_CATS
from src.daily_context import DailyContext,daily_features


def params(device,trees,depth):
    p=dict(iterations=trees,depth=depth,learning_rate=.06,l2_leaf_reg=10,
           loss_function='Logloss',eval_metric='PRAUC:type=Classic;use_weights=false',random_seed=42,
           has_time=True,one_hot_max_size=64,bootstrap_type='Bernoulli',subsample=.85,
           thread_count=5,allow_writing_files=False,verbose=100,task_type=device)
    if device=='GPU':p.update(devices='0:1',data_partition='DocParallel',gpu_ram_part=.85,
                              boosting_type='Plain',max_ctr_complexity=1)
    return p


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--source',type=Path,default=Path(__file__).parent)
    p.add_argument('--output',type=Path,default=Path(__file__).parent);p.add_argument('--cache',type=Path,required=True)
    p.add_argument('--device',choices=['CPU','GPU'],default='GPU');p.add_argument('--stage',choices=['develop','evaluate','all'],default='all')
    a=p.parse_args();reports=a.output/'artifacts'/'reports';models=a.output/'artifacts'/'models';data=a.source/'data'
    reports.mkdir(parents=True,exist_ok=True);models.mkdir(parents=True,exist_ok=True)
    if a.device=='GPU':
        from catboost.utils import get_gpu_device_count
        assert get_gpu_device_count()>=2
    ctx=DailyContext(pd.read_parquet(data/'sensor_daily.parquet'),pd.read_csv(data/'channels.csv'),
                     pd.read_parquet(data/'alarm_features_v3.parquet'),pd.read_parquet(data/'fault_episodes.parquet'))
    def load(start,end,limit=None):
        f,meta=load_period(a.cache,start,end,30,limit)
        return ctx.transform(f),meta
    if a.stage in ('develop','all'):
        tr,_=load('2022-01-01','2025-01-01',300000);va,_=load('2025-01-01','2025-07-01')
        records=[]
        for mode,depth in [('base',6),('metadata',6),('context',6),('context',4)]:
            tick=time.perf_counter();feats=daily_features(FEATURES,tr,mode);cats=[c for c in CAT_FEATURES+META_CATS if c in feats]
            model=CatBoostClassifier(**params(a.device,450,depth))
            model.fit(Pool(tr[feats],tr.target,cat_features=cats,weight=tr.weight),eval_set=Pool(va[feats],va.target,cat_features=cats),early_stopping_rounds=65)
            vs=model.predict_proba(va[feats])[:,1];op=threshold_balanced(va.target,vs)
            name=f'{mode}_d{depth}';model.save_model(str(models/f'daily_develop_{name}.cbm'))
            r=dict(name=name,mode=mode,depth=depth,features=feats,cats=cats,trees=model.tree_count_,h1=op,
                   h1_ap=float(average_precision_score(va.target,vs)),seconds=time.perf_counter()-tick)
            records.append(r);dump_json(reports/'daily_ablation_2025.json',records)
            print('DAILY H1',name,model.tree_count_,op,flush=True)
        del tr,va;gc.collect()
        tr,_=load('2022-01-01','2025-07-01',500000);q3,_=load('2025-07-01','2025-10-01')
        for r in records:
            model=CatBoostClassifier(**params(a.device,r['trees'],r['depth']))
            model.fit(Pool(tr[r['features']],tr.target,cat_features=r['cats'],weight=tr.weight))
            qs=model.predict_proba(q3[r['features']])[:,1];r['q3']=threshold_balanced(q3.target,qs)
            r['q3_ap']=float(average_precision_score(q3.target,qs));r['robust_criterion']=min(r['h1']['criterion_fraction'],r['q3']['criterion_fraction'])
            model.save_model(str(models/f'daily_{r["name"]}.cbm'))
            print('DAILY Q3',r['name'],r['q3'],flush=True)
        dump_json(reports/'daily_ablation_2025.json',records)
        selected=max(records,key=lambda r:(r['robust_criterion'],r['q3_ap']))
        dump_json(reports/'daily_selected_2025.json',dict(selected=selected,device=a.device,
            rule='max min(H1 criterion fraction, Q3 criterion fraction); Q4 only thresholds; test not used'))
        print('DAILY SELECTED',selected['name'],flush=True)
        del tr,q3;gc.collect()
    if a.stage in ('evaluate','all'):
        decision=json.loads((reports/'daily_selected_2025.json').read_text(encoding='utf-8'));assert decision['device']==a.device
        best=decision['selected'];feats=best['features'];model=CatBoostClassifier();model.load_model(str(models/f'daily_{best["name"]}.cbm'))
        model.save_model(str(models/'sensor_failure_selected.cbm'))
        cal,_=load('2025-10-01','2026-01-01');cs=model.predict_proba(cal[feats])[:,1];op=threshold_balanced(cal.target,cs);th=op['threshold']
        result=dict(selected=best['name'],task_type=a.device,threshold=op,calibration=metrics(cal.target,cs,th),test_is_pristine=False)
        cal[['sensor_id','date','target','next_onset']].assign(score=cs).to_parquet(reports/'daily_calibration_predictions.parquet',index=False)
        del cal;gc.collect()
        test,_=load('2026-01-01','2026-06-30');score=model.predict_proba(test[feats])[:,1]
        test['score']=score;result['test']=metrics(test.target,score,th);result['test_episodes']=episode_metrics(test,th)
        old=CatBoostClassifier();old.load_model(str(a.source/'reference_v2'/'sensor_failure_selected.cbm'))
        cfg=json.loads((a.source/'reference_v2'/'model_config.json').read_text(encoding='utf-8'))
        oldscore=old.predict_proba(test[cfg['features']])[:,1];result['frozen_v2']=metrics(test.target,oldscore,cfg['threshold'])
        test[['date','sensor_id','sensor_type','target','next_onset','early_fault','score']].to_parquet(reports/'daily_test_predictions.parquet',index=False,compression='zstd')
        dump_json(reports/'daily_evaluation.json',result)
        dump_json(models/'model_config.json',dict(model_file='sensor_failure_selected.cbm',features=feats,categorical_features=best['cats'],
            threshold=th,horizon_days=30,lead_days=1,task_type=a.device,selection=best['name'],feature_version='v3-causal-neighbors-1'))
        pd.DataFrame({'feature':feats,'importance':model.get_feature_importance()}).sort_values('importance',ascending=False).to_csv(reports/'daily_importance.csv',index=False)
        print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':main()
