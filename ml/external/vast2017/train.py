"""Обучение и проверка vast_gate — проезд без допуска и необычный проезд (VAST 2017 MC1, синтетика).

Данные — data/external/vast2017/prepared (формат docs/architecture/external-data.md, готовит lct-18). Время в файлах —
московское без пояса. Проверка — out-of-time: нормы маршрутов выучены на 01.05–30.06.2015 по проездам без нарушений
допуска, порог — квантиль оценок там же; проверка — 01.07.2015–31.05.2016 по разметке набора. Разметка в обучении не
участвует: ни нормы, ни порог её не видят. Неделя сценария эмулятора 04–11.07.2015 — в периоде проверки.

Запуск из ml-service: `PYTHONPATH=.. uv run python -m ml.external.vast2017.train`.
"""

import argparse
import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from ml.baseline.offline import block, lead_time, rule_block
from ml.external.vast2017.gate import DECISION_LAG, HISTORY, QUANTILE, fit, no_permit, passes, score

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "data" / "external" / "vast2017" / "prepared"
OUT = ROOT / "ml" / "models" / "external" / "vast2017-gate"
REPORT = ROOT / "ml" / "reports" / "external" / "vast2017"
TZ = "Europe/Moscow"
CUT = "2015-07-01"  # нормы — до, проверка — после
WEEK = ("2015-07-04", "2015-07-11")  # сценарий ext-vast2017-week
BOOST = 100.0  # в ранжировании для AP нарушение допуска — выше любой оценки норм


def utc(ts: pd.Series) -> pd.Series:
    ts = pd.to_datetime(ts)
    return ts.dt.tz_localize(TZ).dt.tz_convert("UTC") if ts.dt.tz is None else ts.dt.tz_convert("UTC")


def moment(s: str) -> pd.Timestamp:
    return pd.Timestamp(s, tz=TZ).tz_convert("UTC")


def load(data: Path):
    events = pd.read_parquet(data / "events.parquet")
    events["ts"] = utc(events["ts"])
    channels = pd.read_parquet(data / "channels.parquet").rename(columns={"id": "channel_id"})
    channels = channels.assign(measure=channels["tag"])[["channel_id", "object_id", "measure"]]
    cred = pd.read_parquet(data / "credentials.parquet")
    classes = {c: json.loads(m).get("class", "?") for c, m in zip(cred["credential_id"], cred["meta"], strict=True)}
    permits = pd.read_parquet(data / "permits.parquet")
    labels = pd.read_parquet(data / "labels.parquet")
    meta = [json.loads(m) for m in labels["meta"]]
    labels["subtype"] = [m.get("subtype", "") for m in meta]
    labels["credential_id"] = [m.get("credential_id") for m in meta]
    for c in ("start_ts", "end_ts"):
        labels[c] = utc(labels[c])
    return events, channels, classes, permits, labels


def label_passes(p: pd.DataFrame, labels: pd.DataFrame) -> pd.DataFrame:
    """Проезды билета из метки в [start_ts, end_ts]: у нарушения — сам проезд, у подозрительной поездки — её проезды."""
    rows = []
    by_value = {v: g for v, g in p.groupby("value")}
    for k, lab in enumerate(labels.itertuples()):
        g = by_value.get(lab.credential_id)
        if g is None:
            continue
        hit = g[(g.ts >= lab.start_ts) & (g.ts <= lab.end_ts)]
        rows += [(i, k, lab.label, lab.subtype, lab.credential_id, lab.start_ts) for i in hit.index]
    return pd.DataFrame(rows, columns=["row", "label_k", "label", "subtype", "credential_id", "start_ts"])


def trips(p: pd.DataFrame, lp: pd.DataFrame) -> list[dict]:
    """Поездки из разметки: обнаружена ли, чем и через сколько от начала поездки."""
    out = []
    for (subtype, cred), g in lp.groupby(["subtype", "credential_id"]):
        rows = p.loc[g.row.unique()].sort_values("ts")
        start = g.start_ts.min()
        fired = rows[rows.alert]
        out.append({
            "subtype": subtype,
            "credential_id": cred,
            "class": rows.cls.iloc[0],
            "start": str(start.tz_convert(TZ)),
            "in_test": bool(rows.test.all()),
            "passes": len(rows),
            "by_rule": int(rows.violation.sum()),
            "by_norms": int(rows.route_alert.sum()),
            "detected": bool(len(fired)),
            "first_alert_s": None if fired.empty else float((fired.ts.min() - start).total_seconds()),
            "first_reason": None if fired.empty else reason(fired.iloc[0]),
        })  # fmt: skip
    return out


def reason(r: pd.Series) -> str:
    if r.violation:
        return "no_permit"
    return "route" if r.s_route >= r.s_time else f"segment_time_{r.time_side}"


def evaluate(p: pd.DataFrame, lp: pd.DataFrame, thr: float) -> dict:
    te = p[p.test]
    y = te.index.isin(lp.row).astype(float)
    combined = te.score.to_numpy() + BOOST * te.violation.to_numpy()
    days = (te.ts.max() - te.ts.min()).total_seconds() / 86400
    free = te[te.alert & ~te.index.isin(lp.row)]
    by_label = []
    for label in ("violation", "suspicious"):
        yl = te.index.isin(lp[lp.label == label].row).astype(float)
        by_label.append({"class": label, **block(yl, combined, thr)})
    by_subtype = []
    for subtype, g in lp[lp.row.isin(te.index)].groupby("subtype"):
        rows = te.loc[g.row.unique()]
        by_subtype.append({"subtype": subtype, "passes": len(rows), "by_rule": int(rows.violation.sum()),
                           "by_norms": int(rows.route_alert.sum()), "detected": int(rows.alert.sum())})  # fmt: skip
    t = [x for x in trips(p, lp) if x["in_test"]]
    summary = {}
    for s in sorted({x["subtype"] for x in t}):
        kind = [x for x in t if x["subtype"] == s]
        summary[s] = {"total": len(kind), "detected": sum(x["detected"] for x in kind),
                      "by_norms": sum(x["by_norms"] > 0 for x in kind)}  # fmt: skip
    suspicious = [x["first_alert_s"] for x in t if x["subtype"] in ("overnight_truck", "speeding") and x["detected"]]
    routes = [
        {"route": f"{a} → {b}", "alerts": int(n)}
        for (a, b), n in free.groupby(["prev_gate", "gate"]).size().nlargest(5).items()
    ]
    week = te[(te.ts >= moment(WEEK[0])) & (te.ts < moment(WEEK[1]))]
    wy = week.index.isin(lp.row)
    return {
        "test_period": {
            "from": str(te.ts.min().tz_convert(TZ)),
            "to": str(te.ts.max().tz_convert(TZ)),
            "days": round(days, 1),
            "passes": len(te),
            "tickets": int(te.value.nunique()),
        },  # fmt: skip
        "combined": block(y, combined, thr),
        "norms_only": block(y, te.score.to_numpy(), thr),
        "rule": rule_block("проезд к закрытому объекту без допуска", y, te.violation.to_numpy()),
        "by_class": by_label,
        "by_subtype": by_subtype,
        "trips": t,
        "trips_summary": summary,
        "time_to_detect_suspicious": lead_time(np.array(suspicious)),
        "unlabeled_alerts": {
            "alerts": len(free),
            "per_day": round(len(free) / days, 2),
            "per_1000_passes": round(1000 * len(free) / len(te), 2),
            "tickets": int(free.value.nunique()),
            "top_routes": routes,
        },  # fmt: skip
        "scenario_week": {
            "from": WEEK[0],
            "to": WEEK[1],
            "passes": len(week),
            "labeled_passes": int(wy.sum()),
            "alerts": int(week.alert.sum()),
            "alerts_on_labeled": int((week.alert & wy).sum()),
            "alerts_on_unlabeled": int((week.alert & ~wy).sum()),
        },  # fmt: skip
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=DATA)
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--report", type=Path, default=REPORT)
    a = ap.parse_args()
    tick = time.perf_counter()
    events, channels, classes, permits, labels = load(a.data)
    p = passes(events, channels, classes)
    p["violation"] = no_permit(p, permits)
    p["test"] = p.ts >= moment(CUT)
    train = p[~p.test & ~p.violation]
    norms = fit(train)
    p = p.join(score(norms, p))
    thr = float(np.quantile(p.loc[train.index, "score"], QUANTILE))
    norms.threshold = round(thr, 6)
    p["route_alert"] = p.score >= thr
    p["alert"] = p.violation | p.route_alert
    lp = label_passes(p, labels)
    result = evaluate(p, lp, thr)
    result["train"] = {"from": str(train.ts.min().tz_convert(TZ)), "to": str(train.ts.max().tz_convert(TZ)),
                       "passes": len(train), "violations_excluded": int((~p.test & p.violation).sum())}  # fmt: skip

    a.out.mkdir(parents=True, exist_ok=True)
    path = a.out / "vast_gate.json"
    path.write_text(norms.dump(), encoding="utf-8")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    config = {
        "model": "vast_gate",
        "family": "external",
        "dataset": "vast2017",
        "version": "v0",
        "label": "vast-gate-v0",
        "title": "Проезд без допуска и необычный проезд",
        "artifact": path.name,
        "module": "ml.external.vast2017.gate",
        "synthetic": True,
        "trigger": "on_event",
        "target": "event",
        "target_tag": None,
        "horizon": "now",
        "outcome": "label_at_event",  # метка, в интервале которой лежит проезд, с тем же билетом (meta.credential_id)
        "label_key": "credential_id",
        "positive_labels": ["violation", "suspicious"],
        "ignore_labels": [],
        "classes": None,
        "features": ["no_permit", "prev_gate", "gate", "class", "segment_time"],
        "categorical": [],
        "threshold": round(thr, 6),
        "history_minutes": int(HISTORY.total_seconds() // 60),
        "decision_lag_s": int(DECISION_LAG.total_seconds()),
        "eval_scheme": "out-of-time",
        "periods": {"train": [str(p.ts.min().tz_convert(TZ)), CUT], "test": [CUT, str(p.ts.max().tz_convert(TZ))]},
        "sha256": sha,
        "trained_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source": "VAST Challenge 2017 MC1 (синтетика, IEEE VIS; лицензия не указана — исследования и обучение)",
    }
    (a.out / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    t = result["trips"]
    offline = {
        "sha256": sha,
        "computed_at": config["trained_at"],
        "model": "vast_gate",
        "version": "v0",
        "period": {
            "from": result["test_period"]["from"],
            "to": result["test_period"]["to"],
            "label": "01.07.2015–31.05.2016, после периода норм",
        },  # fmt: skip
        "threshold": round(thr, 6),
        "eval_scheme": "out-of-time",
        "test": result["combined"],
        "seen": None,
        "unseen": None,
        "seen_rule": None,
        "lead_time": result["time_to_detect_suspicious"],
        "pr_curve": None,
        "by_quarter": None,
        "by_type": None,
        "by_episode_kind": None,
        "by_class": result["by_class"],
        "by_experiment": t,
        "rule": result["rule"],
    }
    dump = lambda obj: json.dumps(obj, ensure_ascii=False, indent=1, default=str)  # noqa: E731
    (a.out / "offline.json").write_text(dump(offline), encoding="utf-8")
    a.report.mkdir(parents=True, exist_ok=True)
    (a.report / "metrics.json").write_text(dump(result), encoding="utf-8")
    keys = ("train", "test_period", "combined", "norms_only", "rule", "by_subtype", "trips_summary",
            "time_to_detect_suspicious", "unlabeled_alerts", "scenario_week")  # fmt: skip
    print(dump({k: result[k] for k in keys}))
    print(pd.DataFrame(t).drop(columns=["credential_id"]).to_string(index=False))
    print(f"порог {thr:.3f}, всего {time.perf_counter() - tick:.0f} с")


if __name__ == "__main__":
    main()
