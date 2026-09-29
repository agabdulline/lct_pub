import datetime as dt
import json
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import text

from app.domain.daily import daily_forecast
from app.domain.ingest import RawEvent, ingest_events
from app.domain.metrics import block, live, live_by_run, model_metrics
from app.domain.ml_client import MlClient
from app.domain.outcomes import close_outcomes, close_outcomes_history
from app.domain.runs import create_run, recently_closed_runs, set_run_status
from app.domain.schedule import close_recent_runs, due_daily_date, tick
from app.domain.triage import score_object, verdict

MSK = ZoneInfo("Europe/Moscow")


def at(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s).replace(tzinfo=MSK)


def fake_ml(decide) -> MlClient:
    """ML-сервис: `decide(channel_id, ts)` → поля результата; None вместо функции — сервис отвечает 503."""

    def handler(request: httpx.Request) -> httpx.Response:
        if decide is None:
            return httpx.Response(503, json={"error": {"code": "model_missing", "message": "нет модели"}})
        body = json.loads(request.content)
        results = []
        for a in body["alarms"]:
            ts = dt.datetime.fromisoformat(a["ts"])
            r = {
                "channel_id": a["channel_id"],
                "decision_time": (ts + dt.timedelta(seconds=1)).isoformat(),
                "seen_channel": True,
                "model": "baseline-v0",
                **decide(a["channel_id"], ts),
            }
            results.append(r)
        return httpx.Response(200, json={"model": "baseline-v0", "results": results, "elapsed_ms": 1})

    return MlClient("http://ml", transport=httpx.MockTransport(handler))


def scored(score: float, threshold: float = 0.5) -> dict:
    return {
        "status": "scored",
        "skip_reason": None,
        "score": score,
        "threshold": threshold,
        "alert": score >= threshold,
        "object_history": True,
        "factors": [{"feature": "loop_past_rate", "value": 0.2, "contribution": 0.3}],
    }


def test_verdict_priority():
    assert verdict("ppr", {"status": "scored", "alert": True}) == "ppr"
    assert verdict("solitary", {"status": "scored", "alert": True}) == "sensor_fault"
    assert verdict("solitary", {"status": "scored", "alert": False}) == "reaction"
    assert verdict("solitary", None) == "no_score"
    assert verdict("group", None) == "reaction"


async def add_smoke_channel(session, channel_id: int) -> None:
    await session.execute(
        text(
            "INSERT INTO channels (id, object_id, system_type, sensor_type, tag, name) "
            "VALUES (:id, 100, 'Пожарная охрана', 'Датчик дыма', '15-11.1.4.1.', 'ДД ПК' || :id)"
        ),
        {"id": channel_id},
    )


async def alarm(session, channel: int, ts: str) -> None:
    await ingest_events(None, [RawEvent(channel, at(ts), "Обнаружен дым", True)], session=session)


async def verdicts(session) -> list[tuple]:
    rows = await session.execute(
        text(
            "SELECT a.channel_id, a.verdict, p.status, p.skip_reason, p.alarm_id = a.id AS own "
            "FROM alarms a LEFT JOIN predictions p ON p.id = a.prediction_id ORDER BY a.ts"
        )
    )
    return [tuple(r) for r in rows]


@pytest.mark.anyio
async def test_scoring_sets_verdicts_and_inherits(session):
    await alarm(session, 1001, "2026-05-01T12:00:00")
    ml = fake_ml(lambda ch, ts: scored(0.8))
    result = await score_object(session, None, 10, ml)
    assert (result.alarms, result.scored, result.model_version) == (1, 1, "baseline-v0")
    assert [n.type for n in result.notifications] == ["prediction.created", "alarm.scored"]
    assert result.notifications[0].data["model"] == "smoke_alarm" and result.notifications[0].data["score"] == 0.8
    assert result.notifications[1].data["prediction"]["alert"] is True
    encoded = json.loads(result.notifications[0].encode())["data"]["decision_time"]
    assert "T" in encoded and encoded.endswith("+00:00")  # ISO 8601, как в REST

    # вторая тревога того же канала через час: модель пропускает — одно решение на канал в сутки
    await alarm(session, 1001, "2026-05-01T13:00:00")
    ml = fake_ml(lambda ch, ts: {"status": "skipped", "skip_reason": "decided_within_24h"})
    await score_object(session, None, 10, ml)
    assert await verdicts(session) == [
        (1001, "sensor_fault", "scored", None, True),
        (1001, "sensor_fault", "scored", None, False),  # решение унаследовано
    ]
    horizon = await session.scalar(text("SELECT horizon_end - decision_time FROM predictions WHERE status='scored'"))
    history = (await session.execute(text("SELECT status, object_history FROM predictions ORDER BY id"))).all()
    assert [tuple(h) for h in history] == [("scored", True), ("skipped", None)]  # поле из ml-service — в журнал
    assert horizon == dt.timedelta(hours=24)
    assert await score_object(session, None, 10, ml) == type(result)()  # всё оценено — повторно не шлём


@pytest.mark.anyio
async def test_ml_unavailable_group_and_solitary(session):
    await alarm(session, 1001, "2026-05-01T12:00:00")
    result = await score_object(session, None, 10, fake_ml(None))
    assert result.skipped == 1
    assert await verdicts(session) == [(1001, "no_score", "skipped", "ml_unavailable", True)]
    context = await session.scalar(text("SELECT context FROM predictions"))
    assert context == {"alarm_kind": "solitary", "neighbors_alarmed_1h": 0}

    await add_smoke_channel(session, 1004)
    await alarm(session, 1004, "2026-05-01T12:05:00")  # второй датчик — эпизод стал групповым
    await score_object(session, None, 10, fake_ml(None))
    assert [v[1] for v in await verdicts(session)] == ["reaction", "reaction"]


@pytest.mark.anyio
async def test_outcomes_and_metrics(session):
    await add_smoke_channel(session, 1004)
    await alarm(session, 1001, "2026-05-01T12:00:00")
    await alarm(session, 1004, "2026-05-01T12:00:00")
    await score_object(session, None, 10, fake_ml(lambda ch, ts: scored(0.9 if ch == 1001 else 0.1)))
    # у 1001 через 2 часа неисправность — прогноз сбылся
    await ingest_events(None, [RawEvent(1001, at("2026-05-01T14:00:00"), "Неисправен", True)], session=session)
    # «Отключено устройство» — не исход: цель моделей — только начало «Неисправен»
    await ingest_events(
        None, [RawEvent(1004, at("2026-05-01T14:00:00"), "Отключено устройство", True)], session=session
    )
    counts, updates = await close_outcomes(session, None, at("2026-05-01T15:00:00"))
    assert counts == {"fault": 1, "no_fault": 0}
    assert [(n.type, n.data["outcome"], n.data["model"]) for n in updates] == [
        ("prediction.updated", "fault", "smoke_alarm")
    ]
    assert updates[0].data["outcome_at"] == at("2026-05-01T14:00:00")
    early = await model_metrics(session, None, "smoke_alarm", at("2026-05-01T15:00:00"))
    assert early["all"]["decisions"] == 0 and early["open"] == 2  # горизонт не истёк — в метрики не идёт
    assert early["open_fault"] == 1 and early["horizon_hours"] == [0.0, 24.0]  # неисправность уже видна
    assert "открыто 2, у 1 неисправность уже началась" in early["note"]
    assert (await close_outcomes(session, None, at("2026-05-02T13:00:00")))[0] == {"fault": 0, "no_fault": 1}
    m = await model_metrics(session, None, "smoke_alarm", at("2026-05-02T13:00:00"))
    assert (m["all"]["tp"], m["all"]["tn"], m["all"]["precision"], m["all"]["recall"]) == (1, 1, 1.0, 1.0)
    assert m["all"]["lead_time"]["p50_s"] == pytest.approx(2 * 3600 - 1)
    assert m["model_version"] == "baseline-v0" and m["open"] == 0
    assert m["open_fault"] == 0 and m["note"] is None


@pytest.mark.anyio
async def test_outcomes_closed_after_run_finished(session):
    run = await create_run(session, scenario="t", sim_start=at("2026-05-01T12:00:00"))
    await ingest_events(run.id, [RawEvent(1001, at("2026-05-01T12:00:00"), "Обнаружен дым", True)], session=session)
    await score_object(session, run.id, 10, fake_ml(lambda ch, ts: scored(0.9)))
    run.sim_now = at("2026-05-01T12:30:00")
    summary, _ = await tick(session, run, fake_ml(None))
    assert summary["outcomes"] == {"fault": 0, "no_fault": 0}
    # неисправность началась после последнего такта, и прогон завершился раньше следующего
    await ingest_events(run.id, [RawEvent(1001, at("2026-05-01T12:40:00"), "Неисправен", True)], session=session)
    run.sim_now = at("2026-05-01T12:45:00")
    await set_run_status(session, run.id, "finished")
    counts, updates = await close_recent_runs(session)
    assert counts == {run.id: {"fault": 1, "no_fault": 0, "history_fault": 0, "history_no_fault": 0}}
    assert [(n.run_id, n.data["outcome"], n.data["outcome_at"]) for n in updates] == [
        (run.id, "fault", at("2026-05-01T12:40:00"))
    ]
    assert (await close_recent_runs(session))[0] == {}  # повтор ничего не меняет
    await session.execute(text("UPDATE runs SET finished_at = now() - interval '2 hours' WHERE id = :r"), {"r": run.id})
    assert await recently_closed_runs(session) == []  # закрытый больше часа назад такт уже не трогает


@pytest.mark.anyio
async def test_history_outcomes_after_short_run(session):
    """Прогон короче горизонта (как ppr-failures): после конца прогона исходы досчитываются по журналу."""
    await add_smoke_channel(session, 1004)
    # базовая история — тот же журнал: у 1001 отказ через 4 ч после конца прогона, данные — до 03.05 включительно
    base = [
        RawEvent(1001, at("2026-05-01T09:00:00"), "Норма", False),
        RawEvent(1004, at("2026-05-01T09:00:00"), "Норма", False),
        RawEvent(1001, at("2026-05-01T17:00:00"), "Неисправен", True),
        RawEvent(1003, at("2026-05-03T10:00:00"), "18", False),
    ]
    await ingest_events(None, base, session=session)
    run = await create_run(session, scenario="ppr-failures", sim_start=at("2026-05-01T12:00:00"))
    smoke = [RawEvent(ch, at("2026-05-01T12:10:00"), "Обнаружен дым", True) for ch in (1001, 1004)]
    await ingest_events(run.id, smoke, session=session)
    await score_object(session, run.id, 10, fake_ml(lambda ch, ts: scored(0.9 if ch == 1001 else 0.1)))
    run.sim_now = at("2026-05-01T13:00:00")
    with pytest.raises(ValueError):
        await close_outcomes_history(session, run)  # прогон ещё идёт
    await set_run_status(session, run.id, "finished")
    before = await model_metrics(session, run.id, "smoke_alarm", run.sim_now)
    assert before["all"]["precision"] is None and before["open"] == 2 and before["history"] == 0

    counts, updates = await close_outcomes_history(session, run)
    assert counts == {"fault": 0, "no_fault": 0, "history_fault": 1, "history_no_fault": 1}
    assert {n.data["outcome_source"] for n in updates} == {"history"}
    rows = await session.execute(
        text(
            "SELECT channel_id, outcome, outcome_at, outcome_source FROM predictions "
            "WHERE run_id = :r AND model = 'smoke_alarm' ORDER BY channel_id"
        ),
        {"r": run.id},
    )
    assert [tuple(r) for r in rows] == [
        (1001, "fault", at("2026-05-01T17:00:00"), "history"),  # отказ после конца прогона засчитан
        (1004, "no_fault", at("2026-05-02T12:10:01"), "history"),  # горизонт внутри данных, отказа нет
    ]
    m = await model_metrics(session, run.id, "smoke_alarm", run.sim_now)
    assert (m["all"]["tp"], m["all"]["tn"], m["all"]["precision"], m["all"]["recall"]) == (1, 1, 1.0, 1.0)
    assert m["history"] == 2 and m["outcomes_until"] == at("2026-05-04T00:00:00") and m["open"] == 0
    assert m["all"]["lead_time"]["p50_s"] == pytest.approx(5 * 3600 - 60 * 10 - 1)
    summary = live(m)  # сводка прогона: /runs и дашборд
    assert (summary["history"], summary["alerts"], summary["alerts_confirmed"], summary["open"]) == (2, 1, 1, 0)
    by_run = await live_by_run(session, [run.id])  # таблица прогонов — один запрос, то же правило закрытых
    assert by_run[run.id]["smoke_alarm"] == summary
    assert by_run[run.id]["daily"] == live(await model_metrics(session, run.id, "daily", run.sim_now))
    again, _ = await close_outcomes_history(session, run)
    assert set(again.values()) == {0}  # повтор ничего не меняет


def test_metric_block_edges():
    assert block([])["precision"] is None
    b = block([(False, True, None), (False, False, None)])
    assert (b["precision"], b["recall"], b["f1"], b["positives"]) == (None, 0.0, 0.0, 1)


@pytest.mark.anyio
async def test_daily_skipped_when_model_missing_and_tick(session):
    await ingest_events(None, [RawEvent(1003, at("2026-05-01T09:00:00"), "18", False)], session=session)
    run = await create_run(session, scenario="t", sim_start=at("2026-05-01T12:00:00"))
    # дата старта — сразу, хотя 00:10 этого дня было до старта: признаки — только базовая история до даты
    assert due_daily_date(run) == dt.date(2026, 5, 1)
    summary, notes = await tick(session, run, fake_ml(None))
    assert summary["daily"]["predictions"] == 1 and notes[0].type == "daily_forecast.completed"
    sql = "SELECT status, skip_reason, decision_time, horizon_start FROM predictions"
    row = (await session.execute(text(sql))).one()
    assert tuple(row) == ("skipped", "model_missing", at("2026-05-01T00:00:00"), at("2026-05-02T00:00:00"))
    run.sim_now = at("2026-05-01T23:50:00")
    summary, notes = await tick(session, run, fake_ml(None))  # такт позже в тот же день — без повтора
    assert "daily" not in summary and notes == []

    run.sim_now = at("2026-05-02T00:05:00")
    assert due_daily_date(run) is None  # следующий день — только с 00:10
    run.sim_now = at("2026-05-02T00:15:00")
    assert due_daily_date(run) == dt.date(2026, 5, 2)
    summary, notes = await tick(session, run, fake_ml(None))
    assert summary["daily"]["predictions"] == 1
    again, _ = await daily_forecast(session, run.id, dt.date(2026, 5, 2), fake_ml(None))
    assert again["predictions"] == 0
    assert await session.scalar(text("SELECT count(*) FROM predictions WHERE model = 'daily'")) == 2


def test_archive_run(db_url, tmp_path, seeded):
    import asyncio

    import duckdb
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.domain.maintenance import archive_run
    from app.loader import pg_conninfo

    async def prepare() -> int:
        engine = create_async_engine(db_url, poolclass=NullPool)
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            run = await create_run(s, scenario="a", sim_start=at("2026-05-01T12:00:00"))
            events = [
                RawEvent(1002, at("2026-05-01T12:00:00"), "0.01", False),
                RawEvent(1002, at("2026-05-01T12:01:00"), "0.02", False),
                RawEvent(1003, at("2026-05-01T12:02:00"), "-127", False),
            ]
            await ingest_events(run.id, events, session=s)
            await s.commit()
            run_id = run.id
        await engine.dispose()
        return run_id

    run_id = asyncio.run(prepare())
    report = archive_run(pg_conninfo(db_url.render_as_string(hide_password=False)), tmp_path, run_id)
    assert report["events"] == 3  # весь поток прогона, включая газ внутри зоны нечувствительности
    rows = duckdb.sql(f"SELECT channel_id, value_raw, state FROM '{report['path']}' ORDER BY ts").fetchall()
    assert rows == [(1002, "0.01", None), (1002, "0.02", None), (1003, "-127", "Нет данных")]


# --- гонки: вид эпизода меняется, пока идёт скоринг; потерянные задачи ------------------------------------------


@pytest.mark.anyio
async def test_ppr_recognised_during_scoring_is_not_overwritten(session, db_url):
    """Скоринг прочитал эпизод «группа», а пока ждал ML-сервис, приём событий распознал плановую проверку
    и закоммитил ppr + «уточнён». Вердикт модели не должен перетереть ppr."""
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    await add_smoke_channel(session, 1004)
    await alarm(session, 1001, "2026-05-04T09:30:00")
    await alarm(session, 1004, "2026-05-04T09:31:00")
    await session.commit()

    other = create_async_engine(db_url, poolclass=NullPool)

    async def handler(request: httpx.Request) -> httpx.Response:
        async with other.begin() as conn:  # «приём событий» в своей транзакции
            await conn.execute(text("UPDATE alarm_episodes SET kind = 'ppr'"))
            await conn.execute(text("UPDATE alarms SET verdict = 'ppr', verdict_refined = true"))
        body = json.loads(request.content)
        results = [
            {
                "channel_id": a["channel_id"],
                "status": "scored",
                "score": 0.1,
                "threshold": 0.5,
                "alert": False,
                "model": "baseline-v0",
            }
            for a in body["alarms"]
        ]
        return httpx.Response(200, json={"model": "baseline-v0", "results": results})

    ml = MlClient("http://ml", transport=httpx.MockTransport(handler))
    await score_object(session, None, 10, ml)
    await session.commit()
    await other.dispose()
    rows = (await session.execute(text("SELECT verdict, verdict_refined, prediction_id IS NOT NULL FROM alarms"))).all()
    assert [tuple(r) for r in rows] == [("ppr", True, True), ("ppr", True, True)]


@pytest.mark.anyio
async def test_reconcile_repairs_contradictions(session):
    from app.domain.triage import reconcile_verdicts

    await add_smoke_channel(session, 1004)
    await alarm(session, 1001, "2026-05-04T09:30:00")
    await alarm(session, 1004, "2026-05-04T09:31:00")
    ids = [r[0] for r in await session.execute(text("SELECT id FROM alarms ORDER BY id"))]
    # как на стенде после оборванного прогона: эпизод ППР, а вердикт — реакция с пометкой «уточнён»
    await session.execute(text("UPDATE alarm_episodes SET kind = 'ppr'"))
    await session.execute(text("UPDATE alarms SET verdict = 'reaction', verdict_refined = true"))
    assert sorted(await reconcile_verdicts(session, ids)) == ids
    assert await verdicts(session) == [(1001, "ppr", None, None, None), (1004, "ppr", None, None, None)]

    await session.execute(text("UPDATE alarm_episodes SET kind = 'group'"))
    await session.execute(text("UPDATE alarms SET verdict = 'reaction', verdict_refined = true"))
    await reconcile_verdicts(session, ids)
    flags = (await session.execute(text("SELECT DISTINCT verdict, verdict_refined FROM alarms"))).all()
    assert [tuple(f) for f in flags] == [("reaction", False)]  # «уточнён» — только вместе с ППР


@pytest.mark.anyio
async def test_sweep_finds_alarms_left_without_decision(session):
    from app.domain.scoring_queue import sweep_pending

    run = await create_run(session, scenario="t", sim_start=at("2026-05-04T09:00:00"))
    await ingest_events(run.id, [RawEvent(1001, at("2026-05-04T09:30:00"), "Обнаружен дым", True)], session=session)
    assert await sweep_pending(session) == {run.id: {10}}  # задача потерялась — объект снова в скоринге
    await session.execute(text("UPDATE runs SET status = 'stopped', finished_at = now() - interval '2 hours'"))
    assert await sweep_pending(session) == {}  # давно закрытые прогоны не досчитываются

    await session.execute(text("UPDATE runs SET finished_at = now()"))
    await score_object(session, run.id, 10, fake_ml(None))
    assert await sweep_pending(session) == {}  # решение записано (пропуск тоже решение)
