"""Загрузка рабочей базы из архива Parquet (docs/architecture/data.md §2–3). Всё загруженное — базовая история
(`run_id IS NULL`).

Порядок: справочники → значимые события (газ через зону нечувствительности) → окно сырого потока → итоги из
полного потока → эпизоды неисправностей → текущее состояние → сжатие старых чанков. DuckDB готовит CSV,
PostgreSQL принимает их через `COPY` в несколько соединений параллельно.

Частичная загрузка: `since` — события и итоги с этой даты, `object_ids` — только каналы этих объектов (любого
уровня). Перед загрузкой базовые строки в этих границах удаляются — повторный запуск не создаёт дублей.
Эпизоды и текущее состояние всегда считаются по всему архиву, чтобы не терять открытые до `since` эпизоды.
"""

import datetime as dt
import json
import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import psycopg

from app.archive import _q, connect, events_glob, load_catalog
from app.domain.alarm_episodes import rules, sql_kind
from app.domain.deadband import GAS_DEADBAND, Deadband, hackathon_route
from app.domain.values import FAULT_STATES, GAS_SENSOR, MOTION_SENSOR

log = logging.getLogger("loader")

RAW_WINDOW_DAYS = 90
RAW_SENSORS = (GAS_SENSOR, MOTION_SENSOR)
EVENT_COLUMNS = "run_id, channel_id, event_id, ts, is_alarm, value_raw, value_num"
AGG_COLUMNS = "n_events, n_alarm, n_fault, n_num, min, max, mean"
BASE_TABLES = ("events", "events_raw", "channel_hourly", "channel_daily", "fault_episodes", "channel_state")
_FAULTS = "(" + ", ".join(f"'{s}'" for s in sorted(FAULT_STATES)) + ")"
_MSK = "Europe/Moscow"


def _sql_list(values) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def pg_conninfo(database_url: str) -> str:
    """URL SQLAlchemy → строка подключения psycopg."""
    return database_url.replace("postgresql+psycopg://", "postgresql://", 1)


@dataclass
class Scope:
    since: dt.date | None = None  # по московскому времени
    object_ids: list[int] | None = None

    @property
    def full(self) -> bool:
        return self.since is None and not self.object_ids


@dataclass
class LoadReport:
    rows: dict[str, int] = field(default_factory=dict)
    seconds: dict[str, float] = field(default_factory=dict)
    sizes: dict[str, dict] = field(default_factory=dict)
    info: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"rows": self.rows, "seconds": self.seconds, "sizes": self.sizes, "info": self.info}


class Loader:
    def __init__(
        self,
        archive_dir: Path,
        database_url: str,
        work_dir: Path,
        scope: Scope | None = None,
        workers: int = 4,
        memory_limit: str = "8GB",
        threads: int | None = None,
        compress_before: dt.date | None = dt.date(2026, 1, 1),
        raw_window_days: int | None = RAW_WINDOW_DAYS,
        run_id: int | None = None,
    ) -> None:
        self.archive_dir = archive_dir
        self.conninfo = pg_conninfo(database_url)
        self.work_dir = work_dir
        self.scope = scope or Scope()
        self.workers = workers
        self.compress_before = compress_before
        self.raw_window_days = raw_window_days
        self.run_id = run_id
        self.report = LoadReport()
        self.con = connect(memory_limit, threads, work_dir / "duckdb")
        self._pool = ThreadPoolExecutor(max_workers=workers)
        self._copies: list[Future] = []
        self._lock = threading.Lock()

    # --- общий ход -------------------------------------------------------------------------------------------

    def run(self) -> dict:
        started = time.monotonic()
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._step("prepare", self._prepare)
        with psycopg.connect(self.conninfo, autocommit=True) as pg:
            self._step("catalog", lambda: self._load_catalog(pg))
            self._step("clear", lambda: self._clear(pg))
        self._step("events", self._events)
        self._step("events_gas", self._events_gas)
        self._step("events_raw", self._events_raw)
        self._step("channel_daily", self._channel_daily)
        self._step("channel_hourly", self._channel_hourly)
        self._step("fault_episodes", self._fault_episodes)
        self._step("copy_wait", self._wait_copies)
        with psycopg.connect(self.conninfo, autocommit=True) as pg:
            self._step("alarms", lambda: self._alarms(pg))
            self._step("channel_state", lambda: self._channel_state(pg))
            self.report.sizes["before_compression"] = self._sizes(pg)
            if self.compress_before:
                self._step("compress", lambda: self._compress(pg))
            self._step("analyze", lambda: pg.execute("ANALYZE"))
            self.report.sizes["after_compression"] = self._sizes(pg)
        self._pool.shutdown()
        self.report.seconds["total"] = round(time.monotonic() - started, 1)
        return self.report.as_dict()

    def _step(self, name: str, fn) -> None:
        started = time.monotonic()
        log.info("%s…", name)
        fn()
        self.report.seconds[name] = round(time.monotonic() - started, 1)
        log.info("%s: %s с", name, self.report.seconds[name])

    # --- подготовка ------------------------------------------------------------------------------------------

    def _prepare(self) -> None:
        con = self.con
        load_catalog(con, self.archive_dir)
        if self.scope.object_ids:
            ids = ", ".join(str(int(i)) for i in self.scope.object_ids)
            con.execute(
                f"""
                CREATE OR REPLACE TABLE scope_objects AS
                WITH RECURSIVE sub(id) AS (
                    SELECT id FROM objects WHERE id IN ({ids})
                    UNION SELECT o.id FROM objects o JOIN sub ON o.parent_id = sub.id
                ) SELECT id FROM sub
                """
            )
            con.execute(
                "CREATE OR REPLACE TABLE scope_channels AS SELECT * FROM channels "
                "WHERE object_id IN (SELECT id FROM scope_objects)"
            )
        else:
            con.execute("CREATE OR REPLACE TABLE scope_channels AS SELECT * FROM channels")
        # маршрут приёма — как в справочнике БД (D31, deadband.hackathon_route): газ — raw с зоной, движение — raw
        con.execute(
            f"""
            CREATE OR REPLACE TABLE scope_channels AS SELECT *,
                CASE WHEN sensor_type IN {_sql_list(RAW_SENSORS)} THEN 'raw' ELSE 'significant' END AS stream,
                CASE WHEN sensor_type = '{GAS_SENSOR}' THEN CAST({GAS_DEADBAND} AS DOUBLE) END AS deadband
            FROM scope_channels
            """
        )
        glob = events_glob(self.archive_dir)
        # Все события каналов в границах — для эпизодов и состояния; ev — ещё и с даты since
        con.execute(
            f"""
            CREATE OR REPLACE VIEW ev_all AS
            SELECT e.event_id, e.channel_id, e.ts, e.is_alarm, e.value_raw, e.value_num, e.year,
                CAST(e.month AS INTEGER) AS month, c.sensor_type, c.stream, c.deadband
            FROM read_parquet({_q(Path(glob))}, hive_partitioning = true) e
            JOIN scope_channels c ON c.id = e.channel_id
            """
        )
        since_filter = ""
        if self.scope.since:
            since_filter = (
                f"WHERE year >= {self.scope.since.year} "
                f"AND ts >= timezone('{_MSK}', TIMESTAMP '{self.scope.since.isoformat()} 00:00:00')"
            )
        con.execute(f"CREATE OR REPLACE VIEW ev AS SELECT * FROM ev_all {since_filter}")
        # Эпизоды тревог — на объекте уровня 2 целиком, даже если в границы попала часть его каналов
        con.execute(
            "CREATE OR REPLACE TABLE alarm_objects AS "
            "SELECT DISTINCT o.parent_id AS id FROM scope_channels c JOIN objects o ON o.id = c.object_id"
        )

        self.data_end = con.execute(f"SELECT max(ts) FROM read_parquet({_q(Path(glob))})").fetchone()[0]
        self.raw_start = self.data_end - dt.timedelta(days=self.raw_window_days)
        if self.scope.since:
            since_ts = dt.datetime.combine(self.scope.since, dt.time(), dt.timezone(dt.timedelta(hours=3)))
            self.raw_start = max(self.raw_start, since_ts)
        n_channels = con.execute("SELECT count(*) FROM scope_channels").fetchone()[0]
        self.report.info.update(
            data_end=self.data_end.isoformat(),
            raw_start=self.raw_start.isoformat(),
            channels=n_channels,
            since=self.scope.since.isoformat() if self.scope.since else None,
            object_ids=self.scope.object_ids,
        )

    def _load_catalog(self, pg: psycopg.Connection) -> None:
        objects = self.con.execute("SELECT id, parent_id, level, kind, name FROM objects ORDER BY level, id").fetchall()
        channels = self.con.execute(
            "SELECT id, object_id, system_type, sensor_type, tag, name, picket_number, picket_offset, tag_group "
            "FROM channels ORDER BY id"
        ).fetchall()
        with pg.transaction(), pg.cursor() as cur:
            cur.executemany(
                "INSERT INTO objects (id, parent_id, level, kind, name) VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET parent_id = EXCLUDED.parent_id, level = EXCLUDED.level, "
                "kind = EXCLUDED.kind, name = EXCLUDED.name",
                objects,
            )
            cur.executemany(
                "INSERT INTO channels (id, object_id, system_type, sensor_type, tag, name, picket_number, "
                "picket_offset, tag_group, stream, deadband) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET object_id = EXCLUDED.object_id, system_type = EXCLUDED.system_type, "
                "sensor_type = EXCLUDED.sensor_type, tag = EXCLUDED.tag, name = EXCLUDED.name, "
                "picket_number = EXCLUDED.picket_number, picket_offset = EXCLUDED.picket_offset, "
                "tag_group = EXCLUDED.tag_group, stream = EXCLUDED.stream, deadband = EXCLUDED.deadband",
                [(*c, *hackathon_route(c[3])) for c in channels],
            )
        self.report.rows.update(objects=len(objects), channels=len(channels))

    def _clear(self, pg: psycopg.Connection) -> None:
        """Удалить базовые строки в границах загрузки. Без границ и без прогонов — TRUNCATE."""
        has_runs = pg.execute("SELECT EXISTS (SELECT 1 FROM runs)").fetchone()[0]
        if self.scope.full and not has_runs:
            pg.execute(f"TRUNCATE {', '.join(BASE_TABLES)}")
            return
        for table in ("events", "events_raw", "channel_hourly"):
            self._decompress(pg, table)
        channels = [r[0] for r in self.con.execute("SELECT id FROM scope_channels").fetchall()]
        since = self._since_ts()
        conds = {
            "events": "ts >= %(since)s",
            "events_raw": "ts >= %(since)s",
            "channel_hourly": "hour >= %(since)s",
            "channel_daily": "date >= %(since_date)s",
            "fault_episodes": "(end_ts IS NULL OR end_ts >= %(since)s)",  # как в выгрузке эпизодов
            "channel_state": "true",
        }
        params = {"since": since, "since_date": self.scope.since or dt.date.min, "channels": channels}
        for table, cond in conds.items():
            pg.execute(
                f"DELETE FROM {table} WHERE run_id IS NULL AND {cond} AND channel_id = ANY(%(channels)s)", params
            )

    def _clear_alarms(self, pg: psycopg.Connection) -> None:
        """Базовые тревоги и эпизоды объектов в границах: всегда DELETE — на тревоги ссылаются прогнозы.
        Ссылки базовых прогнозов на удаляемые тревоги обнуляются: после перезагрузки базы их загружают заново."""
        objects = [r[0] for r in self.con.execute("SELECT id FROM alarm_objects").fetchall()]
        with pg.transaction():
            base = "SELECT id FROM alarms WHERE run_id IS NULL AND object_id = ANY(%(o)s)"
            pg.execute(f"UPDATE predictions SET alarm_id = NULL WHERE alarm_id IN ({base})", {"o": objects})
            pg.execute("DELETE FROM alarms WHERE run_id IS NULL AND object_id = ANY(%(o)s)", {"o": objects})
            pg.execute("DELETE FROM alarm_episodes WHERE run_id IS NULL AND object_id = ANY(%(o)s)", {"o": objects})

    def _since_ts(self) -> dt.datetime:
        if not self.scope.since:
            return dt.datetime(1970, 1, 1, tzinfo=dt.UTC)
        return dt.datetime.combine(self.scope.since, dt.time(), dt.timezone(dt.timedelta(hours=3)))

    def _decompress(self, pg: psycopg.Connection, table: str) -> None:
        pg.execute(
            "SELECT decompress_chunk(c, if_compressed => true) FROM show_chunks(%s::regclass, newer_than => %s) c",
            (table, self._since_ts() - dt.timedelta(days=31)),
        )

    # --- выгрузка из архива и COPY ----------------------------------------------------------------------------

    def _export(self, name: str, query: str, table: str, columns: str) -> None:
        """DuckDB → CSV → COPY в фоне. Файл удаляется после загрузки."""
        path = self.work_dir / f"{name}.csv"
        self.con.execute(f"COPY ({query}) TO {_q(path)} (FORMAT csv, HEADER false)")
        self._copies.append(self._pool.submit(self._copy_file, path, table, columns))

    def _copy_file(self, path: Path, table: str, columns: str) -> int:
        with psycopg.connect(self.conninfo) as pg, pg.cursor() as cur:
            with cur.copy(f"COPY {table} ({columns}) FROM STDIN (FORMAT csv)") as copy, open(path, "rb") as f:
                while block := f.read(1 << 20):
                    copy.write(block)
            n = cur.rowcount
            pg.commit()
        path.unlink()
        with self._lock:
            self.report.rows[table] = self.report.rows.get(table, 0) + n
        return n

    def _wait_copies(self) -> None:
        for f in self._copies:
            f.result()
        self._copies.clear()

    def _years(self) -> list[int]:
        return [r[0] for r in self.con.execute("SELECT DISTINCT year FROM ev ORDER BY 1").fetchall()]

    @property
    def _run(self) -> str:
        """run_id строк: NULL — базовая история хакатона, иначе прогон-основа внешнего набора (D28)."""
        return "NULL" if self.run_id is None else str(int(self.run_id))

    def _events(self) -> None:
        """Значимые изменения — каналы `stream = significant`; сырой поток (газ, движение) — отдельно."""
        for year in self._years():
            self._export(
                f"events-{year}",
                f"""
                SELECT {self._run}, channel_id, event_id, ts, is_alarm, value_raw, value_num FROM ev
                WHERE year = {year} AND stream = 'significant'
                """,
                "events",
                EVENT_COLUMNS,
            )

    def _events_gas(self) -> None:
        """Сырой поток с зоной нечувствительности (у хакатона — газ) в `events`: по месяцам, в порядке канал + время;
        опорные значения переходят между месяцами. Количество до отсечения — в отчёте."""
        con = self.con
        total = 0
        for (threshold,) in con.execute(
            "SELECT DISTINCT deadband FROM scope_channels WHERE stream = 'raw' AND deadband IS NOT NULL ORDER BY 1"
        ).fetchall():
            total += self._events_deadband(float(threshold))
        con.execute("DROP TABLE IF EXISTS gas; DROP TABLE IF EXISTS kept")
        self.report.info["gas_readings_before_deadband"] = total

    def _events_deadband(self, threshold: float) -> int:
        deadband = Deadband(threshold)
        con = self.con
        where = f"stream = 'raw' AND deadband = {float(threshold)}"
        months = con.execute(f"SELECT DISTINCT year, month FROM ev WHERE {where} ORDER BY 1, 2").fetchall()
        total = 0
        for year, month in months:
            con.execute(
                f"""
                CREATE OR REPLACE TEMP TABLE gas AS
                SELECT row_number() OVER (ORDER BY channel_id, ts, event_id) - 1 AS rn, *
                FROM ev WHERE year = {year} AND month = {month} AND {where}
                """
            )
            arrays = con.execute(
                "SELECT channel_id, coalesce(value_num, 'NaN'::DOUBLE) FROM gas ORDER BY rn"
            ).fetchnumpy()
            channel_ids, values = (arrays[k].tolist() for k in arrays)
            total += len(channel_ids)
            kept = deadband.keep_sorted(channel_ids, values)
            con.execute("CREATE OR REPLACE TEMP TABLE kept AS SELECT unnest(?::BIGINT[]) AS rn", [kept])
            self._export(
                f"events-gas-{threshold}-{year}-{month:02d}",
                f"SELECT {self._run}, channel_id, event_id, ts, is_alarm, value_raw, value_num FROM gas "
                "SEMI JOIN kept USING (rn)",
                "events",
                EVENT_COLUMNS,
            )
        return total

    def _events_raw(self) -> None:
        start = self.raw_start.isoformat()
        for year in self._years():
            if year < self.raw_start.year:
                continue
            self._export(
                f"events-raw-{year}",
                f"""
                SELECT {self._run}, channel_id, event_id, ts, is_alarm, value_raw, value_num FROM ev
                WHERE year = {year} AND ts >= TIMESTAMPTZ '{start}' AND stream = 'raw'
                """,
                "events_raw",
                EVENT_COLUMNS,
            )

    @staticmethod
    def _aggregates() -> str:
        return (
            f"count(*), count(*) FILTER (is_alarm), count(*) FILTER (value_raw IN {_FAULTS}), count(value_num), "
            f"min(value_num), max(value_num), avg(value_num)"
        )

    def _channel_daily(self) -> None:
        for year in self._years():
            self._export(
                f"channel-daily-{year}",
                f"""
                SELECT {self._run}, channel_id, CAST(timezone('{_MSK}', ts) AS DATE) AS d, {self._aggregates()}
                FROM ev WHERE year = {year} GROUP BY channel_id, d
                """,
                "channel_daily",
                f"run_id, channel_id, date, {AGG_COLUMNS}",
            )

    def _channel_hourly(self) -> None:
        start = self.raw_start.isoformat()
        for year in self._years():
            if year < self.raw_start.year:
                continue
            self._export(
                f"channel-hourly-{year}",
                f"""
                SELECT {self._run}, channel_id, date_trunc('hour', ts) AS h, {self._aggregates()}
                FROM ev WHERE year = {year} AND ts >= date_trunc('hour', TIMESTAMPTZ '{start}') GROUP BY channel_id, h
                """,
                "channel_hourly",
                f"run_id, channel_id, hour, {AGG_COLUMNS}",
            )

    def _fault_episodes(self) -> None:
        """Эпизод: от первого значения неисправности до первого значения другого вида (по всему архиву).

        Смены «неисправен / нет» ищутся по годам — окно по году дешевле окна по всей истории, — затем по короткой
        таблице смен ещё раз: так склеиваются эпизоды через границу года. Несколько смен в одну секунду дают
        острова с одинаковым началом — они склеиваются в один эпизод."""
        con = self.con
        con.execute(
            f"CREATE OR REPLACE TEMP TABLE fault_channels AS "
            f"SELECT DISTINCT channel_id FROM ev_all WHERE value_raw IN {_FAULTS}"
        )
        con.execute(
            "CREATE OR REPLACE TEMP TABLE fault_changes "
            "(channel_id INTEGER, ts TIMESTAMPTZ, event_id BIGINT, value_raw VARCHAR, f BOOLEAN)"
        )
        years = [r[0] for r in con.execute("SELECT DISTINCT year FROM ev_all ORDER BY 1").fetchall()]
        for year in years:
            con.execute(
                f"""
                INSERT INTO fault_changes
                SELECT channel_id, ts, event_id, value_raw, f FROM (
                    SELECT channel_id, ts, event_id, value_raw, value_raw IN {_FAULTS} AS f,
                        lag(value_raw IN {_FAULTS}) OVER (PARTITION BY channel_id ORDER BY ts, event_id) AS prev_f
                    FROM ev_all WHERE year = {year} AND channel_id IN (SELECT channel_id FROM fault_channels)
                ) WHERE prev_f IS NULL OR f <> prev_f
                """
            )
        con.execute(
            """
            CREATE OR REPLACE TEMP TABLE fault_islands AS
            WITH changes AS (
                SELECT * FROM (
                    SELECT *, lag(f) OVER (PARTITION BY channel_id ORDER BY ts, event_id) AS prev_f FROM fault_changes
                ) WHERE prev_f IS NULL OR f <> prev_f
            ), islands AS (
                SELECT channel_id, ts AS start_ts, event_id, value_raw AS state, f,
                    lead(ts) OVER (PARTITION BY channel_id ORDER BY ts, event_id) AS end_ts
                FROM changes
            )
            SELECT channel_id, start_ts, arg_min(state, event_id) AS state,
                CASE WHEN bool_or(end_ts IS NULL) THEN NULL ELSE max(end_ts) END AS end_ts
            FROM islands WHERE f GROUP BY channel_id, start_ts
            """
        )
        con.execute("DROP TABLE fault_changes")
        since = ""
        if self.scope.since:
            since = f"WHERE end_ts IS NULL OR end_ts >= TIMESTAMPTZ '{self._since_ts().isoformat()}'"
        self._export(
            "fault-episodes",
            f"SELECT {self._run}, channel_id, start_ts, end_ts, state FROM fault_islands {since}",
            "fault_episodes",
            "run_id, channel_id, start_ts, end_ts, state",
        )

    def _alarms(self, pg: psycopg.Connection) -> None:
        """Лента дымовых тревог и эпизоды по всему архиву объектов в границах (docs/research/data-findings.md §3).

        Тревога — `Обнаружен дым` у дымового датчика; повтор в ту же секунду — одна тревога. Эпизод — тревоги
        объекта с разрывом не больше `gap`, вид — по `backend/config/triage.yaml`. Вердикт базовых тревог:
        `ppr` в плановой проверке, иначе `no_score` — оценок модели для базы нет."""
        r = rules()
        con = self.con
        self._clear_alarms(pg)
        glob = _q(Path(events_glob(self.archive_dir)))
        con.execute(
            """
            CREATE OR REPLACE TEMP TABLE alarm_channels AS
            SELECT c.id, c.sensor_type, o.parent_id AS object_id FROM channels c JOIN objects o ON o.id = c.object_id
            WHERE o.parent_id IN (SELECT id FROM alarm_objects)
            """
        )
        con.execute(
            f"""
            CREATE OR REPLACE TEMP TABLE smoke AS
            SELECT e.channel_id, e.ts, min(e.event_id) AS event_id, c.object_id
            FROM read_parquet({glob}) e JOIN alarm_channels c ON c.id = e.channel_id
            WHERE c.sensor_type = ? AND e.value_raw = ?
            GROUP BY e.channel_id, e.ts, c.object_id
            """,
            [r.alarm_sensor_type, r.alarm_value],
        )
        con.execute(
            f"""
            CREATE OR REPLACE TEMP TABLE levers AS
            SELECT e.ts, c.object_id FROM read_parquet({glob}) e JOIN alarm_channels c ON c.id = e.channel_id
            WHERE e.value_raw = ?
            """,
            [r.lever_value],
        )
        gap = int(r.gap.total_seconds())
        window = int(r.lever_window.total_seconds())
        offset = pg.execute("SELECT coalesce(max(id), 0) FROM alarm_episodes").fetchone()[0]
        con.execute(
            f"""
            CREATE OR REPLACE TEMP TABLE smoke_grp AS
            SELECT *, sum(new) OVER (PARTITION BY object_id ORDER BY ts, event_id) AS grp FROM (
                SELECT *, CASE WHEN ts - lag(ts) OVER (PARTITION BY object_id ORDER BY ts, event_id)
                    <= to_seconds({gap}) THEN 0 ELSE 1 END AS new
                FROM smoke
            )
            """
        )
        con.execute(
            f"""
            CREATE OR REPLACE TEMP TABLE alarm_ep AS
            WITH g AS (
                SELECT object_id, grp, min(ts) AS start_ts, max(ts) AS end_ts, count(DISTINCT channel_id) AS n_channels,
                    count(*) AS n_alarms, list(DISTINCT channel_id ORDER BY channel_id) AS channel_ids
                FROM smoke_grp GROUP BY object_id, grp
            ), l AS (
                SELECT g.*, EXISTS (
                    SELECT 1 FROM levers v WHERE v.object_id = g.object_id
                    AND v.ts BETWEEN g.start_ts - to_seconds({window}) AND g.end_ts + to_seconds({window})
                ) AS has_lever FROM g
            )
            SELECT {offset} + row_number() OVER (ORDER BY start_ts, object_id) AS id, *,
                {sql_kind("n_channels", "start_ts", "has_lever", r)} AS kind
            FROM l
            """
        )
        path = self.work_dir / "alarm-episodes.csv"
        con.execute(
            f"""
            COPY (
                SELECT id, NULL, object_id, start_ts, end_ts, n_channels, n_alarms, kind, has_lever,
                    '{{' || array_to_string(channel_ids, ',') || '}}'
                FROM alarm_ep
            ) TO {_q(path)} (FORMAT csv, HEADER false)
            """
        )
        self._copy_file(
            path,
            "alarm_episodes",
            "id, run_id, object_id, start_ts, end_ts, n_channels, n_alarms, kind, has_lever, channel_ids",
        )
        pg.execute("SELECT setval('alarm_episodes_id_seq', greatest((SELECT max(id) FROM alarm_episodes), 1))")
        path = self.work_dir / "alarms.csv"
        con.execute(
            f"""
            COPY (
                SELECT NULL, s.channel_id, s.object_id, s.ts, e.id,
                    CASE WHEN e.kind = 'ppr' THEN 'ppr' ELSE 'no_score' END, false
                FROM smoke_grp s JOIN alarm_ep e USING (object_id, grp)
            ) TO {_q(path)} (FORMAT csv, HEADER false)
            """
        )
        self._copy_file(path, "alarms", "run_id, channel_id, object_id, ts, episode_id, verdict, verdict_refined")
        kinds = dict(con.execute("SELECT kind, count(*) FROM alarm_ep GROUP BY 1").fetchall())
        self.report.info["alarm_episodes_by_kind"] = kinds
        for table in ("smoke", "smoke_grp", "levers", "alarm_ep"):
            con.execute(f"DROP TABLE {table}")

    def _channel_state(self, pg: psycopg.Connection) -> None:
        """Последнее событие канала по всему архиву; открытый эпизод неисправности — из fault_episodes."""
        path = self.work_dir / "channel-state.csv"
        # Последняя строка — одной структурой: arg_max пропускает NULL в первом аргументе, и value_num текстового
        # состояния подменился бы предыдущим числом
        self.con.execute(
            f"""
            COPY (
                SELECT {self._run}, channel_id, last.ts, last.v, last.n, last.a, last_alarm_at FROM (
                    SELECT channel_id,
                        arg_max({{'ts': ts, 'v': value_raw, 'n': value_num, 'a': is_alarm}}, {{'t': ts, 'e': event_id}})
                            AS last,
                        max(ts) FILTER (is_alarm) AS last_alarm_at
                    FROM ev_all GROUP BY channel_id
                )
            ) TO {_q(path)} (FORMAT csv, HEADER false)
            """
        )
        self._copy_file(path, "channel_state", "run_id, channel_id, ts, value_raw, value_num, is_alarm, last_alarm_at")
        pg.execute(
            """
            UPDATE channel_state s SET fault_since = f.start_ts
            FROM fault_episodes f
            WHERE f.run_id IS NOT DISTINCT FROM %(run)s AND f.end_ts IS NULL AND s.run_id IS NOT DISTINCT FROM %(run)s
              AND s.channel_id = f.channel_id
            """,
            {"run": self.run_id},
        )

    # --- сжатие и объёмы -------------------------------------------------------------------------------------

    def _compress(self, pg: psycopg.Connection) -> None:
        before = dt.datetime.combine(self.compress_before, dt.time(), dt.timezone(dt.timedelta(hours=3)))
        n = pg.execute(
            "SELECT count(compress_chunk(c, if_not_compressed => true)) FROM show_chunks('events', older_than => %s) c",
            (before,),
        ).fetchone()[0]
        self.report.info["compressed_chunks"] = n

    def _sizes(self, pg: psycopg.Connection) -> dict:
        sizes = {}
        for table in ("events", "events_raw", "channel_hourly"):
            sizes[table] = pg.execute("SELECT hypertable_size(%s::regclass)", (table,)).fetchone()[0]
        for table in ("channel_daily", "fault_episodes", "channel_state", "channels", "objects"):
            sizes[table] = pg.execute("SELECT pg_total_relation_size(%s::regclass)", (table,)).fetchone()[0]
        sizes["database"] = pg.execute("SELECT pg_database_size(current_database())").fetchone()[0]
        return sizes


def load_db(**kwargs) -> dict:
    loader = Loader(**kwargs)
    report = loader.run()
    (loader.work_dir / "load-db-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return report
