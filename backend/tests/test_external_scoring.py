"""Прогнозы внешних моделей в такте прогона и исходы по разметке набора (этап 14, app/domain/external.py)."""

import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from app.domain import external
from app.domain.outcomes import close_outcomes
from app.domain.runs import create_run, set_run_status, update_run_clock
from app.domain.schedule import close_recent_runs, tick

pytestmark = pytest.mark.anyio
MSK = ZoneInfo("Europe/Moscow")
ROOT, HALL, NODE1, NODE2 = 9_090_001, 9_090_002, 9_090_003, 9_090_004
TARGET1, TARGET2, SENSOR = 909_000_001, 909_000_002, 909_000_003


def at(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(f"2023-07-10T{s}").replace(tzinfo=MSK)


FIRE = {
    "name": "test_fire",
    "label": "test-fire-v0",
    "family": "external",
    "dataset": "test",
    "scoring": "available",
    "trigger": "every_60s",
    "target": "object",
    "target_tag": "node",
    "horizon": "now",
    "outcome": "label_at_decision_time",
    "positive_labels": ["fire"],
    "ignore_labels": ["unknown"],
}
FAIL = FIRE | {"name": "test_fail", "horizon": "2m", "outcome": "label_in_horizon", "ignore_labels": []}


class FakeMl:
    """ML-сервис с двумя внешними моделями набора `test`; у первого узла канал цели приходит в ответе."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dt.datetime]] = []

    async def models(self) -> list[dict]:
        return [FIRE, FAIL, FIRE | {"name": "other", "dataset": "magdeburg"}]

    async def score_external(self, run_id: int, model: str, decision_time: dt.datetime) -> dict:
        self.calls.append((model, decision_time))
        score = 0.9 if decision_time.minute == 1 else 0.1
        return {
            "model": f"{model}-v0",
            "results": [
                {
                    "target": NODE1,
                    "target_channel_id": TARGET1,
                    "status": "scored",
                    "score": score,
                    "threshold": 0.5,
                    "alert": score >= 0.5,
                    "classes": {"fire": score},
                    "factors": [],
                },
                {
                    "target": NODE2,
                    "status": "skipped",
                    "skip_reason": "short_history",
                    "score": None,
                    "threshold": 0.5,
                    "alert": None,
                    "classes": None,
                    "factors": None,
                },
            ],
        }

    async def score_daily(self, run_id, date):
        raise AssertionError("ежедневный прогноз хакатона на прогоне внешнего набора")


async def dataset_run(session) -> int:
    await session.execute(
        text(
            "INSERT INTO objects (id, parent_id, level, kind, name, source) VALUES "
            "(:r, NULL, 1, 'site', 'Площадка', 'test'), (:h, :r, 2, 'hall', 'Цех', 'test'), "
            "(:n1, :h, 3, 'node', 'Узел 1', 'test'), (:n2, :h, 3, 'node', 'Узел 2', 'test')"
        ),
        {"r": ROOT, "h": HALL, "n1": NODE1, "n2": NODE2},
    )
    await session.execute(
        text(
            "INSERT INTO channels (id, object_id, system_type, sensor_type, tag, name, source) VALUES "
            "(:t1, :n1, 'Пожарный мониторинг (внешн.)', 'Узел', 'node', 'Узел 1', 'test'), "
            "(:t2, :n2, 'Пожарный мониторинг (внешн.)', 'Узел', 'node', 'Узел 2', 'test'), "
            "(:s, :n1, 'Пожарный мониторинг (внешн.)', 'Термометр', 'T', 'T узла 1', 'test')"
        ),
        {"t1": TARGET1, "t2": TARGET2, "s": SENSOR, "n1": NODE1, "n2": NODE2},
    )
    await session.execute(
        text(
            "INSERT INTO external_labels (dataset, object_id, start_ts, end_ts, label) VALUES "
            "('test', :h, :a, :b, 'fire'), ('test', :h, :c, :d, 'unknown')"
        ),
        {"h": HALL, "a": at("10:01:00"), "b": at("10:02:30"), "c": at("10:03:00"), "d": at("10:05:00")},
    )
    await session.execute(
        text(
            "INSERT INTO runs (scenario, status, kind, sim_start, sim_end, sim_now, params) VALUES "
            "('dataset:test', 'finished', 'dataset', :a, :b, :b, '{\"dataset\": \"test\"}')"
        ),
        {"a": at("09:00:00"), "b": at("10:30:00")},
    )
    run = await create_run(session, scenario="ext", sim_start=at("10:00:30"), dataset="test")
    return run.id


async def set_now(session, run_id: int, now: str) -> None:
    await session.execute(text("UPDATE runs SET sim_now = :t WHERE id = :r"), {"t": at(now), "r": run_id})


async def outcomes(session, run_id: int, model: str) -> list[tuple]:
    rows = await session.execute(
        text(
            "SELECT to_char(decision_time AT TIME ZONE 'Europe/Moscow', 'HH24:MI'), channel_id, status, outcome, "
            "outcome_source FROM predictions WHERE run_id = :r AND model = :m ORDER BY decision_time, channel_id"
        ),
        {"r": run_id, "m": model},
    )
    return [tuple(r) for r in rows]


def test_decision_times():
    assert external.decision_times(at("10:00:30"), None, at("10:02:59"), dt.timedelta(seconds=60)) == [
        at("10:01:00"),
        at("10:02:00"),
    ]
    assert external.decision_times(at("10:00:30"), at("10:02:00"), at("10:02:59"), dt.timedelta(seconds=60)) == []
    assert len(external.decision_times(at("10:00:00"), None, at("12:00:00"), dt.timedelta(seconds=60))) == 30
    assert external.horizon({"horizon": "24h"}) == dt.timedelta(hours=24) and external.horizon({}) == dt.timedelta(0)


async def test_external_tick_and_outcomes(session, monkeypatch):
    monkeypatch.setattr(external, "_models_cache", None)
    ml = FakeMl()
    run_id = await dataset_run(session)
    await set_now(session, run_id, "10:03:40")
    run = await session.get(external.Run, run_id)
    await session.refresh(run)

    summary, notes = await tick(session, run, ml)
    assert summary["external"]["test_fire"] == {"decisions": 3, "predictions": 6, "alerts": 1}
    assert "daily" not in summary and {m for m, _ in ml.calls} == {"test_fire", "test_fail"}
    # класс в момент решения: 10:01 и 10:02 — внутри опыта с пожаром, 10:03 — в интервале unknown, без исхода;
    # канал цели второго узла — по target_tag, пропуск без исхода
    assert await outcomes(session, run_id, "test_fire") == [
        ("10:01", TARGET1, "scored", "fault", "live"),
        ("10:01", TARGET2, "skipped", None, None),
        ("10:02", TARGET1, "scored", "fault", "live"),
        ("10:02", TARGET2, "skipped", None, None),
        ("10:03", TARGET1, "scored", None, None),
        ("10:03", TARGET2, "skipped", None, None),
    ]
    # событие в горизонте 2 мин: пожар начался ровно в 10:01 — в первом решении цель уже в отказе, без исхода;
    # у остальных горизонт не истёк
    fail = [r for r in await outcomes(session, run_id, "test_fail") if r[1] == TARGET1]
    assert fail == [
        ("10:01", TARGET1, "scored", None, None),
        ("10:02", TARGET1, "scored", None, None),
        ("10:03", TARGET1, "scored", None, None),
    ]
    assert [n.type for n in notes].count("prediction.created") == 2  # тревоги двух моделей в 10:01
    classes = await session.execute(
        text("SELECT context->>'class' FROM predictions WHERE run_id = :r AND channel_id = :c ORDER BY decision_time"),
        {"r": run_id, "c": TARGET1},
    )
    assert {c for (c,) in classes} == {"fire"}  # класс решения — самый вероятный из classes, если модель его не дала
    # правила хакатона внешние прогнозы не закрывают
    assert (await close_outcomes(session, run_id, at("23:00:00")))[0] == {"fault": 0, "no_fault": 0}

    await set_now(session, run_id, "10:05:10")
    await session.refresh(run)
    await tick(session, run, ml)
    fire = [r[:4] for r in await outcomes(session, run_id, "test_fire") if r[1] == TARGET1]
    assert fire[3:] == [("10:04", TARGET1, "scored", None), ("10:05", TARGET1, "scored", "no_fault")]
    fail = [r[:4] for r in await outcomes(session, run_id, "test_fail") if r[1] == TARGET1]
    # 10:01 и 10:02 — внутри пожара [10:01, 10:02:30): цель уже «в отказе», решение исхода не получает
    assert [r[3] for r in fail] == [None, None, "no_fault", None, None]

    # после конца прогона: хвост после последнего такта (10:06) дорешивается, горизонты досчитываются по разметке
    # до конца журнала набора
    await set_now(session, run_id, "10:06:20")
    await session.refresh(run)
    await set_run_status(session, run_id, "stopped")
    await session.flush()
    monkeypatch.setattr("app.domain.schedule.MlClient", lambda: ml)
    counts, _ = await close_recent_runs(session)
    assert counts[run_id]["history_test_fail"] == 2
    fire = [r[3:] for r in await outcomes(session, run_id, "test_fire") if r[1] == TARGET1]
    assert fire[-1] == ("no_fault", "live") and len(fire) == 6
    # 10:04 — горизонт кончился к 10:06:20 по часам прогона, 10:05 и 10:06 — по журналу набора
    fail = [r[3:] for r in await outcomes(session, run_id, "test_fail") if r[1] == TARGET1]
    assert fail[1] == (None, None)
    assert fail[3:] == [("no_fault", "live"), ("no_fault", "history"), ("no_fault", "history")]


async def test_decision_at_label_start_gets_no_outcome(session):
    """Отказ Azure PdM размечен [06:00, 07:00): в решении на 06:00 агрегат уже в отказе — без исхода, как строки
    обучения (lead 0 не оценивается). Раньше такое решение получало «сбылся» — 16 лишних за неделю."""
    run_id = await dataset_run(session)
    model = FAIL | {"horizon": "24h", "positive_labels": ["failure"]}
    await session.execute(
        text(
            "INSERT INTO external_labels (dataset, object_id, start_ts, end_ts, label) "
            "VALUES ('test', :n, :a, :b, 'failure')"
        ),
        {"n": NODE1, "a": at("12:00:00"), "b": at("13:00:00")},
    )
    for hour in ("11", "12", "13"):
        t = at(f"{hour}:00:00")
        await session.execute(
            text(
                "INSERT INTO predictions (run_id, model, model_version, channel_id, decision_time, horizon_start, "
                "horizon_end, status, score, threshold, alert) VALUES (:r, 'test_fail', 'v0', :c, :t, :t, :e, "
                "'scored', 0.9, 0.5, true)"
            ),
            {"r": run_id, "c": TARGET1, "t": t, "e": t + dt.timedelta(hours=24)},
        )
    counts, _ = await external.close_external_outcomes(session, run_id, "test", [model], at("23:00:00"))
    # 11:00 — отказ в горизонте; 12:00 — в начале отказа; 13:00 — отказ кончился, горизонт не истёк
    assert await outcomes(session, run_id, "test_fail") == [
        ("11:00", TARGET1, "scored", "fault", "live"),
        ("12:00", TARGET1, "scored", None, None),
        ("13:00", TARGET1, "scored", None, None),
    ]
    assert counts == {"test_fail": 1}


async def run_clock(session, run_id: int) -> str:
    sim_now = await session.scalar(text("SELECT sim_now FROM runs WHERE id = :r"), {"r": run_id})
    return sim_now.astimezone(MSK).strftime("%H:%M:%S")


async def test_finished_run_clock_frozen_at_end(session, monkeypatch):
    """При смене сценария время нового приходило в шлюз раньше его `RunId`: часы прошлого прогона уходили к старту
    следующего, и такт решал внешними моделями за концом прогона (прогоны 30 и 31 стенда: magdeburg_fire — ещё
    100 мин и 18 ч). Часы — только внутри периода идущего прогона, у завершённого — на `sim_end`; хвост после конца
    дорешивается только до `sim_end`."""
    monkeypatch.setattr(external, "_models_cache", None)
    ml = FakeMl()
    run_id = await dataset_run(session)
    await session.execute(text("UPDATE runs SET sim_end = :t WHERE id = :r"), {"t": at("10:03:00"), "r": run_id})
    await update_run_clock(session, run_id, at("10:01:10"))
    assert await run_clock(session, run_id) == "10:01:10"
    # время чужого сценария — следующего (позже конца) или прошлого (раньше старта) — прогону не пишется
    await update_run_clock(session, run_id, at("11:05:00"))
    await update_run_clock(session, run_id, at("09:40:00"))
    assert await run_clock(session, run_id) == "10:01:10"

    run = await set_run_status(session, run_id, "finished")  # сценарий проигран — часы на его конце
    await session.flush()
    assert await run_clock(session, run_id) == "10:03:00"
    await session.execute(text("UPDATE runs SET sim_now = :t WHERE id = :r"), {"t": at("10:02:00"), "r": run_id})
    await update_run_clock(session, run_id, at("10:02:30"))  # у закрытого прогона часы не идут
    assert await run_clock(session, run_id) == "10:02:00"

    # часы, убежавшие до исправления: хвост после конца прогона всё равно дорешивается только до sim_end
    await session.execute(text("UPDATE runs SET sim_now = :t WHERE id = :r"), {"t": at("10:20:00"), "r": run_id})
    await session.refresh(run)
    monkeypatch.setattr("app.domain.schedule.MlClient", lambda: ml)
    await close_recent_runs(session)
    fire = [r[0] for r in await outcomes(session, run_id, "test_fire") if r[1] == TARGET1]
    assert fire == ["10:01", "10:02", "10:03"]
    assert max(t for _, t in ml.calls) == at("10:03:00")


GATE_ROOT, PRESERVE, GROUP = 9_080_001, 9_080_002, 9_080_003
GATE1, GATE2 = 908_000_001, 908_000_002
GATE = {
    "name": "vast_gate",  # настоящее имя задачи: на него настроено правило заявок ext_access_violation
    "label": "test-gate-v0",
    "family": "external",
    "dataset": "gates",
    "scoring": "available",
    "trigger": "on_event",
    "target": "event",
    "horizon": "now",
    "outcome": "label_at_event",
    "label_key": "credential_id",
    "positive_labels": ["violation", "suspicious"],
    "ignore_labels": [],
}


class FakeGateMl:
    """Событийная модель: тревога у билетов car-A и car-B, момент решения — событие + 1 с + доля от билета."""

    def __init__(self) -> None:
        self.batches: list[list[dict]] = []

    async def models(self) -> list[dict]:
        return [GATE]

    async def score_external_events(self, run_id, model, decision_time, events):
        self.batches.append(events)
        results = []
        for e in events:
            ts = dt.datetime.fromisoformat(e["ts"])
            lag = dt.timedelta(seconds=1, microseconds=sum(map(ord, e["value"])))
            score = 3.0 if e["value"] in ("car-A", "car-B") else 1.0
            results.append(
                {
                    "target": e["channel_id"],
                    "target_channel_id": e["channel_id"],
                    "event_ts": e["ts"],
                    "value": e["value"],
                    "decision_time": (ts + lag).isoformat(),
                    "status": "scored",
                    "skip_reason": None,
                    "score": score,
                    "threshold": 2.677,
                    "alert": score >= 2.677,
                    "classes": None,
                    "factors": [{"feature": "no_permit", "value": score > 2, "contribution": None}],
                }
            )
        results.append(
            {
                "target": 1,
                "target_channel_id": 1,
                "event_ts": events[0]["ts"],
                "value": "x",
                "decision_time": events[0]["ts"],
                "status": "skipped",
                "skip_reason": "unknown_target",
            }
        )
        return {"model": "test-gate-v0", "results": results}


async def gates_run(session) -> int:
    await session.execute(
        text(
            "INSERT INTO objects (id, parent_id, level, kind, name, source) VALUES "
            "(:r, NULL, 1, 'site', 'Площадка', 'gates'), (:p, :r, 2, 'preserve', 'Заповедник', 'gates'), "
            "(:g, :p, 3, 'sensor_group', 'Ворота', 'gates')"
        ),
        {"r": GATE_ROOT, "p": PRESERVE, "g": GROUP},
    )
    await session.execute(
        text(
            "INSERT INTO channels (id, object_id, system_type, sensor_type, tag, name, source) VALUES "
            "(:a, :g, 'Контроль доступа (внешн.)', 'Ворота', 'gate1', 'Ворота 1', 'gates'), "
            "(:b, :g, 'Контроль доступа (внешн.)', 'Ворота', 'gate2', 'Ворота 2', 'gates')"
        ),
        {"a": GATE1, "b": GATE2, "g": GROUP},
    )
    # нарушение — на датчике, в момент проезда; подозрительная ночёвка — на заповеднике, интервалом
    await session.execute(
        text(
            "INSERT INTO external_labels (dataset, object_id, channel_id, start_ts, end_ts, label, meta) VALUES "
            "('gates', :g, :a, :t1, :t1e, 'violation', '{\"credential_id\": \"car-A\"}'), "
            "('gates', :p, NULL, :s, :e, 'suspicious', '{\"credential_id\": \"car-B\"}')"
        ),
        {
            "g": GROUP,
            "a": GATE1,
            "p": PRESERVE,
            "t1": at("10:05:00"),
            "t1e": at("10:05:01"),
            "s": at("10:00:00"),
            "e": at("12:00:00"),
        },
    )
    await session.execute(
        text(
            "INSERT INTO runs (scenario, status, kind, sim_start, sim_end, sim_now, params) VALUES "
            "('dataset:gates', 'finished', 'dataset', :a, :b, :b, '{\"dataset\": \"gates\"}')"
        ),
        {"a": at("09:00:00"), "b": at("13:00:00")},
    )
    run = await create_run(session, scenario="gates", sim_start=at("10:00:00"), dataset="gates")
    return run.id


async def test_event_model_scores_passages(session, monkeypatch):
    from app.domain.ingest import RawEvent, ingest_events

    monkeypatch.setattr(external, "_models_cache", None)
    ml = FakeGateMl()
    run_id = await gates_run(session)
    passages = [
        (GATE1, "10:05:00", "car-A"),  # нарушение на датчике — тревога верна
        (GATE1, "10:05:00", "car-C"),  # тот же датчик в ту же секунду, другой билет — отдельное решение
        (GATE2, "10:10:00", "car-B"),  # ночёвка car-B — метка на заповеднике, билет совпал
        (GATE2, "10:11:00", "car-D"),  # внутри ночёвки, но билет чужой — не нарушение
    ]
    events = [RawEvent(c, at(t), v, False) for c, t, v in passages]
    await ingest_events(run_id, events, session=session)
    run = await session.get(external.Run, run_id)
    await session.refresh(run)

    summary, notes = await tick(session, run, ml)
    assert summary["external"]["vast_gate"] == {"events": 4, "predictions": 4, "alerts": 2}
    rows = await session.execute(
        text(
            "SELECT channel_id, context->>'value', alert, outcome, horizon_start FROM predictions "
            "WHERE run_id = :r ORDER BY horizon_start, context->>'value'"
        ),
        {"r": run_id},
    )
    assert [tuple(r) for r in rows] == [
        (GATE1, "car-A", True, "fault", at("10:05:00")),
        (GATE1, "car-C", False, "no_fault", at("10:05:00")),
        (GATE2, "car-B", True, "fault", at("10:10:00")),
        (GATE2, "car-D", False, "no_fault", at("10:11:00")),
    ]
    assert [n.type for n in notes].count("prediction.created") == 2

    # повторный такт: в модель уходит только последняя секунда водяного знака, дублей нет
    await tick(session, run, ml)
    assert [e["value"] for e in ml.batches[-1]] == ["car-D"]
    assert await session.scalar(text("SELECT count(*) FROM predictions WHERE run_id = :r"), {"r": run_id}) == 4

    # таблица прогонов: внешняя модель — в live.external; последний проезд ещё не закрыт — его момент решения позже
    # «сейчас» прогона (событие + 1 с)
    from app.domain.metrics import live_by_run

    gate = (await live_by_run(session, [run_id]))[run_id]["external"]["vast_gate"]
    assert (gate["decisions"], gate["positives"], gate["precision"], gate["recall"]) == (3, 2, 1.0, 1.0)

    # заявки по тревогам событийной модели: по одной на ворота, в названии — билет
    orders = await session.execute(
        text("SELECT channel_id, rule, priority, title FROM work_orders WHERE run_id = :r ORDER BY channel_id"),
        {"r": run_id},
    )
    assert [tuple(o) for o in orders] == [
        (GATE1, "ext_access_violation", "medium", "Разобрать проезд через Ворота 1: билет car-A"),
        (GATE2, "ext_access_violation", "medium", "Разобрать проезд через Ворота 2: билет car-B"),
    ]
    assert [n.type for n in notes].count("work_order.created") == 2
