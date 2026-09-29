"""Fixed calendar ablation and type-specific thresholds; freeze before test scoring."""

import argparse
import gc
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from urllib.request import urlopen

import numpy as np
import pandas as pd
from catboost import CatBoostClassifier
from ml.baseline.daily import FEATURES, build_daily
from ml.each_channel.features import TYPE_KEYS, build_features, load_inputs
from ml.each_channel.train import assess, choose_threshold, split, write_json
from ml.remaining_channels.run import KEYS

CALENDAR = ["ru_day_off", "ru_short_day", "ru_off_prev1", "ru_off_prev7", "ru_off_prev30"]
PARAMS = dict(
    iterations=180,
    depth=5,
    learning_rate=0.05,
    l2_leaf_reg=20,
    loss_function="Logloss",
    random_seed=42,
    thread_count=6,
    has_time=True,
    cat_features=["sensor_type"],
    verbose=False,
    allow_writing_files=False,
)


def calendar_features(folder, download=False):
    blocks, sources = [], []
    folder.mkdir(parents=True, exist_ok=True)
    for year in range(2022, 2027):
        path = folder / f"ru-{year}.json"
        url = f"https://xmlcalendar.ru/data/ru/{year}/calendar.json"
        if download and not path.exists():
            with urlopen(url, timeout=30) as response:
                path.write_bytes(response.read())
        raw = path.read_bytes()
        obj = json.loads(raw)
        if obj["year"] != year or len(obj["months"]) != 12:
            raise ValueError("Incomplete calendar")
        days = pd.date_range(f"{year}-01-01", f"{year}-12-31")
        data = pd.DataFrame({"ru_day_off": 0, "ru_short_day": 0}, index=days)
        for month in obj["months"]:
            for token in month["days"].split(","):
                if not re.fullmatch(r"\d{1,2}[+*]?", token):
                    raise ValueError(f"Unsupported calendar day {token!r}")
                d = pd.Timestamp(year, month["month"], int(token.rstrip("*+")))
                data.loc[d, "ru_short_day" if token.endswith("*") else "ru_day_off"] = 1
        if int(data.ru_day_off.sum()) != obj["statistic"]["holidays"]:
            raise ValueError("Calendar total disagrees with source")
        blocks.append(data)
        sources.append({"url": url, "sha256": hashlib.sha256(raw).hexdigest()})
    result = pd.concat(blocks)
    result["ru_off_prev1"] = result.ru_day_off.shift(1)
    for w in (7, 30):
        result[f"ru_off_prev{w}"] = result.ru_day_off.shift(1).rolling(w).sum()
    result.index.name = "date"
    return result.reset_index(), sources


def read_model(folder, filename="model.cbm"):
    c = json.loads((folder / "config.json").read_text(encoding="utf-8"))
    if c.get("status") == "insufficient_classes":
        return c, None
    path = folder / filename
    if hashlib.sha256(path.read_bytes()).hexdigest() != c["sha256"]:
        raise ValueError(f"SHA256 mismatch: {path}")
    model = CatBoostClassifier().load_model(str(path))
    names = c.get("selected", {}).get("features", c.get("features"))
    if model.feature_names_ != names:
        raise ValueError("Feature schema mismatch")
    return c, model


def baseline_rows(daily, episodes, channels, dates, ids, types):
    """Preserve v0's feature definitions and eight-type object context."""
    ch = channels.loc[channels.sensor_type.isin(types)]
    eps = episodes.loc[episodes.channel_id.isin(ch.channel_id)]
    base = build_daily(daily, eps, ch, dates, only=ids)
    # The live loader excludes episode starts at T; remove only that boundary contribution.
    exact = eps.loc[eps.start.isin(dates), ["channel_id", "start"]].merge(
        ch[["channel_id", "object_id"]], on="channel_id"
    )
    boundary = exact.groupby(["object_id", "start"]).size()
    obj = base.channel_id.map(ch.set_index("channel_id").object_id)
    lookup = pd.MultiIndex.from_arrays([obj, base.date])
    base["obj_in_fault"] -= boundary.reindex(lookup, fill_value=0).to_numpy()
    return base


def rank(metrics):
    return (
        int(metrics["passes_both"]),
        min((metrics["precision"] or 0) / 0.7, (metrics["recall"] or 0) / 0.5),
        metrics["ap"] or 0,
    )


def subset(frame, lo, hi):
    return frame.loc[
        frame.date.ge(lo)
        & (frame.date + pd.Timedelta(days=30)).le(hi)
        & frame.status.eq("scored")
        & frame.target.notna()
    ].copy()


def summarize(frame, scores, threshold, seen):
    result = assess(frame, scores, threshold)
    masks = frame.channel_id.isin(seen).to_numpy()
    result["by_history"] = {
        label: assess(frame.loc[mask], scores[mask], threshold)
        for label, mask in (("with_history", masks), ("without_history", ~masks))
    }
    result["by_month"] = {
        str(month): assess(group, scores[frame.index.get_indexer(group.index)], threshold)
        for month, group in frame.groupby(frame.date.dt.to_period("M"))
    }
    return result


def fit(args):
    if (args.out / "protocol.json").exists():
        raise ValueError("Use a new output directory; protocol is already frozen")
    channels, daily, episodes = load_inputs(args.data_dir)
    calendar, sources = calendar_features(args.calendar_dir)
    types = {**TYPE_KEYS, **KEYS}
    if set(types) != set(channels.sensor_type):
        raise ValueError("Unexpected catalog types")
    vcfg, vmodel = read_model(args.baseline_dir, "daily.cbm")
    write_json(
        args.out / "protocol.json",
        {
            "parameters": PARAMS,
            "features": FEATURES,
            "calendar_features": CALENDAR,
            "sources": sources,
            "v0_sha256": vcfg["sha256"],
            "train": "Mondays 2022-2024 plus every eligible day H1 2025; 30-day purge",
            "selection": "Q3 2025; max(passes_both, min(P/.7,R/.5), AP)",
            "threshold": "Q4 2025; min 10 alerts; fixed before test",
            "diagnostic_fold": "train 2022-2024, calibrate Q1 2025, audit Q2 2025",
            "test": "2026-01-01 through 2026-05-02; previously viewed",
            "source_manifest": json.loads(
                (args.data_dir / "manifest.json").read_text(encoding="utf-8")
            ),
            "source_hashes": {
                p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in args.data_dir.glob("*.parquet")
                if p.name in ("sensor_daily.parquet", "fault_episodes.parquet")
            },
        },
    )
    dates = pd.date_range("2022-01-03", "2024-12-30", freq="W-MON").union(
        pd.date_range("2025-01-01", "2026-05-02")
    )
    for name, key in types.items():
        started = time.perf_counter()
        print(f"BUILD {key}", flush=True)
        ids = channels.loc[channels.sensor_type.eq(name), "channel_id"].tolist()
        cache = args.previous / "data/interim/each_channel/features" / f"{key}_advanced.parquet"
        if not cache.exists():
            cache = cache.with_name(f"{key}.parquet")
        frame = (
            pd.read_parquet(cache)
            if cache.exists()
            else build_features(daily, episodes, channels, dates, ids)
        )
        frame = frame.merge(calendar, on="date", how="left", validate="many_to_one")
        frame = frame.sort_values(["date", "channel_id"]).reset_index(drop=True)
        parts = {
            n: split(frame, n) for n in ("train", "early_stop", "selection", "calibration", "test")
        }
        train = pd.concat([parts["train"], parts["early_stop"]]).sort_values(["date", "channel_id"])
        selection, cal, test = (parts[n] for n in ("selection", "calibration", "test"))
        folder = args.out / key
        folder.mkdir(parents=True, exist_ok=True)
        result = {
            "name": name,
            "key": key,
            "channels": len(ids),
            "candidates": {},
            "counts": {
                n: {
                    "rows": len(p),
                    "positives": int(p.target.sum()),
                    "positive_channels": int(p.loc[p.target.eq(1), "channel_id"].nunique()),
                }
                for n, p in parts.items()
            },
            "train_rows": len(train),
            "train_positives": int(train.target.sum()),
        }
        seen = (
            episodes.loc[
                episodes.start.ge("2022-01-01")
                & episodes.start.lt("2025-07-01")
                & episodes.start_is_observed,
                "channel_id",
            ]
            .unique()
            .tolist()
        )
        result["history_channels"] = seen
        oldfolder = (
            args.previous / "ml/each_channel/models" / key
            if name in TYPE_KEYS
            else args.remaining / key
        )
        oldcfg, old = read_model(oldfolder)
        candidates = {}
        if old is not None:
            candidates["incumbent"] = (
                old,
                oldcfg["selected"]["features"],
                oldfolder / "model.cbm",
                oldcfg["threshold"],
            )
        if train.target.nunique() == 2:
            for mode, names in (("dense_base", FEATURES), ("dense_calendar", FEATURES + CALENDAR)):
                print(f"FIT {key} {mode} rows={len(train)}", flush=True)
                model = CatBoostClassifier(**PARAMS)
                model.fit(train[names], train.target)
                path = folder / f"{mode}.cbm"
                model.save_model(str(path))
                candidates[mode] = (model, names, path, None)
                # Earlier diagnostic fold, with no use of Q2 labels in fit/calibration.
                q1, q2 = (
                    subset(frame, a, b)
                    for a, b in (("2025-01-01", "2025-04-01"), ("2025-04-01", "2025-07-01"))
                )
                if parts["train"].target.nunique() == 2 and len(q1) and len(q2):
                    diag = CatBoostClassifier(**PARAMS)
                    diag.fit(parts["train"][names], parts["train"].target)
                    threshold = choose_threshold(q1.target, diag.predict_proba(q1[names])[:, 1])
                    result.setdefault("q2_diagnostic", {})[mode] = assess(
                        q2, diag.predict_proba(q2[names])[:, 1], threshold
                    )
        if name in vcfg["types"]:
            vdates = dates[dates >= "2025-07-01"]
            base = baseline_rows(daily, episodes, channels, vdates, ids, vcfg["types"])
            base["v0_score"] = np.nan
            before_test = base.date.lt("2026-01-01")
            base.loc[before_test, "v0_score"] = vmodel.predict_proba(
                base.loc[before_test, vcfg["features"]]
            )[:, 1]
            base.to_parquet(folder / "v0_features.parquet", index=False)
            for n in ("selection", "calibration", "test"):
                parts[n] = parts[n].merge(
                    base.loc[base.status.eq("scored"), ["channel_id", "date", "v0_score"]],
                    on=["channel_id", "date"],
                    validate="one_to_one",
                )
            selection, cal, test = (parts[n] for n in ("selection", "calibration", "test"))
            result["v0_threshold"] = vcfg["threshold"]
            candidates["v0_type_threshold"] = (None, [], args.baseline_dir / "daily.cbm", None)
        for tag, (model, names, path, saved_threshold) in candidates.items():
            ps = (
                selection.v0_score.to_numpy()
                if model is None
                else model.predict_proba(selection[names])[:, 1]
            )
            pc = cal.v0_score.to_numpy() if model is None else model.predict_proba(cal[names])[:, 1]
            st = choose_threshold(selection.target, ps)
            th = (
                saved_threshold if saved_threshold is not None else choose_threshold(cal.target, pc)
            )
            result["candidates"][tag] = {
                "features": names,
                "path": str(path.resolve()),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "threshold": th,
                "selection": assess(selection, ps, st),
                "calibration": assess(cal, pc, th),
                "selection_threshold": st,
            }
        winner = max(
            result["candidates"],
            key=lambda tag: rank(result["candidates"][tag]["selection"]),
            default=None,
        )
        result["winner"] = winner
        result["test_shape"] = {"rows": len(test), "positives": int(test.target.sum())}
        result["seconds_to_freeze"] = time.perf_counter() - started
        write_json(folder / "selection_frozen.json", result)
        test.to_parquet(folder / "test.parquet", index=False)
        print(f"FROZEN {key} winner={winner}", flush=True)
        del frame, train, parts, selection, cal, test, candidates
        gc.collect()
    write_json(args.out / "all_frozen.json", {"keys": list(types.values())})


def evaluate(args):
    keys = json.loads((args.out / "all_frozen.json").read_text())["keys"]
    results = []
    for key in keys:
        folder = args.out / key
        result = json.loads((folder / "selection_frozen.json").read_text(encoding="utf-8"))
        test = pd.read_parquet(folder / "test.parquet")
        if "v0_score" in test:
            vcfg, vmodel = read_model(args.baseline_dir, "daily.cbm")
            base = pd.read_parquet(folder / "v0_features.parquet")
            base = test[["channel_id", "date"]].merge(
                base, on=["channel_id", "date"], validate="one_to_one"
            )
            test["v0_score"] = vmodel.predict_proba(base[vcfg["features"]])[:, 1]
        result["test"] = {}
        for tag, spec in result["candidates"].items():
            path = Path(spec["path"])
            if hashlib.sha256(path.read_bytes()).hexdigest() != spec["sha256"]:
                raise ValueError("Model changed after selection")
            if tag == "v0_type_threshold":
                scores = test.v0_score.to_numpy()
            else:
                model = CatBoostClassifier().load_model(str(path))
                scores = model.predict_proba(test[spec["features"]])[:, 1]
            result["test"][tag] = summarize(
                test, scores, spec["threshold"], result["history_channels"]
            )
            test[f"score_{tag}"] = scores
        if "v0_score" in test:
            result["test"]["v0_original"] = summarize(
                test, test.v0_score.to_numpy(), result["v0_threshold"], result["history_channels"]
            )
        result["always_alert"] = assess(test, np.ones(len(test)), 0.5)
        result.pop("history_channels")
        test.to_parquet(folder / "test_scored.parquet", index=False)
        write_json(folder / "report.json", result)
        results.append(result)
        print(
            key,
            result["winner"],
            {k: (v["precision"], v["recall"]) for k, v in result["test"].items()},
            flush=True,
        )
    write_json(args.out / "summary.json", results)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=["download", "fit", "evaluate"], required=True)
    parser.add_argument("--calendar-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--previous", type=Path)
    parser.add_argument("--remaining", type=Path)
    parser.add_argument("--baseline-dir", type=Path, default=Path("ml/models/baseline-v0-daily"))
    args = parser.parse_args()
    if args.phase == "download":
        _, sources = calendar_features(args.calendar_dir, download=True)
        write_json(args.calendar_dir / "sources.json", sources)
        print(sources)
    elif args.phase == "fit":
        fit(args)
    else:
        evaluate(args)


if __name__ == "__main__":
    main()
