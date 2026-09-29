"""Ежедневный прогноз по таблице маршрутизации (задача C): модели по типам датчиков в `/models`, метрики по моделям
и типам, лимит заявок в сутки — по доле оценки внутри своей модели."""

import datetime as dt
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from app.domain.metrics import model_metrics
from app.domain.work_orders import apply_prediction_rules, config
from app.schemas.api import Metrics

MSK = ZoneInfo("Europe/Moscow")
DAY = dt.datetime(2026, 5, 10, tzinfo=MSK)
FIXTURES = Path(__file__).parent / "fixtures" / "ml"


async def ups_and_call_point(session) -> None:
    """Каналы типов, которые ведут отдельные модели ML-разработчика: ИБП и ручной извещатель."""
    await session.execute(
        text(
            "INSERT INTO channels (id, object_id, system_type, sensor_type, tag, name) VALUES "
            "(1004, 100, 'Электропитание', 'ИБП', '15-11.1.4.1.', 'ИБП ПК4'), "
            "(1005, 100, 'Пожарная охрана', 'Ручной извещатель', '15-11.1.5.1.', 'ИПР ПК5')"
        )
    )


async def run(session) -> int:
    return await session.scalar(
        text(
            "INSERT INTO runs (id, scenario, status, sim_start, sim_now) VALUES (7, 't', 'running', :t, :t) "
            "RETURNING id"
        ),
        {"t": DAY},
    )


async def daily(session, run_id, channel, version, score, threshold, outcome=None) -> int:
    return await session.scalar(
        text(
            "INSERT INTO predictions (run_id, model, model_version, channel_id, decision_time, horizon_start, "
            "horizon_end, status, score, threshold, alert, outcome, outcome_at) VALUES (:run, 'daily', :version, "
            ":ch, :t, :t + interval '1 day', :t + interval '30 days', 'scored', :score, :thr, :score >= :thr, "
            ":outcome, CASE WHEN :outcome = 'fault' THEN :t + interval '3 days' END) RETURNING id"
        ),
        {
            "run": run_id,
            "version": version,
            "ch": channel,
            "t": DAY,
            "score": score,
            "thr": threshold,
            "outcome": outcome,
        },
    )


@pytest.mark.anyio
async def test_daily_cap_ranks_by_share_within_model(session, monkeypatch):
    """Порог v0 — 0.64, извещателя — 0.013: по оценке тревога извещателя (0.05) всегда проиграет второй тревоге v0
    (0.7). По доле внутри модели лучшие решения дня у каждой — 1.0, при равной доле — по запасу над порогом."""
    cfg = {k: dict(v) for k, v in config().items()}
    cfg["daily_model_alert"]["max_per_day"] = 2
    cfg["daily_model_alert"]["models"] = ["daily"]
    monkeypatch.setattr("app.domain.work_orders.config", lambda: cfg)
    await ups_and_call_point(session)
    run_id = await run(session)
    ids = [
        await daily(session, run_id, 1001, "baseline-v0", 0.9, 0.64),
        await daily(session, run_id, 1002, "baseline-v0", 0.7, 0.64),
        await daily(session, run_id, 1003, "baseline-v0", 0.1, 0.64),
        await daily(session, run_id, 1005, "manual_alarm", 0.05, 0.013),
        await daily(
            session, run_id, 1004, "manual_alarm", 0.01, 0.013
        ),  # без тревоги: второе решение модели за сутки — для доли
    ]
    notes = await apply_prediction_rules(session, run_id, ids)
    assert sorted(n.data["channel_id"] for n in notes) == [1001, 1005]


@pytest.mark.anyio
async def test_daily_metrics_by_model_and_sensor_type(session):
    await ups_and_call_point(session)
    run_id = await run(session)
    await daily(session, run_id, 1001, "baseline-v0", 0.9, 0.64, "fault")
    await daily(session, run_id, 1002, "baseline-v0", 0.2, 0.64, "no_fault")
    await daily(session, run_id, 1004, "ups_ensemble", 0.5, 0.3, "fault")
    await daily(session, run_id, 1005, "manual_alarm", 0.05, 0.013, "no_fault")
    now = DAY + dt.timedelta(days=40)

    m = await model_metrics(session, run_id, "daily", now)
    Metrics.model_validate(m)
    assert (m["model_version"], m["all"]["decisions"]) == (None, 4)  # смесь моделей — версии нет
    assert {v: (b["decisions"], b["tp"], b["fp"]) for v, b in m["by_version"].items()} == {
        "baseline-v0": (2, 1, 0),
        "manual_alarm": (1, 0, 1),
        "ups_ensemble": (1, 1, 0),
    }
    assert {t: b["decisions"] for t, b in m["by_sensor_type"].items()} == {
        "Газовый датчик": 1,
        "Датчик дыма": 1,
        "ИБП": 1,
        "Ручной извещатель": 1,
    }
    ups = await model_metrics(session, run_id, "daily", now, sensor_type="ИБП")
    assert (ups["model_version"], ups["all"]["decisions"], list(ups["by_version"])) == (
        "ups_ensemble",
        1,
        ["ups_ensemble"],
    )
    v0 = await model_metrics(session, run_id, "daily", now, model_version="baseline-v0")
    assert (v0["model_version"], v0["all"]["decisions"]) == ("baseline-v0", 2)
    smoke = await model_metrics(session, run_id, "smoke_alarm", now)
    assert (smoke["by_version"], smoke["by_sensor_type"]) == (None, None)


def test_models_daily_routing_snapshot(client, monkeypatch):
    """Снимок `/v1/models` с таблицей маршрутизации (lct-25): у ежедневных моделей — типы по таблице и автор; версия
    модели команды — `baseline-v0`, как в `predictions.model_version` и маршрутах."""
    snapshot = json.loads((FIXTURES / "models.json").read_text(encoding="utf-8"))

    async def fake_models(self):
        self.daily_routing = snapshot["daily_routing"]
        return snapshot["models"]

    async def no_compare(self, model):
        return None

    monkeypatch.setattr("app.domain.ml_client.MlClient.models", fake_models)
    monkeypatch.setattr("app.domain.ml_client.MlClient.models_compare", no_compare)
    r = client.get("/api/v1/models")
    assert r.status_code == 200, r.text
    body = r.json()
    daily = {m["version"]: m for m in body["items"] if m["model"] == "daily"}
    routed = {route["model"] for route in body["daily_routing"]["routes"]}
    assert routed == {"baseline-v0", "ups_ensemble", "manual_alarm"} and routed <= daily.keys()
    assert {v: (m["author"], m["scoring"], m["active"], m["routes"]) for v, m in daily.items() if m["routes"]} == {
        "baseline-v0": (
            "команда",
            True,
            True,
            [
                "Датчик дыма",
                "Состояние насоса",
                "Состояние вентилятора",
                "Датчик температуры",
                "Газовый датчик",
                "Состояние фазы",
            ],
        ),
        "ups_ensemble": ("ML-разработчик", True, False, ["ИБП"]),
        "manual_alarm": ("ML-разработчик", True, False, ["Ручной извещатель"]),
    }
    assert daily["fan"]["routes"] == [] and daily["fan"]["scoring"] is False  # не ведёт ни одного типа
    routing = body["daily_routing"]
    assert routing["version"] == "2026-09-29" and {u["sensor_type"] for u in routing["unrouted"]} >= {"КД Дверь"}
    assert routing["test"]["from"] == "2026-01-01" and routing["offline"]["routing"]["caught_episodes"] == 5177
    smoke = [m for m in body["items"] if m["model"] == "smoke_alarm"]
    assert all(m["routes"] is None for m in smoke) and {m["author"] for m in smoke} >= {"команда", "ML-разработчик"}
