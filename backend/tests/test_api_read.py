"""Эндпоинты чтения по docs/api.md v0.2 на минимальном справочнике: район 1 → объект 10 → подобъект 100,
каналы 1001 (дым), 1002 (газ), 1003 (температура)."""

import asyncio
import json

import pytest
from sqlalchemy import create_engine, text

API = "/api/v1"


def ingest(client, events: list[dict]) -> dict:
    r = client.post(f"{API}/ingest/events", json=events)
    assert r.status_code == 202, r.text
    return r.json()


@pytest.fixture
def data(client):
    ingest(
        client,
        [
            {"channel_id": 1003, "ts": "2026-05-01T10:00:00+03:00", "value": "18"},
            {"channel_id": 1003, "ts": "2026-05-01T11:00:00+03:00", "value": "Неисправен", "is_alarm": True},
            {"channel_id": 1002, "ts": "2026-05-01T10:00:00+03:00", "value": "0.01"},
            {"channel_id": 1001, "ts": "2026-05-01T09:00:00+03:00", "value": "Дыма нет"},
            {"channel_id": 1001, "ts": "2026-05-01T12:00:00+03:00", "value": "Обнаружен дым", "is_alarm": True},
        ],
    )
    return client


def test_objects_tree_and_detail(data):
    tree = data.get(f"{API}/objects").json()
    district = tree[0]
    assert (district["id"], district["level"], district["channels"]) == (1, "district", 3)
    assert district["active_alarms"] == 1 and district["in_fault"] == 1 and district["status"] == "alarm"
    sub = district["children"][0]["children"][0]
    assert (sub["id"], sub["level"]) == (100, "sub_object")

    detail = data.get(f"{API}/objects/10").json()
    assert [p["id"] for p in detail["path"]] == [1, 10]
    assert [s["id"] for s in detail["sub_objects"]] == [100]
    assert {s["sensor_type"] for s in detail["by_sensor_type"]} == {
        "Датчик дыма",
        "Газовый датчик",
        "Датчик температуры",
    }
    assert data.get(f"{API}/objects/999").json()["error"]["code"] == "object_not_found"


def test_graph(data):
    g = data.get(f"{API}/graph").json()
    ids = {n["id"] for n in g["nodes"]}
    assert ids == {"district:1", "object:10", "sub_object:100"}
    assert {(e["source"], e["target"]) for e in g["edges"]} == {
        ("district:1", "object:10"),
        ("object:10", "sub_object:100"),
    }

    sub = data.get(f"{API}/graph/100").json()
    channels = {n["id"]: n for n in sub["nodes"] if n["kind"] == "channel"}
    assert channels["channel:1003"]["status"] == "fault" and channels["channel:1001"]["status"] == "alarm"
    group = next(n for n in sub["nodes"] if n["kind"] == "tag_group")
    assert group["id"] == "tag_group:100:без группы" and group["channels"] == 3 and group["in_fault"] == 1
    assert data.get(f"{API}/graph/10").status_code == 404  # не подобъект


def test_channels_list_detail_timeline(data):
    page = data.get(f"{API}/channels", params={"state": "fault"}).json()
    assert page["total"] == 1 and page["items"][0]["id"] == 1003
    assert page["items"][0]["value"] == "Неисправен" and page["items"][0]["object_name"] == "Объект А"
    assert data.get(f"{API}/channels", params={"q": "газ"}).json()["items"][0]["id"] == 1002
    assert data.get(f"{API}/channels", params={"object_id": 100, "size": 2}).json()["total"] == 3

    card = data.get(f"{API}/channels/1003").json()
    assert card["counters"]["faults_365d"] == 1 and card["counters"]["fault_hours_30d"] > 0
    assert {n["id"] for n in card["neighbors"]} == {1001, 1002}
    assert card["last_alarm_prediction"] is None and card["open_work_orders"] == []
    assert card["fault_since"] == "2026-05-01T08:00:00Z" and card["data_gap"] is None  # база — разрыва нет

    tl = data.get(f"{API}/channels/1003/timeline").json()
    assert [e["value"] for e in tl["events"]] == ["18", "Неисправен"]
    assert tl["episodes"] == [{"kind": "fault", "start_ts": "2026-05-01T08:00:00Z", "end_ts": None}]
    assert tl["daily"][0]["n_fault"] == 1 and "from" in tl
    smoke = data.get(f"{API}/channels/1001/timeline").json()
    assert [e["kind"] for e in smoke["episodes"]] == ["alarm"]
    assert data.get(f"{API}/channels/1003/predictions").json() == {"items": [], "total": 0, "page": 1, "size": 50}


def test_channel_detail_data_gap(data, seeded):
    # прогон через месяц после конца базы (журнал после 30.06, A18): неисправность канала — «на начало журнала»
    with create_engine(seeded).begin() as conn:
        conn.execute(
            text("INSERT INTO runs (id, scenario, status, sim_start) VALUES (60, 'upload', 'running', :s)"),
            {"s": "2026-06-01T00:00:00+03:00"},
        )
        conn.execute(
            text(
                "INSERT INTO channel_state (run_id, channel_id, ts, value_raw, value_num, is_alarm, last_alarm_at, "
                "fault_since) SELECT 60, channel_id, ts, value_raw, value_num, is_alarm, last_alarm_at, fault_since "
                "FROM channel_state WHERE run_id IS NULL"
            )
        )
    card = data.get(f"{API}/channels/1003", params={"run": 60}).json()
    assert card["fault_since"] == "2026-05-01T08:00:00Z"
    assert card["data_gap"] == {"start_ts": "2026-05-01T09:00:00Z", "end_ts": "2026-05-31T21:00:00Z"}


def test_alarms_feed(data):
    page = data.get(f"{API}/alarms").json()
    assert page["total"] == 1
    alarm = page["items"][0]
    assert alarm["verdict"] == "pending" and alarm["episode"]["kind"] == "solitary"
    assert page["counts"] == {
        "active": 1,
        "ppr": 0,
        "ppr_alert": 0,
        "sensor_fault": 0,
        "reaction": 0,
        "no_score": 0,
        "disagree": 0,
    }
    assert data.get(f"{API}/alarms", params={"alert": "true"}).json()["total"] == 0  # решения модели ещё нет
    assert data.get(f"{API}/alarms", params={"alert": "false"}).json()["total"] == 1
    assert data.get(f"{API}/alarms", params={"verdict": "ppr"}).json()["total"] == 0
    assert data.get(f"{API}/alarms", params={"verdict": "ppr"}).json()["counts"]["active"] == 1  # без verdict
    assert data.get(f"{API}/alarms", params={"object_id": 100, "kind": "solitary"}).json()["total"] == 1

    detail = data.get(f"{API}/alarms/{alarm['id']}").json()
    assert detail["neighbors"] == [] and detail["prediction_detail"] is None  # других дымовых на подобъекте нет
    assert data.get(f"{API}/alarms/999999").json()["error"]["code"] == "alarm_not_found"


def test_clock_and_unknown_run(data):
    assert data.get(f"{API}/clock").json()["mode"] in ("real", "stream")
    assert data.get(f"{API}/objects", params={"run": 999}).json()["error"]["code"] == "run_not_found"


def test_stream_relays_published_events(seeded):
    from app.api.stream import events
    from app.core.redis import async_client
    from app.domain.notify import Notification, publish

    class Request:
        calls = 0

        async def is_disconnected(self):
            self.calls += 1
            return self.calls > 6

    async def run() -> list[str]:
        try:
            await async_client().ping()
        except Exception:  # noqa: BLE001
            pytest.skip("Redis стенда недоступен")
        out = []
        gen = events(Request(), 424242, None, None)
        out.append(await anext(gen))  # подписка оформлена
        await publish([Notification(424242, "alarm.created", {"id": 1})])
        async for chunk in gen:
            out.append(chunk)
        return out

    chunks = asyncio.run(run())
    assert chunks[0] == ": connected\n\n"
    event = next(c for c in chunks if c.startswith("event: alarm.created"))
    assert json.loads(event.split("data: ", 1)[1]) == {"id": 1}
