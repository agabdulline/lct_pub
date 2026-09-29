"""Скоринг: признаки считаются тем же кодом, что при обучении — ml/baseline (v0), ml/v3 через ml/v3_bridge (v3),
ml/external (внешние наборы)."""

import importlib
import zlib

import numpy as np
import pandas as pd
from catboost import Pool

from app.models import LoadedModel
from ml.baseline.daily import FEATURES as DAILY_FEATURES
from ml.baseline.daily import build_daily
from ml.baseline.features import FEATURES, build, skip_reason
from ml.v3_bridge import alarm as v3

SECOND = pd.Timedelta(seconds=1)
BIN_GRACE = pd.Timedelta(seconds=10)  # первая корзина ряда может начаться на шаг позже начала окна
HISTORY_KEYS = ["obj_past_n", "loop_past_n"]


def object_history(row) -> bool:
    """Есть ли у объекта и у шлейфа исходы прошлых решений. Нет у любого — модель слепа: на 2025–2026 у таких
    решений ни одной тревоги (ml/reports/baseline-v0/REPORT.md, «Каналы и объекты без истории»)."""
    return bool(all(np.nan_to_num(row[k]) > 0 for k in HISTORY_KEYS))


def score_object(
    model: LoadedModel,
    events: pd.DataFrame,
    channels: pd.DataFrame,
    alarms: list[tuple[int, pd.Timestamp]],
    explain: bool,
) -> list[dict]:
    """Решения по тревогам одного объекта. events/channels — история объекта (наивное московское время)."""
    until = max(ts for _, ts in alarms)
    dec = build(events, channels, data_end=until + SECOND, only=alarms)
    keyed = dec.set_index(["channel_id", "alarm_ts"]) if not dec.empty else None
    results, rows, row_pos = [], [], []
    for cid, ts in alarms:
        base = {
            "channel_id": cid,
            "alarm_ts": ts,
            "decision_time": ts + SECOND,
            "threshold": model.threshold,
            "seen_channel": cid in model.seen_channels,
            "object_history": None,
            "model": model.label,
        }
        if keyed is not None and (cid, ts) in keyed.index:
            row = keyed.loc[(cid, ts), FEATURES]
            row_pos.append(len(results))
            rows.append(row.to_numpy(dtype=float))
            results.append({**base, "object_history": object_history(row), "status": "scored", "skip_reason": None})
        else:
            results.append(
                {
                    **base,
                    "status": "skipped",
                    "score": None,
                    "alert": None,
                    "factors": [],
                    "skip_reason": skip_reason(events, channels, cid, ts) or "not_eligible",
                }
            )
    if rows:
        x = pd.DataFrame(np.vstack(rows), columns=FEATURES)
        scores = model.model.predict_proba(x)[:, 1]
        shap = model.model.get_feature_importance(Pool(x), type="ShapValues")[:, :-1] if explain else None
        for k, pos in enumerate(row_pos):
            r = results[pos]
            r["score"] = round(float(scores[k]), 6)
            r["alert"] = bool(scores[k] >= model.threshold)
            if shap is not None:
                top = np.argsort(-np.abs(shap[k]))[:5]
                r["factors"] = [
                    {
                        "feature": FEATURES[j],
                        "value": None if np.isnan(x.iat[k, j]) else float(x.iat[k, j]),
                        "contribution": round(float(shap[k, j]), 4),
                    }
                    for j in top
                ]
            else:
                r["factors"] = []
    return results


def score_object_v3(
    model: LoadedModel,
    events: pd.DataFrame,
    channels: pd.DataFrame,
    coverage: set[pd.Timestamp],
    rows: v3.AlarmRows,
    alarms: list[tuple[int, pd.Timestamp]],
    explain: bool,
) -> list[dict]:
    """Решения событийной v3 по тревогам одного объекта — их код признаков (ml/v3 через ml/v3_bridge).

    events/channels — история и дымовые каналы объекта (наивное московское время), coverage — дни с данными журнала,
    rows — кэш их решений по объекту. Формат результата — как у score_object (v0).
    """
    ev = v3.frame(events)
    cov = pd.DatetimeIndex(sorted(coverage))
    until = max(ts for _, ts in alarms) + SECOND
    f = v3.decide(ev, v3.registry(channels), alarms, rows.get(ev, cov, until))
    names, cats = model.config["features"], model.config.get("categorical", [])
    at = {(int(c), t): k for k, (c, t) in enumerate(zip(f["sensor_id"], f["alarm_ts"], strict=True))}
    x = f[names] if len(f) else None
    scores = model.model.predict_proba(x)[:, 1] if len(f) else []
    shap = None
    if explain and len(f):
        shap = model.model.get_feature_importance(Pool(x, cat_features=cats), type="ShapValues")[:, :-1]
    results = []
    for cid, ts in alarms:
        base = {
            "channel_id": cid,
            "alarm_ts": ts,
            "decision_time": ts + SECOND,
            "threshold": model.threshold,
            "seen_channel": cid in model.seen_channels,  # у v3 идентификатор канала — признак (channel_key)
            "object_history": None,
            "model": model.label,
        }
        k = at.get((cid, ts))
        if k is None:
            reason = v3.skip_reason(ev[ev["ts"] < until], cov, cid, ts)
            results.append({**base, "status": "skipped", "skip_reason": reason, "score": None, "alert": None,
                            "factors": []})  # fmt: skip
            continue
        # исходы прошлых решений и у объекта, и у шлейфа — как obj_past_n / loop_past_n у v0
        seen_history = bool(f["history_object_n_3650d"].iat[k] > 0 and f["history_loop_n_3650d"].iat[k] > 0)
        item = {
            **base,
            "object_history": seen_history,
            "status": "scored",
            "skip_reason": None,
            "score": round(float(scores[k]), 6),
            "alert": bool(scores[k] >= model.threshold),
            "factors": [],
        }
        if shap is not None:
            top = np.argsort(-np.abs(shap[k]))[:5]
            item["factors"] = [
                {"feature": names[j], "value": _value(x.iat[k, j]), "contribution": round(float(shap[k, j]), 4)}
                for j in top
            ]
        results.append(item)
    return results


def score_daily(
    model: LoadedModel,
    daily: pd.DataFrame,
    episodes: pd.DataFrame,
    channels: pd.DataFrame,
    day: pd.Timestamp,
    explain: bool,
    only: list[int] | None = None,
) -> list[dict]:
    """Ежедневный прогноз на сутки day: окно цели [day + 1 сут, day + 30 сут).

    channels — все каналы нужных типов на объектах (для признаков объекта), only — для каких каналов нужен прогноз.
    """
    day = day.normalize()
    rows = build_daily(daily, episodes, channels, [day], only=only)
    target_start, target_end = day + pd.Timedelta(days=1), day + pd.Timedelta(days=30)
    scored = rows[rows["status"] == "scored"]
    feats = model.config.get("features", DAILY_FEATURES)  # набор признаков — тот, на котором обучена модель
    x = scored[feats]
    scores = model.model.predict_proba(x)[:, 1] if len(scored) else []
    shap = None
    if explain and len(scored):
        pool = Pool(x, cat_features=model.config.get("categorical", []))
        shap = model.model.get_feature_importance(pool, type="ShapValues")[:, :-1]
    by_channel = {cid: k for k, cid in enumerate(scored["channel_id"])}
    out = []
    for r in rows.itertuples(index=False):
        base = {
            "channel_id": int(r.channel_id),
            "target_start": target_start,
            "target_end": target_end,
            "threshold": model.threshold,
            "seen_channel": None,  # формат как у /v1/alarm/score; идентификатора канала в признаках нет
            "object_history": None,
            "model": model.label,
        }
        k = by_channel.get(r.channel_id)
        if k is None:
            out.append(
                {**base, "status": "skipped", "skip_reason": r.status, "score": None, "alert": None, "factors": []}
            )
            continue
        item = {
            **base,
            "status": "scored",
            "skip_reason": None,
            "score": round(float(scores[k]), 6),
            "alert": bool(scores[k] >= model.threshold),
            "factors": [],
        }
        if shap is not None:
            top = np.argsort(-np.abs(shap[k]))[:5]
            item["factors"] = [
                {"feature": feats[j], "value": _value(x.iat[k, j]), "contribution": round(float(shap[k, j]), 4)}
                for j in top
            ]
        out.append(item)
    return out


def _value(v):
    if isinstance(v, str):
        return v
    return None if pd.isna(v) else float(v)


def feature_module(model: LoadedModel):
    """Код признаков внешней модели — ml/external/<набор>/features.py, тот же, что при обучении (D1)."""
    return importlib.import_module(f"ml.external.{model.config['dataset']}.features")


def target_channels(model: LoadedModel, channels: pd.DataFrame) -> dict[int, int]:
    """Служебный канал цели объекта (external-data.md §3.2) — tag = config.target_tag; в него пишутся прогнозы."""
    tag = model.config.get("target_tag")
    if not tag or channels.empty:
        return {}
    own = channels[channels["measure"] == tag]
    return {int(o): int(c) for o, c in own.groupby("object_id")["channel_id"].min().items()}


def score_external(
    model: LoadedModel,
    events: pd.DataFrame,
    channels: pd.DataFrame,
    targets: list[int],
    decision_time: pd.Timestamp,
    explain: bool,
    units: pd.DataFrame | None = None,
) -> list[dict]:
    """Решения внешней модели по целям (узлам, агрегатам) в момент decision_time (UTC); события — [T − history, T).

    Признаки — те, на которых обучена модель (config.features): код признаков набора может знать и более новые.
    Готовность цели — история не короче fm.READY (по умолчанию history_minutes) с допуском fm.READY_GRACE: у редких
    сигналов (ТО, отказы) окно длиннее, чем нужно для решения. units — паспорт объектов, если он нужен коду признаков.
    """
    fm = feature_module(model)
    names = model.config.get("features", fm.FEATURES)
    cats = model.config.get("categorical", [])
    target_channel = target_channels(model, channels)
    classes = model.config.get("classes") or []
    history = getattr(fm, "READY", pd.Timedelta(minutes=model.config.get("history_minutes", 0)))
    grace = getattr(fm, "READY_GRACE", BIN_GRACE)
    known = set(channels["object_id"])
    span = events.join(channels.set_index("channel_id")["object_id"], on="channel_id").groupby("object_id")["ts"]
    first = span.min() if len(events) else pd.Series(dtype="datetime64[ns, UTC]")
    wanted = [t for t in targets if t in known and t in first.index and first[t] <= decision_time - history + grace]
    extra = {"units": units} if units is not None else {}
    decisions = pd.DataFrame({"object_id": wanted, "decision_time": decision_time})
    feats = fm.build(events, channels, decisions, **extra) if wanted else pd.DataFrame(columns=["object_id", *names])
    x = None
    if wanted:
        x = feats[names].copy()
        num = [c for c in names if c not in cats]
        x[num] = x[num].astype(float)
    proba = model.model.predict_proba(Pool(x, cat_features=cats)) if wanted else None
    fire = classes.index("fire") if "fire" in classes else -1  # класс, по которому тревога
    shap = None
    if explain and wanted:
        shap = model.model.get_feature_importance(Pool(x, cat_features=cats), type="ShapValues")
        shap = shap[:, fire, :-1] if shap.ndim == 3 else shap[:, :-1]
    out, k_of = [], {t: k for k, t in enumerate(wanted)}
    for t in targets:
        base = {"target": t, "target_channel_id": target_channel.get(t), "threshold": model.threshold,
                "model": model.label}  # fmt: skip
        k = k_of.get(t)
        if k is None:
            reason = "unknown_target" if t not in known else "no_data" if t not in first.index else "short_history"
            out.append({**base, "status": "skipped", "skip_reason": reason, "score": None, "alert": None,
                        "classes": None, "class": None, "factors": []})  # fmt: skip
            continue
        p = proba[k]
        alert = bool(p[fire] >= model.threshold)
        # класс решения для ленты: у многоклассовой — самый вероятный, иначе config.alert_class при тревоге
        label = classes[int(np.argmax(p))] if classes else (model.config.get("alert_class") if alert else None)
        item = {
            **base,
            "status": "scored",
            "skip_reason": None,
            "score": round(float(p[fire]), 6),
            "alert": alert,
            "classes": {c: round(float(v), 6) for c, v in zip(classes, p, strict=False)} or None,
            "class": label,
            "factors": [],
        }
        if shap is not None:
            top = np.argsort(-np.abs(shap[k]))[:5]
            item["factors"] = [
                {"feature": names[j], "value": _value(x.iat[k, j]), "contribution": round(float(shap[k, j]), 4)}
                for j in top
            ]
        out.append(item)
    return out


def value_offset(value: str) -> pd.Timedelta:
    """Доля секунды от значения события (crc32 % 10⁶ мкс): два проезда разных билетов через один датчик в одну секунду
    получают разные моменты решения и не сливаются в predictions (ключ — канал и момент). Детерминирована: не зависит
    от того, в какой пачке пришло событие."""
    return pd.Timedelta(microseconds=zlib.crc32(str(value).encode()) % 1_000_000)


def score_external_events(
    model: LoadedModel,
    targets: pd.DataFrame,
    history: pd.DataFrame,
    channels: pd.DataFrame,
    classes: dict[str, str],
    permits: pd.DataFrame,
    decision_time: pd.Timestamp,
) -> list[dict]:
    """Решения событийной внешней модели (target = event, vast_gate): по проезду в момент ts + decision_lag_s.

    Код решения — module.decide модели, тот же, что при обучении; history — прошлые события тех же пропусков.
    """
    lag = pd.Timedelta(seconds=model.config.get("decision_lag_s", 0))
    decided = importlib.import_module(model.config["module"]).decide(
        model.artifact, targets, history, channels, classes, permits
    )
    out = []
    for (_, t), (_, d) in zip(targets.iterrows(), decided.iterrows(), strict=True):
        at = pd.Timestamp(t["ts"]).tz_convert("UTC") + lag + value_offset(t["value"])
        channel = int(t["channel_id"])
        base = {"target": channel, "target_channel_id": channel, "event_ts": t["ts"], "value": t["value"],
                "decision_time": at, "threshold": model.threshold, "model": model.label}  # fmt: skip
        reason = "unknown_target" if pd.isna(d.get("gate")) else "future_event" if at > decision_time else None
        if reason:
            out.append({**base, "status": "skipped", "skip_reason": reason, "score": None, "alert": None,
                        "classes": None, "class": None, "factors": []})  # fmt: skip
            continue
        factors = importlib.import_module(model.config["module"]).factors(d, bool(d["violation"]))
        # класс решения: нарушение допуска — правилом, необычный проезд — нормами маршрутов
        label = "violation" if d["violation"] else "suspicious" if d["alert"] else None
        out.append({**base, "status": "scored", "skip_reason": None, "score": round(float(d["score"]), 6),
                    "alert": bool(d["alert"]), "classes": None, "class": label, "factors": factors})  # fmt: skip
    return out
