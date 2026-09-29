"""Внешние данные: признаки Magdeburg на окне сервиса совпадают с расчётом на всём ряду; скоринг и пропуски."""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import history
from app.main import app
from ml.external.magdeburg.features import FEATURES, HISTORY, MEASURES, build

T0 = pd.Timestamp("2023-07-10 09:30:07", tz="UTC")
NODES = [9_000_101, 9_000_102, 9_000_103]


def journal(hours: float = 3, fire_node: int = 9_000_101, fire_at: float = 2.5) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Три узла, показания раз в 10 с; у одного узла после fire_at часов растут частицы и CO."""
    rng = np.random.default_rng(0)
    channels = pd.DataFrame(
        [(900_000_000 + i * 100 + j, n, m) for i, n in enumerate(NODES) for j, m in enumerate(MEASURES)]
        + [(800_000_000 + i, n, "node") for i, n in enumerate(NODES)],  # служебный канал цели, событий у него нет
        columns=["channel_id", "object_id", "measure"],
    )
    times = pd.date_range(T0, T0 + pd.Timedelta(hours=hours), freq="10s")
    rows = []
    for c, n, m in channels[channels.measure != "node"].itertuples(index=False):
        base = 20 + rng.normal(0, 1, len(times))
        if n == fire_node and m in ("PM_Total_Room", "CO_Room"):
            base = base + np.clip((times - (T0 + pd.Timedelta(hours=fire_at))).total_seconds(), 0, None) / 6
        rows.append(pd.DataFrame({"channel_id": c, "ts": times, "value": [f"{v:g}" for v in base]}))
    return pd.concat(rows, ignore_index=True), channels


def test_features_on_service_window_match_full_history():
    events, channels = journal()
    t = pd.Timestamp("2023-07-10 12:21:00", tz="UTC")
    decisions = pd.DataFrame({"object_id": NODES, "decision_time": t})
    full = build(events, channels, decisions)
    window = events[(events.ts >= t - HISTORY) & (events.ts < t)]
    served = build(window, channels, decisions)
    pd.testing.assert_frame_equal(full[FEATURES], served[FEATURES])
    # признаки не видят события в момент T и позже
    later = pd.concat([events, events.assign(ts=t, value="99999")], ignore_index=True)
    pd.testing.assert_frame_equal(full[FEATURES], build(later, channels, decisions)[FEATURES])


def test_fire_node_stands_out_from_room():
    events, channels = journal()
    t = pd.Timestamp("2023-07-10 12:25:00", tz="UTC")
    f = build(events, channels, pd.DataFrame({"object_id": NODES, "decision_time": t})).set_index("object_id")
    assert f.loc[NODES[0], "PM_Total_Room_d_base"] > 50 > abs(f.loc[NODES[1], "PM_Total_Room_d_base"])
    assert f.loc[NODES[0], "room_PM_Total_Room_rank"] == 1.0


@pytest.fixture
def external_model():
    from app.main import state

    with TestClient(app) as client:
        model = next((m for m in state["models"] if m.config.get("model") == "magdeburg_fire"), None)
        if model is None:
            pytest.skip("нет модели magdeburg_fire")
        yield client


def test_external_score_endpoint_and_skip_reasons(monkeypatch, external_model):
    events, channels = journal(hours=3)
    monkeypatch.setattr(history, "external_channels", lambda dataset: channels)
    monkeypatch.setattr(
        history, "external_events",
        lambda run, ids, since, until: events[(events.ts >= since) & (events.ts < until)],
    )  # fmt: skip
    body = external_model.post(
        "/v1/external/score",
        json={"model": "magdeburg_fire", "decision_time": "2023-07-10T14:25:00+02:00", "targets": [*NODES, 1],
              "explain": True},
    ).json()  # fmt: skip
    assert body["model"].startswith("magdeburg-fire-v") and body["dataset"] == "magdeburg"
    by = {r["target"]: r for r in body["results"]}
    assert by[1]["skip_reason"] == "unknown_target"
    fire = by[NODES[0]]
    assert fire["status"] == "scored" and 0 <= fire["score"] <= 1 and len(fire["factors"]) == 5
    assert fire["target_channel_id"] == 800_000_000 and by[1]["target_channel_id"] is None
    assert set(fire["classes"]) == {"background", "nuisance", "fire"}
    assert abs(sum(fire["classes"].values()) - 1) < 1e-5
    early = external_model.post(
        "/v1/external/score", json={"model": "magdeburg_fire", "decision_time": "2023-07-10T12:00:00+02:00"}
    ).json()  # 30 минут от начала ряда — истории меньше часа; цели по умолчанию — узлы с каналом цели
    assert {r["skip_reason"] for r in early["results"]} == {"short_history"}
    assert [r["target"] for r in early["results"]] == NODES


def test_unknown_external_model_is_404(external_model):
    r = external_model.post("/v1/external/score", json={"model": "smoke_alarm", "decision_time": "2023-07-10T12:00:00"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "model_not_found"


PREPARED = Path(__file__).resolve().parents[2] / "data" / "external" / "magdeburg" / "prepared"


@pytest.mark.skipif(not (PREPARED / "events.parquet").is_file(), reason="нет data/external/magdeburg/prepared")
def test_real_journal_service_window_matches_training():
    """На журнале набора: признаки по окну сервиса [T − history, T) равны признакам обучения по всему ряду."""
    from ml.external.magdeburg.train import load

    events, channels, _, _ = load(PREPARED)
    nodes = sorted(channels["object_id"].unique())
    times = [pd.Timestamp(t, tz="Europe/Moscow").tz_convert("UTC") for t in ("2023-07-12 16:50", "2023-07-13 13:05")]
    decisions = pd.DataFrame([(n, t) for t in times for n in nodes], columns=["object_id", "decision_time"])
    full = build(events, channels, decisions)
    for t in times:
        window = events[(events.ts >= t - HISTORY) & (events.ts < t)]
        served = build(window, channels, decisions[decisions.decision_time == t])
        expected = full[full.decision_time == t][FEATURES].reset_index(drop=True)
        pd.testing.assert_frame_equal(expected, served[FEATURES].reset_index(drop=True))
