"""Temporal experiments; only training negatives may be sampled."""
from pathlib import Path
import gc,json,time
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from catboost import CatBoostClassifier,Pool
from sklearn.metrics import precision_recall_curve,average_precision_score
from .features import FEATURES,CAT_FEATURES
from .baseline_core import metrics,dump_json

SEED=42

def feature_set(spec):
    feats=FEATURES if spec.get('identity',True) else [f for f in FEATURES if f!='channel_key']
    if spec.get('temporal',False):
        from .temporal_features import TEMPORAL_FEATURES
        feats=feats+TEMPORAL_FEATURES
    return feats

def threshold_balanced(y,score,min_alerts=30):
    """Meet BOTH contest criteria or maximize the weaker normalized metric.

    This deliberately avoids a near-perfect precision at negligible recall.
    Must be called on calibration/development only, never test.
    """
    p,r,t=precision_recall_curve(y,score)
    p,r=p[:-1],r[:-1]
    alerts=len(score)-np.searchsorted(np.sort(score),t,side='left')
    objective=np.minimum(p/.70,r/.50)
    objective[alerts<min_alerts]=-1
    i=int(np.argmax(objective))
    return {'threshold':float(t[i]),'precision':float(p[i]),'recall':float(r[i]),
            'alerts':int(alerts[i]),'criterion_fraction':float(objective[i]),
            'both_criteria':bool(p[i]>.7 and r[i]>.5),
            'rule':'maximize min(precision/0.70, recall/0.50), min 30 alerts'}


def load_period(cache,start,end,horizon,max_negatives=None):
    """Keep a label only if the complete outcome window is within its split."""
    start,end=pd.Timestamp(start),pd.Timestamp(end)
    pieces=[];rng=np.random.default_rng(SEED)
    coverage=pd.read_csv(Path(__file__).resolve().parents[1]/'data'/'global_coverage.csv',parse_dates=['date']).date
    def add_target(f):
        if horizon==1:
            f['y1']=((f.next_onset>=f.date)&(f.next_onset<f.date+pd.Timedelta(days=1))).astype('float32')
            f.loc[~f.date.isin(coverage)|(f.date>=pd.Timestamp('2026-06-30')),'y1']=np.nan
        return f
    # First pass only target/date: exact negative sampling probability.
    counts={'full_rows':0,'full_positives':0}
    def eligible(f):
        return (f.date>=start)&(f.date+pd.Timedelta(days=horizon)<=end)&f[f'y{horizon}'].notna()
    paths=[Path(cache)/f'features_{y}.parquet' for y in range(start.year,end.year+1) if (Path(cache)/f'features_{y}.parquet').is_file()]
    for path in paths:
        for batch in pq.ParquetFile(path).iter_batches(columns=['date','next_onset'] if horizon==1 else ['date',f'y{horizon}'],batch_size=200000):
            f=add_target(batch.to_pandas());f=f.loc[eligible(f)]
            counts['full_rows']+=len(f);counts['full_positives']+=int(f[f'y{horizon}'].sum())
    negatives=counts['full_rows']-counts['full_positives']
    probability=1. if max_negatives is None else min(1.,max_negatives/max(1,negatives))
    columns=['date','sensor_id',*FEATURES,'next_onset','early_fault']+([] if horizon==1 else [f'y{horizon}'])
    for path in paths:
        for batch in pq.ParquetFile(path).iter_batches(columns=columns,batch_size=120000):
            f=add_target(batch.to_pandas());f=f.loc[eligible(f)].rename(columns={f'y{horizon}':'target'})
            keep=f.target.eq(1).to_numpy()|(rng.random(len(f))<probability)
            f=f.loc[keep].copy()
            f['weight']=np.where(f.target.eq(1),1.,1/probability).astype('float32')
            pieces.append(f)
    f=pd.concat(pieces,ignore_index=True).sort_values(['date','sensor_id'],kind='stable').reset_index(drop=True)
    f['target']=f.target.astype('int8')
    for c in CAT_FEATURES:f[c]=f[c].astype(str)
    counts.update(sampled_rows=len(f),negative_keep_probability=probability,inverse_probability_weights=True,
                  start=str(start),end=str(end),horizon=horizon)
    return f,counts


def fit_trial(train,valid,spec,task_type='CPU',threads=6,iterations=600):
    params=dict(iterations=iterations,depth=spec.get('depth',6),learning_rate=.06,
                l2_leaf_reg=spec.get('l2_leaf_reg',10),loss_function='Logloss',
                eval_metric='PRAUC:type=Classic;use_weights=false',random_seed=SEED,
                class_weights=[1.,spec.get('positive_weight',1.)],
                thread_count=threads,has_time=True,one_hot_max_size=64,
                bootstrap_type='Bernoulli',subsample=.85,allow_writing_files=False,
                task_type=task_type,verbose=100)
    if task_type=='GPU':params.update(devices='0:1',data_partition='DocParallel',gpu_ram_part=.85)
    feats=feature_set(spec)
    cats=[c for c in CAT_FEATURES if c in feats]
    model=CatBoostClassifier(**params)
    tick=time.perf_counter()
    tr=Pool(train[feats],train.target,cat_features=cats,weight=train.weight)
    va=Pool(valid[feats],valid.target,cat_features=cats)
    model.fit(tr,eval_set=va,early_stopping_rounds=80,use_best_model=True)
    score=model.predict_proba(va)[:,1]
    chosen=threshold_balanced(valid.target,score)
    result={'spec':spec,'task_type':task_type,'trees':model.tree_count_,
            'seconds':time.perf_counter()-tick,'average_precision':float(average_precision_score(valid.target,score)),
            'development_operating_point':chosen,'metrics':metrics(valid.target,score,chosen['threshold']),
            'features':feats,'categorical_features':cats}
    return model,result,score


def refit_fixed(train,spec,iterations,task_type='CPU',threads=6):
    """Fixed tree count from development; calibration labels not used for fit."""
    feats=feature_set(spec)
    cats=[c for c in CAT_FEATURES if c in feats]
    params=dict(iterations=int(iterations),depth=spec.get('depth',6),learning_rate=.06,
                l2_leaf_reg=spec.get('l2_leaf_reg',10),loss_function='Logloss',random_seed=SEED,
                class_weights=[1.,spec.get('positive_weight',1.)],thread_count=threads,has_time=True,
                one_hot_max_size=64,bootstrap_type='Bernoulli',subsample=.85,allow_writing_files=False,
                task_type=task_type,verbose=100)
    if task_type=='GPU':params.update(devices='0:1',data_partition='DocParallel',gpu_ram_part=.85)
    model=CatBoostClassifier(**params)
    model.fit(Pool(train[feats],train.target,cat_features=cats,weight=train.weight))
    return model


def episode_metrics(rows,threshold):
    """Unique first-next-onsets with at least one eligible forecast opportunity.

    Not recall over every raw state oscillation. No point adjustment is applied
    to primary per-row metrics.
    """
    true=rows.loc[rows.target.eq(1),['sensor_id','next_onset']].drop_duplicates()
    hit=rows.loc[rows.target.eq(1)&rows.score.ge(threshold),['sensor_id','next_onset']].drop_duplicates()
    alerts=rows.loc[rows.score.ge(threshold)]
    good=alerts.loc[alerts.target.eq(1)].copy()
    leads=(good.next_onset-good.date).dt.total_seconds()/3600
    return {'forecastable_first_next_onsets':len(true),'detected_unique_onsets':len(hit),
            'recall_among_forecastable_onsets':len(hit)/len(true) if len(true) else None,
            'positive_alert_rows':len(good),'lead_hours_quantiles':leads.quantile([0,.25,.5,.75,1]).to_dict(),
            'warning':'Not a metric over all raw oscillations; repeated daily alerts counted separately in primary precision.'}
