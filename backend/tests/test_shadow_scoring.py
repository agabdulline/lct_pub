"""Теневой скоринг (этап 15.2): v0 и v3 оценивают одни тревоги, вердикт и заявки — от основной версии прогона."""

import datetime as dt
import json
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import text

from app.domain.ingest import RawEvent, ingest_events
from app.domain.metrics import live_by_run, model_metrics
from app.domain.ml_client import MlClient, MlUnavailable
from app.domain.model_versions import select_versions
from app.domain.runs import RunError, create_run
from app.domain.scoring_queue import sweep_pending
from app.domain.triage import score_object
from app.domain.work_orders import apply_prediction_rules

MSK = ZoneInfo("Europe/Moscow")
MODELS = [
    {"name": "smoke_alarm", "label": "baseline-v0", "family": "baseline", "scoring": "available"},
    {"name": "daily", "label": "baseline-v0", "family": "baseline", "scoring": "available"},
    {"name": "alarm_all_d7", "label": "v3", "family": "v3", "scoring": "available", "active": True},
    {"name": "magdeburg_fire", "label": "magdeburg-fire-v0", "family": "external", "scoring": "available"},
]


def at(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s).replace(tzinfo=MSK)


def v3_alerts(version: str | None, ts: dt.datetime) -> float | None:
    return 0.9 if version == "v3" else 0.1


class Ml(MlClient):
    """ML-сервис с двумя версиями событийной модели; `score(версия, время тревоги)` — оценка, None или причина —
    пропуск. По
    умолчанию v0 тревоги не видит, v3 — видит. `answer_as` — версия в ответе вместо запрошенной. Запросы
    запоминаются: версия и число тревог."""

    def __init__(self, models: list[dict] | None = MODELS, score=v3_alerts, answer_as: str | None = None) -> None:
        self.versions: list[str | None] = []
        self.sizes: list[int] = []

        def result(version: str | None, alarm: dict) -> dict:
            ts = dt.datetime.fromisoformat(alarm["ts"])
            value = score(version, ts)
            base = {
                "channel_id": alarm["channel_id"],
                "decision_time": (ts + dt.timedelta(seconds=1)).isoformat(),
                "model": version or "baseline-v0",
                "threshold": 0.5,
                "seen_channel": True,
                "object_history": True,
                "factors": [],
            }
            if value is None or isinstance(value, str):  # пропуск: причина строкой, None — coverage_gap
                reason = value or "coverage_gap"
                return base | {"status": "skipped", "skip_reason": reason, "score": None, "alert": None}
            return base | {"status": "scored", "score": value, "alert": value >= 0.5}

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            version = body.get("model_version")
            self.versions.append(version)
            self.sizes.append(len(body["alarms"]))
            results = [result(version, a) for a in body["alarms"]]
            return httpx.Response(200, json={"model": answer_as or version or "baseline-v0", "results": results})

        super().__init__("http://ml", transport=httpx.MockTransport(handler))
        self._models = models

    async def models(self) -> list[dict]:
        if self._models is None:
            raise MlUnavailable("ml-service недоступен")
        return self._models


@pytest.mark.anyio
async def test_select_versions():
    # по умолчанию — связка «v3, где оценила, иначе v0», если обе готовы; теневых нет — обе её части
    assert await select_versions(None, Ml()) == ("v3>baseline-v0", [])
    assert await select_versions("v3>baseline-v0", Ml()) == ("v3>baseline-v0", [])
    assert await select_versions("baseline-v0", Ml()) == ("baseline-v0", ["v3"])
    assert await select_versions("v3", Ml()) == ("v3", ["baseline-v0"])
    with pytest.raises(RunError) as e:
        await select_versions("v9>baseline-v0", Ml())
    assert e.value.code == "unknown_version"
    with pytest.raises(RunError) as e:
        await select_versions("v9", Ml())
    assert e.value.code == "unknown_version"
    assert await select_versions(None, Ml(None)) == (None, [])  # сервис недоступен — одна активная модель
    waiting = [m | {"scoring": "waiting_feature_code"} if m["label"] == "v3" else m for m in MODELS]
    assert await select_versions(None, Ml(waiting)) == ("baseline-v0", [])  # v3 ещё не скорит — v0, теневой нет


async def smoke_alarm(session, run_id: int, ts: str) -> None:
    await ingest_events(run_id, [RawEvent(1001, at(ts), "Обнаружен дым", True)], session=session)


async def by_version(session, run_id: int) -> list[tuple]:
    rows = await session.execute(
        text(
            "SELECT p.model_version, p.alert, a.prediction_id = p.id AS primary_link, a.verdict FROM predictions p "
            "JOIN alarms a ON a.id = p.alarm_id WHERE p.run_id = :r ORDER BY a.ts, p.model_version"
        ),
        {"r": run_id},
    )
    return [tuple(r) for r in rows]


@pytest.mark.anyio
async def test_shadow_scoring_primary_verdict(session):
    ml = Ml()
    primary, shadows = await select_versions("baseline-v0", ml)  # v0 основная, v3 теневая — как до связки
    run = await create_run(
        session, scenario="t", sim_start=at("2026-05-01T11:00:00"), primary_model=primary, shadow_models=shadows
    )
    await smoke_alarm(session, run.id, "2026-05-01T12:00:00")

    result = await score_object(session, run.id, 10, ml)
    assert ml.versions == ["baseline-v0", "v3"] and result.shadow == {"v3": 1}
    # оценки обеих версий, вердикт и ссылка — от основной v0: тревоги модели нет, реакция
    assert await by_version(session, run.id) == [
        ("baseline-v0", False, True, "reaction"),
        ("v3", True, False, "reaction"),
    ]
    # заявки — только по прогнозам основной версии: тревога v3 заявку не создаёт
    assert await apply_prediction_rules(session, run.id, result.prediction_ids) == []
    assert await score_object(session, run.id, 10, ml) == type(result)()  # обе версии оценили — повторно не шлём

    # смена основной на v3: новые тревоги получают её вердикт, прошлые не пересчитываются
    await session.execute(
        text("UPDATE runs SET primary_model = 'v3', shadow_models = '{baseline-v0}' WHERE id = :r"), {"r": run.id}
    )
    await smoke_alarm(session, run.id, "2026-05-01T13:00:00")
    result = await score_object(session, run.id, 10, ml)
    rows = await by_version(session, run.id)
    assert rows[2:] == [("baseline-v0", False, False, "sensor_fault"), ("v3", True, True, "sensor_fault")]
    assert rows[0][3] == "reaction"
    assert len(await apply_prediction_rules(session, run.id, result.prediction_ids)) == 1

    # метрики и live — только основная версия прогона, версии не смешиваются
    await session.execute(text("UPDATE predictions SET outcome = 'fault', outcome_at = decision_time"))
    m = await model_metrics(session, run.id, "smoke_alarm", at("2026-05-03T00:00:00"))
    assert (m["all"]["decisions"], m["model_version"]) == (2, "v3")
    await session.execute(
        text("UPDATE runs SET sim_now = :t WHERE id = :r"), {"t": at("2026-05-03T00:00:00"), "r": run.id}
    )
    live = (await live_by_run(session, [run.id]))[run.id]["smoke_alarm"]
    assert (live["decisions"], live["alerts"]) == (2, 2)


@pytest.mark.anyio
async def test_sweep_finds_missing_shadow_version(session):
    run = await create_run(
        session, scenario="t", sim_start=at("2026-05-01T11:00:00"), primary_model="baseline-v0", shadow_models=["v3"]
    )
    await smoke_alarm(session, run.id, "2026-05-01T12:00:00")
    only_v0 = Ml([m for m in MODELS if m["label"] != "v3"])
    await session.execute(text("UPDATE runs SET shadow_models = '{}' WHERE id = :r"), {"r": run.id})
    await score_object(session, run.id, 10, only_v0)
    assert await sweep_pending(session) == {}
    # вернули теневую v3 — у тревоги нет её прогноза, досчёт ставит объект заново
    await session.execute(text("UPDATE runs SET shadow_models = '{v3}' WHERE id = :r"), {"r": run.id})
    assert await sweep_pending(session) == {run.id: {10}}
    ml = Ml()
    await score_object(session, run.id, 10, ml)
    assert ml.versions == ["v3"] and await sweep_pending(session) == {}


def test_patch_primary_model(client, monkeypatch):
    async def fake_models(self):
        return MODELS

    monkeypatch.setattr("app.domain.ml_client.MlClient.models", fake_models)
    import asyncio

    from app.db.session import SessionLocal

    async def make() -> int:
        async with SessionLocal() as s:
            run = await create_run(
                s, scenario="t", sim_start=at("2026-05-01T11:00:00"), primary_model="baseline-v0", shadow_models=["v3"]
            )
            await s.commit()
            return run.id

    run_id = asyncio.run(make())
    r = client.get(f"/api/v1/runs/{run_id}").json()
    assert (r["primary_model"], r["shadow_models"]) == ("baseline-v0", ["v3"])
    r = client.patch(f"/api/v1/runs/{run_id}", json={"primary_model": "v3"})
    assert r.status_code == 200, r.text
    assert (r.json()["primary_model"], r.json()["shadow_models"]) == ("v3", ["baseline-v0"])
    assert r.json()["reverdict"] is None  # без apply_to_past прошлые вердикты не трогаются
    assert client.patch(f"/api/v1/runs/{run_id}", json={"primary_model": "v9"}).json()["error"]["code"] == (
        "unknown_version"
    )
    asyncio.run(_stop(run_id))
    r = client.patch(f"/api/v1/runs/{run_id}", json={"primary_model": "baseline-v0"})
    assert (r.status_code, r.json()["error"]["code"]) == (409, "run_finished")
    # прогон внешнего набора: дымовых тревог хакатона нет — и переключать нечего
    base = asyncio.run(_dataset_run())
    r = client.patch(f"/api/v1/runs/{base}", json={"primary_model": "v3"})
    assert (r.status_code, r.json()["error"]["code"]) == (409, "no_alarm_model")


async def _dataset_run() -> int:
    from app.db.session import SessionLocal

    async with SessionLocal() as s:
        run_id = await s.scalar(
            text(
                "INSERT INTO runs (scenario, status, kind, sim_start, params) VALUES "
                "('magdeburg', 'running', 'dataset', '2023-07-13 00:00+03', '{}') RETURNING id"
            )
        )
        await s.commit()
        return run_id


async def _stop(run_id: int) -> None:
    from app.db.session import SessionLocal
    from app.domain.runs import set_run_status

    async with SessionLocal() as s:
        await set_run_status(s, run_id, "stopped")
        await s.commit()


def compare_scores(version: str | None, ts: dt.datetime) -> float | None:
    """12:00 и 15:00 — тревога только у v3, 13:05 — обе без тревоги, 14:00 — v3 пропускает."""
    if version != "v3":
        return 0.1
    if ts.astimezone(MSK).hour == 14:
        return None
    return 0.9 if ts.minute == 0 else 0.1


def test_versions_side_by_side_in_api(client, monkeypatch):
    """Оценки обеих версий и расхождение в ленте и журнале, метрики по версии и на парных решениях, `live.versions`,
    `/models` и `/models/compare` (15.2, контракт интерфейса #171)."""
    import asyncio

    from app.db.session import SessionLocal

    async def setup() -> tuple[int, list]:
        ml = Ml(score=compare_scores)
        async with SessionLocal() as s:
            run = await create_run(
                s, scenario="t", sim_start=at("2026-05-01T11:00:00"), primary_model="baseline-v0", shadow_models=["v3"]
            )
            for t in ("12:00", "13:05", "14:00"):
                await smoke_alarm(s, run.id, f"2026-05-01T{t}:00")
            first = await score_object(s, run.id, 10, ml)
            # смена основной: новая тревога — вердикт v3, прошлые остаются с версией, что их решила
            await s.execute(
                text("UPDATE runs SET primary_model = 'v3', shadow_models = '{baseline-v0}' WHERE id = :r"),
                {"r": run.id},
            )
            await smoke_alarm(s, run.id, "2026-05-01T15:00:00")
            await score_object(s, run.id, 10, ml)
            await s.execute(
                text(
                    "UPDATE predictions SET outcome = CASE WHEN extract(hour FROM decision_time AT TIME ZONE "
                    "'Europe/Moscow') = 12 THEN 'fault' ELSE 'no_fault' END, outcome_at = decision_time"
                )
            )
            await s.execute(
                text("UPDATE runs SET sim_now = :t WHERE id = :r"), {"t": at("2026-05-03T00:00:00"), "r": run.id}
            )
            await s.commit()
            return run.id, first.notifications

    run_id, notes = asyncio.run(setup())
    # SSE alarm.scored — после всех версий: в событии оценки обеих
    scored = [n.data for n in notes if n.type == "alarm.scored"]
    assert len(scored) == 3 and all(len(a["scores"]) == 2 for a in scored)

    page = client.get("/api/v1/alarms", params={"run": run_id}).json()
    by_hour = {a["ts"][11:13]: a for a in page["items"]}
    assert [by_hour[h]["disagree"] for h in ("09", "10", "11", "12")] == [True, False, None, True]  # UTC
    assert page["counts"]["disagree"] == 2
    first = by_hour["09"]["scores"]
    assert [(x["model_version"], x["primary"], x["alert"]) for x in first] == [
        ("baseline-v0", True, False),
        ("v3", False, True),
    ]
    assert [x["model_version"] for x in by_hour["12"]["scores"] if x["primary"]] == ["v3"]
    assert by_hour["11"]["scores"][1]["status"] == "skipped"
    only = client.get("/api/v1/alarms", params={"run": run_id, "disagree": "true"}).json()
    assert only["total"] == 2 and only["counts"]["disagree"] == 2

    def journal(**params) -> dict:
        return client.get("/api/v1/predictions", params={"run": run_id, "model": "smoke_alarm", **params}).json()

    v3 = journal(model_version="v3")
    assert v3["total"] == 4 and {p["model_version"] for p in v3["items"]} == {"v3"}
    assert all([o["model_version"] for o in p["paired"]] == ["baseline-v0"] for p in v3["items"])
    assert journal(model_version="v3", disagree="true")["total"] == 2
    assert journal(model_version="baseline-v0", paired="true")["total"] == 3  # 14:00 v3 пропустила

    def metrics(**params) -> dict:
        return client.get("/api/v1/metrics", params={"run": run_id, "model": "smoke_alarm", **params}).json()

    m = metrics()  # основная прогона — теперь v3
    assert (m["model_version"], m["all"]["decisions"], m["all"]["tp"], m["all"]["fp"]) == ("v3", 3, 1, 1)
    assert metrics(model_version="baseline-v0")["all"]["decisions"] == 4
    v0 = metrics(model_version="baseline-v0", paired="true")["all"]
    assert (v0["decisions"], v0["tp"], v0["fn"]) == (3, 0, 1)

    live = client.get(f"/api/v1/runs/{run_id}").json()["live"]
    assert {v: x["decisions"] for v, x in live["versions"].items()} == {"baseline-v0": 3, "v3": 3}
    assert (live["smoke_alarm"]["decisions"], live["smoke_alarm"]["alerts"]) == (3, 2)

    async def fake_models(self):
        return MODELS

    monkeypatch.setattr("app.domain.ml_client.MlClient.models", fake_models)
    event = [(m["version"], m["active"], m["scoring"]) for m in client.get("/api/v1/models").json()["items"]]
    assert ("baseline-v0", True, True) in event and ("v3", False, True) in event

    compare = {
        "period": {"from": "2026-01-01", "to": "2026-07-01", "label": "I полугодие 2026"},
        "decisions": 6100,
        "positives": 480,
        "versions": {
            v: {"decisions": 6100, "positives": 480, "alerts": 450, "tp": 400, "fp": 50, "fn": 80, "tn": 5570}
            | {"precision": 0.89, "recall": 0.83, "f1": 0.86, "ap": 0.9, "lead_time": None}
            for v in ("baseline-v0", "v3")
        },
        "disagree": {"decisions": 310, "alert_only": {"baseline-v0": 190, "v3": 120}, "right": {"v3": 140}},
    }

    async def fake_compare(self, model):
        return compare if model == "smoke_alarm" else None

    monkeypatch.setattr("app.domain.ml_client.MlClient.models_compare", fake_compare)
    r = client.get("/api/v1/models/compare", params={"model": "smoke_alarm"})
    assert (
        r.status_code == 200 and r.json()["period"]["from"] == "2026-01-01" and r.json()["disagree"]["decisions"] == 310
    )
    r = client.get("/api/v1/models/compare", params={"model": "daily"})
    assert (r.status_code, r.json()["error"]["code"]) == (404, "compare_not_found")


@pytest.mark.anyio
async def test_answer_of_other_version_is_not_written_as_requested(session):
    """ML-сервис без выбора версии оценил бы активной моделью: ответ не той версии — пропуск `ml_unavailable`, а
    не прогноз v0 под именем v3."""
    run = await create_run(
        session, scenario="t", sim_start=at("2026-05-01T11:00:00"), primary_model="baseline-v0", shadow_models=["v3"]
    )
    await smoke_alarm(session, run.id, "2026-05-01T12:00:00")
    await score_object(session, run.id, 10, Ml(answer_as="baseline-v0"))
    rows = await session.execute(
        text("SELECT model_version, status, skip_reason FROM predictions WHERE run_id = :r ORDER BY model_version"),
        {"r": run.id},
    )
    assert [tuple(r) for r in rows] == [("baseline-v0", "scored", None), ("v3", "skipped", "ml_unavailable")]


def test_task_commits_verdict_before_shadow_and_retries(seeded, monkeypatch):
    """Задача скоринга: вердикт основной версии коммитится до теневых — теневой шаг его не задерживает и не
    отменяет. Упал теневой шаг — досчёт находит тревогу без v3 и дописывает только её; повтор ничего не дублирует."""
    import asyncio

    from app.db.session import SessionLocal
    from app.tasks import scoring as task

    async def setup() -> int:
        async with SessionLocal() as s:
            run = await create_run(
                s, scenario="t", sim_start=at("2026-05-01T11:00:00"), primary_model="baseline-v0", shadow_models=["v3"]
            )
            await smoke_alarm(s, run.id, "2026-05-01T12:00:00")
            await s.commit()
            return run.id

    async def rows(run_id: int) -> tuple[list[tuple], list[tuple]]:
        async with SessionLocal() as s:
            predictions = await s.execute(
                text("SELECT model_version, alert FROM predictions WHERE run_id = :r ORDER BY model_version"),
                {"r": run_id},
            )
            alarms = await s.execute(
                text(
                    "SELECT a.verdict, p.model_version FROM alarms a JOIN predictions p ON p.id = a.prediction_id "
                    "WHERE a.run_id = :r"
                ),
                {"r": run_id},
            )
            return [tuple(r) for r in predictions], [tuple(r) for r in alarms]

    async def pending() -> dict:
        async with SessionLocal() as s:
            return await sweep_pending(s)

    def v3_breaks(version, ts):
        if version == "v3":
            raise RuntimeError("v3 упала")
        return 0.1

    run_id = asyncio.run(setup())
    monkeypatch.setattr("app.domain.triage.MlClient", lambda: Ml(score=v3_breaks))
    with pytest.raises(RuntimeError):
        task.score_alarms.run(run_id, 10)
    assert asyncio.run(rows(run_id)) == ([("baseline-v0", False)], [("reaction", "baseline-v0")])
    assert asyncio.run(pending()) == {run_id: {10}}

    ml = Ml()
    monkeypatch.setattr("app.domain.triage.MlClient", lambda: ml)
    summary = task.score_alarms.run(run_id, 10)
    assert (summary["alarms"], summary["shadow"], ml.versions) == (0, {"v3": 1}, ["v3"])
    assert asyncio.run(rows(run_id)) == (
        [("baseline-v0", False), ("v3", True)],
        [("reaction", "baseline-v0")],  # вердикт — по-прежнему от основной
    )
    assert task.score_alarms.run(run_id, 10)["shadow"] == {} and asyncio.run(pending()) == {}


def reverdict_scores(version, ts):
    """12:00 — тревога только у v3; 13:00 — v3 наследует решение за сутки; 14:00 — v3 пропускает; 15:00 — v3 нет."""
    if version != "v3":
        return 0.1
    return {12: 0.9, 13: "decided_within_24h", 14: None}[ts.astimezone(MSK).hour]


def test_apply_to_past_reverdicts_with_audit(client, monkeypatch):
    """Смена основной с `apply_to_past`: вердикты прошлых тревог — по решению новой основной, как при скоринге
    (своё, унаследованное за сутки, без решения); тревоги без решения этой версии не меняются; заявка — по тревоге
    новой основной; смена и пересчёт — в журнале прогона. Повтор ничего не меняет."""
    import asyncio

    from app.db.session import SessionLocal
    from app.domain.reverdict import reverdict

    async def setup() -> int:
        ml = Ml(score=reverdict_scores)
        async with SessionLocal() as s:
            run = await create_run(
                s, scenario="t", sim_start=at("2026-05-01T11:00:00"), primary_model="baseline-v0", shadow_models=["v3"]
            )
            for hour in (12, 13, 14):
                await smoke_alarm(s, run.id, f"2026-05-01T{hour}:00:00")
                await score_object(s, run.id, 10, ml)
            # 15:00 — v3 в прогоне не было: решения этой версии у тревоги нет
            await s.execute(text("UPDATE runs SET shadow_models = '{}' WHERE id = :r"), {"r": run.id})
            await smoke_alarm(s, run.id, "2026-05-01T15:00:00")
            await score_object(s, run.id, 10, ml)
            await s.execute(text("UPDATE runs SET shadow_models = '{v3}' WHERE id = :r"), {"r": run.id})
            await s.commit()
            return run.id

    async def verdicts(run_id: int) -> list[tuple]:
        async with SessionLocal() as s:
            rows = await s.execute(
                text(
                    "SELECT to_char(a.ts AT TIME ZONE 'Europe/Moscow', 'HH24'), a.verdict, p.model_version, "
                    "to_char(p.decision_time AT TIME ZONE 'Europe/Moscow', 'HH24') FROM alarms a "
                    "JOIN predictions p ON p.id = a.prediction_id WHERE a.run_id = :r ORDER BY a.ts"
                ),
                {"r": run_id},
            )
            return [tuple(r) for r in rows]

    run_id = asyncio.run(setup())
    assert [v for _, v, *_ in asyncio.run(verdicts(run_id))] == ["reaction"] * 4

    async def fake_models(self):
        return MODELS

    monkeypatch.setattr("app.domain.ml_client.MlClient.models", fake_models)
    r = client.patch(f"/api/v1/runs/{run_id}", json={"primary_model": "v3", "apply_to_past": True})
    assert r.status_code == 200, r.text
    assert r.json()["reverdict"] == {
        "version": "v3",
        "alarms": 4,
        "changed": 3,
        "verdicts": {"reaction→sensor_fault": 2, "reaction→no_score": 1},
        "no_decision": 1,
        "orders_created": 1,
    }
    assert asyncio.run(verdicts(run_id)) == [
        ("12", "sensor_fault", "v3", "12"),
        ("13", "sensor_fault", "v3", "12"),  # унаследовано решение v3 в 12:00
        ("14", "no_score", "v3", "14"),
        ("15", "reaction", "baseline-v0", "15"),  # у v3 решения нет — не изменена
    ]
    page = client.get("/api/v1/alarms", params={"run": run_id}).json()
    primary = {a["ts"][11:13]: [x["model_version"] for x in a["scores"] if x["primary"]] for a in page["items"]}
    assert primary == {"09": ["v3"], "10": ["v3"], "11": ["v3"], "12": ["baseline-v0"]}  # UTC
    orders = client.get("/api/v1/work-orders", params={"run": run_id}).json()["items"]
    assert [(o["rule"], o["channel_id"]) for o in orders] == [("alarm_model_alert", 1001)]

    audit = client.get(f"/api/v1/runs/{run_id}/audit").json()["items"]
    assert [a["action"] for a in audit] == ["reverdict", "primary_model"]
    assert audit[1]["details"] == {
        "from": "baseline-v0",
        "to": "v3",
        "shadow_models": ["baseline-v0"],
        "apply_to_past": True,
    }
    assert len(audit[0]["details"]["changes"]) == 3

    async def again() -> dict:
        async with SessionLocal() as s:
            summary, _ = await reverdict(s, run_id)
            await s.commit()
            return summary

    repeat = asyncio.run(again())
    assert (repeat["changed"], repeat["orders_created"]) == (0, 0)


def inherit_scores(version: str | None, ts: dt.datetime) -> float | str | None:
    """12:00 — обе версии оценили (v3 с тревогой), 13:00 — обе пропускают «одно решение на канал в сутки», 14:00 —
    v3 пропускает по другой причине."""
    hour = ts.astimezone(MSK).hour
    if hour == 12:
        return 0.53 if version == "v3" else 0.43
    if hour == 13:
        return "decided_within_24h"
    return "state_not_normal" if version == "v3" else 0.2


@pytest.mark.anyio
async def test_scores_inherit_decision_of_each_version(session):
    """Прогон 56, тревоги 166376 и 166377: у основной v3 решение унаследовано (`Alarm.prediction.inherited`), а в
    `scores` у обеих версий был пропуск без оценки и причины — в ленте «v0 пропуск», хотя решение v0 за сутки тоже
    действует. Пропуск `decided_within_24h` при действующем решении версии — `inherited` с его оценкой; расхождение —
    по унаследованным решениям; у прочих пропусков — причина."""
    from app.domain.feed import alarms_by_ids

    run = await create_run(
        session, scenario="t", sim_start=at("2026-05-01T11:00:00"), primary_model="v3", shadow_models=["baseline-v0"]
    )
    for t in ("12:00", "13:00", "14:00"):
        await smoke_alarm(session, run.id, f"2026-05-01T{t}:00")
    await score_object(session, run.id, 10, Ml(score=inherit_scores))
    ids = [r[0] for r in (await session.execute(text("SELECT id FROM alarms ORDER BY ts"))).all()]
    first, inherited, other = await alarms_by_ids(session, run.id, ids, at("2026-05-01T15:00:00"))

    def view(alarm: dict) -> list[tuple]:
        return [
            (s["model_version"], s["status"], s["score"], s["alert"], s["skip_reason"], s["inherited"], s["primary"])
            for s in alarm["scores"]
        ]

    assert view(first) == [
        ("baseline-v0", "scored", 0.43, False, None, False, False),
        ("v3", "scored", 0.53, True, None, False, True),
    ]
    assert first["disagree"] is True
    # решение каждой версии — её же за 12:00, как у Alarm.prediction основной
    assert view(inherited) == [
        ("baseline-v0", "scored", 0.43, False, None, True, False),
        ("v3", "scored", 0.53, True, None, True, True),
    ]
    assert inherited["prediction"]["inherited"] is True and inherited["disagree"] is True
    assert [s["prediction_id"] for s in inherited["scores"]] == [s["prediction_id"] for s in first["scores"]]
    assert view(other) == [
        ("baseline-v0", "scored", 0.2, False, None, False, False),
        ("v3", "skipped", None, None, "state_not_normal", False, True),
    ]
    assert other["disagree"] is None


def chain_scores(version: str | None, ts: dt.datetime) -> float | str:
    """Связка v3>baseline-v0 на одном канале. 1.05 12:00 — обе оценили, тревога только у v3; 13:00 — обе
    пропускают «одно решение на канал в сутки»; 2.05 14:00 — v3 молчит (`coverage_gap`), v0 оценила с тревогой;
    15:00 — обе пропускают «одно решение в сутки», но решения v3 за сутки нет; 4.05 12:00 — не решила ни одна."""
    v3 = version == "v3"
    return {
        "01 12": 0.9 if v3 else 0.1,
        "01 13": "decided_within_24h",
        "02 14": "coverage_gap" if v3 else 0.8,
        "02 15": "decided_within_24h",
        "04 12": "coverage_gap" if v3 else "state_not_normal",
    }[ts.astimezone(MSK).strftime("%d %H")]


CHAIN_ALARMS = ("2026-05-01T12:00", "2026-05-01T13:00", "2026-05-02T14:00", "2026-05-02T15:00", "2026-05-04T12:00")


async def chain_verdicts(session, run_id: int) -> list[tuple]:
    """Тревога, вердикт, версия и время прогноза по ссылке `alarms.prediction_id`."""
    rows = await session.execute(
        text(
            "SELECT to_char(a.ts AT TIME ZONE 'Europe/Moscow', 'DD HH24'), a.verdict, p.model_version, "
            "to_char(p.decision_time AT TIME ZONE 'Europe/Moscow', 'DD HH24') FROM alarms a "
            "LEFT JOIN predictions p ON p.id = a.prediction_id WHERE a.run_id = :r ORDER BY a.ts"
        ),
        {"r": run_id},
    )
    return [tuple(r) for r in rows]


@pytest.mark.anyio
async def test_chain_decides_by_first_version_with_decision(session):
    """Связка `v3>baseline-v0`: решение тревоги — v3, где она решила (оценила или унаследовала своё за сутки), где
    нет — v0. Вердикт ждёт v0 только там, где v3 не решила; остальное v0 оценивает на шаге теневых — для сравнения
    версий на одних решениях. Заявки — от решившей версии; в ленте — кто решил и почему не v3."""
    from app.domain.feed import alarms_by_ids
    from app.domain.triage import score_primary, score_shadows

    ml = Ml(score=chain_scores)
    primary, shadows = await select_versions(None, ml)
    run = await create_run(
        session, scenario="t", sim_start=at("2026-05-01T11:00:00"), primary_model=primary, shadow_models=shadows
    )
    for ts in CHAIN_ALARMS[:2]:
        await smoke_alarm(session, run.id, f"{ts}:00")
    first = await score_primary(session, run.id, 10, ml)
    assert (ml.versions, first.model_version) == (["v3"], "v3>baseline-v0")  # v3 решила обе — v0 вердикт не ждёт
    shadow = await score_shadows(session, run.id, 10, ml)
    assert (ml.versions, ml.sizes, shadow.shadow) == (["v3", "baseline-v0"], [2, 2], {"baseline-v0": 2})
    orders = await apply_prediction_rules(session, run.id, first.prediction_ids)
    assert [o.data["prediction_id"] for o in orders] == first.prediction_ids  # решение v3 12:00 с тревогой
    await session.execute(text("UPDATE work_orders SET status = 'done' WHERE run_id = :r"), {"r": run.id})

    for ts in CHAIN_ALARMS[2:]:
        await smoke_alarm(session, run.id, f"{ts}:00")
    result = await score_primary(session, run.id, 10, ml)
    # v3 пропустила все три — v0 оценивает их на шаге основной; на шаге теневых оценивать нечего
    assert (ml.versions[2:], ml.sizes[2:]) == (["v3", "baseline-v0"], [3, 3])
    assert (result.alarms, result.scored, result.skipped) == (3, 1, 5)
    assert (await score_shadows(session, run.id, 10, ml)).shadow == {} and len(ml.versions) == 4
    assert await chain_verdicts(session, run.id) == [
        ("01 12", "sensor_fault", "v3", "01 12"),
        ("01 13", "sensor_fault", "v3", "01 12"),  # унаследовано решение v3
        ("02 14", "sensor_fault", "baseline-v0", "02 14"),  # v3 пропустила — решила v0
        ("02 15", "sensor_fault", "baseline-v0", "02 14"),  # решения v3 за сутки нет — унаследовано решение v0
        ("04 12", "no_score", "baseline-v0", "04 12"),  # не решила ни одна — ссылка на пропуск последней
    ]
    orders = await apply_prediction_rules(session, run.id, result.prediction_ids)
    made = await session.execute(
        text("SELECT model_version FROM predictions WHERE id = ANY(:ids)"),
        {"ids": [o.data["prediction_id"] for o in orders]},
    )
    assert made.scalars().all() == ["baseline-v0"]  # заявка — по решению v0 14:00, тревога v0 12:00 заявку не дала

    ids = [r[0] for r in (await session.execute(text("SELECT id FROM alarms ORDER BY ts"))).all()]
    feed = await alarms_by_ids(session, run.id, ids, at("2026-05-05T00:00:00"))
    assert [(a["prediction"]["decided_by"], a["prediction"]["fallback_reason"]) for a in feed] == [
        ("v3", None),
        ("v3", None),
        ("baseline-v0", "coverage_gap"),
        ("baseline-v0", "decided_within_24h"),
        (None, "coverage_gap"),
    ]
    assert [(s["model_version"], s["status"], s["primary"]) for s in feed[2]["scores"]] == [
        ("baseline-v0", "scored", True),
        ("v3", "skipped", False),
    ]

    # метрики: связка — её решения (v3 12:00 и v0 14:00), версии — свои; парные — оценённые обеими (1.05 12:00)
    await session.execute(text("UPDATE predictions SET outcome = 'fault', outcome_at = decision_time"))
    now = at("2026-05-10T00:00:00")

    async def decisions(version: str | None = None, paired: bool = False) -> tuple:
        m = await model_metrics(session, run.id, "smoke_alarm", now, model_version=version, paired=paired)
        return m["model_version"], m["all"]["decisions"]

    assert await decisions() == ("v3>baseline-v0", 2)
    assert await decisions("v3") == ("v3", 1)
    assert await decisions("baseline-v0") == ("baseline-v0", 2)
    assert await decisions(paired=True) == ("v3>baseline-v0", 1)
    assert await decisions("baseline-v0", paired=True) == ("baseline-v0", 1)
    await session.execute(text("UPDATE runs SET sim_now = :t WHERE id = :r"), {"t": now, "r": run.id})
    live = (await live_by_run(session, [run.id]))[run.id]
    assert live["smoke_alarm"]["decisions"] == 2
    assert {v: m["decisions"] for v, m in live["versions"].items()} == {"baseline-v0": 1, "v3": 1}


def test_chain_via_api(client, monkeypatch):
    """Связка через API: смена основной v3 → связка с `apply_to_past` пересчитывает вердикты тревог, где v3 не решила,
    по решениям v0; журнал прогнозов по связке — прогнозы, давшие вердикт, и флаг `decided`; в `/models` — запись
    связки с офлайн-метриками из `fallback` сравнения версий."""
    import asyncio

    from app.db.session import SessionLocal

    async def setup() -> int:
        async with SessionLocal() as s:
            run = await create_run(
                s, scenario="t", sim_start=at("2026-05-01T11:00:00"), primary_model="v3", shadow_models=["baseline-v0"]
            )
            for ts in CHAIN_ALARMS:
                await smoke_alarm(s, run.id, f"{ts}:00")
            await score_object(s, run.id, 10, Ml(score=chain_scores))
            await s.execute(
                text("UPDATE runs SET sim_now = :t WHERE id = :r"), {"t": at("2026-05-05T00:00:00"), "r": run.id}
            )
            await s.commit()
            return run.id

    async def verdicts(run_id: int) -> list[tuple]:
        async with SessionLocal() as s:
            return await chain_verdicts(s, run_id)

    run_id = asyncio.run(setup())
    assert [v for _, v, *_ in asyncio.run(verdicts(run_id))] == ["sensor_fault"] * 2 + ["no_score"] * 3

    async def fake_models(self):
        return MODELS

    combo = {"decisions": 7563, "positives": 537, "tp": 423, "fp": 64, "precision": 0.8686, "recall": 0.7877}

    async def fake_compare(self, model: str) -> dict:
        period = {"label": "I полугодие 2026, решения, допустимые для обеих моделей"}
        return {"period": period, "fallback": {"half_year": {"combo": combo}}}

    monkeypatch.setattr("app.domain.ml_client.MlClient.models", fake_models)
    monkeypatch.setattr("app.domain.ml_client.MlClient.models_compare", fake_compare)
    chain = "v3>baseline-v0"
    r = client.patch(f"/api/v1/runs/{run_id}", json={"primary_model": chain, "apply_to_past": True})
    assert r.status_code == 200, r.text
    assert (r.json()["primary_model"], r.json()["shadow_models"]) == (chain, [])  # v0 — часть связки, не теневая
    assert r.json()["reverdict"] == {
        "version": chain,
        "alarms": 5,
        "changed": 3,  # две тревоги получили решение v0, у третьей ссылка — на пропуск v0
        "verdicts": {"no_score→sensor_fault": 2},
        "no_decision": 0,
        "orders_created": 1,  # решения v3 12:00 и v0 14:00 с тревогой — один канал, одна открытая заявка
    }
    assert asyncio.run(verdicts(run_id)) == [
        ("01 12", "sensor_fault", "v3", "01 12"),
        ("01 13", "sensor_fault", "v3", "01 12"),
        ("02 14", "sensor_fault", "baseline-v0", "02 14"),
        ("02 15", "sensor_fault", "baseline-v0", "02 14"),
        ("04 12", "no_score", "baseline-v0", "04 12"),
    ]
    audit = client.get(f"/api/v1/runs/{run_id}/audit").json()["items"]
    assert [a["action"] for a in audit] == ["reverdict", "primary_model"]
    assert audit[1]["details"] == {"from": "v3", "to": chain, "shadow_models": [], "apply_to_past": True}

    def journal(version: str, **params) -> list[tuple]:
        page = client.get("/api/v1/predictions", params={"run": run_id, "model_version": version, **params}).json()
        return [(p["model_version"], p["decision_time"][8:13], p["status"], p["decided"]) for p in page["items"]]

    # связка — прогнозы, давшие вердикт своей тревоге (по одному на тревогу; унаследованные — у тревоги решения)
    assert journal(chain) == [
        ("baseline-v0", "04T09", "skipped", True),
        ("baseline-v0", "02T11", "scored", True),
        ("v3", "01T09", "scored", True),
    ]
    assert [d for *_, d in journal("v3")] == [False, False, False, False, True]
    assert len(journal(chain, status="scored")) == 2

    alarms = client.get("/api/v1/alarms", params={"run": run_id}).json()["items"]
    shown = {a["ts"][8:13]: (a["prediction"]["decided_by"], a["prediction"]["fallback_reason"]) for a in alarms}
    assert shown["02T11"] == ("baseline-v0", "coverage_gap") and shown["01T09"] == ("v3", None)

    items = client.get("/api/v1/models").json()["items"]
    entry = next(i for i in items if i["family"] == "chain")
    assert (entry["version"], entry["chain"], entry["active"], entry["scoring"]) == (
        chain,
        ["v3", "baseline-v0"],
        True,
        True,
    )
    assert [i["active"] for i in items if i["version"] in ("v3", "baseline-v0") and i["model"] == "smoke_alarm"] == [
        False,
        False,
    ]
    offline = entry["offline_metrics"]
    assert (offline["precision"], offline["recall"], offline["decisions"], offline["ap"]) == (
        0.8686,
        0.7877,
        7563,
        None,
    )
    assert offline["period"] == "I полугодие 2026, общие решения и неделя после пропуска журнала"
