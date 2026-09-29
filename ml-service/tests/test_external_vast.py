"""vast_gate: решение сервиса по проезду совпадает с расчётом обучения; правило допуска, нормы маршрутов, эндпоинт."""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import history
from app.main import app
from ml.external.vast2017.gate import HISTORY, decide, fit, no_permit, passes, score

T0 = pd.Timestamp("2015-06-01 08:00", tz="UTC")
CHANNELS = pd.DataFrame(
    [(1, 11, "entrance0"), (2, 12, "general-gate1"), (3, 12, "general-gate2"), (4, 15, "gate0")]
    + [(5, 16, "ranger-stop1")],
    columns=["channel_id", "object_id", "measure"],
)
PERMITS = pd.DataFrame(
    {"credential_id": ["ranger"] * 2, "object_id": [15, 16], "valid_from": [None] * 2, "valid_to": [None] * 2}
)


def trip(ticket: str, start: pd.Timestamp, route: list[int], step_min: float = 10) -> list[tuple]:
    return [(c, start + pd.Timedelta(minutes=k * step_min), ticket) for k, c in enumerate(route)]


def journal() -> tuple[pd.DataFrame, dict]:
    """Обычные поездки: въезд → ворота 1 → ворота 2 → въезд; у рейнджера — через закрытые ворота к посту."""
    rows = []
    for i in range(60):
        rows += trip(f"car{i}", T0 + pd.Timedelta(hours=i), [1, 2, 3, 1], step_min=10 + (i % 5))
    rows += trip("ranger", T0, [1, 4, 5, 4, 1])
    ev = pd.DataFrame(rows, columns=["channel_id", "ts", "value"])
    classes = {f"car{i}": "1" for i in range(60)} | {"ranger": "2P", "moto": "1", "truck": "4"}
    return ev, classes


def fitted():
    ev, classes = journal()
    p = passes(ev, CHANNELS, classes)
    p["violation"] = no_permit(p, PERMITS)
    norms = fit(p[~p.violation])
    norms.threshold = 2.0  # поездки синтетики одинаковы — квантиль оценок здесь вырожден
    return norms, ev, classes


def test_permit_rule_and_rare_route():
    norms, ev, classes = fitted()
    later = T0 + pd.Timedelta(days=5)
    # мотоцикл — от въезда сразу к посту за воротами; грузовик — через закрытые ворота; машина — обычным маршрутом
    new = pd.DataFrame(
        trip("moto", later, [1, 5]) + trip("truck", later, [1, 4]) + trip("car0x", later, [1, 2]),
        columns=["channel_id", "ts", "value"],
    )
    classes = classes | {"car0x": "1"}
    d = decide(norms, new, ev, CHANNELS, classes, PERMITS)
    assert d.violation.tolist() == [False, True, False, True, False, False]
    assert bool(d.alert.iloc[1]) and bool(d.alert.iloc[3]) and not bool(d.alert.iloc[5])
    # маршрут «въезд → пост за воротами» в нормах не встречался — редкость выше, чем у обычного «въезд → ворота 1»
    assert d.s_route.iloc[1] > d.s_route.iloc[5] + 1


def test_decide_ignores_target_already_in_history_and_later_events():
    norms, ev, classes = fitted()
    target = ev[ev.value == "car7"].iloc[[2]]  # третий проезд поездки
    alone = decide(norms, target, ev[ev.ts < target.ts.iloc[0]], CHANNELS, classes, PERMITS)
    # в истории прогона уже лежит сам проезд и более поздние события — решение то же
    full = decide(norms, target, ev, CHANNELS, classes, PERMITS)
    cols = ["prev_gate", "gate", "dt", "violation", "s_route", "s_time", "score", "alert"]
    pd.testing.assert_frame_equal(alone[cols], full[cols])
    assert alone.prev_gate.iloc[0] == "general-gate1"


PREPARED = Path(__file__).resolve().parents[2] / "data" / "external" / "vast2017" / "prepared"


@pytest.mark.skipif(not (PREPARED / "events.parquet").is_file(), reason="нет data/external/vast2017/prepared")
def test_real_journal_service_decision_matches_training(external_vast):
    """Проезды журнала: решение сервиса (история билета за HISTORY) = расчёт обучения по всему журналу."""
    from ml.external.vast2017 import train

    model = next(m for m in external_vast.app.state_models if m.config.get("model") == "vast_gate")
    events, channels, classes, permits, labels = train.load(PREPARED)
    p = passes(events, channels, classes)
    p["violation"] = no_permit(p, permits)
    p = p.join(score(model.artifact, p))
    p["alert"] = p.violation | (p.score >= model.artifact.threshold)
    lp = train.label_passes(p, labels)
    rng = np.random.default_rng(0)
    rows = np.unique(np.r_[lp.row.to_numpy(), rng.choice(p.index.to_numpy(), 400, replace=False)])
    sample = p.loc[rows].sort_values("ts")
    past = events[events.value.isin(sample.value)]
    got = []
    for _, t in sample.iterrows():  # как сервис: история билета за [ts − HISTORY, ts]
        h = past[(past.value == t.value) & (past.ts >= t.ts - HISTORY) & (past.ts <= t.ts)]
        got.append(decide(model.artifact, t.to_frame().T[["channel_id", "ts", "value"]], h, channels, classes, permits))
    got = pd.concat(got, ignore_index=True)
    cols = ["prev_gate", "gate", "violation", "s_route", "s_time", "score", "alert"]
    want = sample[cols].reset_index(drop=True)
    pd.testing.assert_frame_equal(got[cols].astype(want.dtypes.to_dict()), want)
    assert np.allclose(got.dt.fillna(-1).astype(float), sample.dt.fillna(-1).to_numpy())


@pytest.fixture(scope="module")
def external_vast():
    from app.main import state

    with TestClient(app) as client:
        client.app.state_models = state["models"]
        if not any(m.config.get("model") == "vast_gate" for m in state["models"]):
            pytest.skip("нет модели vast_gate")
        yield client


def test_vast_gate_endpoint(monkeypatch, external_vast):
    ch = pd.DataFrame(
        [(902001100, 9020011, "entrance0"), (902001601, 9020016, "ranger-stop1")],
        columns=["channel_id", "object_id", "measure"],
    )
    past = pd.DataFrame({"channel_id": [902001100], "ts": [pd.Timestamp("2015-07-10 07:28:03", tz="UTC")],
                         "value": ["moto-1"]})  # fmt: skip
    monkeypatch.setattr(history, "external_channels", lambda dataset: ch)
    monkeypatch.setattr(history, "external_passes", lambda run, ids, values, since, until: past)
    monkeypatch.setattr(history, "external_classes", lambda dataset, ids: {"moto-1": "1"})
    monkeypatch.setattr(
        history, "external_permits",
        lambda dataset: pd.DataFrame({"credential_id": ["r"], "object_id": [9020016], "valid_from": [None],
                                      "valid_to": [None]}),
    )  # fmt: skip
    body = external_vast.post(
        "/v1/external/score",
        json={"model": "vast_gate", "decision_time": "2015-07-10T10:40:00+03:00",
              "events": [{"channel_id": 902001601, "ts": "2015-07-10T10:35:47+03:00", "value": "moto-1"},
                         {"channel_id": 1, "ts": "2015-07-10T10:36:00+03:00", "value": "moto-1"},
                         {"channel_id": 902001100, "ts": "2015-07-10T10:45:00+03:00", "value": "moto-2"}]},
    ).json()  # fmt: skip
    assert body["model"] == "vast-gate-v0" and body["dataset"] == "vast2017"
    first, unknown, future = body["results"]
    assert first["status"] == "scored" and first["alert"] is True and first["classes"] is None
    assert first["decision_time"].startswith("2015-07-10T10:35:48.") and first["target_channel_id"] == 902001601
    assert first["factors"][0]["feature"] == "no_permit" and first["factors"][1]["value"] == "entrance0 → ranger-stop1"
    known = ("no_permit", "route", "segment_time_fast", "segment_time_slow")
    assert all(f["feature"] in known for f in first["factors"])
    assert unknown["skip_reason"] == "unknown_target" and future["skip_reason"] == "future_event"
    r = external_vast.post("/v1/external/score", json={"model": "vast_gate", "decision_time": "2015-07-10T10:40:00"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "events_required"


def test_vast_gate_in_models_list(external_vast):
    m = next(x for x in external_vast.get("/v1/models").json()["models"] if x["name"] == "vast_gate")
    assert m["family"] == "external" and m["trees"] is None and m["trigger"] == "on_event"
    assert m["target"] == "event" and m["outcome"] == "label_at_event" and m["label_key"] == "credential_id"
    assert m["offline"] is not None and m["offline"]["eval_scheme"] == "out-of-time"


def test_same_second_passes_get_distinct_decision_times():
    from app.scoring import value_offset

    a, b = value_offset("20151709091702-944"), value_offset("20153809093819-671")  # один датчик, одна секунда в наборе
    assert a != b and pd.Timedelta(0) <= min(a, b) and max(a, b) < pd.Timedelta(seconds=1)
    assert value_offset("20151709091702-944") == a
