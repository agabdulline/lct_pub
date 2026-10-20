"""Режим «Вся история»: дашборд по месяцам и метрики решений истории на базе (run=0)."""

from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from app.domain import history


def _sql(url, *statements: str) -> None:
    engine = create_engine(url, poolclass=NullPool)
    with engine.begin() as conn:
        for s in statements:
            conn.execute(text(s))
    engine.dispose()


def test_dashboard_history_months_and_top_objects(seeded, client):
    history._cache.clear()
    _sql(
        seeded,
        "INSERT INTO channel_state (run_id, channel_id, ts, value_raw, is_alarm) "
        "VALUES (NULL, 1001, '2026-02-20 10:00+03', 'Норма', false)",
        "INSERT INTO alarms (run_id, channel_id, object_id, ts, verdict) VALUES "
        "(NULL, 1001, 10, '2026-01-05 10:00+03', 'ppr'), (NULL, 1001, 10, '2026-01-31 23:30+03', 'no_score'), "
        "(NULL, 1001, 10, '2026-02-01 00:30+03', 'no_score')",
        "INSERT INTO fault_episodes (run_id, channel_id, start_ts, end_ts, state) VALUES "
        "(NULL, 1003, '2026-01-10 10:00+03', '2026-01-11 10:00+03', 'Неисправен'), "
        "(NULL, 1001, '2026-02-02 10:00+03', NULL, 'Неисправен')",
    )
    r = client.get("/api/v1/dashboard/history")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["from"] == "2026-01-01"
    jan, feb = body["months"]
    # месяц — по Москве: тревога 31.01 23:30 МСК — январь, хотя по UTC тоже январь; 01.02 00:30 МСК — февраль
    assert (jan["month"], jan["smoke_alarms"], jan["ppr_alarms"], jan["ppr_share"]) == ("2026-01-01", 2, 1, 0.5)
    assert (jan["faults_started"], jan["faults_equipment"]) == (1, 1)
    assert (feb["smoke_alarms"], feb["faults_started"], feb["faults_equipment"]) == (1, 1, 0)
    (top,) = body["top_objects"]
    assert (top["object_id"], top["object_name"]) == (10, "Объект А")  # подобъект — к объекту уровня 2
    assert (top["faults"], top["fault_channels"], top["alarms"], top["alarms_out_of_ppr"]) == (2, 2, 3, 2)


def _base_decisions(url, v3: bool) -> None:
    """Тревога базы и решения истории по ней: v0 и, если `v3`, v3 — она и дала вердикт связки."""
    rows = ["(1, 'baseline-v0', 0.9, 0.43, true)"] + (["(2, 'v3', 0.2, 0.49, false)"] if v3 else [])
    _sql(
        url,
        "INSERT INTO alarms (id, run_id, channel_id, object_id, ts, verdict) VALUES "
        "(1, NULL, 1001, 10, '2026-01-05 10:00+03', 'no_score')",
        "INSERT INTO predictions (id, run_id, model, model_version, channel_id, alarm_id, decision_time, "
        "horizon_start, horizon_end, status, score, threshold, alert, seen_channel, outcome, outcome_at, "
        "outcome_source) "
        "SELECT p.id, NULL, 'smoke_alarm', p.version, 1001, 1, '2026-01-05 10:00:01+03', '2026-01-05 10:00:01+03', "
        "'2026-01-06 10:00:01+03', 'scored', p.score, p.thr, p.alert, true, 'no_fault', '2026-01-06 10:00:01+03', "
        f"'history' FROM (VALUES {', '.join(rows)}) AS p(id, version, score, thr, alert)",
        f"UPDATE alarms SET prediction_id = {2 if v3 else 1} WHERE id = 1",
        "INSERT INTO channel_state (run_id, channel_id, ts, value_raw, is_alarm) "
        "VALUES (NULL, 1001, '2026-06-30 23:59+03', 'Норма', false)",
    )


def test_base_metrics_default_to_chain_with_both_versions(seeded, client):
    _base_decisions(seeded, v3=True)
    m = client.get("/api/v1/metrics", params={"model": "smoke_alarm", "run": 0}).json()
    # связка: решение одно на тревогу — v3, без тревоги, исход «нет» — TN; версии не смешаны
    assert m["model_version"] == "v3>baseline-v0"
    assert (m["all"]["decisions"], m["all"]["fp"], m["all"]["tn"]) == (1, 0, 1)
    v0 = client.get("/api/v1/metrics", params={"model": "smoke_alarm", "run": 0, "model_version": "baseline-v0"}).json()
    assert (v0["model_version"], v0["all"]["decisions"], v0["all"]["fp"]) == ("baseline-v0", 1, 1)
    paired = client.get(
        "/api/v1/metrics", params={"model": "smoke_alarm", "run": 0, "model_version": "v3", "paired": "true"}
    ).json()
    assert paired["all"]["decisions"] == 1  # у базы обе версии оценили тревогу — решение парное


def test_base_metrics_without_history_decisions_of_both_versions(seeded, client):
    _base_decisions(seeded, v3=False)
    m = client.get("/api/v1/metrics", params={"model": "smoke_alarm", "run": 0}).json()
    assert m["model_version"] == "baseline-v0"  # как раньше: все решения базы
    assert m["all"]["decisions"] == 1


def test_history_decisions_stay_out_of_run_feed(seeded, client):
    """Лента прогона показывает базовые тревоги до его старта (`run_alarms`). Решения истории по ним (`run_id NULL`,
    ссылка `alarms.prediction_id`) в ленту прогона не подмешиваются: ни оценки версий, ни решение, ни расхождение."""
    _base_decisions(seeded, v3=True)
    _sql(
        seeded,
        "INSERT INTO runs (id, scenario, status, sim_start, sim_now, primary_model) "
        "VALUES (7, 't', 'running', '2026-01-05 12:00+03', '2026-01-05 13:00+03', 'v3>baseline-v0')",
    )
    run = client.get("/api/v1/alarms", params={"run": 7}).json()
    (alarm,) = run["items"]  # базовая тревога 05.01 10:00 — до старта прогона
    assert alarm["scores"] == [] and alarm["prediction"] is None
    assert run["counts"]["disagree"] == 0
    history = client.get("/api/v1/alarms", params={"run": 0}).json()
    (alarm,) = history["items"]  # в самой истории — её решения
    assert {s["model_version"] for s in alarm["scores"]} == {"baseline-v0", "v3"}
    assert (alarm["prediction"]["id"], alarm["prediction"]["decided_by"]) == (2, "v3")


def test_all_data_scope(seeded, client):
    """«Все данные» (`run=all`): хакатон и прогоны-основы наборов вместе, каждый со своим временем."""
    from app.domain import history as h

    h._dataset_cache.clear()
    _sql(
        seeded,
        "INSERT INTO objects (id, parent_id, level, kind, name, source) VALUES "
        "(200, NULL, 1, 'district', 'Цех набора', 'magdeburg'), (210, 200, 2, 'controlHouse', 'Узел', 'magdeburg'), "
        "(220, 210, 3, 'controlHouse', 'Узел · 1', 'magdeburg')",
        "INSERT INTO channels (id, object_id, system_type, sensor_type, tag, name, stream, source) VALUES "
        "(2001, 220, 'Пожарная охрана', 'Датчик дыма', 'm-1', 'Узел 1', 'significant', 'magdeburg')",
        "INSERT INTO runs (id, scenario, status, kind, sim_start, sim_end, sim_now, params) VALUES "
        "(28, 'dataset:magdeburg', 'finished', 'dataset', '2023-07-10 10:00+03', '2023-07-13 15:00+03', "
        "'2023-07-13 15:00+03', '{\"dataset\": \"magdeburg\"}')",
        "INSERT INTO channel_daily (run_id, channel_id, date, n_events, n_alarm, n_fault, n_num) VALUES "
        "(28, 2001, '2023-07-10', 40, 3, 0, 0), (28, 2001, '2023-07-11', 60, 0, 0, 0)",
        "INSERT INTO channel_state (run_id, channel_id, ts, value_raw, is_alarm) VALUES "
        "(NULL, 1001, '2026-06-30 23:59+03', 'Норма', false), (28, 2001, '2023-07-13 14:00+03', 'пожар', true)",
        "INSERT INTO predictions (run_id, model, model_version, channel_id, decision_time, horizon_start, horizon_end, "
        "status, score, threshold, alert) VALUES "
        "(NULL, 'daily', 'baseline-v0', 1001, '2026-01-05 00:00+03', '2026-01-06 00:00+03', '2026-02-04 00:00+03', "
        "'scored', 0.2, 0.6, false), "
        "(28, 'magdeburg_fire', 'v0', 2001, '2023-07-11 10:00+03', '2023-07-11 10:00+03', '2023-07-11 10:00+03', "
        "'scored', 0.9, 0.5, true)",
    )
    roots = client.get("/api/v1/objects", params={"run": "all"}).json()
    assert [(r["name"], r["source"]) for r in roots] == [("Район", "hackathon"), ("Цех набора", "magdeburg")]
    graph = client.get("/api/v1/graph", params={"run": "all"}).json()
    assert {n["source"] for n in graph["nodes"]} == {"hackathon", "magdeburg"}
    assert next(n for n in graph["nodes"] if n["id"] == "object:210")["channels"] == 1

    def total(**q):
        return client.get("/api/v1/predictions", params={"run": "all", **q}).json()["total"]

    assert (total(), total(source="hackathon"), total(source="magdeburg"), total(source="vast2017")) == (2, 1, 1, 0)
    assert client.get("/api/v1/predictions", params={"run": 0}).json()["total"] == 1  # «Вся история» — только хакатон

    (ds,) = client.get("/api/v1/dashboard/datasets-history").json()
    assert (ds["dataset"], ds["run_id"], ds["objects"], ds["channels"]) == ("magdeburg", 28, 1, 1)
    assert ds["months"] == [{"month": "2023-07-01", "events": 100, "alarm_events": 3, "decisions": 1, "alerts": 1}]
    # решения, дописанные в основу после первого ответа (`history external`), видны без перезапуска api
    _sql(
        seeded,
        "INSERT INTO predictions (run_id, model, model_version, channel_id, decision_time, horizon_start, horizon_end, "
        "status, score, threshold, alert) VALUES (28, 'magdeburg_fire', 'v0', 2001, '2023-07-11 10:01+03', "
        "'2023-07-11 10:01+03', '2023-07-11 10:01+03', 'scored', 0.1, 0.5, false)",
    )
    (ds,) = client.get("/api/v1/dashboard/datasets-history").json()
    assert (ds["months"][0]["decisions"], ds["months"][0]["alerts"]) == (2, 1)
    # канал набора в «Все данные» — в прогоне-основе своего набора
    card = client.get("/api/v1/channels/2001", params={"run": "all"}).json()
    assert card["value"] == "пожар"  # состояние — из прогона-основы набора, не из базы хакатона
    # в истории хакатона канала набора нет в справочнике
    assert client.get("/api/v1/channels/2001", params={"run": 0}).status_code == 404
