"""Загрузка внешнего набора (app/external_loader.py) и невмешательство в данные хакатона (этап 14)."""

import hashlib
import json

import duckdb
import psycopg
import pytest

from app.external_loader import ExternalLoader, ExternalLoadError
from app.loader import pg_conninfo

K = 9  # набор «test»: объекты 9 090 000…, каналы 909 000 000…
ROOT, HALL, NODE = 9_090_001, 9_090_002, 9_090_003
TEMP, TARGET, CODE = 909_000_001, 909_000_002, 909_000_003


def write_prepared(path, k: int = K, codes: bool = False) -> None:
    """Маленький prepared/ в формате lct-18: справочник, события, разметка, пропуска, допуски, report.json.
    `codes` — ещё канал «Код ошибки»: событийный, единственное событие — с флагом тревоги, как у Azure PdM."""
    path.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    files = {
        "objects": f"""SELECT * FROM (VALUES
            ({ROOT}, NULL, 1, 'site', 'Площадка', NULL),
            ({HALL}, {ROOT}, 2, 'hall', 'Цех', '{{"area_m2": 350}}'),
            ({NODE}, {HALL}, 3, 'node', 'Узел 1', NULL)
        ) t(id, parent_id, level, kind, name, meta)""",
        "channels": f"""SELECT * FROM (VALUES
            ({TEMP}, {NODE}, 'Пожарная охрана', 'Термометр', 'node1/T', 'T узла 1', NULL, NULL, 'node1',
             'significant', CAST(NULL AS DOUBLE), '°C'),
            ({TARGET}, {NODE}, 'Пожарная охрана', 'Узел', 'node1', 'Узел 1', NULL, NULL, 'node1',
             'significant', NULL, NULL)
            {
            f", ({CODE}, {NODE}, 'Пожарная охрана', 'Код ошибки', 'node1/err', 'Коды', NULL, NULL, 'node1', "
            "'significant', NULL, NULL)"
            if codes
            else ""
        }
        ) t(id, object_id, system_type, sensor_type, tag, name, picket_number, picket_offset, tag_group, stream,
            deadband, unit)""",
        "events": f"""SELECT * FROM (VALUES
            (1, {TEMP}, TIMESTAMP '2023-07-10 10:00:00', false, '21.0'),
            (2, {TEMP}, TIMESTAMP '2023-07-10 10:00:10', false, '21.5'),
            (3, {TEMP}, TIMESTAMP '2023-07-10 11:00:00', false, 'Неисправен'),
            (4, {TEMP}, TIMESTAMP '2023-07-10 11:30:00', false, '22.0'),
            (5, {TEMP}, TIMESTAMP '2023-07-11 09:00:00', false, '23.0')
            {f", (6, {CODE}, TIMESTAMP '2023-07-10 10:05:00', true, 'error3')" if codes else ""}
        ) t(event_id, channel_id, ts, is_alarm, value)""",
        "labels": f"""SELECT * FROM (VALUES
            ({HALL}, CAST(NULL AS INTEGER), TIMESTAMP '2023-07-10 10:00:00', TIMESTAMP '2023-07-10 10:30:00',
             'fire', '{{"experiment": 1}}')
        ) t(object_id, channel_id, start_ts, end_ts, label, meta)""",
        "credentials": "SELECT * FROM (VALUES ('car-1', 'vehicle', NULL)) t(credential_id, kind, meta)",
        "permits": f"""SELECT * FROM (VALUES ('car-1', {HALL}, CAST(NULL AS TIMESTAMP), CAST(NULL AS TIMESTAMP)))
            t(credential_id, object_id, valid_from, valid_to)""",
    }
    rows, outputs = {}, {}
    for name, sql in files.items():
        file = path / f"{name}.parquet"
        con.execute(f"COPY ({sql}) TO '{file}' (FORMAT parquet)")
        rows[name] = con.execute(f"SELECT count(*) FROM '{file}'").fetchone()[0]
        outputs[file.name] = {"sha256": hashlib.sha256(file.read_bytes()).hexdigest()}
    report = {"dataset": "test", "k": k, "title": "Тестовый набор", "rows": rows, "outputs": outputs}
    (path / "report.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")


def load(seeded, prepared, tmp_path) -> dict:
    url = seeded.render_as_string(hide_password=False)
    return ExternalLoader("test", prepared, url, tmp_path / "work", workers=2, memory_limit="1GB", threads=2).run()


def query(seeded, sql: str, params=None) -> list[tuple]:
    with psycopg.connect(pg_conninfo(seeded.render_as_string(hide_password=False))) as pg:
        return pg.execute(sql, params).fetchall()


def test_load_external(seeded, tmp_path):
    prepared = tmp_path / "prepared"
    write_prepared(prepared)
    report = load(seeded, prepared, tmp_path)
    run = report["info"]["run_id"]
    assert report["rows"]["events"] == 5 and report["rows"]["external_labels"] == 1
    assert query(seeded, "SELECT kind, status, params->>'dataset', base_run_id FROM runs WHERE id = %s", (run,)) == [
        ("dataset", "finished", "test", None)
    ]
    assert query(seeded, "SELECT count(*) FROM run_events(%s)", (run,)) == [(5,)]
    # счётчик прогона для /runs — тем же числом, что и поток прогона
    assert query(seeded, "SELECT events FROM runs WHERE id = %s", (run,)) == [(5,)] and report["info"]["events"] == 5
    assert query(seeded, "SELECT count(*) FROM run_events(NULL) WHERE channel_id = %s", (TEMP,)) == [(0,)]
    state = query(seeded, "SELECT value_raw, value_num FROM channel_state WHERE run_id = %s", (run,))
    assert state == [("23.0", 23.0)]
    faults = query(seeded, "SELECT state, end_ts IS NOT NULL FROM fault_episodes WHERE run_id = %s", (run,))
    assert faults == [("Неисправен", True)]
    daily = query(seeded, "SELECT date::text, n_events FROM channel_daily WHERE run_id = %s ORDER BY 1", (run,))
    assert daily == [("2023-07-10", 4), ("2023-07-11", 1)]
    catalog = query(seeded, "SELECT id, source, unit FROM channels WHERE id >= %s ORDER BY id", (TEMP,))
    assert catalog == [(TEMP, "test", "°C"), (TARGET, "test", None)]
    assert query(seeded, "SELECT meta->>'area_m2' FROM objects WHERE id = %s", (HALL,)) == [("350",)]
    assert query(seeded, "SELECT label, meta->>'experiment' FROM external_labels") == [("fire", "1")]
    assert query(seeded, "SELECT credential_id, object_id FROM external_permits") == [("car-1", HALL)]

    again = load(seeded, prepared, tmp_path)  # повторная загрузка заменяет основу и разметку
    assert again["info"]["replaced_runs"] == [run]
    assert query(seeded, "SELECT count(*) FROM runs WHERE kind = 'dataset'") == [(1,)]
    assert query(seeded, "SELECT count(*) FROM events WHERE channel_id = %s", (TEMP,)) == [(5,)]
    assert query(seeded, "SELECT count(*) FROM external_labels") == [(1,)]


def test_load_external_refuses(seeded, tmp_path):
    prepared = tmp_path / "prepared"
    write_prepared(prepared, k=8)  # id набора — из диапазона k = 9, а в отчёте k = 8
    with pytest.raises(ExternalLoadError, match="вне диапазона"):
        load(seeded, prepared, tmp_path)

    write_prepared(prepared)
    (prepared / "labels.parquet").write_bytes((prepared / "labels.parquet").read_bytes() + b"x")
    with pytest.raises(ExternalLoadError, match="sha256"):
        load(seeded, prepared, tmp_path)

    write_prepared(prepared)
    run = load(seeded, prepared, tmp_path)["info"]["run_id"]
    with psycopg.connect(pg_conninfo(seeded.render_as_string(hide_password=False))) as pg:
        pg.execute(
            "INSERT INTO runs (scenario, status, sim_start, base_run_id) VALUES ('ext', 'running', now(), %s)", (run,)
        )
    with pytest.raises(ExternalLoadError, match="сценарные прогоны"):
        load(seeded, prepared, tmp_path)


ENDPOINTS = [
    "/objects",
    "/graph",
    "/channels",
    "/alarms",
    "/predictions",
    "/work-orders",
    "/dashboard/summary",
    "/dashboard/risk",
    "/equipment-episodes",
]


def test_external_data_does_not_touch_hackathon(client, seeded, tmp_path):
    """Тест невмешательства: ответы API хакатона с загруженным внешним набором и без него одинаковы."""
    events = [
        {"channel_id": 1001, "ts": "2026-05-01T12:00:00+03:00", "value": "Обнаружен дым", "is_alarm": True},
        {"channel_id": 1003, "ts": "2026-05-01T12:05:00+03:00", "value": "18"},
        {"channel_id": 1002, "ts": "2026-05-01T12:06:00+03:00", "value": "0.05"},
    ]
    assert client.post("/api/v1/ingest/events", json=events).status_code == 202

    def snapshot() -> dict:
        out = {}
        for path in ENDPOINTS:
            for params in ({}, {"run": 0}):  # без параметра — «последний прогон», у нас — база
                r = client.get(f"/api/v1{path}", params=params)
                assert r.status_code == 200, (path, r.text)
                out[(path, tuple(params))] = r.json()
        return out

    before = snapshot()
    prepared = tmp_path / "prepared"
    write_prepared(prepared)
    load(seeded, prepared, tmp_path)
    after = snapshot()
    for key in before:
        assert after[key] == before[key], key
    runs = client.get("/api/v1/runs").json()["items"]
    assert [(r["kind"], r["dataset"], r["events"]) for r in runs] == [("dataset", "test", 5)]
    # в прогоне набора — справочник набора, у каждого объекта, канала и узла графа свой источник
    base = runs[0]["id"]
    assert {c["source"] for c in client.get("/api/v1/channels", params={"run": base}).json()["items"]} == {"test"}
    tree = client.get("/api/v1/objects", params={"run": base}).json()
    assert (tree[0]["id"], tree[0]["source"], tree[0]["children"][0]["source"]) == (ROOT, "test", "test")
    assert client.get(f"/api/v1/objects/{HALL}", params={"run": base}).json()["sub_objects"][0]["source"] == "test"
    nodes = client.get(f"/api/v1/graph/{NODE}", params={"run": base}).json()["nodes"]
    assert {n["source"] for n in nodes} == {"test"}
    assert {n["source"] for n in after[("/graph", ())]["nodes"]} == {"hackathon"}


def test_event_channel_does_not_hold_alarm(client, seeded, tmp_path):
    """Код ошибки — событие, а не состояние: последний код с флагом тревоги не держит канал и объект в тревоге
    (прогон 53 `ext-pdm-week`: все 100 агрегатов красные, «Каналы в тревоге 100»). В истории канала само событие —
    тревожное."""
    prepared = tmp_path / "prepared"
    write_prepared(prepared, codes=True)
    base = load(seeded, prepared, tmp_path)["info"]["run_id"]
    state = "SELECT value_raw, is_alarm FROM run_channel_state(%s) WHERE channel_id = %s"
    assert query(seeded, state, (base, CODE)) == [("error3", True)]  # состояние в базе как было — статус нет
    channels = {c["id"]: c for c in client.get("/api/v1/channels", params={"run": base}).json()["items"]}
    assert channels[CODE]["status"] == "ok"
    node = client.get(f"/api/v1/objects/{HALL}", params={"run": base}).json()["sub_objects"][0]
    assert (node["status"], node["active_alarms"]) == ("ok", 0)
    assert client.get("/api/v1/dashboard/summary", params={"run": base}).json()["active_alarms"] == 0
    history = client.get(f"/api/v1/channels/{CODE}/timeline", params={"run": base}).json()
    assert [e["status"] for e in history["events"]] == ["alarm"]
    assert [e for e in history["episodes"] if e["kind"] == "alarm"] == []  # не интервал «тревога до сих пор»


def test_datasets(client, seeded):
    """Реестр наборов и что загружено: без основы — пусто, с основой — период и объём."""
    items = {d["key"]: d for d in client.get("/api/v1/datasets").json()["items"]}
    magdeburg = items["magdeburg"]
    assert (magdeburg["direction"], magdeburg["synthetic"], magdeburg["base_run_id"]) == ("fire_risk", False, None)
    assert magdeburg["doi"] and magdeburg["license_url"] and magdeburg["channels"] == 0
    assert items["vast2017"]["doi"] is None
    base = query(
        seeded,
        "INSERT INTO runs (scenario, status, kind, sim_start, sim_end, params) VALUES ('dataset:magdeburg', "
        "'finished', 'dataset', '2023-07-10 10:00+03', '2023-07-13 12:00+03', "
        '\'{"dataset": "magdeburg", "rows": {"events": 5}}\') RETURNING id',
    )[0][0]
    query(
        seeded,
        "INSERT INTO objects (id, parent_id, level, kind, name, source) VALUES "
        "(9010001, NULL, 1, 'site', 'Цех', 'magdeburg') RETURNING id",
    )
    magdeburg = next(d for d in client.get("/api/v1/datasets").json()["items"] if d["key"] == "magdeburg")
    assert (magdeburg["base_run_id"], magdeburg["events"], magdeburg["objects"]) == (base, 5, 1)
    assert magdeburg["period_start"] == "2023-07-10T07:00:00Z"
