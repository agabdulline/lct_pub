"""Контракт с ML-сервисом на снимках его настоящих ответов (tests/fixtures/ml/). Backend и ml-service проверяются
порознь, и без снимков схема backend расходится с ответами сервиса незаметно: так `/models` отдавал 500 после
первой внешней модели (#140) и после модели-правила vast_gate (#156). Снимки пишет команда ml-service
(`cd ml-service && PYTHONPATH=.. uv run python -m app.contract_fixtures`), руками их не править."""

import datetime as dt
import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text

from app.domain import external
from app.domain.runs import create_run

FIXTURES = Path(__file__).parent / "fixtures" / "ml"
MSK = dt.timezone(dt.timedelta(hours=3))
API = "/api/v1"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_models_snapshot_passes_backend_schema(client, monkeypatch):
    models = fixture("models.json")["models"]

    async def fake_models(self):
        return models

    async def no_compare(self, model):
        return None

    monkeypatch.setattr("app.domain.ml_client.MlClient.models", fake_models)
    monkeypatch.setattr("app.domain.ml_client.MlClient.models_compare", no_compare)
    r = client.get(f"{API}/models")
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    # модели ml-service — как есть, по порядку; за ними связка событийных версий v3 → v0 (обе готовы)
    assert len(items) == len(models) + 1 and items[-1]["family"] == "chain"
    items = items[: len(models)]
    assert {m["family"] for m in items} >= {"baseline", "external"}
    # отчёт проходит без потерь: поля, которые показывает интерфейс, не выброшены схемой (порядок моделей — как у
    # ml-service); так пропало бы fire_vs_nuisance
    for m, item in zip(models, items, strict=True):
        if m.get("offline"):
            assert set(m["offline"]) <= set(item["offline"] or {}), m["name"]
    # у внешних моделей есть всё, что нужно такту, приёму и исходам по разметке
    for m, item in zip(models, items, strict=True):  # исход и положительные метки внешних — как у ml-service
        assert (item["outcome"], item["positive_labels"]) == (m.get("outcome"), m.get("positive_labels")), m["name"]
    for m in (m for m in models if m.get("family") == "external"):
        assert m["dataset"] and m["outcome"] in external._OUTCOME_SQL, m["name"]
        assert isinstance(m.get("positive_labels"), list), m["name"]
        external.horizon(m)
        if m["trigger"] == "on_event":
            assert m.get("label_key"), m["name"]  # исход — по совпадению ключа метки со значением события
        else:
            assert m["trigger"] in external.STEPS and m.get("target_tag"), m["name"]


@pytest.mark.anyio
async def test_external_score_snapshot_passes_worker(session, monkeypatch):
    """Ответ `/v1/external/score` пишется в журнал прогнозов кодом такта: цели, каналы цели, классы."""
    response = fixture("external_score_magdeburg.json")
    model = next(m for m in fixture("models.json")["models"] if m.get("name") == "magdeburg_fire")
    targets = {r["target"]: r.get("target_channel_id") for r in response["results"]}
    await session.execute(
        text(
            "INSERT INTO objects (id, parent_id, level, kind, name, source) "
            "SELECT t, NULL, 3, 'sensor_node', 'узел', 'magdeburg' FROM unnest(CAST(:t AS integer[])) t"
        ),
        {"t": list(targets)},
    )
    await session.execute(
        text(
            "INSERT INTO channels (id, object_id, system_type, sensor_type, tag, name, source) "
            "SELECT c, t, 'x', 'Узел', :tag, 'узел', 'magdeburg' "
            "FROM unnest(CAST(:t AS integer[]), CAST(:c AS integer[])) AS x(t, c)"
        ),
        {"t": list(targets), "c": list(targets.values()), "tag": model["target_tag"]},
    )
    await session.execute(
        text(
            "INSERT INTO runs (scenario, status, kind, sim_start, sim_end, params) VALUES "
            "('dataset:magdeburg', 'finished', 'dataset', :a, :b, '{\"dataset\": \"magdeburg\"}')"
        ),
        {"a": dt.datetime(2023, 7, 10, tzinfo=dt.UTC), "b": dt.datetime(2023, 7, 14, tzinfo=dt.UTC)},
    )
    t = dt.datetime.fromisoformat(response["decision_time"])
    run = await create_run(session, scenario="ext", sim_start=t - dt.timedelta(seconds=30), dataset="magdeburg")
    run.sim_now = t

    class Ml:
        async def score_external(self, run_id, name, decision_time):
            return response

    summary, _ = await external.score_model(session, run, "magdeburg", model, Ml())
    assert summary["predictions"] == len(response["results"])
    rows = await session.execute(
        text("SELECT channel_id, status, context FROM predictions WHERE run_id = :r ORDER BY channel_id"),
        {"r": run.id},
    )
    written = {r.channel_id: r for r in rows}
    assert set(written) == set(targets.values())
    scored = [r for r in response["results"] if r["status"] == "scored"]
    assert scored and all(written[r["target_channel_id"]].context["classes"] == r["classes"] for r in scored)


@pytest.mark.anyio
async def test_event_score_snapshot_passes_worker(session):
    """Ответ событийной модели (`vast_gate`) пишется кодом приёма: оценённые проезды — прогнозами на канал датчика,
    цель не из набора и событие из будущего — не пишутся."""
    from app.domain.ingest import RawEvent, ingest_events

    request = fixture("external_score_vast_gate_request.json")
    response = fixture("external_score_vast_gate.json")
    model = next(m for m in fixture("models.json")["models"] if m.get("name") == "vast_gate")
    known = [e for e in request["events"] if e["channel_id"] > 900_000_000]
    await session.execute(
        text(
            "INSERT INTO objects (id, parent_id, level, kind, name, source) "
            "VALUES (9020015, NULL, 3, 'sensor_group', 'ворота', 'vast2017')"
        )
    )
    await session.execute(
        text(
            "INSERT INTO channels (id, object_id, system_type, sensor_type, tag, name, source) "
            "SELECT DISTINCT c, 9020015, 'x', 'Ворота', 'gate', 'ворота', 'vast2017' "
            "FROM unnest(CAST(:c AS integer[])) c"
        ),
        {"c": [e["channel_id"] for e in known]},
    )
    await session.execute(
        text(
            "INSERT INTO runs (scenario, status, kind, sim_start, sim_end, params) VALUES "
            "('dataset:vast2017', 'finished', 'dataset', :a, :b, '{\"dataset\": \"vast2017\"}')"
        ),
        {"a": dt.datetime(2015, 5, 1, tzinfo=dt.UTC), "b": dt.datetime(2016, 6, 1, tzinfo=dt.UTC)},
    )
    now = dt.datetime.fromisoformat(request["decision_time"])
    run = await create_run(session, scenario="ext", sim_start=now - dt.timedelta(hours=1), dataset="vast2017")
    events = [RawEvent(e["channel_id"], dt.datetime.fromisoformat(e["ts"]), e["value"], False) for e in known]
    await ingest_events(run.id, events, session=session)
    run.sim_now = now

    class Ml:
        async def score_external_events(self, run_id, name, decision_time, batch):
            return response

    summary, _ = await external.score_events(session, run, "vast2017", model, Ml())
    scored = [r for r in response["results"] if r["status"] == "scored"]
    assert summary["predictions"] == len(scored)
    rows = await session.execute(
        text("SELECT channel_id, context->>'value', decision_time FROM predictions WHERE run_id = :r"), {"r": run.id}
    )
    assert {(c, v, t) for c, v, t in rows} == {
        (r["target_channel_id"], r["value"], dt.datetime.fromisoformat(r["decision_time"])) for r in scored
    }


@pytest.mark.anyio
async def test_daily_score_snapshot_passes_worker(session):
    """Ежедневный прогноз по типам (15.4): версия прогноза и порог — из строки ответа (v0, ансамбль ИБП, извещатель),
    пропуски — с причиной сервиса. Канала 99 в справочнике нет — worker его и не запрашивает (channel_ids = None)."""
    from app.domain.daily import daily_forecast
    from app.domain.ml_client import MlClient

    request, response = fixture("daily_score_request.json"), fixture("daily_score.json")
    await session.execute(
        text(
            "INSERT INTO channels (id, object_id, system_type, sensor_type, tag, name) VALUES "
            "(1004, 100, 'Электроснабжение', 'ИБП', '15-11.1.1.1.', 'ИБП 1'), "
            "(1005, 100, 'Пожарная охрана', 'Ручной извещатель', '15-11.1.2.1.', 'ИПР ПК2'), "
            "(1006, 100, 'Электроснабжение', 'ИБП', '15-11.1.3.1.', 'ИБП 2'), "
            "(1007, 100, 'Пожарная охрана', 'Тепловой датчик', '15-11.1.4.1.', 'ТД ПК3')"
        )
    )
    results = [r for r in response["results"] if r["channel_id"] != 99]
    body = response | {"results": results}
    ml = MlClient("http://ml", transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body)))
    day = dt.date.fromisoformat(request["date"])
    run = await create_run(session, scenario="daily", sim_start=dt.datetime.combine(day, dt.time(), MSK))
    summary, _ = await daily_forecast(session, run.id, day, ml)
    assert summary["predictions"] == len(results)
    assert summary["alerts"] == sum(1 for r in results if r["alert"])
    rows = await session.execute(
        text(
            "SELECT channel_id, model_version, status, skip_reason, score, threshold, alert FROM predictions "
            "WHERE run_id = :r AND model = 'daily'"
        ),
        {"r": run.id},
    )
    assert {r[0]: tuple(r[1:]) for r in rows} == {
        r["channel_id"]: (r["model"], r["status"], r["skip_reason"], r["score"], r["threshold"], r["alert"])
        for r in results
    }
    assert {r["model"] for r in results} == {"baseline-v0", "ups_ensemble", "manual_alarm"}


def test_event_score_snapshot_passes_factor_schema():
    """Факторы событийной модели (флаг без вклада — `no_permit`) проходят схему журнала прогнозов."""
    from app.schemas.api import Factor

    for r in fixture("external_score_vast_gate.json")["results"]:
        for f in r.get("factors") or []:
            Factor.model_validate(f)


def test_models_snapshot_event_versions(client, monkeypatch):
    """15.1–15.2, 15.5b: v3 в снимке — вторая версия событийной модели; основная по умолчанию — связка v3 → v0
    отдельной записью `/models`, её офлайн — итог связки из снимка сравнения версий (`fallback`)."""
    import asyncio

    from app.domain.model_versions import select_versions

    models = fixture("models.json")["models"]
    compare = fixture("models_compare.json")

    async def fake_models(self):
        return models

    async def fake_compare(self, model):
        return compare

    monkeypatch.setattr("app.domain.ml_client.MlClient.models", fake_models)
    monkeypatch.setattr("app.domain.ml_client.MlClient.models_compare", fake_compare)
    items = client.get(f"{API}/models").json()["items"]
    event = {m["version"]: (m["active"], m["scoring"]) for m in items if m["model"] == "smoke_alarm"}
    # основная новых прогонов — связка; её части скорят, но не основные
    assert event == {"baseline-v0": (False, True), "v3": (False, True), "v3>baseline-v0": (True, True)}
    # отдельные модели типов ML-разработчика (#243) — ежедневные своего семейства и не попадают в версии событийной
    # модели; скорят только те, кого выбрала таблица маршрутизации (15.4): ансамбль ИБП и извещатель
    each = {m["version"]: (m["model"], m["active"], m["scoring"]) for m in items if m["family"] == "each_channel"}
    assert each == {v: ("daily", False, v in ("manual_alarm", "ups_ensemble"))
                    for v in ("fan", "manual_alarm", "pump", "ups_context", "ups_ensemble", "ups_old")}  # fmt: skip
    assert (
        next(m for m in items if m["version"] == "ups_context")["title"]
        == "Ежедневная — ИБП: модель контекста объектов"
    )
    chain = next(m for m in items if m["family"] == "chain")
    combo = compare["fallback"]["half_year"]["combo"]
    offline = chain["offline_metrics"]
    assert (chain["chain"], offline["decisions"], offline["positives"]) == (
        ["v3", "baseline-v0"],
        combo["decisions"],
        combo["positives"],
    )
    assert (offline["precision"], offline["recall"], offline["tp"], offline["fp"], offline["ap"]) == (
        combo["precision"],
        combo["recall"],
        combo["tp"],
        combo["fp"],
        None,
    )
    assert all("task" in m and "active" in m for m in models)
    from app.domain.ml_client import MlClient

    assert asyncio.run(select_versions(None, MlClient())) == ("v3>baseline-v0", [])


def test_models_compare_snapshot_passes_backend_schema(client, monkeypatch):
    """`/v1/models/compare` проходит схему `/models/compare` без потери полей, которые показывает интерфейс."""
    compare = fixture("models_compare.json")

    async def fake_compare(self, model):
        return compare

    monkeypatch.setattr("app.domain.ml_client.MlClient.models_compare", fake_compare)
    r = client.get(f"{API}/models/compare", params={"model": "smoke_alarm"})
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["period"] == compare["period"] and got["disagree"] == compare["disagree"]
    assert (got["decisions"], got["positives"]) == (compare["decisions"], compare["positives"])
    for version, block in compare["versions"].items():
        assert {k: got["versions"][version][k] for k in block} == block
    # итоги отчёта сравнения (ml/reports/compare-v0-v3) проходят без потерь — их показывает экран моделей (15.3)
    for key in ("ci", "diff", "clusters", "by_kind", "coverage", "fallback"):
        assert got[key] == compare[key], key


def test_alarm_score_snapshots_pass_shadow_scoring(client):
    """Ответы `/v1/alarm/score` обеих версий на одну пачку тревог проходят теневой скоринг и API: прогноз на каждую
    версию, вердикт — от основной (v0), свои причины пропуска у v3 (`coverage_gap`), расхождение там, где тревога
    только у одной версии."""
    import asyncio

    from app.db.session import SessionLocal
    from app.domain.triage import score_object

    snapshots = {r["model"]: r for r in (fixture("alarm_score_baseline.json"), fixture("alarm_score_v3.json"))}
    request = fixture("alarm_score_v3_request.json")
    assert fixture("alarm_score_baseline_request.json")["alarms"] == request["alarms"]
    channels = sorted({a["channel_id"] for a in request["alarms"]})

    class Ml:
        """Отвечает снимком запрошенной версии в порядке запроса."""

        async def score_alarms(self, run_id, alarms, model_version=None):
            snapshot = snapshots[model_version]
            by_key = {(r["channel_id"], dt.datetime.fromisoformat(r["alarm_ts"])): r for r in snapshot["results"]}
            return {**snapshot, "results": [by_key[(c, ts)] for c, ts in alarms]}

    async def setup() -> int:
        async with SessionLocal() as s:
            await s.execute(
                text(
                    "INSERT INTO channels (id, object_id, system_type, sensor_type, tag, name) "
                    "SELECT c, 100, 'Пожарная охрана', 'Датчик дыма', '15-11.9.' || c, 'ДД ' || c "
                    "FROM unnest(CAST(:c AS integer[])) c"
                ),
                {"c": channels},
            )
            run = await create_run(
                s,
                scenario="t",
                sim_start=dt.datetime(2025, 3, 1, tzinfo=dt.UTC),
                primary_model="baseline-v0",
                shadow_models=["v3"],
            )
            for a in request["alarms"]:
                await s.execute(
                    text(
                        "INSERT INTO alarms (run_id, channel_id, object_id, ts, verdict) "
                        "VALUES (:r, :c, 10, :ts, 'pending')"
                    ),
                    {"r": run.id, "c": a["channel_id"], "ts": dt.datetime.fromisoformat(a["ts"])},
                )
            result = await score_object(s, run.id, 10, Ml())
            assert result.alarms == len(request["alarms"]) and result.shadow == {"v3": len(request["alarms"])}
            run.sim_now = dt.datetime(2025, 4, 10, tzinfo=dt.UTC)
            await s.commit()
            return run.id

    run_id = asyncio.run(setup())

    def journal(version: str) -> dict:
        """Прогнозы версии по (канал, время тревоги): решение — через 1 с после тревоги."""
        page = client.get(
            f"{API}/predictions", params={"run": run_id, "model": "smoke_alarm", "model_version": version}
        )
        assert page.status_code == 200, page.text
        one = dt.timedelta(seconds=1)
        return {(p["channel_id"], dt.datetime.fromisoformat(p["decision_time"]) - one): p for p in page.json()["items"]}

    for version, snapshot in snapshots.items():
        rows = journal(version)
        assert len(rows) == len(request["alarms"])
        for r in snapshot["results"]:
            p = rows[(r["channel_id"], dt.datetime.fromisoformat(r["alarm_ts"]))]
            assert (p["model_version"], p["status"], p["skip_reason"], p["score"], p["alert"]) == (
                version,
                r["status"],
                r.get("skip_reason"),
                r.get("score"),
                r.get("alert"),
            )
            assert [o["model_version"] for o in p["paired"]] == [v for v in snapshots if v != version]

    page = client.get(f"{API}/alarms", params={"run": run_id}).json()
    alert_only_v0 = next(a for a in page["items"] if a["channel_id"] == 13)
    assert alert_only_v0["verdict"] == "sensor_fault" and alert_only_v0["disagree"] is True
    assert [s["model_version"] for s in alert_only_v0["scores"] if s["primary"]] == ["baseline-v0"]
    both_skipped = next(a for a in page["items"] if a["channel_id"] == 14)
    assert both_skipped["disagree"] is None
    assert {s["model_version"]: s["status"] for s in both_skipped["scores"]} == {
        "baseline-v0": "skipped",
        "v3": "skipped",
    }
    assert page["counts"]["disagree"] == 1 and page["total"] == len(request["alarms"])
    m = client.get(f"{API}/metrics", params={"run": run_id, "model": "smoke_alarm", "model_version": "v3"})
    assert m.status_code == 200 and m.json()["model_version"] == "v3"
