import asyncio

import httpx
from conftest import FakeEmulator, write_scenario, write_slice, write_upload

from app.control import create_control
from app.player import Player


def test_control_api(catalog, tmp_path):
    write_scenario(tmp_path / "sc", "a", "2026-03-28T05:00:00", "2026-03-28T06:00:00", description="Тест")
    write_scenario(tmp_path / "sc", "b", "2026-03-28T05:00:00", "2026-03-28T06:00:00")
    write_slice(tmp_path / "sl" / "a.parquet", [(1, 287267, "2026-03-28 05:30:00", True, "Обнаружен дым")])
    player = Player(FakeEmulator(catalog), tmp_path / "sc", tmp_path / "sl", max_rate=200)
    app = create_control(player)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://emulator") as http:
            listed = (await http.get("/scenarios")).json()["scenarios"]
            assert [(s["name"], s["ready"]) for s in listed] == [("a", True), ("b", False)]
            assert listed[0]["period"]["start"] == "2026-03-28T05:00:00+03:00"

            r = await http.post("/start", json={"scenario": "b"})
            assert r.status_code == 409 and r.json()["error"]["code"] == "slice_not_ready"
            r = await http.post("/pause")
            assert r.status_code == 409 and r.json()["error"]["code"] == "invalid_state"

            r = await http.post("/start", json={"scenario": "a", "speed": 30, "run_id": 5})
            assert r.status_code == 200
            state = r.json()
            assert (state["state"], state["run_id"], state["target_speed"]) == ("running", 5, 30)
            assert state["channels"] == 4 and state["progress"] == {"emitted": 0, "total": 1}

            r = await http.post("/speed", json={"speed": 100})
            assert r.status_code == 422 and r.json()["error"]["code"] == "speed_out_of_range"
            assert (await http.post("/pause")).json()["state"] == "paused"
            assert (await http.post("/resume")).json()["state"] == "running"
            assert (await http.post("/stop")).json()["state"] == "stopped"
            assert (await http.get("/state")).json()["sim_time"] == "2026-03-28T05:00:00+03:00"

    asyncio.run(scenario())


def test_scenario_data_endpoints(catalog, tmp_path):
    write_scenario(tmp_path / "sc", "a", "2026-03-28T05:00:00", "2026-03-28T06:00:00")
    write_scenario(tmp_path / "sc", "b", "2026-03-28T05:00:00", "2026-03-28T06:00:00")
    write_slice(
        tmp_path / "sl" / "a.parquet",
        [
            (1, 287267, "2026-03-28 05:30:00", True, "Обнаружен дым"),
            (2, 287268, "2026-03-28 05:31:00", False, "Норма"),
            (3, 120298, "2026-03-28 05:32:00", False, "18"),
        ],
    )
    player = Player(FakeEmulator(catalog), tmp_path / "sc", tmp_path / "sl", max_rate=200)
    app = create_control(player)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://emulator") as http:
            summary = (await http.get("/scenarios/a/summary")).json()
            assert (summary["events"], summary["alarm_events"], summary["channels"]) == (3, 1, 3)

            r = await http.get(
                "/scenarios/a/events", params=[("sensor_type", "Датчик дыма"), ("sensor_type", "Ручной извещатель")]
            )
            assert [i["seq"] for i in r.json()["items"]] == [1, 2]
            r = await http.get(
                "/scenarios/a/events", params={"sensor_type": "Датчик дыма,Ручной извещатель", "limit": 1}
            )
            assert r.json()["total"] == 2 and r.json()["next_cursor"] == "1"
            r = await http.get("/scenarios/a/events", params={"from": "2026-03-28T05:31:00", "alarm": "false"})
            assert [i["seq"] for i in r.json()["items"]] == [2, 3]

            for path, status, code in [
                ("/scenarios/zzz/summary", 404, "scenario_not_found"),
                ("/scenarios/b/events", 409, "slice_not_ready"),
                ("/scenarios/a/events?limit=много", 422, "validation_error"),
                ("/scenarios/a/events?cursor=x", 422, "bad_cursor"),
            ]:
                r = await http.get(path)
                assert (r.status_code, r.json()["error"]["code"]) == (status, code), path

            # загруженный сценарий появляется без перезапуска и открывается так же
            write_upload(
                tmp_path / "sl",
                "upload-20260927-abc123",
                [(None, 287267, "2026-08-01 00:10:00", True, "Обнаружен дым")],
            )
            listed = (await http.get("/scenarios")).json()["scenarios"]
            assert [(s["name"], s["source"], s["ready"]) for s in listed] == [
                ("a", "builtin", True),
                ("b", "builtin", False),
                ("upload-20260927-abc123", "upload", True),
            ]
            r = await http.get("/scenarios/upload-20260927-abc123/events")
            assert r.json()["items"][0]["event_id"] is None and r.json()["total"] == 1
            r = await http.post("/start", json={"scenario": "upload-20260927-abc123"})
            assert r.json()["state"] == "running" and r.json()["progress"]["total"] == 1

    asyncio.run(scenario())
