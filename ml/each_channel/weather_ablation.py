"""Offline ablation of seven-day-lagged Moscow weather for phase and smoke."""

import json
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd
from ml.each_channel.features import load_inputs, selected_types
from ml.each_channel.improve_regularized import validate_artifact
from ml.each_channel.train import fit_type


def build_weather_features(daily):
    """Only weather dated T-7 or earlier can enter a decision at T."""
    daily = daily.copy()
    daily["date"] = pd.to_datetime(daily.time)
    daily = daily.set_index("date").asfreq("D")
    daily["temp_mean"] = (daily.temperature_2m_max + daily.temperature_2m_min) / 2
    daily["freezing"] = daily.temperature_2m_max.lt(0).astype(int)
    names = []
    for col, window, agg in (
        ("temp_mean", 7, "mean"),
        ("temp_mean", 30, "mean"),
        ("temperature_2m_min", 30, "min"),
        ("precipitation_sum", 7, "sum"),
        ("precipitation_sum", 30, "sum"),
        ("snowfall_sum", 30, "sum"),
        ("freezing", 30, "sum"),
    ):
        name = f"weather_{col}_{agg}_{window}d_lag7"
        names.append(name)
        daily[name] = getattr(daily[col].shift(7).rolling(window, min_periods=window), agg)()
    return daily[names].reset_index(), names


def load_weather(folder):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "moscow_era5.json"
    if not path.exists():
        params = urllib.parse.urlencode(
            {
                "latitude": 55.7558,
                "longitude": 37.6173,
                "start_date": "2021-01-01",
                "end_date": "2026-06-30",
                "daily": ",".join(
                    (
                        "temperature_2m_max",
                        "temperature_2m_min",
                        "precipitation_sum",
                        "snowfall_sum",
                    )
                ),
                "timezone": "Europe/Moscow",
                "models": "era5",
            }
        )
        url = "https://archive-api.open-meteo.com/v1/archive?" + params
        with urllib.request.urlopen(url, timeout=60) as response:
            data = json.load(response)
        path.write_text(json.dumps(data), encoding="utf-8")
    data = json.loads(path.read_text(encoding="utf-8"))
    weather, names = build_weather_features(pd.DataFrame(data["daily"]))
    complete = weather.loc[weather.date.between("2022-01-03", "2026-05-02"), names]
    if not complete.notna().all().all():
        raise ValueError("Historical weather is incomplete for the decision period")
    return weather, names


def main():
    root = Path("ml/each_channel")
    source = Path("data/interim/each_channel/source")
    output = Path("data/interim/each_channel/weather_ablation")
    (output / "cache").mkdir(parents=True, exist_ok=True)
    weather, names = load_weather(output)
    print("weather rows", len(weather), "features", names, flush=True)
    channels, _, episodes = load_inputs(source)
    types = selected_types(channels)
    for key, incumbent in (
        ("phase", root / "stage4/models/phase"),
        ("smoke", root / "models/smoke"),
    ):
        if not validate_artifact(incumbent):
            raise RuntimeError(f"Missing incumbent model: {incumbent}")
        old = json.loads((incumbent / "config.json").read_text(encoding="utf-8"))
        selected = old["selected"]
        frame = pd.read_parquet(
            Path("data/interim/each_channel/features") / f"{key}_advanced.parquet"
        )
        frame = frame.merge(weather, on="date", how="left", validate="many_to_one")
        if not frame[names].notna().all().all():
            raise ValueError(f"Missing weather features for {key}")
        recipe = dict(
            selected["recipe"],
            name=f"{key}_weather_lag7",
            feature_names=[*selected["features"], *names],
            categorical=selected["categorical"],
            fixed_trees=selected["trees"],
            refit_all_days=True,
        )
        result = fit_type(
            key,
            types[key],
            frame,
            episodes,
            channels,
            output / "models",
            output / "cache",
            recipes=[recipe],
            incumbent=incumbent,
        )
        print(
            "RESULT",
            key,
            result["selected"]["recipe"]["name"],
            result["selected"]["selection"],
            result["calibration"],
            result["test"],
            flush=True,
        )


if __name__ == "__main__":
    main()
