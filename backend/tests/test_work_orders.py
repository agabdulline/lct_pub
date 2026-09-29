"""Правила заявок v1 (docs/product/v1-scope.md): по прогнозам и без ML, одна открытая заявка на канал и правило."""

import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from app.db.models import PRIORITIES
from app.domain.work_orders import RULES, apply_periodic_rules, apply_prediction_rules, config, rule_description
from app.tasks import rules as rules_task

pytestmark = pytest.mark.anyio
MSK = ZoneInfo("Europe/Moscow")
NOW = dt.datetime(2026, 5, 10, 12, 0, tzinfo=MSK)


async def make_run(session, run_id: int = 7, sim_now: dt.datetime = NOW, status: str = "running") -> int:
    await session.execute(
        text("INSERT INTO runs (id, scenario, status, sim_start, sim_now) VALUES (:id, 'test', :s, :start, :now)"),
        {"id": run_id, "s": status, "start": NOW - dt.timedelta(days=1), "now": sim_now},
    )
    return run_id


async def prediction(session, run_id, channel_id, model, score, alert, status="scored", at=NOW) -> int:
    return await session.scalar(
        text(
            "INSERT INTO predictions (run_id, model, model_version, channel_id, decision_time, horizon_start, "
            "horizon_end, status, score, threshold, alert) VALUES (:run, :model, 'v0', :ch, :t, :t, :end, :status, "
            ":score, 0.3, :alert) RETURNING id"
        ),
        {
            "run": run_id,
            "model": model,
            "ch": channel_id,
            "t": at,
            "end": at + dt.timedelta(days=1),
            "status": status,
            "score": score,
            "alert": alert,
        },
    )


async def orders(session, run_id=7) -> list[tuple]:
    rows = await session.execute(
        text(
            "SELECT channel_id, rule, priority, status, prediction_id IS NOT NULL, due_date FROM work_orders "
            "WHERE run_id = :run ORDER BY id"
        ),
        {"run": run_id},
    )
    return [tuple(r) for r in rows]


def test_config_covers_all_rules_with_valid_priorities():
    cfg = config()
    assert set(cfg) == set(RULES)
    assert all(rc["priority"] in PRIORITIES for rc in cfg.values())
    assert rule_description("fault_persisting") and rule_description("manual") is None


async def test_prediction_rules(session):
    run = await make_run(session)
    smoke = await prediction(session, run, 1001, "smoke_alarm", 0.82, True)
    quiet = await prediction(session, run, 1001, "smoke_alarm", 0.1, False, at=NOW + dt.timedelta(minutes=1))
    skipped = await prediction(session, run, 1002, "smoke_alarm", None, None, "skipped")
    # порог 0.3: средний приоритет — от 0.3 + 0.5 × 0.7 = 0.65
    daily_hi = await prediction(session, run, 1002, "daily", 0.7, True)
    daily_lo = await prediction(session, run, 1003, "daily", 0.6, True)

    notes = await apply_prediction_rules(session, run, [smoke, quiet, skipped, daily_hi, daily_lo])
    assert await orders(session) == [
        (1001, "alarm_model_alert", "high", "new", True, dt.date(2026, 5, 11)),  # срок 24 ч
        (1002, "daily_model_alert", "medium", "new", True, dt.date(2026, 5, 17)),  # верхняя половина над порогом
        (1003, "daily_model_alert", "low", "new", True, dt.date(2026, 5, 17)),
    ]
    first = notes[0]
    assert first.type == "work_order.created" and first.run_id == run
    assert first.data["channel_name"] == "ДД ПК1" and first.data["object_name"] == "Объект А"
    assert first.data["object_id"] == 10 and first.data["prediction_id"] == smoke
    # подобъект канала: имена каналов повторяются на подобъектах объекта
    assert first.data["sub_object_id"] == 100 and first.data["sub_object_name"] == "Подобъект А1"
    assert first.data["title"] == "Проверить датчик дыма ДД ПК1: высокая вероятность неисправности в ближайшие 24 ч"
    assert first.data["created_at"] == NOW  # время решения модели

    # новая тревога модели по каналу с открытой заявкой — второй не будет
    again = await prediction(session, run, 1001, "smoke_alarm", 0.9, True, at=NOW + dt.timedelta(hours=2))
    assert await apply_prediction_rules(session, run, [again]) == []
    # закрыли — следующая тревога модели даёт новую заявку
    await session.execute(text("UPDATE work_orders SET status = 'done' WHERE channel_id = 1001"))
    assert len(await apply_prediction_rules(session, run, [again])) == 1


async def state(session, run, channel_id, value, fault_since):
    await session.execute(
        text(
            "INSERT INTO channel_state (run_id, channel_id, ts, value_raw, is_alarm, fault_since) "
            "VALUES (:run, :ch, :ts, :v, false, :since) "
            "ON CONFLICT (run_id, channel_id) DO UPDATE SET value_raw = :v, fault_since = :since, ts = :ts"
        ),
        {"run": run, "ch": channel_id, "ts": fault_since or NOW, "v": value, "since": fault_since},
    )


async def test_fault_persisting(session):
    run = await make_run(session)
    await state(session, run, 1001, "Неисправен", NOW - dt.timedelta(hours=80))
    await state(session, run, 1002, "Отключено устройство", NOW - dt.timedelta(hours=100))  # не «Неисправен»
    await state(session, run, 1003, "Неисправен", NOW - dt.timedelta(hours=10))  # меньше 72 ч

    notes = await apply_periodic_rules(session, run, NOW)
    assert await orders(session) == [(1001, "fault_persisting", "high", "new", False, dt.date(2026, 5, 11))]
    assert notes[0].data["title"] == "Устранить неисправность: датчик дыма ДД ПК1 в «Неисправен» больше 80 ч"

    # закрыли, а неисправность та же — заявка не повторяется
    await session.execute(text("UPDATE work_orders SET status = 'done'"))
    assert await apply_periodic_rules(session, run, NOW + dt.timedelta(hours=1)) == []
    # новая неисправность 1001 — новая заявка; к этому времени и 1003 в «Неисправен» больше 72 ч
    await state(session, run, 1001, "Неисправен", NOW + dt.timedelta(hours=2))
    notes = await apply_periodic_rules(session, run, NOW + dt.timedelta(hours=75))
    assert sorted(n.data["channel_id"] for n in notes) == [1001, 1003]


async def test_fault_persisting_after_data_gap_counts_from_journal_start(session):
    # журнал 01.08.2026 после конца базы 30.06 (прогон 60): неисправность с 13.06 через месяц без данных не длится
    base_end = dt.datetime(2026, 6, 30, 23, 59, tzinfo=MSK)
    start = dt.datetime(2026, 8, 1, 0, 0, tzinfo=MSK)
    fault = dt.datetime(2026, 6, 13, 10, 0, tzinfo=MSK)
    await state(session, None, 1002, "Норма", None)
    await session.execute(text("UPDATE channel_state SET ts = :t WHERE run_id IS NULL"), {"t": base_end})
    await session.execute(
        text("INSERT INTO runs (id, scenario, status, sim_start, sim_now) VALUES (60, 'upload', 'running', :s, :s)"),
        {"s": start},
    )
    await state(session, 60, 1001, "Неисправен", fault)

    assert await apply_periodic_rules(session, 60, start + dt.timedelta(minutes=19)) == []  # было «больше 1170 ч»
    assert await apply_periodic_rules(session, 60, start + dt.timedelta(hours=71)) == []
    notes = await apply_periodic_rules(session, 60, start + dt.timedelta(hours=73))
    assert [n.data["channel_id"] for n in notes] == [1001]
    assert notes[0].data["title"].endswith("больше 73 ч")


async def test_fault_persisting_inside_base_keeps_fault_duration(session):
    # сценарий внутри базы: старт раньше её конца — разрыва нет, длительность от начала неисправности, как раньше
    await state(session, None, 1002, "Норма", None)
    await session.execute(
        text("UPDATE channel_state SET ts = :t WHERE run_id IS NULL"), {"t": dt.datetime(2026, 6, 30, tzinfo=MSK)}
    )
    run = await make_run(session)
    await state(session, run, 1001, "Неисправен", NOW - dt.timedelta(hours=80))
    notes = await apply_periodic_rules(session, run, NOW)
    assert notes[0].data["title"].endswith("больше 80 ч")


async def episode(session, run, channel_ids, start, kind="solitary"):
    await session.execute(
        text(
            "INSERT INTO alarm_episodes (run_id, object_id, start_ts, end_ts, n_channels, n_alarms, kind, "
            "channel_ids) VALUES (:run, 10, :s, :s, :n, :n, :kind, :ids)"
        ),
        {"run": run, "s": start, "n": len(channel_ids), "kind": kind, "ids": channel_ids},
    )


async def test_solitary_repeat_counts_episodes_in_window(session):
    run = await make_run(session)
    day = dt.timedelta(days=1)
    for d in (1, 5, 20):
        await episode(session, run, [1001], NOW - d * day)
    await episode(session, run, [1003], NOW - 40 * day)  # вне окна 30 суток
    for d in (2, 3):
        await episode(session, run, [1003], NOW - d * day)
    await episode(session, run, [1002, 1003], NOW - 4 * day, kind="group")  # групповой не считается
    await episode(session, run, [1002], NOW + day)  # в будущем относительно часов прогона

    notes = await apply_periodic_rules(session, run, NOW)
    assert await orders(session) == [(1001, "solitary_repeat", "medium", "new", False, dt.date(2026, 5, 17))]
    assert "3 одиночных тревог" in notes[0].data["title"]
    await session.execute(text("UPDATE work_orders SET status = 'rejected'"))
    assert await apply_periodic_rules(session, run, NOW + day) == []  # то же окно — не повторяется


async def test_apply_rules_task_uses_active_run_clock(session):
    await make_run(session, run_id=3, status="stopped")
    run = await make_run(session, run_id=4, sim_now=NOW + dt.timedelta(days=4))
    await state(session, run, 1001, "Неисправен", NOW)
    assert await rules_task.apply(session) == 1  # на часах прогона 4 суток неисправности
    assert await orders(session, run_id=4) == [(1001, "fault_persisting", "high", "new", False, dt.date(2026, 5, 15))]


async def test_daily_orders_are_capped_by_score_per_day(session, monkeypatch):
    cfg = {k: dict(v) for k, v in config().items()}
    cfg["daily_model_alert"]["max_per_day"] = 2
    monkeypatch.setattr("app.domain.work_orders.config", lambda: cfg)
    run = await make_run(session)
    ids = [
        await prediction(session, run, ch, "daily", score, True)
        for ch, score in ((1001, 0.4), (1002, 0.9), (1003, 0.7))
    ]
    notes = await apply_prediction_rules(session, run, ids)
    assert sorted(n.data["channel_id"] for n in notes) == [1002, 1003]  # две лучшие по оценке
    # тот же день — лимит исчерпан; следующий день — снова до двух
    late = await prediction(session, run, 1001, "daily", 0.95, True, at=NOW + dt.timedelta(hours=1))
    assert await apply_prediction_rules(session, run, [late]) == []
    nxt = await prediction(session, run, 1001, "daily", 0.5, True, at=NOW + dt.timedelta(days=1))
    assert len(await apply_prediction_rules(session, run, [nxt])) == 1


async def test_external_order_names_target_object(session):
    """У внешних моделей прогноз — на служебном канале цели «Узел 12 · оценка»; в названии заявки — объект «Узел 12»."""
    await session.execute(
        text(
            "INSERT INTO objects (id, parent_id, level, kind, name, source) VALUES "
            "(9010112, NULL, 3, 'sensor_node', 'Узел 12', 'magdeburg')"
        )
    )
    await session.execute(
        text(
            "INSERT INTO channels (id, object_id, system_type, sensor_type, tag, name, source) VALUES "
            "(901011200, 9010112, 'Пожарный мониторинг (внешн.)', 'Узел', 'node', 'Узел 12 · оценка', 'magdeburg')"
        )
    )
    run_id = await make_run(session)
    pid = await prediction(session, run_id, 901011200, "magdeburg_fire", 0.64, True)
    await apply_prediction_rules(session, run_id, [pid])
    title = await session.scalar(text("SELECT title FROM work_orders WHERE prediction_id = :p"), {"p": pid})
    assert title == "Проверить Узел 12: признаки пожара (0.64)"
