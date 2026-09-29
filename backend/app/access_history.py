"""Базовая история «вскрытия под охраной» по архиву Parquet (docs/architecture/access.md §4).

`python -m app.cli access backfill` — инциденты всего журнала тем же движком, что у прогонов (`app.domain.access`),
в `access_incidents` с `run_id = NULL`; `--check` — только сверка итогов с эталоном
`backend/tests/fixtures/access/journal_2025_2026h1.json`, без записи. Источник — архив, а не рабочая база: движение
хакатона — сырой поток, и его базовые строки старше 90 дней рабочая база не хранит (`runs.trim_raw_window`).
"""

import datetime as dt
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

from app.archive import CATALOG_DIR, connect, events_glob
from app.core.config import settings
from app.domain.access import (
    MSK,
    AccessRules,
    Event,
    Incident,
    Result,
    SubObject,
    evaluate,
    mark_repeated,
    motion_window,
)

FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "access" / "journal_2025_2026h1.json"
TEMP_DIR = settings.interim_dir / "duckdb-access"  # сброс DuckDB на диск — в data/interim, не в рабочую папку


def _catalog(con, archive_dir: Path, r: AccessRules) -> dict[int, SubObject]:
    cat = archive_dir / CATALOG_DIR
    con.execute(f"CREATE OR REPLACE TEMP VIEW channels AS SELECT * FROM '{cat / 'channels.parquet'}'")
    con.execute(f"CREATE OR REPLACE TEMP VIEW objects AS SELECT * FROM '{cat / 'objects.parquet'}'")
    guards = con.execute(
        "SELECT c.object_id, o.parent_id, o.name, c.id FROM channels c JOIN objects o ON o.id = c.object_id "
        "WHERE c.sensor_type = ? ORDER BY c.object_id",
        [r.guard_type],
    ).fetchall()
    openers: dict[int, dict[int, tuple[str, bool]]] = defaultdict(dict)
    for sub, channel, name, sensor in con.execute(
        "SELECT object_id, id, name, sensor_type FROM channels WHERE list_contains(?, sensor_type)",
        [list(r.opening_types)],
    ).fetchall():
        openers[sub][channel] = (name, sensor in r.hatch_types)
    return {sub: SubObject(sub, parent or sub, name, guard, dict(openers[sub])) for sub, parent, name, guard in guards}


def _ids(values) -> str:
    return ",".join(str(int(v)) for v in sorted(values)) or "NULL"


def _events(con, archive_dir: Path, subs: dict[int, SubObject], r: AccessRules) -> dict[int, list[Event]]:
    """Поток каждого подобъекта: охрана и её исправность, двери и люки. Каналы — постоянным списком: архив
    отсортирован по каналу, и чтение пропускает чужие группы строк. Одинаковые события канала в одну секунду (разные
    `ид_события`) — одно, как в живом потоке: шлюз отбрасывает миллисекунды, приём отсекает дубли."""
    sub_of = {s.guard_channel_id: s.id for s in subs.values()} | {c: s.id for s in subs.values() for c in s.openers}
    guards = {s.guard_channel_id for s in subs.values()}
    openers = {c for s in subs.values() for c in s.openers}
    rows = con.execute(
        f"""
        SELECT channel_id, ts, value_raw, bool_or(is_alarm)
        FROM read_parquet('{events_glob(archive_dir)}', hive_partitioning = true)
        WHERE (channel_id IN ({_ids(guards)}) AND value_raw IN (?, ?, ?, ?))
           OR (channel_id IN ({_ids(openers)}) AND value_raw IN (?, ?))
        GROUP BY 1, 2, 3
        """,
        [r.armed, r.disarmed, r.faulty, r.healthy, r.opened, r.closed],
    ).fetchall()
    out: dict[int, list[Event]] = defaultdict(list)
    for channel, ts, value, alarm in rows:
        out[sub_of[channel]].append(Event(ts, channel, value, bool(alarm)))
    return out


def _motion(con, archive_dir: Path, incidents: list[Incident], r: AccessRules) -> None:
    """Отметка «движение»: «Обнаружено движение» с датчиков движения подобъекта в окне инцидента — одним запросом."""
    if not incidents:
        return
    windows = [(k, i.sub_object_id, *motion_window(i, r)) for k, i in enumerate(incidents)]
    con.execute("CREATE OR REPLACE TEMP TABLE win (k INTEGER, sub INTEGER, lo TIMESTAMPTZ, hi TIMESTAMPTZ)")
    con.executemany("INSERT INTO win VALUES (?, ?, ?, ?)", windows)
    channels = [
        c
        for (c,) in con.execute(
            "SELECT id FROM channels WHERE list_contains(?, object_id) AND sensor_type = ?",
            [sorted({w[1] for w in windows}), r.motion_type],
        ).fetchall()
    ]
    hits = con.execute(
        f"""
        SELECT DISTINCT win.k
        FROM read_parquet('{events_glob(archive_dir)}', hive_partitioning = true) e
        JOIN channels c ON c.id = e.channel_id
        JOIN win ON win.sub = c.object_id AND e.ts BETWEEN win.lo AND win.hi
        WHERE e.channel_id IN ({_ids(channels)}) AND e.value_raw = ?
        """,
        [r.motion],
    ).fetchall()
    for (k,) in hits:
        incidents[k].motion = True


def _bursts(
    con, archive_dir: Path, subs: dict[int, SubObject], keys: set[tuple[int, int, dt.datetime]], r: AccessRules
) -> dict[tuple[int, dt.datetime], int]:
    """Каналы подобъекта (без газа и канала охраны) с событием в ±2 с от открытий `keys` = (подобъект, канал, T)."""
    guards = [s.guard_channel_id for s in subs.values()]
    channels = con.execute(
        "SELECT id, object_id FROM channels WHERE list_contains(?, object_id) AND sensor_type <> ? "
        "AND NOT list_contains(?, id)",
        [list(subs), r.gas_type, guards],
    ).fetchall()
    con.execute("CREATE OR REPLACE TEMP TABLE sc (channel_id INTEGER, sub INTEGER)")
    con.executemany("INSERT INTO sc VALUES (?, ?)", channels)
    con.execute("CREATE OR REPLACE TEMP TABLE op (sub INTEGER, channel_id INTEGER, ts TIMESTAMPTZ)")
    con.executemany("INSERT INTO op VALUES (?, ?, ?)", sorted(keys))
    width = int(r.burst_window.total_seconds())
    rows = con.execute(
        f"""
        WITH want AS (SELECT DISTINCT op.sub, op.ts + to_seconds(d.k) AS s
                      FROM op CROSS JOIN range(-{width}, {width} + 1) d(k)),
        near AS (SELECT DISTINCT sc.sub, e.ts AS s, e.channel_id
                 FROM read_parquet('{events_glob(archive_dir)}', hive_partitioning = true) e
                 JOIN sc USING (channel_id)
                 JOIN want ON want.sub = sc.sub AND want.s = e.ts
                 WHERE e.channel_id IN ({_ids(c for c, _ in channels)}))
        SELECT op.channel_id, op.ts, count(DISTINCT near.channel_id)
        FROM op CROSS JOIN range(-{width}, {width} + 1) d(k)
        JOIN near ON near.sub = op.sub AND near.s = op.ts + to_seconds(d.k)
        GROUP BY 1, 2
        """
    ).fetchall()
    return {(channel, ts): n for channel, ts, n in rows}


def _journal_until(con, archive_dir: Path, subs: dict[int, SubObject]) -> dt.datetime:
    """Конец журнала для правила — последнее событие охраны, дверей и люков."""
    ids = {s.guard_channel_id for s in subs.values()} | {c for s in subs.values() for c in s.openers}
    found = con.execute(
        f"SELECT max(ts) FROM read_parquet('{events_glob(archive_dir)}', hive_partitioning = true) "
        f"WHERE channel_id IN ({_ids(ids)})"
    ).fetchone()[0]
    return found or dt.datetime.now(dt.UTC)


def _sub_history(con, archive_dir: Path, sub: SubObject, until: dt.datetime, r: AccessRules):
    """Весь журнал одного подобъекта: события, число каналов волны у открытий под охраной и инциденты. Два прохода
    движка: первый собирает открытия, которым нужна проверка волны (от волн это не зависит), второй — с числом
    каналов. По подобъекту за раз — в памяти поток одного подобъекта, а не всей сети."""
    one = {sub.id: sub}
    events = _events(con, archive_dir, one, r).get(sub.id, [])
    keys: set[tuple[int, int, dt.datetime]] = set()

    def want(channel: int, ts: dt.datetime) -> int:
        keys.add((sub.id, channel, ts))
        return 1

    evaluate(sub, events, want, until, r)
    burst = _bursts(con, archive_dir, one, keys, r) if keys else {}
    result = evaluate(sub, events, lambda c, t: burst.get((c, t), 1), until, r)
    mark_repeated(result.incidents, (), r)
    return events, burst, result


def compute(archive_dir: Path, r: AccessRules, memory_limit: str = "1GB") -> tuple[dict[int, SubObject], dict]:
    """Инциденты каждого подобъекта по всему журналу: `{подобъект: Result}`."""
    con = connect(memory_limit, temp_dir=TEMP_DIR)
    subs = _catalog(con, archive_dir, r)
    until = _journal_until(con, archive_dir, subs)
    results = {sub.id: _sub_history(con, archive_dir, sub, until, r)[2] for sub in subs.values()}
    _motion(con, archive_dir, [i for res in results.values() for i in res.incidents], r)
    return subs, results


def summary(results: dict[int, Result], years: tuple[int, ...] = (2025, 2026)) -> dict:
    """Итоги по годам старта (по Москве) — в формате эталона `journal_2025_2026h1.json`."""
    out = {}
    for year in years:
        incidents: list[Incident] = [
            i for res in results.values() for i in res.incidents if i.start_ts.astimezone(MSK).year == year
        ]
        real = [i for i in incidents if i.status in ("open", "closed")]
        sizes = [len(i.openings) for i in real]
        skipped = Counter(
            reason for res in results.values() for reason, ts in res.skipped if ts.astimezone(MSK).year == year
        )
        out[str(year)] = {
            "incidents": len(real),
            "sub_objects": len({i.sub_object_id for i in real}),
            "dismissed": sum(i.status == "dismissed" for i in incidents),
            "off_hours": sum(i.off_hours for i in real),
            "hatch": sum(i.hatch_openings > 0 for i in real),
            "motion": sum(i.motion for i in real),
            "guard_faulty": sum(i.guard_faulty for i in real),
            "repeated": sum(i.repeated for i in real),
            "priority_high": sum(i.priority == "high" for i in real),
            "end_reason": dict(Counter(i.end_reason for i in real if i.end_reason)),
            "openings_median": float(statistics.median(sizes)) if sizes else None,
            "openings_max": max(sizes, default=0),
            "by_sub_object": {str(k): v for k, v in sorted(Counter(i.sub_object_id for i in real).items())},
            "skipped_openings": dict(skipped),
        }
    return out


def compare(got: dict, path: Path = FIXTURE) -> list[str]:
    """Расхождения итогов с эталоном; пусто — совпало."""
    want = json.loads(path.read_text(encoding="utf-8"))
    diffs = []
    for year, expected in want.items():
        for key, value in expected.items():
            actual = got.get(year, {}).get(key)
            if actual != value:
                diffs.append(f"{year} {key}: эталон {value}, у нас {actual}")
    return diffs


async def write(session, results: dict[int, Result]) -> dict:
    """Базовые инциденты (`run_id = NULL`) заменяются целиком: повторный запуск даёт то же. Коммит — на вызывающем."""
    from sqlalchemy import delete, insert

    from app.db.models import AccessIncident, AccessIncidentOpening

    await session.execute(delete(AccessIncident).where(AccessIncident.run_id.is_(None)))
    incidents = [i for res in results.values() for i in res.incidents]
    written, openings = 0, 0
    for chunk in range(0, len(incidents), 1000):
        part = incidents[chunk : chunk + 1000]
        rows = [
            {
                "run_id": None,
                "object_id": i.object_id,
                "sub_object_id": i.sub_object_id,
                "guard_channel_id": i.guard_channel_id,
                "status": i.status,
                "start_ts": i.start_ts,
                "qualified_ts": i.qualified_ts,
                "end_ts": i.end_ts,
                "end_reason": i.end_reason,
                "armed_since": i.armed_since,
                "disarmed_ts": i.disarmed_ts,
                "openings": len(i.openings),
                "hatch_openings": i.hatch_openings,
                "off_hours": i.off_hours,
                "motion": i.motion,
                "guard_faulty": i.guard_faulty,
                "repeated": i.repeated,
                "priority": i.priority,
            }
            for i in part
        ]
        ids = (await session.scalars(insert(AccessIncident).returning(AccessIncident.id), rows)).all()
        opening_rows = [
            {"incident_id": k, "channel_id": o.channel_id, "ts": o.ts, "is_alarm": o.is_alarm, "closed_ts": o.closed_ts}
            for k, i in zip(ids, part, strict=True)
            for o in i.openings
        ]
        if opening_rows:
            await session.execute(insert(AccessIncidentOpening), opening_rows)
        written += len(part)
        openings += len(opening_rows)
    return {"incidents": written, "openings": openings}


SCENARIOS = FIXTURE.with_name("scenarios.json")


def scenarios(archive_dir: Path, r: AccessRules, path: Path = SCENARIOS, memory_limit: str = "1GB") -> list[str]:
    """Сверка с `scenarios.json`: инциденты окна сценария так, как их посчитает прогон, — открытия с начала окна,
    события до его водяного знака, состояние охраны и «повторяется» — по базовой истории до начала окна."""
    want = json.loads(path.read_text(encoding="utf-8"))
    con = connect(memory_limit, temp_dir=TEMP_DIR)
    subs = _catalog(con, archive_dir, r)
    until = _journal_until(con, archive_dir, subs)
    history = {
        sid: _sub_history(con, archive_dir, subs[sid], until, r)
        for sid in sorted({i["sub_object_id"] for sc in want.values() for i in sc["incidents"]})
    }
    diffs = []
    for name, sc in want.items():
        start = dt.datetime.fromisoformat(sc["period"]["start"])
        window_until = dt.datetime.fromisoformat(sc["events_until"])
        got: list[Incident] = []
        for sid in sorted({i["sub_object_id"] for i in sc["incidents"]}):
            events, burst, past = history[sid]
            window = [e for e in events if e.ts <= window_until]
            res = evaluate(subs[sid], window, lambda c, t, b=burst: b.get((c, t), 1), window_until, r, since=start)
            prior = [i.start_ts for i in past.incidents if i.status in ("open", "closed") and i.start_ts < start]
            mark_repeated(res.incidents, prior, r)
            got += res.incidents
        _motion(con, archive_dir, got, r)
        rows = [_as_fixture(i, subs[i.sub_object_id]) for i in got]
        if rows != sc["incidents"]:
            diffs.append(f"{name}: эталон {sc['incidents']}\n  у нас {rows}")
    return diffs


def _as_fixture(i: Incident, sub: SubObject) -> dict:
    def iso(ts: dt.datetime | None) -> str | None:
        return ts.astimezone(MSK).isoformat() if ts else None

    return {
        "object_id": i.object_id,
        "sub_object_id": i.sub_object_id,
        "status": i.status,
        "start_ts": iso(i.start_ts),
        "qualified_ts": iso(i.qualified_ts),
        "end_ts": iso(i.end_ts),
        "end_reason": i.end_reason,
        "armed_since": iso(i.armed_since),
        "disarmed_ts": iso(i.disarmed_ts),
        "openings": len(i.openings),
        "hatch_openings": i.hatch_openings,
        "channels": [{"id": c, "name": sub.openers[c][0]} for c in i.channels()],
        "flags": {"off_hours": i.off_hours, "motion": i.motion, "guard_faulty": i.guard_faulty, "repeated": i.repeated},
        "priority": i.priority,
    }
