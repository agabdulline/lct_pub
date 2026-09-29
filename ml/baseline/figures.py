"""Графики базовых моделей v0 для презентации (12.2): `ml/reports/baseline-v0/figures/*.png` и `*.svg`.

Данные — те же решения, что у офлайн-метрик (`ml.baseline.offline`): событийная модель — из архива, ежедневная —
из рабочей базы, вид эпизода тревоги — из рабочей базы. Интервалы — кластерный бутстрап, 90%: у событийной модели
кластер — объект × сутки, у ежедневной — канал. Первый запуск ~3 мин, решения кэшируются в `data/interim/figures`.

Запуск из ml-service: `PYTHONPATH=.. uv run --with matplotlib python -m ml.baseline.figures [--refresh]`.
"""

import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402
from matplotlib.ticker import PercentFormatter  # noqa: E402
from sklearn.metrics import precision_recall_curve  # noqa: E402

from ml.baseline.offline import ROOT, daily_decisions, load_model, smoke_decisions  # noqa: E402

OUT = ROOT / "ml" / "reports" / "baseline-v0" / "figures"
CACHE = ROOT / "data" / "interim" / "figures"
TEST = ("2026-01-01", "2026-07-01")
REQ_P, REQ_R = 0.7, 0.5
SLIDE = (13.33, 7.5)  # 16:9

# Светлая тема для проектора: цвета серий — слоты 1–2 проверенной категориальной палитры, текст — нейтральные тона
SURFACE, INK, INK2, MUTED = "#fcfcfb", "#0b0b0b", "#52514e", "#898781"
GRID, AXIS, ZONE = "#e1e0d9", "#c3c2b7", "#eceae4"
SMOKE_C, DAILY_C = "#2a78d6", "#eb6834"  # событийная — синий, ежедневная — оранжевый, на всех графиках
SENSOR_RU = {
    "Состояние насоса": "Насосы",
    "Состояние вентилятора": "Вентиляторы",
    "Состояние фазы": "Фаза",
    "Датчик дыма": "Дым",
    "Ручной извещатель": "Ручные извещатели",
    "Газовый датчик": "Газ",
    "ИБП": "ИБП",
    "Датчик температуры": "Температура",
}


def style() -> None:
    plt.rcParams.update(
        {
            "figure.facecolor": SURFACE,
            "axes.facecolor": SURFACE,
            "savefig.facecolor": SURFACE,
            "font.family": ["Arial", "DejaVu Sans"],
            "font.size": 18,
            "axes.titlesize": 20,
            "axes.titleweight": "bold",
            "axes.titlelocation": "left",
            "axes.labelsize": 18,
            "axes.labelcolor": INK2,
            "axes.edgecolor": AXIS,
            "axes.linewidth": 1,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.grid": True,
            "grid.color": GRID,
            "grid.linewidth": 1,
            "grid.linestyle": "-",
            "xtick.color": AXIS,
            "ytick.color": AXIS,
            "xtick.labelcolor": INK2,
            "ytick.labelcolor": INK2,
            "xtick.labelsize": 16,
            "ytick.labelsize": 16,
            "legend.fontsize": 16,
            "legend.frameon": False,
            "text.color": INK,
            "svg.fonttype": "path",
        }
    )


def header(fig, title: str, sub: str, top: float = 0.84) -> None:
    """Заголовок и подзаголовок слайда над областью графиков — без наложений на заголовки панелей."""
    fig.tight_layout(rect=(0, 0, 1, top))
    fig.text(0.01, 0.985, title, fontsize=26, weight="bold", color=INK, ha="left", va="top")
    fig.text(0.01, 0.915, sub, fontsize=17, color=INK2, ha="left", va="top")


def save(fig, name: str) -> list[Path]:
    OUT.mkdir(parents=True, exist_ok=True)
    paths = [OUT / f"{name}.png", OUT / f"{name}.svg"]
    fig.savefig(paths[0], dpi=150, bbox_inches="tight", pad_inches=0.3, metadata={"Software": None})
    fig.savefig(paths[1], bbox_inches="tight", pad_inches=0.3, metadata={"Date": None, "Creator": None})
    plt.close(fig)
    return paths


def pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def n_ru(n: int) -> str:
    return f"{n:,}".replace(",", " ")


# ---------- данные


def load(refresh: bool, db: str, archive: Path) -> tuple[pd.DataFrame, float, pd.DataFrame, float]:
    """Решения обеих моделей 2025–2026 с оценками — из кэша или заново."""
    CACHE.mkdir(parents=True, exist_ok=True)
    smoke_path, daily_path = CACHE / "smoke.parquet", CACHE / "daily.parquet"
    build_cache = CACHE / "smoke_build.parquet"
    if refresh:
        for p in (smoke_path, daily_path, build_cache):
            p.unlink(missing_ok=True)
    if not smoke_path.exists():
        d, _, _ = smoke_decisions(archive, build_cache, db)
        cols = ["decision_time", "quarter", "object_id", "channel_id", "target", "score", "lead_hours", "kind"]
        d.loc[d.decision_time >= "2025-01-01", cols].to_parquet(smoke_path)
    if not daily_path.exists():
        d, _, _, _ = daily_decisions(db)
        d.drop(columns=["onsets_30d"]).to_parquet(daily_path)
    _, smoke_cfg, _ = load_model("baseline-v0", "smoke_alarm")
    _, daily_cfg, _ = load_model("baseline-v0-daily", "daily")
    return pd.read_parquet(smoke_path), smoke_cfg["threshold"], pd.read_parquet(daily_path), daily_cfg["threshold"]


def test_part(d: pd.DataFrame, col: str) -> pd.DataFrame:
    return d[(d[col] >= TEST[0]) & (d[col] < TEST[1])]


def pr_ci(d: pd.DataFrame, thr: float, cluster: pd.Series, n: int = 2000, seed: int = 42) -> dict:
    """Precision и Recall с 90% интервалом кластерного бутстрапа."""
    pred = d["score"].to_numpy() >= thr
    y = d["target"].to_numpy() == 1
    agg = pd.DataFrame({"tp": pred & y, "fp": pred & ~y, "pos": y}).groupby(cluster.to_numpy()).sum().to_numpy()
    tp, fp, pos = agg.sum(0)
    s = agg[np.random.default_rng(seed).integers(0, len(agg), (n, len(agg)))].sum(1)  # n × (tp, fp, pos)
    with np.errstate(invalid="ignore", divide="ignore"):
        ps, rs = s[:, 0] / (s[:, 0] + s[:, 1]), s[:, 0] / s[:, 2]
    return {
        "n": len(d),
        "pos": int(pos),
        "P": tp / (tp + fp) if tp + fp else np.nan,
        "R": tp / pos if pos else np.nan,
        "P_ci": np.nanquantile(ps, [0.05, 0.95]),
        "R_ci": np.nanquantile(rs, [0.05, 0.95]),
    }


def smoke_cluster(d: pd.DataFrame) -> pd.Series:
    return d["object_id"].astype(str) + "_" + d["decision_time"].dt.date.astype(str)


def dot(ax, x, y, color, **kw) -> None:
    """Точка ≥ 8 px с кольцом цвета фона — видна поверх линий и интервалов."""
    ax.plot(x, y, "o", ms=13, color=color, mec=SURFACE, mew=2.5, zorder=4, **kw)


# ---------- графики


def fig_pr_curves(smoke, smoke_thr, daily, daily_thr) -> list[Path]:
    fig, ax = plt.subplots(figsize=SLIDE)
    ax.add_patch(Rectangle((REQ_R, REQ_P), 1 - REQ_R, 1 - REQ_P, facecolor=ZONE, edgecolor="none", zorder=0))
    ax.text(0.985, 0.985, "требование ТЗ:\nP > 70% и R > 50%", ha="right", va="top", color=INK2, fontsize=16)
    for d, thr, color, name in (
        (test_part(smoke, "decision_time"), smoke_thr, SMOKE_C, "Событийная: отказ за 24 ч после дымовой тревоги"),
        (test_part(daily, "date"), daily_thr, DAILY_C, "Ежедневная: отказ через 1–30 суток, 8 типов"),
    ):
        y, s = d["target"].to_numpy(), d["score"].to_numpy()
        p, r, _ = precision_recall_curve(y, s)
        ax.plot(r, p, color=color, lw=2.5, label=name, zorder=3)
        pred = s >= thr
        tp = (pred & (y == 1)).sum()
        wp, wr = tp / pred.sum(), tp / (y == 1).sum()
        dot(ax, wr, wp, color)
        ax.annotate(
            f"рабочая точка\nP {pct(wp)}, R {pct(wr)}",
            (wr, wp),
            xytext=(-18, -14),
            textcoords="offset points",
            ha="right",
            va="top",
            fontsize=16,
        )
    ax.set(xlim=(0, 1), ylim=(0, 1.0))
    ax.xaxis.set_major_formatter(PercentFormatter(1))
    ax.yaxis.set_major_formatter(PercentFormatter(1))
    ax.set_xlabel("Recall — доля пойманных отказов")
    ax.set_ylabel("Precision — доля верных тревог")
    ax.legend(loc="lower left")
    header(
        fig,
        "Точность и полнота базовых моделей на проверке 2026 года",
        "Кривая — все пороги модели; событийная — I полугодие 2026, ежедневная — 1 января — 2 мая 2026",
    )
    return save(fig, "01_pr_curves")


def fig_episode_kinds(smoke, thr) -> list[Path]:
    te = test_part(smoke, "decision_time")
    parts = [
        ("Все решения", te),
        ("Внутри плановых проверок", te[te["kind"] == "ppr"]),
        ("Вне плановых проверок", te[te["kind"] != "ppr"]),
    ]
    stats = [(name, pr_ci(d, thr, smoke_cluster(d))) for name, d in parts]
    lo_all = min(min(st["P_ci"][0], st["R_ci"][0]) for _, st in stats)
    xmin = min(0.4, np.floor(lo_all * 10) / 10)
    fig, axes = plt.subplots(1, 2, figsize=(SLIDE[0], 6.6), sharey=True)
    ypos = np.arange(len(stats))[::-1]
    for ax, key, req, label in ((axes[0], "P", REQ_P, "Precision"), (axes[1], "R", REQ_R, "Recall")):
        ax.axvspan(req, 1.0, color=ZONE, zorder=0, lw=0)
        ax.text(0.99, len(stats) - 0.45, f"ТЗ: > {pct(req)}", color=INK2, fontsize=15, va="bottom", ha="right")
        for yv, (_, st) in zip(ypos, stats, strict=True):
            lo, hi = st[f"{key}_ci"]
            ax.plot([lo, hi], [yv, yv], color=SMOKE_C, lw=3, solid_capstyle="round", zorder=2)
            dot(ax, st[key], yv, SMOKE_C)
            ax.text(st[key], yv + 0.2, pct(st[key]), ha="center", va="bottom", fontsize=18, weight="bold")
        ax.set_xlim(xmin, 1.0)
        ax.set_ylim(-0.6, len(stats) - 0.1)
        ax.xaxis.set_major_formatter(PercentFormatter(1))
        ax.grid(axis="y", visible=False)
        ax.set_xlabel(label)
    axes[0].set_yticks(ypos)
    axes[0].set_yticklabels([f"{name}\n{n_ru(st['n'])} решений · {n_ru(st['pos'])} отказов" for name, st in stats])
    axes[0].tick_params(axis="y", length=0, labelsize=16)
    axes[1].tick_params(axis="y", length=0)
    header(
        fig,
        "Событийная модель: плановые проверки и остальные тревоги",
        "Проверка 2026 года; точка — значение, отрезок — 90% интервал бутстрапа по объекто-суткам",
        top=0.82,
    )
    return save(fig, "02_episode_kinds")


def fig_quarters(smoke, smoke_thr, daily, daily_thr) -> list[Path]:
    quarters = ["2025Q1", "2025Q2", "2025Q3", "2025Q4", "2026Q1", "2026Q2"]
    labels = ["I кв.\n2025", "II кв.\n2025", "III кв.\n2025", "IV кв.\n2025", "I кв.\n2026", "II кв.\n2026"]
    series = []
    for d, thr, color, name, cluster in (
        (smoke, smoke_thr, SMOKE_C, "Событийная", smoke_cluster),
        (daily, daily_thr, DAILY_C, "Ежедневная", lambda g: g["channel_id"]),
    ):
        st = [pr_ci(g, thr, cluster(g), n=1000) for _, g in d[d["quarter"].isin(quarters)].groupby("quarter")]
        series.append((name, color, st))
    fig, axes = plt.subplots(1, 2, figsize=(SLIDE[0], 7.8), sharey=True)
    x = np.arange(len(quarters))
    for ax, key, req, label in ((axes[0], "P", REQ_P, "Precision"), (axes[1], "R", REQ_R, "Recall")):
        ax.axhspan(req, 1.0, color=ZONE, zorder=0, lw=0)
        ax.text(-0.42, 0.985, f"ТЗ: > {pct(req)}", color=INK2, fontsize=15, va="top")
        for k, (name, color, st) in enumerate(series):
            dx = (k - 0.5) * 0.2
            v = np.array([s[key] for s in st])
            ci = np.array([s[f"{key}_ci"] for s in st])
            ax.vlines(x + dx, ci[:, 0], ci[:, 1], color=color, lw=3, zorder=2)
            ax.plot(x + dx, v, "-", color=color, lw=1.5, alpha=0.45, zorder=2)
            dot(ax, x + dx, v, color, label=name)
        for b in (1.5, 3.5):
            ax.axvline(b, color=AXIS, lw=1, zorder=1)
        for xc, role in ((0.5, "ранняя остановка"), (2.5, "подбор порога"), (4.5, "проверка")):
            ax.text(xc, 1.015, role, ha="center", va="bottom", fontsize=15, color=INK2)
        ax.set_xticks(x)
        ax.set_xticklabels(labels, fontsize=15)
        ax.set_xlim(-0.5, len(quarters) - 0.5)
        ax.set_ylim(0, 1.0)
        ax.yaxis.set_major_formatter(PercentFormatter(1))
        ax.grid(axis="x", visible=False)
        ax.set_title(label, pad=34)
    dip = series[0][2][1]  # событийная, II кв. 2025
    axes[1].annotate(
        "три плановые\nпроверки в июне",
        (1 - 0.1, dip["R"]),
        xytext=(1.35, 0.03),
        fontsize=15,
        arrowprops={"arrowstyle": "-", "color": INK2, "lw": 1},
    )
    axes[0].legend(loc="lower left", ncol=2, handletextpad=0.2, columnspacing=1.2)
    header(
        fig,
        "По кварталам: провал событийной модели во II квартале 2025",
        "Точка — значение, отрезок — 90% интервал бутстрапа; кварталы 2025 участвовали в подборе модели",
        top=0.86,
    )
    return save(fig, "03_quarters")


def fig_lead_time(smoke, smoke_thr, daily, daily_thr) -> list[Path]:
    s = test_part(smoke, "decision_time")
    s_lead = s.loc[(s["score"] >= smoke_thr) & (s["target"] == 1), "lead_hours"].to_numpy() * 60
    d = test_part(daily, "date")
    d_lead = d.loc[(d["score"] >= daily_thr) & (d["target"] == 1), "lead_days"].to_numpy()
    cap = 15
    fig, axes = plt.subplots(1, 2, figsize=SLIDE)
    for ax, v, bins, color, med, unit, label, xlabel in (
        (axes[0], np.minimum(s_lead, cap + 0.5), np.arange(0, cap + 1.5), SMOKE_C, np.median(s_lead), "мин",
         "Событийная", "минуты"),
        (axes[1], d_lead, np.arange(1, 31), DAILY_C, np.median(d_lead), "сут", "Ежедневная", "сутки"),
    ):  # fmt: skip
        ax.hist(v, bins=bins, color=color, rwidth=0.85, zorder=2)
        ax.axvline(med, color=INK, lw=1.5, zorder=3)
        ax.text(med, ax.get_ylim()[1] * 0.98, f"  медиана {med:.1f} {unit}".replace(".", ","), va="top", fontsize=17)
        ax.set_title(label)
        ax.set_xlabel(f"от прогноза до начала «Неисправен», {xlabel}")
        ax.grid(axis="x", visible=False)
    axes[0].set_ylabel("верных тревог")
    axes[0].set_xticks([0, 5, 10, 15.5])  # границы интервалов; последний столбец — все дольше 15 мин
    axes[0].set_xticklabels(["0", "5", "10", "> 15"])
    over = pct((s_lead > cap).mean())
    axes[0].text(0.98, 0.7, f"дольше 15 мин — {over}", transform=axes[0].transAxes, ha="right", fontsize=15, color=INK2)
    header(fig, "Упреждение: событийная — минуты, ежедневная — сутки", "Верные тревоги на проверке 2026 года")
    return save(fig, "04_lead_time")


def fig_daily_types(daily, thr) -> list[Path]:
    te = test_part(daily, "date")
    rows = []
    for st, g in te.groupby("sensor_type"):
        pred, y = g["score"].to_numpy() >= thr, g["target"].to_numpy() == 1
        tp, alerts, pos = int((pred & y).sum()), int(pred.sum()), int(y.sum())
        rows.append({"name": SENSOR_RU.get(st, st), "R": tp / pos, "tp": tp, "alerts": alerts, "pos": pos,
                     "base": pos / len(g)})  # fmt: skip
    rows.sort(key=lambda r: (-r["R"], -r["pos"]))
    fig, ax = plt.subplots(figsize=SLIDE)
    ypos = np.arange(len(rows))[::-1]
    for yv, r in zip(ypos, rows, strict=True):
        if r["alerts"] >= 10:
            ax.barh(yv, r["R"], height=0.5, color=DAILY_C, zorder=2)
            text = f"R {pct(r['R'])} · P {pct(r['tp'] / r['alerts'])} · доля отказов {pct(r['base'])}"
            ax.text(r["R"] + 0.015, yv, text, va="center", fontsize=16)
        else:
            text = "прогноза нет" if r["alerts"] == 0 else f"прогноза нет — {r['alerts']} тревоги, верных {r['tp']}"
            ax.text(0.015, yv, text, va="center", fontsize=16, color=MUTED)
    ax.set_yticks(ypos)
    ax.set_ylim(-0.7, len(rows) - 0.3)
    ax.set_yticklabels([f"{r['name']} · {n_ru(r['pos'])} отказов" for r in rows], fontsize=17)
    ax.tick_params(axis="y", length=0)
    ax.set_xlim(0, 1.45)
    ax.set_xticks([0, 0.25, 0.5, 0.75, 1.0])
    ax.spines["bottom"].set_bounds(0, 1)
    ax.spines["left"].set_visible(False)
    ax.xaxis.set_major_formatter(PercentFormatter(1))
    ax.set_xlabel("Recall — доля пойманных отказов", loc="left")
    ax.grid(axis="y", visible=False)
    header(
        fig,
        "Ежедневная модель по типам: работает на насосах, вентиляторах и фазе",
        "Проверка 1 января — 2 мая 2026. У насосов отказ в 81% суток — модель близка к «тревоге всегда»",
    )
    return save(fig, "05_daily_by_type")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="пересобрать решения, не брать из кэша")
    ap.add_argument("--db", default="postgresql+psycopg://lct:lct@localhost:15432/lct")
    ap.add_argument("--archive", type=Path, default=ROOT / "data" / "archive")
    a = ap.parse_args()
    style()
    smoke, smoke_thr, daily, daily_thr = load(a.refresh, a.db, a.archive)
    paths = [
        *fig_pr_curves(smoke, smoke_thr, daily, daily_thr),
        *fig_episode_kinds(smoke, smoke_thr),
        *fig_quarters(smoke, smoke_thr, daily, daily_thr),
        *fig_lead_time(smoke, smoke_thr, daily, daily_thr),
        *fig_daily_types(daily, daily_thr),
    ]
    for p in paths:
        print(p.relative_to(ROOT))


if __name__ == "__main__":
    main()
