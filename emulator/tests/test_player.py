import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import duckdb
import pytest
from conftest import FakeEmulator, write_scenario, write_slice

from app.player import FINISHED, PAUSED, RUNNING, STOPPED, Player, PlayerError, load_slice
from app.scenarios import density_max_speed


@pytest.fixture
def dirs(tmp_path):
    return tmp_path / "scenarios", tmp_path / "slices"


def make_player(catalog, dirs, max_rate=200.0, tick=0.1) -> Player:
    return Player(FakeEmulator(catalog), dirs[0], dirs[1], max_rate=max_rate, tick=tick)


def utc(moscow: str) -> datetime:
    return datetime.fromisoformat(moscow + "+03:00").astimezone(UTC)


def test_load_slice_orders_same_second_events_by_milliseconds(tmp_path):
    path = tmp_path / "s.parquet"
    write_slice(
        path,
        [
            (3, 287267, "2026-03-28 08:04:19", False, "Дыма нет"),
            (2, 287267, "2026-03-28 08:04:19", False, "Норма"),
            (4, 120298, "2026-03-28 08:04:19", False, "18"),
            (5, 999999, "2026-03-28 08:04:20", True, "вне справочника"),
        ],
    )
    data = load_slice(path, known_channels={287267, 120298})
    t = utc("2026-03-28T08:04:19")
    assert list(zip(data.channel, data.value, data.source, strict=True)) == [
        (287267, "Норма", t),
        (287267, "Дыма нет", t + timedelta(milliseconds=1)),
        (120298, "18", t),
    ]
    assert data.ts == [t.timestamp()] * 3


def test_sim_time_follows_target_speed(catalog, dirs):
    write_scenario(dirs[0], "quiet", "2026-03-28T05:00:00", "2026-03-28T06:00:00")
    write_slice(dirs[1] / "quiet.parquet", [(1, 287267, "2026-03-28 05:30:00", True, "Обнаружен дым")])
    player = make_player(catalog, dirs)

    async def scenario():
        await player.start("quiet", speed=60, run_id=7)
        for _ in range(50):
            await player.step(0.1)
        return player.describe()

    state = asyncio.run(scenario())
    assert state["state"] == RUNNING and state["run_id"] == 7
    assert state["sim_time"] == "2026-03-28T05:05:00+03:00"
    assert state["effective_speed"] == pytest.approx(60, rel=0.05)
    assert player.emulator.service["RunId"] == 7 and player.emulator.service["State"] == RUNNING


def test_burst_is_stretched_not_dropped(catalog, dirs):
    """Всплеск сверх предела выхода: время замедляется, события не теряются, порядок сохраняется."""
    write_scenario(dirs[0], "burst", "2026-03-28T05:00:00", "2026-03-28T05:01:00")
    rows = [(i, 287267 if i % 2 else 287268, "2026-03-28 05:00:10", True, f"v{i}") for i in range(100)]
    rows.append((100, 120298, "2026-03-28 05:00:20", False, "18"))
    write_slice(dirs[1] / "burst.parquet", rows)
    player = make_player(catalog, dirs, max_rate=50)
    burst_end = utc("2026-03-28T05:00:10").timestamp()

    async def scenario():
        await player.start("burst", speed=60)
        per_step, slow = [], []
        while player.state == RUNNING:
            before = len(player.emulator.published)
            await player.step(0.1)
            per_step.append(len(player.emulator.published) - before)
            if player.cursor < 100:
                assert player.sim_now <= burst_end  # время не обгоняет неотправленные события
                slow.append(player.effective_speed)
        return per_step, slow

    per_step, slow = asyncio.run(scenario())
    published = player.emulator.published
    assert [v for _, _, v, _ in published] == [f"v{i}" for i in range(100)] + ["18"]
    assert max(per_step) <= 5  # 50 событий/с × шаг 0.1 с
    assert min(slow) < 30  # во всплеске фактическая скорость падает ниже целевой
    assert player.state == FINISHED
    assert player.describe()["sim_time"] == "2026-03-28T05:01:00+03:00"


def test_state_transitions_and_speed_limits(catalog, dirs):
    write_scenario(dirs[0], "a", "2026-03-28T05:00:00", "2026-03-28T06:00:00")
    write_scenario(dirs[0], "fast", "2026-03-28T05:00:00", "2026-03-28T06:00:00", speed=600, max_speed=1000)
    write_scenario(dirs[0], "no-slice", "2026-03-28T05:00:00", "2026-03-28T06:00:00")
    for name in ("a", "fast"):
        write_slice(dirs[1] / f"{name}.parquet", [(1, 287267, "2026-03-28 05:30:00", True, "Обнаружен дым")])
    player = make_player(catalog, dirs)

    async def expect(code, coro):
        with pytest.raises(PlayerError) as e:
            await coro
        assert e.value.code == code

    async def scenario():
        await expect("invalid_state", player.pause())
        await expect("scenario_not_found", player.start("nope"))
        await expect("slice_not_ready", player.start("no-slice"))
        await expect("speed_out_of_range", player.start("a", speed=61))
        await player.start("a")
        assert (await player.pause())["state"] == PAUSED
        await player.step(1.0)
        assert player.sim_now == utc("2026-03-28T05:00:00").timestamp()  # на паузе время стоит
        assert (await player.resume())["state"] == RUNNING
        await expect("speed_out_of_range", player.set_speed(0.5))
        assert (await player.set_speed(30))["target_speed"] == 30
        assert (await player.stop())["state"] == STOPPED
        await expect("invalid_state", player.resume())
        assert (await player.start("fast"))["target_speed"] == 600
        assert (await player.set_speed(1000))["target_speed"] == 1000

    asyncio.run(scenario())


def test_new_start_resets_channels_of_previous_run(catalog, dirs):
    write_scenario(dirs[0], "a", "2026-03-28T05:00:00", "2026-03-28T06:00:00")
    write_slice(
        dirs[1] / "a.parquet",
        [(1, 287267, "2026-03-28 05:00:01", True, "Обнаружен дым"), (2, 120298, "2026-03-28 05:00:02", False, "18")],
    )
    player = make_player(catalog, dirs)

    async def scenario():
        await player.start("a", run_id=1)
        await player.step(0.1)
        await player.start("a", run_id=2)

    asyncio.run(scenario())
    assert player.emulator.resets == [set(), {287267, 120298}]
    assert player.run_id == 2 and player.cursor == 0


def test_slice_reads_from_threads(tmp_path):
    """Срез читается при старте (поток asyncio), а список сценариев — в потоках HTTP: у каждого вызова своё
    соединение DuckDB. Общее по умолчанию ломалось — «closed pending query result», старт прогона 500 (lct, d93540d)."""
    path = tmp_path / "big.parquet"
    duckdb.connect().execute(
        "COPY (SELECT i AS event_id, 1000 + i % 50 AS channel_id, "
        "TIMESTAMP '2026-03-28 05:00:00' + to_seconds(i) AS ts, false AS is_alarm, CAST(i AS VARCHAR) AS value "
        f"FROM range(200000) t(i)) TO '{path}' (FORMAT parquet)"
    )
    calls = [lambda: len(load_slice(path)), lambda: density_max_speed(path, 200.0)] * 12
    with ThreadPoolExecutor(8) as pool:
        results = [f.result() for f in [pool.submit(c) for c in calls]]
    assert results[0] == 200000 and all(r for r in results)
