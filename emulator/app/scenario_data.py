"""Данные сценария до запуска (13.2, docs/api.md §3): сводка среза и сигналы с фильтрами и курсором.

Срез читается DuckDB в своё in-memory соединение; справочник — таблицы `ch` и `obj`. Сигналы — только каналы
справочника, как их передаст проигрыватель; `seq` — номер в порядке передачи (`SLICE_ORDER`), с 1. Срезы
последних сценариев держатся таблицами.

Сводка считается в отдельном процессе (`SummaryWorker`): ключевые моменты — Python на десятки тысяч строк, в процессе
эмулятора они держали GIL, и публикация OPC UA замирала на секунды, на стенде — на десятки. Готовая сводка — в памяти
и на диске рядом со срезом (`<срез>.summary.json`); ключ — файл среза, период, справочник и код сводки. На диск её
пишет подготовка срезов (`make emulator-slices`) и эмулятор, где каталог срезов доступен на запись; в контейнере срезы
только на чтение — там сводка встроенных сценариев берётся с диска, а загрузок и прочих считается в процессе сводки.

Ключевые моменты считает `app.key_moments.key_moments(con)` (lct-50): в `con` — временное представление `s`
(срез) и таблицы `ch`, `obj`. Модуль упал — моментов нет, сводка всё равно отдаётся.
"""

import contextlib
import csv
import hashlib
import inspect
import json
import logging
import math
import multiprocessing
import os
import tempfile
import threading
import time
from collections import OrderedDict
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from datetime import datetime, timedelta
from pathlib import Path

import duckdb

from app.catalog import Catalog
from app.key_moments import key_moments
from app.scenarios import MOSCOW, SLICE_ORDER, SUMMARY_SUFFIX, Scenario

log = logging.getLogger("emulator.scenario_data")

NUMERIC = "числовые показания"
OTHER = "прочие"
TOP_VALUES = 20
BUCKET_STEPS = (60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400)
MAX_BUCKETS = 200
DEFAULT_LIMIT, MAX_LIMIT = 200, 1000
CACHED_SLICES = 2
# код сводки: правка сводки или ключевых моментов делает сводки на диске устаревшими
SUMMARY_CODE = hashlib.md5(Path(__file__).read_bytes() + Path(inspect.getfile(key_moments)).read_bytes()).hexdigest()[
    :12
]


class DataError(Exception):
    """Ошибка запроса данных сценария: `code` — машинный код, `status` — HTTP-статус."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message

    def __reduce__(self):  # ошибка из процесса сводки переходит в процесс эмулятора
        return DataError, (self.status, self.code, self.message)


def catalog_signature(catalog: Catalog) -> str:
    """Отпечаток справочника для ключа сводки: имена, типы и объекты каналов, иерархия объектов."""
    h = hashlib.md5()
    for c in sorted(catalog.channels.values(), key=lambda c: c.id):
        h.update(repr((c.id, c.name, c.sensor_type, c.subsystem, c.tag, c.object_id)).encode())
    for o in sorted(catalog.objects.values(), key=lambda o: o.id):
        h.update(repr((o.id, o.level, o.parent_id, o.name)).encode())
    return h.hexdigest()[:12]


def summary_file(path: Path) -> Path:
    return path.with_name(path.stem + SUMMARY_SUFFIX)


def read_summary(path: Path, key: list) -> dict | None:
    """Сводка с диска, если ключ совпал; иначе — нет."""
    try:
        cached = json.loads(summary_file(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return cached.get("summary") if cached.get("key") == key else None


def write_summary(path: Path, key: list, summary: dict) -> None:
    """Сводку — на диск рядом со срезом; каталог только на чтение (контейнер) — молча пропускаем."""
    target = summary_file(path)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps({"key": key, "summary": summary}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, target)
    except OSError as e:
        log.debug("сводка %s не записана: %s", target, e)
        # каталог, смонтированный только на чтение (uploaded/ в контейнере), и на удаление отвечает EROFS, а не «нет
        # файла»: сводка всё равно отдаётся, кэш остаётся в памяти процесса
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)


def _iso(ts: datetime | None) -> str | None:
    return None if ts is None else ts.replace(tzinfo=MOSCOW).isoformat()


def _moscow(value: str | None, name: str) -> datetime | None:
    """Время фильтра: ISO с поясом или без (тогда Москва) → Москва без пояса, как в срезе."""
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(value)
    except ValueError as e:
        raise DataError(422, "validation_error", f"{name}: ожидается время ISO 8601, получено {value!r}") from e
    return ts.astimezone(MOSCOW).replace(tzinfo=None) if ts.tzinfo else ts


def flat(values: list[str] | None) -> list[str]:
    """Список из повторов параметра и значений через запятую — оба вида (`a=1&a=2`, `a=1,2`)."""
    return [v.strip() for value in values or [] for v in value.split(",") if v.strip()]


def _ints(values: list[str] | None, name: str) -> list[int]:
    try:
        return [int(v) for v in flat(values)]
    except ValueError as e:
        raise DataError(422, "validation_error", f"{name}: ожидаются целые числа") from e


def bucket_seconds(duration: float) -> int:
    """Ширина корзины плотности: кратна минуте, не больше MAX_BUCKETS корзин на период."""
    return next((b for b in BUCKET_STEPS if duration / b <= MAX_BUCKETS), math.ceil(duration / MAX_BUCKETS / 60) * 60)


class ScenarioData:
    def __init__(self, catalog: Catalog, worker: "SummaryWorker | None" = None, disk: bool = True) -> None:
        """`worker` — процесс сводки; без него (тесты, подготовка срезов) сводка считается здесь же. `disk` — сводки
        на диске рядом со срезом."""
        self.catalog = catalog
        self.worker = worker
        self.disk = disk
        self.con: duckdb.DuckDBPyConnection | None = None
        self._lock = threading.Lock()
        self._slices: OrderedDict[str, tuple[tuple, str]] = OrderedDict()  # name → (ключ файла, таблица)
        self._summaries: dict[str, tuple[list, dict]] = {}
        self._signature: str | None = None

    # --- справочник и срезы -------------------------------------------------------------------------------------

    def _connect(self) -> duckdb.DuckDBPyConnection:
        """Соединение со справочником — при первом запросе; эмулятору память нужнее для узлов OPC UA."""
        if self.con is not None:
            return self.con
        con = duckdb.connect(config={"threads": 2, "memory_limit": "384MB"})
        objects = self.catalog.objects

        def level2(object_id: int) -> int | None:
            o = objects.get(object_id)
            while o is not None and o.level > 2:
                o = objects.get(o.parent_id)
            return o.id if o is not None and o.level == 2 else None

        channels = [
            (c.id, c.name, c.sensor_type, c.subsystem, c.tag, c.object_id, level2(c.object_id))
            for c in self.catalog.channels.values()
        ]
        tables = {
            # owner_id — объект канала в справочнике, object_id — объект уровня 2 над ним
            "ch_raw": (
                {
                    "channel_id": "INTEGER",
                    "name": "VARCHAR",
                    "sensor_type": "VARCHAR",
                    "subsystem": "VARCHAR",
                    "tag": "VARCHAR",
                    "owner_id": "INTEGER",
                    "object_id": "INTEGER",
                },
                channels,
            ),
            "obj": (
                {"id": "INTEGER", "level": "INTEGER", "parent_id": "INTEGER", "name": "VARCHAR"},
                [(o.id, o.level, o.parent_id, o.name) for o in objects.values()],
            ),
        }
        # executemany на 11 тыс. строк — 5 с, через CSV — 0.04 с
        with tempfile.TemporaryDirectory() as tmp:
            for name, (columns, rows) in tables.items():
                path = Path(tmp) / f"{name}.csv"
                with path.open("w", encoding="utf-8", newline="") as f:
                    csv.writer(f).writerows(rows)
                spec = ", ".join(f"'{c}': '{t}'" for c, t in columns.items())
                con.execute(
                    f"CREATE TABLE {name} AS SELECT * FROM read_csv($path, header = false, columns = {{{spec}}})",
                    {"path": str(path)},
                )
        # sub_object_id — подобъект (уровень 3); у всех каналов справочника объект — уровня 3
        con.execute(
            "CREATE TABLE ch AS SELECT r.channel_id, r.name, r.sensor_type, r.subsystem, r.tag, "
            "CASE WHEN o.level >= 3 THEN r.owner_id END AS sub_object_id, r.object_id, r.owner_id "
            "FROM ch_raw r LEFT JOIN obj o ON o.id = r.owner_id"
        )
        con.execute("DROP TABLE ch_raw")
        self.con = con
        return con

    def _table(self, scenario: Scenario, path: Path) -> tuple[tuple, str]:
        """Таблица среза с `seq`; пересоздаётся, если файл среза изменился."""
        if not path.is_file():
            raise DataError(409, "slice_not_ready", f"Срез сценария {scenario.name!r} не подготовлен")
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
        with self._lock:
            con = self._connect()
            cached = self._slices.get(scenario.name)
            if cached and cached[0] == key:
                self._slices.move_to_end(scenario.name)
                return cached
            table = "slice_" + hashlib.md5(scenario.name.encode()).hexdigest()[:12]
            con.execute(
                f"CREATE OR REPLACE TABLE {table} AS "
                f"SELECT row_number() OVER (ORDER BY {SLICE_ORDER}) AS seq, event_id, channel_id, ts, is_alarm, "
                "value, TRY_CAST(value AS DOUBLE) IS NOT NULL AS numeric "
                "FROM read_parquet($path, file_row_number = true) "
                "WHERE channel_id IN (SELECT channel_id FROM ch)",
                {"path": str(path)},
            )
            self._slices[scenario.name] = (key, table)
            self._slices.move_to_end(scenario.name)
            while len(self._slices) > CACHED_SLICES:
                _, (_, old) = self._slices.popitem(last=False)
                con.execute(f"DROP TABLE IF EXISTS {old}")
            return key, table

    # --- сводка ---------------------------------------------------------------------------------------------------

    def summary_key(self, scenario: Scenario, path: Path) -> list:
        """Файл среза (размер и время изменения; в смонтированном каталоге контейнера — те же, до наносекунды),
        период сценария, справочник, код сводки."""
        if not path.is_file():
            raise DataError(409, "slice_not_ready", f"Срез сценария {scenario.name!r} не подготовлен")
        if self._signature is None:
            self._signature = catalog_signature(self.catalog)
        st = path.stat()
        return [st.st_size, st.st_mtime_ns, _iso(scenario.start), _iso(scenario.end), self._signature, SUMMARY_CODE]

    def summary(self, scenario: Scenario, path: Path) -> dict:
        """Сводка: из памяти, с диска или посчитанная — в процессе сводки, если он есть."""
        key = self.summary_key(scenario, path)
        cached = self._summaries.get(scenario.name)
        if cached and cached[0] == key:
            return cached[1]
        result = read_summary(path, key) if self.disk else None
        if result is None:
            result = self.worker.summary(scenario, path) if self.worker else self.compute_summary(scenario, path)
            if self.disk:
                write_summary(path, key, result)
        self._summaries[scenario.name] = (key, result)
        return result

    def compute_summary(self, scenario: Scenario, path: Path) -> dict:
        _, table = self._table(scenario, path)
        cur = self.con.cursor()
        try:
            return self._summary(cur, table, scenario, path)
        finally:
            cur.close()

    def _summary(self, cur: duckdb.DuckDBPyConnection, table: str, scenario: Scenario, path: Path) -> dict:
        names = {o.id: o.name for o in self.catalog.objects.values()}
        events, alarms, channels = cur.execute(
            f"SELECT count(*), count(*) FILTER (is_alarm), count(DISTINCT channel_id) FROM {table}"
        ).fetchone()
        unknown_channels, unknown_events = cur.execute(
            "SELECT count(DISTINCT channel_id), count(*) FROM read_parquet($path) "
            "WHERE channel_id NOT IN (SELECT channel_id FROM ch)",
            {"path": str(path)},
        ).fetchone()

        objects: dict[int | None, dict] = {}
        for object_id, sub_id, n_channels, n_events in cur.execute(
            f"SELECT ch.object_id, ch.sub_object_id, count(DISTINCT s.channel_id), count(*) "
            f"FROM {table} s JOIN ch USING (channel_id) GROUP BY ALL"
        ).fetchall():
            node = objects.setdefault(
                object_id,
                {"id": object_id, "name": names.get(object_id), "channels": 0, "events": 0, "sub_objects": []},
            )
            node["channels"] += n_channels
            node["events"] += n_events
            node["sub_objects"].append(
                {"id": sub_id, "name": names.get(sub_id), "channels": n_channels, "events": n_events}
            )
        for node in objects.values():
            node["sub_objects"].sort(key=lambda o: (-o["events"], o["name"] or ""))

        def grouped(column: str) -> list[dict]:
            rows = cur.execute(
                f"SELECT ch.{column}, count(DISTINCT s.channel_id), count(*) FROM {table} s JOIN ch USING (channel_id) "
                "GROUP BY ALL ORDER BY 3 DESC, 1"
            ).fetchall()
            return [{column: v, "channels": c, "events": e} for v, c, e in rows]

        return {
            "scenario_id": scenario.name,
            "period_start": _iso(scenario.start),
            "period_end": _iso(scenario.end),
            "events": events,
            "alarm_events": alarms,
            "channels": channels,
            "unknown_channels": unknown_channels,
            "unknown_events": unknown_events,
            "objects": sorted(objects.values(), key=lambda o: (-o["events"], o["name"] or "")),
            "by_type": grouped("sensor_type"),
            "by_subsystem": grouped("subsystem"),
            "by_value": self._by_value(cur, table),
            "density": self._density(cur, table, scenario, events),
            "moments": self._moments(cur, table, names),
        }

    @staticmethod
    def _by_value(cur: duckdb.DuckDBPyConnection, table: str) -> list[dict]:
        """Счётчики по значениям: показания-числа (газ, температура) — одной строкой, хвост после TOP_VALUES —
        строкой «прочие»; по убыванию числа событий."""
        rows = cur.execute(
            f"SELECT CASE WHEN numeric THEN '{NUMERIC}' ELSE value END, numeric, count(*), count(*) FILTER (is_alarm) "
            f"FROM {table} GROUP BY ALL ORDER BY 3 DESC, 1"
        ).fetchall()
        items = [{"value": v, "events": e, "alarm_events": a, "numeric": n} for v, n, e, a in rows]
        numeric = [i for i in items if i["numeric"]]
        text = [i for i in items if not i["numeric"]]
        head, tail = text[:TOP_VALUES], text[TOP_VALUES:]
        if tail:
            head.append(
                {
                    "value": OTHER,
                    "events": sum(i["events"] for i in tail),
                    "alarm_events": sum(i["alarm_events"] for i in tail),
                    "numeric": False,
                    "values": len(tail),
                }
            )
        return sorted(head + numeric, key=lambda i: (i["value"] == OTHER, -i["events"]))

    @staticmethod
    def _density(cur: duckdb.DuckDBPyConnection, table: str, scenario: Scenario, events: int) -> dict:
        duration = (scenario.end - scenario.start).total_seconds()
        bucket = bucket_seconds(duration)
        n = max(1, math.ceil(duration / bucket))
        counts = {
            k: (e, a)
            for k, e, a in cur.execute(
                f"SELECT least(greatest(floor(epoch(ts - $start::TIMESTAMP) / $bucket), 0), $last)::BIGINT AS k, "
                f"count(*), count(*) FILTER (is_alarm) FROM {table} GROUP BY k",
                {"start": scenario.start, "bucket": bucket, "last": n - 1},
            ).fetchall()
        }
        peak = cur.execute(f"SELECT max(n) FROM (SELECT count(*) AS n FROM {table} GROUP BY date_trunc('minute', ts))")
        return {
            "bucket_seconds": bucket,
            "items": [
                {
                    "ts": _iso(scenario.start + timedelta(seconds=k * bucket)),
                    "events": counts.get(k, (0, 0))[0],
                    "alarm_events": counts.get(k, (0, 0))[1],
                }
                for k in range(n)
            ],
            "peak_minute_events": peak.fetchone()[0] or 0,
            "avg_per_second": round(events / duration, 3) if duration else 0.0,
        }

    def _moments(self, cur: duckdb.DuckDBPyConnection, table: str, names: dict[int, str]) -> list[dict]:
        # временное представление видно только этому курсору — параллельные запросы других сценариев не мешают
        cur.execute(f"CREATE OR REPLACE TEMP VIEW s AS SELECT event_id, channel_id, ts, is_alarm, value FROM {table}")
        try:
            raw = key_moments(cur)
        except Exception:  # noqa: BLE001 — сводка нужна и без моментов
            log.exception("ключевые моменты не посчитаны")
            return []
        moments = []
        for m in raw or []:
            channel_ids = [int(c) for c in m.get("channel_ids") or []]
            object_id, sub_id = m.get("object_id"), m.get("sub_object_id")
            moments.append(
                {
                    "ts": _iso(m.get("at")),
                    "until": _iso(m.get("until")),
                    "kind": m.get("kind"),
                    "state": m.get("state"),
                    "title": m.get("title"),
                    "detail": m.get("text"),
                    "object_id": object_id,
                    "object_name": names.get(object_id),
                    "sub_object_id": sub_id,
                    "sub_object_name": names.get(sub_id),
                    "channel_id": channel_ids[0] if len(channel_ids) == 1 else None,
                    "channel_ids": channel_ids,
                    "events": m.get("count"),
                }
            )
        return sorted(moments, key=lambda m: m["ts"] or "")

    # --- сигналы --------------------------------------------------------------------------------------------------

    def events(
        self,
        scenario: Scenario,
        path: Path,
        *,
        cursor: str | None = None,
        limit: int = DEFAULT_LIMIT,
        from_: str | None = None,
        to: str | None = None,
        object_id: list[str] | None = None,
        channel_id: list[str] | None = None,
        sensor_type: list[str] | None = None,
        value: list[str] | None = None,
        alarm: bool | None = None,
        q: str | None = None,
    ) -> dict:
        """Сигналы в порядке передачи. `object_id` — любой уровень, вложенные включаются; `value` «числовые
        показания» — все числовые значения; `from` включается, `to` — нет; `q` — подстрока названия канала."""
        _, table = self._table(scenario, path)
        where, params = [], {}
        if (start := _moscow(from_, "from")) is not None:
            where.append("s.ts >= $from")
            params["from"] = start
        if (end := _moscow(to, "to")) is not None:
            where.append("s.ts < $to")
            params["to"] = end
        if objects := _ints(object_id, "object_id"):
            where.append("list_contains($owners, ch.owner_id)")
            params["owners"] = sorted(self.catalog.descendants(objects))
        if channels := _ints(channel_id, "channel_id"):
            where.append("list_contains($channels, s.channel_id)")
            params["channels"] = channels
        if types := flat(sensor_type):
            where.append("list_contains($types, ch.sensor_type)")
            params["types"] = types
        if values := flat(value):
            texts = [v for v in values if v != NUMERIC]
            clause = ["list_contains($values, s.value)"] if texts else []
            if NUMERIC in values:
                clause.append("s.numeric")
            where.append("(" + " OR ".join(clause) + ")")
            if texts:
                params["values"] = texts
        if alarm is not None:
            where.append("s.is_alarm = $alarm")
            params["alarm"] = alarm
        if q and q.strip():
            where.append("contains(lower(ch.name), $q)")
            params["q"] = q.strip().lower()
        base = f"FROM {table} s JOIN ch USING (channel_id)" + (" WHERE " + " AND ".join(where) if where else "")

        after = 0
        if cursor:
            try:
                after = int(cursor)
            except ValueError as e:
                raise DataError(422, "bad_cursor", "Курсор не распознан — начните с первой страницы") from e
        limit = min(max(1, int(limit)), MAX_LIMIT)
        names = {o.id: o.name for o in self.catalog.objects.values()}
        cur = self.con.cursor()
        try:
            total = cur.execute(f"SELECT count(*) {base}", params).fetchone()[0]
            page_where = " AND " if where else " WHERE "
            rows = cur.execute(
                f"SELECT s.seq, s.ts, s.event_id, s.channel_id, ch.name, ch.sensor_type, ch.object_id, "
                f"ch.sub_object_id, s.value, s.is_alarm {base}{page_where}s.seq > $after ORDER BY s.seq LIMIT $limit",
                {**params, "after": after, "limit": limit + 1},
            ).fetchall()
        finally:
            cur.close()
        items = [
            {
                "seq": seq,
                "ts": _iso(ts),
                "event_id": event_id,
                "channel_id": ch_id,
                "channel_name": name,
                "sensor_type": sensor,
                "object_id": obj,
                "object_name": names.get(obj),
                "sub_object_id": sub,
                "sub_object_name": names.get(sub),
                "value": val,
                "is_alarm": is_alarm,
            }
            for seq, ts, event_id, ch_id, name, sensor, obj, sub, val, is_alarm in rows[:limit]
        ]
        more = len(rows) > limit
        return {"items": items, "next_cursor": str(items[-1]["seq"]) if more else None, "total": total}


# --- процесс сводки -------------------------------------------------------------------------------------------------

_worker_data: ScenarioData | None = None


def _init_worker(catalog: Catalog, parent: int) -> None:
    global _worker_data
    _worker_data = ScenarioData(catalog, disk=False)
    threading.Thread(target=_watch_parent, args=(parent,), daemon=True).start()


def _watch_parent(parent: int) -> None:
    """Процесс эмулятора завершился посреди сводки — процесс сводки не остаётся сиротой."""
    while os.getppid() == parent:
        time.sleep(1)
    os._exit(0)


def _worker_summary(scenario: Scenario, path: Path) -> dict:
    return _worker_data.compute_summary(scenario, path)


class SummaryWorker:
    """Процесс сводки: свой GIL и своё соединение DuckDB — публикация OPC UA в процессе эмулятора не ждёт ни
    ключевых моментов, ни чтения среза. Живёт одну сводку: готовые сводки — в памяти и на диске, а простаивающий
    процесс держал бы ~170 МБ (память Docker на стендах общая)."""

    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog
        self._pool: ProcessPoolExecutor | None = None
        self._lock = threading.Lock()

    def _executor(self) -> ProcessPoolExecutor:
        with self._lock:
            if self._pool is None:
                # spawn, а не fork: в процессе эмулятора работают цикл asyncio и потоки
                self._pool = ProcessPoolExecutor(
                    max_workers=1,
                    mp_context=multiprocessing.get_context("spawn"),
                    initializer=_init_worker,
                    initargs=(self.catalog, os.getpid()),
                    max_tasks_per_child=1,
                )
            return self._pool

    def summary(self, scenario: Scenario, path: Path) -> dict:
        pool = self._executor()
        try:
            return pool.submit(_worker_summary, scenario, path).result()
        except BrokenProcessPool as e:
            # процесс сводки упал (например, по памяти) — следующий запрос поднимет новый
            with self._lock:
                if self._pool is pool:
                    self._pool = None
            raise DataError(503, "summary_failed", "Сводка не посчитана: процесс сводки упал, повторите запрос") from e

    def close(self) -> None:
        with self._lock:
            if self._pool is not None:
                self._pool.shutdown(wait=False, cancel_futures=True)
                self._pool = None
