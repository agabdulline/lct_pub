"""Загрузка внешнего набора в рабочую базу (этап 14, docs/architecture/external-data.md, D28).

`data/external/<набор>/prepared/` (готовит `python -m app.cli external prepare`, lct-18):
`objects`, `channels`, `events` и, если есть, `labels`, `credentials`, `permits` в Parquet плюс `report.json` — число
строк и sha256 файлов. Загрузчик сверяет файлы с отчётом и диапазоны id набора, пишет справочник с `source` набора,
создаёт служебный прогон-основу (`runs.kind = 'dataset'`) и кладёт в него историю — тем же кодом, что базу хакатона
(`app/loader.py`: значимые события и сырой поток по атрибутам канала, итоги, эпизоды неисправностей, состояние).
База хакатона (`run_id IS NULL`) не меняется.

Повторная загрузка заменяет прогон-основу и разметку набора. Сценарные прогоны на наборе ссылаются на основу —
пока они есть, загрузка отказывает: сначала `python -m app.cli runs delete <id>`.

Строки прогона-основы попадают и в сжатые чанки истории хакатона (набор 2023 года — в сжатый чанк `events`). Поэтому
удаление старой основы — только по каналам набора и времени: так TimescaleDB не распаковывает чанки хакатона.
"""

import datetime as dt
import hashlib
import json
import time
from pathlib import Path

import psycopg

from app.archive import _q
from app.core.config import settings
from app.loader import Loader

OBJECT_BASE, OBJECT_STEP = 9_000_000, 10_000
CHANNEL_BASE, CHANNEL_STEP = 900_000_000, 1_000_000
REQUIRED = ("objects", "channels", "events")
OPTIONAL = ("labels", "credentials", "permits")
_MSK = "Europe/Moscow"
_HYPERTABLES = ("events", "events_raw", "channel_hourly")  # остальные таблицы прогона — обычные
_RUN_TABLES = (
    "equipment_episodes",
    "work_orders",
    "predictions",
    "alarms",
    "alarm_episodes",
    "fault_episodes",
    "channel_state",
    "channel_daily",
)


class ExternalLoadError(ValueError):
    pass


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(1 << 20):
            h.update(block)
    return h.hexdigest()


def prepared_dir(dataset: str) -> Path:
    return settings.external_dir / dataset / "prepared"


class ExternalLoader(Loader):
    def __init__(
        self,
        dataset: str,
        prepared: Path,
        database_url: str,
        work_dir: Path,
        workers: int = 4,
        memory_limit: str = "4GB",
        threads: int | None = None,
    ) -> None:
        super().__init__(
            archive_dir=prepared,
            database_url=database_url,
            work_dir=work_dir,
            workers=workers,
            memory_limit=memory_limit,
            threads=threads,
            compress_before=None,
            raw_window_days=None,
        )
        self.dataset = dataset
        self.prepared = prepared
        path = prepared / "report.json"
        if not path.exists():
            raise ExternalLoadError(f"нет {path}: сначала python -m app.cli external prepare --dataset {dataset}")
        self.meta = json.loads(path.read_text(encoding="utf-8"))
        if self.meta.get("dataset") != dataset:
            raise ExternalLoadError(f"report.json — набор {self.meta.get('dataset')!r}, ожидался {dataset!r}")
        self.k = int(self.meta["k"])
        self.files = {
            name: prepared / f"{name}.parquet"
            for name in REQUIRED + OPTIONAL
            if name in REQUIRED or (prepared / f"{name}.parquet").exists()
        }

    # --- общий ход -------------------------------------------------------------------------------------------

    def run(self) -> dict:
        started = time.monotonic()
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._step("verify", self._verify)
        self._step("prepare", self._prepare)
        with psycopg.connect(self.conninfo) as pg:
            self._step("replace", lambda: self._replace(pg))
            pg.commit()
        self._step("events", self._events)
        self._step("events_gas", self._events_gas)
        self._step("events_raw", self._events_raw)
        self._step("channel_daily", self._channel_daily)
        self._step("channel_hourly", self._channel_hourly)
        self._step("fault_episodes", self._fault_episodes)
        self._step("reference", self._reference)
        self._step("copy_wait", self._wait_copies)
        with psycopg.connect(self.conninfo, autocommit=True) as pg:
            self._step("run_events", lambda: self._run_events(pg))
            self._step("channel_state", lambda: self._channel_state(pg))
            self._step("analyze", lambda: pg.execute("ANALYZE"))
        self._pool.shutdown()
        self.report.seconds["total"] = round(time.monotonic() - started, 1)
        self.report.info.update(dataset=self.dataset, run_id=self.run_id, k=self.k)
        return self.report.as_dict()

    def _run_events(self, pg: psycopg.Connection) -> None:
        """Счётчик событий прогона-основы для `/runs` — как у приёма: `events_raw` + `events` значимых каналов."""
        n = pg.execute(
            "UPDATE runs SET events = (SELECT count(*) FROM events_raw WHERE run_id = %(run)s) + ("
            "SELECT count(*) FROM events e JOIN channels c ON c.id = e.channel_id "
            "WHERE e.run_id = %(run)s AND c.stream = 'significant') WHERE id = %(run)s RETURNING events",
            {"run": self.run_id},
        ).fetchone()[0]
        self.report.info["events"] = n

    # --- проверки --------------------------------------------------------------------------------------------

    def _verify(self) -> None:
        """Файлы на месте и совпадают с report.json по sha256 и числу строк."""
        outputs = self.meta.get("outputs", {})
        rows = self.meta.get("rows", {})
        for name, path in self.files.items():
            if not path.exists():
                raise ExternalLoadError(f"нет {path}")
            expected = outputs.get(path.name, {}).get("sha256")
            if expected and _sha256(path) != expected:
                raise ExternalLoadError(f"{path.name}: sha256 не совпадает с report.json — файл изменён после prepare")
            if name in rows:
                n = self.con.execute(f"SELECT count(*) FROM read_parquet({_q(path)})").fetchone()[0]
                if n != rows[name]:
                    raise ExternalLoadError(f"{path.name}: {n} строк, в report.json — {rows[name]}")

    def _prepare(self) -> None:
        con = self.con
        o_lo, c_lo = OBJECT_BASE + self.k * OBJECT_STEP, CHANNEL_BASE + self.k * CHANNEL_STEP
        con.execute(
            f"CREATE OR REPLACE TABLE objects AS SELECT id, parent_id, level, kind, name, meta "
            f"FROM read_parquet({_q(self.files['objects'])})"
        )
        con.execute(
            f"""
            CREATE OR REPLACE TABLE scope_channels AS
            SELECT id, object_id, system_type, sensor_type, tag, name, picket_number, picket_offset, tag_group,
                coalesce(stream, 'significant') AS stream, CAST(deadband AS DOUBLE) AS deadband, unit
            FROM read_parquet({_q(self.files["channels"])})
            """
        )
        checks = {
            f"объекты вне диапазона набора [{o_lo}, {o_lo + OBJECT_STEP})": (
                f"SELECT count(*) FROM objects WHERE id < {o_lo} OR id >= {o_lo + OBJECT_STEP}"
            ),
            f"каналы вне диапазона набора [{c_lo}, {c_lo + CHANNEL_STEP})": (
                f"SELECT count(*) FROM scope_channels WHERE id < {c_lo} OR id >= {c_lo + CHANNEL_STEP}"
            ),
            "объекты с родителем не из набора": (
                "SELECT count(*) FROM objects WHERE parent_id IS NOT NULL AND parent_id NOT IN (SELECT id FROM objects)"
            ),
            "каналы на объектах не из набора": (
                "SELECT count(*) FROM scope_channels WHERE object_id NOT IN (SELECT id FROM objects)"
            ),
            "маршрут канала не significant / raw": (
                "SELECT count(*) FROM scope_channels WHERE stream NOT IN ('significant', 'raw')"
            ),
            "события каналов не из набора": (
                f"SELECT count(*) FROM read_parquet({_q(self.files['events'])}) "
                "WHERE channel_id NOT IN (SELECT id FROM scope_channels)"
            ),
        }
        for problem, sql in checks.items():
            n = con.execute(sql).fetchone()[0]
            if n:
                raise ExternalLoadError(f"{self.dataset}: {problem} — {n}")
        con.execute(
            f"""
            CREATE OR REPLACE VIEW ev_all AS
            SELECT e.event_id, e.channel_id, timezone('{_MSK}', e.ts) AS ts, e.is_alarm, e.value AS value_raw,
                TRY_CAST(e.value AS DOUBLE) AS value_num, year(e.ts) AS year, month(e.ts) AS month,
                c.sensor_type, c.stream, c.deadband
            FROM read_parquet({_q(self.files["events"])}) e JOIN scope_channels c ON c.id = e.channel_id
            """
        )
        con.execute("CREATE OR REPLACE VIEW ev AS SELECT * FROM ev_all")
        start, end = con.execute("SELECT min(ts), max(ts) FROM ev_all").fetchone()
        if start is None:
            raise ExternalLoadError(f"{self.dataset}: событий нет")
        self.data_start, self.data_end = start, end
        self.raw_start = start  # у набора нет окна сырого потока: весь поток — история основы
        n_channels = con.execute("SELECT count(*) FROM scope_channels").fetchone()[0]
        self.report.info.update(period=[start.isoformat(), end.isoformat()], channels=n_channels)

    # --- справочник и прогон-основа --------------------------------------------------------------------------

    def _replace(self, pg: psycopg.Connection) -> None:
        """Одной транзакцией: старая основа и разметка набора — долой, справочник — с source набора, новая основа."""
        old = [
            r[0]
            for r in pg.execute(
                "SELECT id FROM runs WHERE kind = 'dataset' AND params->>'dataset' = %s", (self.dataset,)
            )
        ]
        if old:
            dependent = [r[0] for r in pg.execute("SELECT id FROM runs WHERE base_run_id = ANY(%s)", (old,))]
            if dependent:
                raise ExternalLoadError(
                    f"на наборе {self.dataset} есть сценарные прогоны {dependent} — удалите их: "
                    "python -m app.cli runs delete <id> --yes"
                )
        channels = [r[0] for r in self.con.execute("SELECT id FROM scope_channels ORDER BY id").fetchall()]
        for run_id in old:
            start, end = pg.execute("SELECT sim_start, sim_end FROM runs WHERE id = %s", (run_id,)).fetchone()
            for table in _RUN_TABLES:
                pg.execute(f"DELETE FROM {table} WHERE run_id = %s", (run_id,))
            for table in _HYPERTABLES:  # по каналам набора и времени — сжатые чанки хакатона не распаковываются
                time_col = "hour" if table == "channel_hourly" else "ts"
                pg.execute(
                    f"DELETE FROM {table} WHERE run_id = %s AND channel_id = ANY(%s) "
                    f"AND {time_col} >= %s - interval '1 hour' AND {time_col} <= %s",
                    (run_id, channels, start, end),
                )
            pg.execute("DELETE FROM runs WHERE id = %s", (run_id,))
        for table in ("external_labels", "external_credentials", "external_permits"):
            pg.execute(f"DELETE FROM {table} WHERE dataset = %s", (self.dataset,))

        objects = self.con.execute(
            "SELECT id, parent_id, level, kind, name, meta FROM objects ORDER BY level, id"
        ).fetchall()
        with pg.cursor() as cur:
            cur.executemany(
                "INSERT INTO objects (id, parent_id, level, kind, name, source, meta) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO UPDATE SET parent_id = EXCLUDED.parent_id, "
                "level = EXCLUDED.level, kind = EXCLUDED.kind, name = EXCLUDED.name, source = EXCLUDED.source, "
                "meta = EXCLUDED.meta",
                [(i, p, lv, k, n, self.dataset, m) for i, p, lv, k, n, m in objects],
            )
            cur.executemany(
                "INSERT INTO channels (id, object_id, system_type, sensor_type, tag, name, picket_number, "
                "picket_offset, tag_group, source, stream, deadband, unit) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO UPDATE SET "
                "object_id = EXCLUDED.object_id, system_type = EXCLUDED.system_type, "
                "sensor_type = EXCLUDED.sensor_type, tag = EXCLUDED.tag, name = EXCLUDED.name, "
                "picket_number = EXCLUDED.picket_number, picket_offset = EXCLUDED.picket_offset, "
                "tag_group = EXCLUDED.tag_group, source = EXCLUDED.source, stream = EXCLUDED.stream, "
                "deadband = EXCLUDED.deadband, unit = EXCLUDED.unit",
                [
                    (*c[:9], self.dataset, *c[9:])
                    for c in self.con.execute(
                        "SELECT id, object_id, system_type, sensor_type, tag, name, picket_number, picket_offset, "
                        "tag_group, stream, deadband, unit FROM scope_channels ORDER BY id"
                    ).fetchall()
                ],
            )
        end = self.data_end + dt.timedelta(seconds=1)
        params = {
            "dataset": self.dataset,
            "title": self.meta.get("title"),
            "k": self.k,
            "rows": self.meta.get("rows"),
            "synthetic": self.meta.get("synthetic"),
        }
        self.run_id = pg.execute(
            "INSERT INTO runs (scenario, status, kind, sim_start, sim_end, sim_now, speed, params, finished_at) "
            "VALUES (%s, 'finished', 'dataset', %s, %s, %s, 1, %s, now()) RETURNING id",
            (f"dataset:{self.dataset}", self.data_start, end, end, json.dumps(params, ensure_ascii=False)),
        ).fetchone()[0]
        self.report.info["replaced_runs"] = old

    # --- разметка, пропуска, допуски -------------------------------------------------------------------------

    def _reference(self) -> None:
        ds = self.dataset.replace("'", "''")
        if "labels" in self.files:
            self._export(
                "labels",
                f"""
                SELECT '{ds}', object_id, channel_id, timezone('{_MSK}', start_ts), timezone('{_MSK}', end_ts), label,
                    meta FROM read_parquet({_q(self.files["labels"])})
                """,
                "external_labels",
                "dataset, object_id, channel_id, start_ts, end_ts, label, meta",
            )
        if "credentials" in self.files:
            self._export(
                "credentials",
                f"SELECT '{ds}', credential_id, kind, meta FROM read_parquet({_q(self.files['credentials'])})",
                "external_credentials",
                "dataset, credential_id, kind, meta",
            )
        if "permits" in self.files:
            self._export(
                "permits",
                f"""
                SELECT '{ds}', credential_id, object_id, timezone('{_MSK}', valid_from), timezone('{_MSK}', valid_to)
                FROM read_parquet({_q(self.files["permits"])})
                """,
                "external_permits",
                "dataset, credential_id, object_id, valid_from, valid_to",
            )


def load_external(dataset: str, prepared: Path | None = None, **kwargs) -> dict:
    loader = ExternalLoader(dataset, prepared or prepared_dir(dataset), **kwargs)
    report = loader.run()
    (loader.work_dir / f"load-external-{dataset}-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str)
    )
    return report
