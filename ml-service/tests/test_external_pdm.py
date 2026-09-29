"""pdm_failure_24h: признаки по окнам сервиса (свое окно на сигнал) равны признакам обучения; эндпоинт и пропуски."""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import history
from app.main import app
from ml.external.azure_pdm.features import FEATURES, HISTORY_BY_TAG, TELEMETRY, build

T0 = pd.Timestamp("2015-11-20 03:00", tz="UTC")
UNITS = pd.DataFrame({"object_id": [9040101, 9040102], "model": ["model3", "model4"], "age_years": [18.0, 7.0]})
TAGS = ["machine", *TELEMETRY, "errors", "maint", "failures"]


def channels() -> pd.DataFrame:
    rows = [(904000000 + (u - 9040100) * 100 + k, u, tag) for u in UNITS.object_id for k, tag in enumerate(TAGS)]
    return pd.DataFrame(rows, columns=["channel_id", "object_id", "measure"])


def journal(hours_b: int = 10) -> pd.DataFrame:
    """Агрегат 101 — 40 ч телеметрии, ошибки и ТО; у 102 телеметрия только последние hours_b часов."""
    rng = np.random.default_rng(1)
    ch = channels().set_index(["object_id", "measure"]).channel_id
    rows = []
    for u, hours in ((9040101, 40), (9040102, hours_b)):
        for h in range(hours):
            ts = T0 + pd.Timedelta(hours=40 - hours + h)
            for k, t in enumerate(TELEMETRY):
                rows.append((ch[(u, t)], ts, f"{[170, 450, 100, 40][k] + rng.normal(0, 5):.4f}"))
    rows += [(ch[(9040101, "errors")], T0 + pd.Timedelta(hours=30), "error1")]
    rows += [(ch[(9040101, "maint")], T0 - pd.Timedelta(days=20), "comp2")]
    return pd.DataFrame(rows, columns=["channel_id", "ts", "value"])


def service_events(events: pd.DataFrame, ch: pd.DataFrame, t: pd.Timestamp) -> pd.DataFrame:
    """Как main.py: каждый сигнал — за своё окно [T − m, T)."""
    parts = []
    for tag, ids in ch.groupby("measure")["channel_id"]:
        m = pd.Timedelta(minutes=HISTORY_BY_TAG.get(tag, max(HISTORY_BY_TAG.values())))
        parts.append(events[events.channel_id.isin(ids) & (events.ts >= t - m) & (events.ts < t)])
    return pd.concat(parts, ignore_index=True)


def test_service_windows_match_full_history_on_synthetic():
    ev, ch = journal(), channels()
    times = [T0 + pd.Timedelta(hours=h) for h in (30, 35, 40)]
    decisions = pd.DataFrame([(u, t) for u in UNITS.object_id for t in times], columns=["object_id", "decision_time"])
    full = build(ev, ch, decisions, UNITS)
    for t in times:
        d = decisions[decisions.decision_time == t]
        served = build(service_events(ev, ch, t), ch, d, UNITS)
        pd.testing.assert_frame_equal(
            full[full.decision_time == t][FEATURES].reset_index(drop=True), served[FEATURES].reset_index(drop=True)
        )
    # событие в момент T и позже не видно
    later = pd.concat([ev, pd.DataFrame({"channel_id": [904000105], "ts": [times[-1]], "value": ["error5"]})])
    pd.testing.assert_frame_equal(full[FEATURES], build(later, ch, decisions, UNITS)[FEATURES])
    assert full.loc[full.decision_time == times[0], "h_since_maint_comp2"].iloc[0] == 20 * 24 + 30


PREPARED = Path(__file__).resolve().parents[2] / "data" / "external" / "azure_pdm" / "prepared"


@pytest.mark.skipif(not (PREPARED / "events.parquet").is_file(), reason="нет data/external/azure_pdm/prepared")
def test_real_journal_service_windows_match_training():
    """Журнал набора: 5 агрегатов × 24 часа вокруг отказов и обычных дней — окна сервиса = расчёт обучения."""
    from ml.external.azure_pdm import train

    events, ch, units, labels = train.load(PREPARED)
    fails = labels.sort_values("start_ts").drop_duplicates("object_id").head(5)
    times = [t - pd.Timedelta(hours=h) for t in fails.start_ts for h in (1, 12, 30)]
    decisions = pd.DataFrame([(u, t) for u in fails.object_id for t in times], columns=["object_id", "decision_time"])
    mine = events[events.channel_id.isin(ch[ch.object_id.isin(fails.object_id)].channel_id)]
    full = build(mine, ch, decisions, units)
    for t in times:
        d = decisions[decisions.decision_time == t]
        served = build(service_events(mine, ch, t), ch, d, units)
        pd.testing.assert_frame_equal(
            full[full.decision_time == t][FEATURES].reset_index(drop=True), served[FEATURES].reset_index(drop=True)
        )


@pytest.fixture(scope="module")
def client():
    from app.main import state

    with TestClient(app) as c:
        if not any(m.config.get("model") == "pdm_failure_24h" for m in state["models"]):
            pytest.skip("нет модели pdm_failure_24h")
        yield c


def test_pdm_endpoint_and_short_history(monkeypatch, client):
    ev, ch = journal(hours_b=10), channels()
    monkeypatch.setattr(history, "external_channels", lambda dataset: ch)
    monkeypatch.setattr(
        history, "external_events",
        lambda run, ids, since, until: ev[ev.channel_id.isin(ids) & (ev.ts >= since) & (ev.ts < until)],
    )  # fmt: skip
    monkeypatch.setattr(history, "external_units", lambda dataset: UNITS)
    t = (T0 + pd.Timedelta(hours=40)).tz_convert("Europe/Moscow").isoformat()
    body = client.post(
        "/v1/external/score", json={"model": "pdm_failure_24h", "decision_time": t, "explain": True}
    ).json()
    assert body["model"] == "pdm-failure-v0" and body["dataset"] == "azure_pdm"
    ok, short = body["results"]
    assert ok["target"] == 9040101 and ok["target_channel_id"] == 904000100 and ok["status"] == "scored"
    assert 0 <= ok["score"] <= 1 and ok["classes"] is None and len(ok["factors"]) == 5
    assert short["target"] == 9040102 and short["skip_reason"] == "short_history"


def test_pdm_in_models_list(client):
    m = next(x for x in client.get("/v1/models").json()["models"] if x["name"] == "pdm_failure_24h")
    assert m["trigger"] == "hourly" and m["horizon"] == "24h" and m["outcome"] == "label_in_horizon"
    assert m["target_tag"] == "machine" and m["synthetic"] is True and m["offline"]["eval_scheme"] == "out-of-time"
