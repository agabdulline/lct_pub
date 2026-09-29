"""Сравнение событийных моделей v0 и v3 на одних решениях: `ml/reports/compare-v0-v3/` (metrics.json, графики).

Решения — проверка I полугодия 2026, допустимые для обеих моделей; каждая — на своём рабочем пороге. Решения v0 —
код обучения v0 по архиву (`ml.baseline.offline.smoke_decisions`), решения v3 — их код целиком по архиву, отчётный
расчёт (`ml.v3_bridge.offline.reference`); вид эпизода тревоги — из рабочей базы. Интервалы — 90% кластерного
бутстрапа по объекто-суткам, парного: обе модели и их разность — на одних и тех же выборках.

Запуск из ml-service: `PYTHONPATH=.. uv run --with matplotlib python -m ml.v3_bridge.compare`. Нужны кэши
`ml.v3_bridge.offline` в --work (эталон v3 и решения v0); без них скрипт посчитает их сам (~1 мин и ~3 мин).

Итоги для интерфейса дописываются в `ml/models/compare_smoke_alarm.json` (GET /v1/models/compare, файл пишет
`ml.v3_bridge.offline`): интервалы, разность, объекто-сутки, виды эпизода, расхождения по видам, цена правила покрытия.
`--only-compare` — только это, из готового metrics.json, без пересчёта.
"""

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.patches import Patch, Rectangle  # noqa: E402
from matplotlib.ticker import PercentFormatter  # noqa: E402
from sklearn.metrics import average_precision_score, precision_recall_curve  # noqa: E402

from ml.baseline import offline as v0off  # noqa: E402
from ml.baseline.figures import INK, INK2, MUTED, SLIDE, SURFACE, ZONE, dot, header, n_ru, pct, style  # noqa: E402
from ml.v3.src.event_model import build_alarm_rows  # noqa: E402
from ml.v3_bridge import offline as v3off  # noqa: E402
from ml.v3_bridge.alarm import FAULT_WASHOUT_HOURS, SMOKE  # noqa: E402

OUT = v3off.ROOT / "ml" / "reports" / "compare-v0-v3"
V0, V3 = "baseline-v0", "v3"
NAME = {V0: "v0 (команда)", V3: "v3 (ML-разработчик)"}
COLOR = {V0: "#2a78d6", V3: "#eb6834"}  # слоты 1–2 проверенной палитры; синий — v0, оранжевый — v3 на всех графиках
KIND_RU = {"ppr": "Плановая проверка", "group": "Группа тревог", "solitary": "Одиночная", "unknown": "Не найден в базе"}
REQ_P, REQ_R = 0.7, 0.5
BOOT, SEED = 2000, 42


# ---------- данные


def decisions(work: Path, db: str, archive: Path, raw: Path) -> tuple[pd.DataFrame, dict, dict]:
    """Общие решения проверки 2026 с оценками обеих моделей, исходом, видом эпизода, объектом, историей."""
    events, coverage, channels = v3off.build_inputs(archive, raw, work)
    ref = v3off.reference(events, coverage, channels, work)
    model3, cfg3 = v3off.load_model()
    conf3 = json.loads((v3off.MODEL_DIR / "config.json").read_text(encoding="utf-8"))
    t3 = v3off.period(ref, *v3off.TEST).copy()
    t3["score"] = model3.predict_proba(t3[cfg3["features"]])[:, 1]
    t3 = t3.rename(columns={"sensor_id": "channel_id"})
    d0, cfg0, _ = v0off.smoke_decisions(archive, work / "v0_decisions.parquet", db)
    lo, hi = cfg0["periods"]["test"]
    t0 = d0[(d0.decision_time >= lo) & (d0.decision_time < hi)]
    keep0 = ["channel_id", "decision_time", "object_id", "target", "score", "lead_hours", "kind", "obj_past_n",
             "loop_past_n"]  # fmt: skip
    both = t3[["channel_id", "decision_time", "target", "score", "lead_hours"]].merge(
        t0[keep0], on=["channel_id", "decision_time"], suffixes=("_v3", "_v0")
    )
    assert (both.target_v3 == both.target_v0).all(), "метки исхода на общих решениях разошлись"
    both = both.rename(columns={"target_v3": "target"}).drop(columns="target_v0")
    both["seen_v3"] = both.channel_id.isin(set(conf3["seen_channels"]))
    both["history_v0"] = (both.obj_past_n.fillna(0) > 0) & (both.loop_past_n.fillna(0) > 0)
    both["cluster"] = both.object_id.astype(str) + "_" + both.decision_time.dt.date.astype(str)
    thr = {V0: cfg0["threshold"], V3: cfg3["threshold"]}
    ctx = {"events": events, "coverage": coverage, "t0": t0, "t3": t3}
    return both, thr, ctx


# ---------- метрики


def counts(y: np.ndarray, s: np.ndarray, thr: float) -> dict:
    a, y = s >= thr, y == 1
    tp, fp, fn = int((a & y).sum()), int((a & ~y).sum()), int((~a & y).sum())
    p = tp / (tp + fp) if tp + fp else None
    r = tp / (tp + fn) if tp + fn else None
    return {"decisions": len(y), "positives": int(y.sum()), "alerts": int(a.sum()), "tp": tp, "fp": fp, "fn": fn,
            "precision": p, "recall": r}  # fmt: skip


def bootstrap(d: pd.DataFrame, thr: dict) -> dict:
    """P, R, AP обеих моделей и разность v3 − v0: точка и 90% интервал парного кластерного бутстрапа."""
    y = d.target.to_numpy() == 1
    s = {v: d[f"score_{'v0' if v == V0 else 'v3'}"].to_numpy() for v in thr}
    groups = d.groupby("cluster").indices
    keys = list(groups)
    rng = np.random.default_rng(SEED)
    stats = {v: {"P": [], "R": [], "AP": []} for v in thr}
    for _ in range(BOOT):
        idx = np.concatenate([groups[keys[k]] for k in rng.integers(0, len(keys), len(keys))])
        yb = y[idx]
        for v in thr:
            a = s[v][idx] >= thr[v]
            tp = (a & yb).sum()
            stats[v]["P"].append(tp / a.sum() if a.sum() else np.nan)
            stats[v]["R"].append(tp / yb.sum() if yb.sum() else np.nan)
            stats[v]["AP"].append(average_precision_score(yb, s[v][idx]) if 0 < yb.sum() < len(yb) else np.nan)
    out = {}
    for v in thr:
        c = counts(y, s[v], thr[v])
        point = {"P": c["precision"], "R": c["recall"], "AP": average_precision_score(y, s[v])}
        out[v] = {m: {"value": point[m], "ci90": list(np.nanquantile(stats[v][m], [0.05, 0.95]))} for m in point}
        out[v]["counts"] = c
    out["diff_v3_minus_v0"] = {
        m: {
            "value": out[V3][m]["value"] - out[V0][m]["value"],
            "ci90": list(np.nanquantile(np.array(stats[V3][m]) - np.array(stats[V0][m]), [0.05, 0.95])),
        }
        for m in ("P", "R", "AP")
    }
    pos_clusters = d[d.target == 1].cluster.nunique()
    out["clusters"] = {"object_days": len(keys), "with_faults": int(pos_clusters), "boot": BOOT, "seed": SEED}
    return out


def disagreements(d: pd.DataFrame, thr: dict) -> tuple[pd.DataFrame, dict]:
    """Решения, где тревога только у одной модели, и разбивки: вид эпизода, объект, история канала, кто прав."""
    a0, a3 = d.score_v0 >= thr[V0], d.score_v3 >= thr[V3]
    x = d[a0 != a3].copy()
    x["only"] = np.where(a0[a0 != a3], V0, V3)
    x["right"] = x.target == 1  # тревога одной модели права, если неисправность была; иначе права молчавшая
    obj = x.groupby("object_id").agg(n=("only", "size"), only_v0=("only", lambda s: int((s == V0).sum())),
                                     only_v3=("only", lambda s: int((s == V3).sum())),
                                     faults=("right", "sum")).sort_values("n", ascending=False)  # fmt: skip
    summary = {
        "decisions": len(x),
        "alert_only": {v: int((x.only == v).sum()) for v in (V0, V3)},
        "alert_only_fault": {v: int(((x.only == v) & x.right).sum()) for v in (V0, V3)},
        "right": {
            V0: int(((x.only == V0) & x.right).sum() + ((x.only == V3) & ~x.right).sum()),
            V3: int(((x.only == V3) & x.right).sum() + ((x.only == V0) & ~x.right).sum()),
        },  # fmt: skip
        "by_kind": split(x, "kind"),
        "by_seen_v3": split(x, "seen_v3"),
        "by_history_v0": split(x, "history_v0"),
        "objects": int(x.object_id.nunique()),
        "top_objects": [{"object_id": int(o), **{k: int(v) for k, v in r.items()}} for o, r in obj.head(8).iterrows()],
    }
    return x, summary


def split(x: pd.DataFrame, col: str) -> dict:
    """Расхождения по группе: {группа: {<версия>_fault | <версия>_no_fault: решений с тревогой только у версии}}."""
    g = x.groupby([col, "only", "right"]).size().unstack(["only", "right"], fill_value=0)
    return {str(k): {f"{o}_{'fault' if r else 'no_fault'}": int(n) for (o, r), n in row.items()}
            for k, row in g.iterrows()}  # fmt: skip


def by_group(d: pd.DataFrame, thr: dict, col: str) -> dict:
    """P/R обеих моделей по группам (вид эпизода, история канала) на общих решениях."""
    out = {}
    for k, g in d.groupby(col):
        out[str(k)] = {v: counts(g.target.to_numpy(), g[f"score_{'v0' if v == V0 else 'v3'}"].to_numpy(), thr[v])
                       for v in thr}  # fmt: skip
    return out


def lead(d: pd.DataFrame, thr: dict) -> dict:
    """Упреждение верных тревог, минуты: решение одно на обе модели, поэтому у общих верных тревог оно одинаково."""
    assert (d.lead_hours_v0.fillna(-1) - d.lead_hours_v3.fillna(-1)).abs().max() < 1e-6, "упреждение разошлось"
    q = {"p10": 0.1, "p25": 0.25, "p50": 0.5, "p75": 0.75, "p90": 0.9, "max": 1.0}
    out = {}
    tp = {v: (d[f"score_{'v0' if v == V0 else 'v3'}"] >= thr[v]) & (d.target == 1) for v in thr}
    for v in thr:
        m = d.loc[tp[v], "lead_hours_v3"] * 60
        share = {"share_ge_10min": round(float((m >= 10).mean()), 3), "share_ge_1h": round(float((m >= 60).mean()), 3)}
        out[v] = {"tp": int(tp[v].sum()), **{k: round(float(m.quantile(p)), 1) for k, p in q.items()}, **share}
    for name, mask in (("only_v0", tp[V0] & ~tp[V3]), ("only_v3", tp[V3] & ~tp[V0])):
        m = d.loc[mask, "lead_hours_v3"] * 60
        out[name] = {"tp": int(mask.sum()), "p50": round(float(m.median()), 1) if len(m) else None}
    return out


def coverage_rule(ctx: dict, thr: dict) -> dict:
    """Цена правила v3 «за 8 суток до решения нет дня без данных» в 2026: тревоги и решения, которых без правила
    было бы больше, и что на них сделала v0."""
    ev, cov = ctx["events"], ctx["coverage"]
    gap_days = pd.date_range(cov.min(), cov.max()).difference(pd.DatetimeIndex(cov))
    alarms = ev[(ev.value == SMOKE) & (ev.ts >= "2026-01-01") & (ev.ts < "2026-07-01")][["sensor_id", "ts"]]
    alarms = alarms.drop_duplicates()
    day = (alarms.ts + pd.Timedelta(seconds=1)).dt.normalize()
    covered = set(pd.DatetimeIndex(cov))
    hit = day.map(lambda t: any(t - pd.Timedelta(days=k) not in covered for k in range(8)))
    full = pd.DatetimeIndex(sorted(covered | set(gap_days)))
    with_rule = build_alarm_rows(ev, cov, v3off.LABEL_END, FAULT_WASHOUT_HOURS)
    without = build_alarm_rows(ev, full, v3off.LABEL_END, FAULT_WASHOUT_HOURS)
    key = ["sensor_id", "decision_time"]
    w = with_rule[(with_rule.decision_time >= "2026-01-01")][key]
    wo = without[(without.decision_time >= "2026-01-01")]
    lost = wo.merge(w, on=key, how="left", indicator=True)
    lost = lost[lost._merge == "left_only"].rename(columns={"sensor_id": "channel_id"})
    gained = w.merge(wo[key], on=key, how="left", indicator=True)
    labelled = lost[lost.target.notna()]
    t0 = ctx["t0"][["channel_id", "decision_time", "score", "object_id"]]
    v0_on = labelled.merge(t0, on=["channel_id", "decision_time"])
    v0c = counts(v0_on.target.to_numpy(), v0_on.score.to_numpy(), thr[V0])
    ctx["lost"] = v0_on.rename(columns={"score": "score_v0"}).assign(score_v3=-1.0)  # v3 молчит — тревоги нет
    return {
        "gap_days": [str(t.date()) for t in gap_days],
        "alarms_2026": int(len(alarms)),
        "alarms_in_gap_window": int(hit.sum()),
        "alarm_days": sorted({str(t.date()) for t in day[hit]}),
        "decisions_lost": int(len(lost)),
        "decisions_lost_labelled": int(len(labelled)),
        "positives_lost": int((labelled.target == 1).sum()),
        "decisions_only_with_rule": int((gained._merge == "left_only").sum()),
        "v0_on_lost": v0c,
    }


def with_gap_week(both: pd.DataFrame, ctx: dict, thr: dict) -> dict:
    """P, R, AP обеих моделей на общих решениях плюс решениях v0 недели после пропуска журнала (v3 на них молчит)."""
    lost = ctx["lost"].assign(cluster=lambda d: d.object_id.astype(str) + "_" + d.decision_time.dt.date.astype(str))
    union = pd.concat([both[["target", "score_v0", "score_v3", "cluster"]], lost[["target", "score_v0", "score_v3",
                       "cluster"]]], ignore_index=True)  # fmt: skip
    return bootstrap(union, thr)


def v0_only(ctx: dict, both: pd.DataFrame, thr: dict) -> dict:
    """Решения проверки, допустимые только для v0: сколько, по дням, и как на них работает v0."""
    t0 = ctx["t0"]
    x = t0.merge(both[["channel_id", "decision_time"]], on=["channel_id", "decision_time"], how="left", indicator=True)
    x = x[x._merge == "left_only"]
    in_gap = x.decision_time.between("2026-06-02", "2026-06-09")
    after_test = x.decision_time + pd.Timedelta(days=1) > v3off.LABEL_END
    return {
        "decisions": int(len(x)),
        "positives": int((x.target == 1).sum()),
        "in_gap_window": int(in_gap.sum()),
        "after_v3_test_end": int(after_test.sum()),
        "v0": counts(x.target.to_numpy(), x.score.to_numpy(), thr[V0]),
    }


# ---------- графики


def _fmt(x: float) -> str:
    return f"{x * 100:.1f}%".replace(".", ",")


def fig_metrics(boot: dict, both: pd.DataFrame) -> list[Path]:
    """P, R, AP с 90% интервалами — обе модели на одних решениях."""
    style()
    fig, ax = plt.subplots(figsize=SLIDE)
    rows = [("P", "Precision\nдоля верных тревог"), ("R", "Recall\nдоля найденных\nнеисправностей"),
            ("AP", "AP\nкачество\nранжирования")]  # fmt: skip
    for i, (m, _) in enumerate(rows):
        yv = len(rows) - 1 - i
        for j, v in enumerate((V0, V3)):
            yy = yv + (0.14 if j == 0 else -0.14)
            lo, hi = boot[v][m]["ci90"]
            ax.plot([lo, hi], [yy, yy], color=COLOR[v], lw=3, solid_capstyle="round", zorder=2)
            dot(ax, boot[v][m]["value"], yy, COLOR[v])
            label = f"{boot[v][m]['value']:.3f}".replace(".", ",") if m == "AP" else _fmt(boot[v][m]["value"])
            ax.text(hi + 0.008, yy, label, va="center", ha="left", fontsize=16, color=INK)
    ax.axvspan(REQ_P, 1.0, ymin=2 / 3 + 0.02, ymax=0.98, color=ZONE, lw=0, zorder=0)
    ax.axvspan(REQ_R, 1.0, ymin=1 / 3 + 0.02, ymax=2 / 3 - 0.02, color=ZONE, lw=0, zorder=0)
    ax.text(0.995, 2.42, "ТЗ: P > 70%", ha="right", va="top", color=INK2, fontsize=15)
    ax.text(0.995, 1.42, "ТЗ: R > 50%", ha="right", va="top", color=INK2, fontsize=15)
    ax.set_yticks(range(len(rows)), [r[1] for r in rows[::-1]])
    ax.set_ylim(-0.6, len(rows) - 0.4)
    ax.set_xlim(0.45, 1.0)
    ax.xaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
    ax.grid(axis="y", visible=False)
    ax.legend(handles=[Patch(color=COLOR[v], label=NAME[v]) for v in (V0, V3)], loc="lower left", ncols=2)
    c = boot["clusters"]
    header(fig, "На одних решениях: v3 точнее, v0 находит больше и лучше ранжирует",
           f"I полугодие 2026 · {n_ru(len(both))} решений, {int(both.target.sum())} неисправностей · точка — значение, "
           f"линия — 90% интервал (бутстрап по {c['object_days']} объекто-суткам)")  # fmt: skip
    return v3off_save(fig, "01_metrics")


def fig_disagree(x: pd.DataFrame, total: int) -> list[Path]:
    """Решения, где тревога только у одной модели: по виду эпизода, была ли неисправность."""
    style()
    fig, axes = plt.subplots(1, 2, figsize=SLIDE, sharey=True)
    kinds = [k for k in ("ppr", "group", "solitary", "unknown") if (x.kind == k).any()]
    for ax, v in zip(axes, (V0, V3), strict=True):
        g = x[x.only == v]
        right = [int(((g.kind == k) & g.right).sum()) for k in kinds]
        wrong = [int(((g.kind == k) & ~g.right).sum()) for k in kinds]
        yv = np.arange(len(kinds))[::-1]
        ax.barh(yv, right, height=0.55, color=COLOR[v], edgecolor=SURFACE, linewidth=2, zorder=2)
        ax.barh(yv, wrong, left=right, height=0.55, color=SURFACE, edgecolor=COLOR[v], linewidth=2, hatch="//",
                zorder=2)  # fmt: skip
        for yy, r, w in zip(yv, right, wrong, strict=True):
            text = f"верных: {r} · ложных: {w}" if r + w else "нет"
            ax.text(r + w + 0.6, yy, text, va="center", ha="left", fontsize=15, color=INK if r + w else MUTED)
        ax.set_title(f"Тревога только у {NAME[v]} — {len(g)}", color=INK)
        ax.set_yticks(yv, [KIND_RU[k] for k in kinds])
        ax.set_xlim(0, max(len(x[x.only == V0]), 1) * 0.9 + 12)
        ax.grid(axis="y", visible=False)
        ax.set_xlabel("решений")
    keys = [
        Patch(color=MUTED, label="неисправность была — тревога верна"),
        Patch(facecolor=SURFACE, edgecolor=MUTED, hatch="//", label="неисправности не было — тревога ложна"),
    ]
    fig.legend(handles=keys, loc="lower center", ncols=2, bbox_to_anchor=(0.5, -0.06))
    r0 = int(((x.only == V0) & x.right).sum() + ((x.only == V3) & ~x.right).sum())
    header(fig, f"Модели расходятся на {len(x)} решениях из {n_ru(total)}",
           f"Права по исходу: v0 — в {r0}, v3 — в {len(x) - r0} · вид эпизода тревоги — из рабочей базы")  # fmt: skip
    return v3off_save(fig, "02_disagreements")


def fig_pr(both: pd.DataFrame, thr: dict) -> list[Path]:
    """PR-кривые обеих моделей на общих решениях и их рабочие точки."""
    style()
    fig, ax = plt.subplots(figsize=SLIDE)
    ax.add_patch(Rectangle((REQ_R, REQ_P), 1 - REQ_R, 1 - REQ_P, facecolor=ZONE, edgecolor="none", zorder=0))
    ax.text(0.985, 0.985, "требование ТЗ:\nP > 70% и R > 50%", ha="right", va="top", color=INK2, fontsize=16)
    y = both.target.to_numpy()
    for v in (V0, V3):
        s = both[f"score_{'v0' if v == V0 else 'v3'}"].to_numpy()
        p, r, _ = precision_recall_curve(y, s)
        ax.plot(r, p, color=COLOR[v], lw=2.5, zorder=3, label=f"{NAME[v]}, AP {average_precision_score(y, s):.3f}")
        c = counts(y, s, thr[v])
        dot(ax, c["recall"], c["precision"], COLOR[v])
        yl = 0.70 if v == V0 else 0.62  # подписи рабочих точек — в пустой области под кривыми, точка-ключ рядом
        dot(ax, 0.22, yl, COLOR[v])
        ax.text(0.24, yl, f"рабочая точка {NAME[v].split()[0]}: P {pct(c['precision'])}, R {pct(c['recall'])}",
                va="center", ha="left", fontsize=16, color=INK)  # fmt: skip
    ax.set_xlim(0, 1.01)
    ax.set_ylim(0, 1.01)
    ax.xaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
    ax.yaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
    ax.set_xlabel("Recall — доля найденных неисправностей")
    ax.set_ylabel("Precision — доля верных тревог")
    ax.legend(loc="lower left")
    header(fig, "PR-кривые на одних решениях: v0 ранжирует чуть лучше",
           f"I полугодие 2026 · {n_ru(len(both))} решений, допустимых для обеих · каждая на своём пороге")  # fmt: skip
    return v3off_save(fig, "03_pr_curves")


def v3off_save(fig, name: str) -> list[Path]:
    out = OUT / "figures"
    out.mkdir(parents=True, exist_ok=True)
    paths = [out / f"{name}.png", out / f"{name}.svg"]
    fig.savefig(paths[0], dpi=150, bbox_inches="tight", pad_inches=0.3, metadata={"Software": None})
    fig.savefig(paths[1], bbox_inches="tight", pad_inches=0.3, metadata={"Date": None, "Creator": None})
    plt.close(fig)
    return paths


def _r(x):
    return None if x is None else round(float(x), 4)


def enrich_compare(m: dict, path: Path = v3off.COMPARE) -> dict:
    """Дописать в файл сравнения версий итоги metrics.json — поля для интерфейса (все необязательные):
    `ci`, `diff`, `clusters`, `by_kind`, `disagree.alert_only_fault`, `disagree.by_kind`, `coverage`."""
    c = json.loads(path.read_text(encoding="utf-8"))
    b, keys = m["metrics"], {"precision": "P", "recall": "R", "ap": "AP"}
    c["ci"] = {v: {k: [_r(x) for x in b[v][s]["ci90"]] for k, s in keys.items()} for v in (V0, V3)}
    d = b["diff_v3_minus_v0"]
    c["diff"] = {"a": V3, "b": V0, **{k: {"value": _r(d[s]["value"]), "ci90": [_r(x) for x in d[s]["ci90"]]}
                                     for k, s in keys.items()}}  # fmt: skip
    c["clusters"] = {k: b["clusters"][k] for k in ("object_days", "with_faults")}
    cols = ("decisions", "positives", "alerts", "tp", "fp", "fn", "precision", "recall")
    c["by_kind"] = {kind: {v: {k: _r(r[v][k]) if k in ("precision", "recall") else r[v][k] for k in cols} for v in r}
                    for kind, r in m["by_kind"].items()}  # fmt: skip
    dis = m["disagreements"]
    c["disagree"]["alert_only_fault"] = dis["alert_only_fault"]
    c["disagree"]["by_kind"] = {
        kind: {v: {"fault": g.get(f"{v}_fault", 0), "no_fault": g.get(f"{v}_no_fault", 0)} for v in (V0, V3)}
        for kind, g in dis["by_kind"].items()
    }
    cov, lo, hi = m["coverage_rule"], *v3off.TEST
    others = {k: _r(val) if k in ("precision", "recall") else val for k, val in cov["v0_on_lost"].items()
              if k not in ("decisions", "positives")}  # fmt: skip
    c["coverage"] = {
        "version": V3,
        "silent_days": 8,  # правило v3: за 8 суток до решения нет дня без данных журнала
        "gap_days": [d for d in cov["gap_days"] if lo <= d < hi],
        "from": cov["alarm_days"][0],
        "to": cov["alarm_days"][-1],
        "decisions": cov["decisions_lost"],
        "positives": cov["positives_lost"],
        "others": {V0: others},
    }
    path.write_text(json.dumps(c, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return c


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", type=Path, default=v3off.ROOT / "data" / "interim" / "v3-check")
    ap.add_argument("--archive", type=Path, default=v3off.ROOT / "data" / "archive")
    ap.add_argument(
        "--raw-registry", type=Path, default=v3off.ROOT / "data" / "raw" / "справочник_каналов_датчиков.csv"
    )
    ap.add_argument("--db", default="postgresql+psycopg://lct:lct@localhost:15432/lct")
    ap.add_argument("--only-compare", action="store_true", help="только дописать поля в compare_smoke_alarm.json")
    a = ap.parse_args()
    if a.only_compare:
        enrich_compare(json.loads((OUT / "metrics.json").read_text(encoding="utf-8")))
        print(v3off.COMPARE.relative_to(v3off.ROOT))
        return
    both, thr, ctx = decisions(a.work, a.db, a.archive, a.raw_registry)
    boot = bootstrap(both, thr)
    x, dis = disagreements(both, thr)
    out = {
        "period": {
            "from": v3off.TEST[0],
            "to": v3off.TEST[1],
            "label": "I полугодие 2026, решения, допустимые для обеих",
        },
        "thresholds": thr,
        "metrics": boot,
        "by_kind": by_group(both, thr, "kind"),
        "by_seen_v3": by_group(both, thr, "seen_v3"),
        "by_history_v0": by_group(both, thr, "history_v0"),
        "disagreements": dis,
        "lead_minutes": lead(both, thr),
        "coverage_rule": coverage_rule(ctx, thr),
        # как если бы модель работала основной всё полугодие: к общим решениям — неделя после пропуска журнала,
        # где v0 решает, а v3 молчит по своему правилу (её молчание — пропущенная неисправность)
        "with_gap_week": with_gap_week(both, ctx, thr),
        "v0_only": v0_only(ctx, both, thr),
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "metrics.json").write_text(json.dumps(out, ensure_ascii=False, indent=1, default=float) + "\n",
                                      encoding="utf-8")  # fmt: skip
    for f in (fig_metrics(boot, both), fig_disagree(x, len(both)), fig_pr(both, thr)):
        print(f[0].relative_to(v3off.ROOT))
    enrich_compare(json.loads(json.dumps(out, default=float)))
    print(json.dumps({k: v for k, v in out.items() if k not in ("by_kind", "by_seen_v3", "by_history_v0")},
                     ensure_ascii=False, indent=1, default=float))  # fmt: skip


if __name__ == "__main__":
    main()
