"""Сверка рабочей базы с архивом и с `docs/data-format.md` — этап 6.5. `python -m app.cli check-db`.

Сверяются только базовая история (`run_id IS NULL`) и полная загрузка. Каждая проверка — ожидание, факт
и допуск; расхождения печатаются, команда завершается с ошибкой.
"""

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path

import psycopg

from app.archive import REPORT_FILE, _q, connect, events_glob, load_catalog
from app.domain.alarm_episodes import rules
from app.domain.values import FAULT_STATES, GAS_SENSOR, MOTION_SENSOR, SMOKE_SENSOR

# docs/data-format.md §1 и §3: строк в журналах и каналов вне справочника по годам
SOURCE_ROWS = {
    2019: 20_993_328,
    2020: 28_172_398,
    2021: 53_698_948,
    2022: 43_483_149,
    2023: 31_937_125,
    2024: 49_023_883,
    2025: 55_541_796,
    2026: 30_695_389,
}
OUT_OF_CATALOG_CHANNELS = {2019: 1004, 2020: 990, 2021: 829, 2022: 558, 2023: 106, 2024: 30, 2025: 12, 2026: 2}
# docs/data-format.md §4: «Неисправен» у дымовых датчиков по годам
SMOKE_FAULTS = {2019: 2412, 2020: 22525, 2021: 1041333, 2022: 23286, 2023: 4661, 2024: 5081, 2025: 10005, 2026: 59879}


@dataclass
class Check:
    name: str
    expected: float
    actual: float
    tolerance: float = 0.0  # доля от ожидания

    @property
    def ok(self) -> bool:
        return abs(self.actual - self.expected) <= self.tolerance * abs(self.expected)

    def line(self) -> str:
        mark = "ok  " if self.ok else "FAIL"
        return f"{mark} {self.name}: ожидалось {_n(self.expected)}, факт {_n(self.actual)}"


def _n(x: float) -> str:
    return f"{x:,.0f}".replace(",", " ")


def run_checks(archive_dir: Path, conninfo: str, memory_limit: str = "8GB") -> list[Check]:
    con = connect(memory_limit)
    load_catalog(con, archive_dir)
    con.execute(
        f"CREATE VIEW a AS SELECT e.*, c.sensor_type FROM read_parquet({_q(Path(events_glob(archive_dir)))}, "
        f"hive_partitioning = true) e LEFT JOIN channels c ON c.id = e.channel_id"
    )
    faults = "(" + ", ".join(f"'{s}'" for s in sorted(FAULT_STATES)) + ")"
    raw_types = f"('{GAS_SENSOR}', '{MOTION_SENSOR}')"
    checks: list[Check] = []

    # Архив против исходного формата
    per_year = dict(con.execute("SELECT year, count(*) FROM a GROUP BY 1").fetchall())
    report = json.loads((archive_dir / REPORT_FILE).read_text())["years"]
    for year, rows in SOURCE_ROWS.items():
        dups = report[str(year)]["duplicates_dropped"]  # в data-format.md строки-заголовки уже не считаются
        checks.append(Check(f"архив {year}: событий журнала − точные дубли", rows - dups, per_year.get(year, 0)))
    ooc = dict(con.execute("SELECT year, count(DISTINCT channel_id) FROM a WHERE NOT in_catalog GROUP BY 1").fetchall())
    for year, n in OUT_OF_CATALOG_CHANNELS.items():
        checks.append(Check(f"архив {year}: каналов вне справочника", n, ooc.get(year, 0)))
    smoke = dict(
        con.execute(
            f"SELECT year, count(*) FROM a WHERE sensor_type = '{SMOKE_SENSOR}' AND value_raw = 'Неисправен' GROUP BY 1"
        ).fetchall()
    )
    for year, n in SMOKE_FAULTS.items():
        # 2023 — точные дубли отброшены при сборке, допуск 1%
        checks.append(Check(f"архив {year}: «Неисправен» у дымовых", n, smoke.get(year, 0), 0.01))

    in_catalog, significant, channels_seen = con.execute(
        f"SELECT count(*), count(*) FILTER (sensor_type NOT IN {raw_types}), count(DISTINCT channel_id) "
        f"FROM a WHERE in_catalog"
    ).fetchone()
    data_end = con.execute("SELECT max(ts) FROM a").fetchone()[0]
    raw_window = con.execute(
        f"SELECT count(*) FROM a WHERE in_catalog AND sensor_type IN {raw_types} AND ts >= ?",
        [data_end - dt.timedelta(days=90)],
    ).fetchone()[0]
    r = rules()
    alarms = con.execute(
        "SELECT count(*) FROM (SELECT DISTINCT channel_id, ts FROM a WHERE sensor_type = ? AND value_raw = ?)",
        [r.alarm_sensor_type, r.alarm_value],
    ).fetchone()[0]
    fault_open = con.execute(
        f"SELECT count(*) FROM (SELECT arg_max(value_raw, {{'t': ts, 'e': event_id}}) v FROM a WHERE in_catalog "
        f"GROUP BY channel_id) WHERE v IN {faults}"
    ).fetchone()[0]

    # Рабочая база против архива; autocommit — между запросами идёт подсчёт по архиву, транзакцию не держим
    with psycopg.connect(conninfo, autocommit=True) as pg:

        def q(sql: str) -> float:
            return pg.execute(sql).fetchone()[0] or 0

        base = "run_id IS NULL"
        checks += [
            Check(
                "channel_daily: событий = событий архива в справочнике",
                in_catalog,
                q(f"SELECT sum(n_events) FROM channel_daily WHERE {base}"),
            ),
            Check(
                "events без газа: = значимых событий архива",
                significant,
                q(
                    f"SELECT count(*) FROM events e JOIN channels c ON c.id = e.channel_id "
                    f"WHERE e.{base} AND c.sensor_type <> '{GAS_SENSOR}'"
                ),
            ),
            Check(
                "events_raw: = газ и движение за 90 дней",
                raw_window,
                q(f"SELECT count(*) FROM events_raw WHERE {base}"),
            ),
            Check(
                "channel_state: каналов = каналов архива в справочнике",
                channels_seen,
                q(f"SELECT count(*) FROM channel_state WHERE {base}"),
            ),
            Check(
                "fault_episodes открытых = каналов, последнее значение которых — неисправность",
                fault_open,
                q(f"SELECT count(*) FROM fault_episodes WHERE {base} AND end_ts IS NULL"),
            ),
            Check(
                "channel_state.fault_since = открытым эпизодам",
                fault_open,
                q(f"SELECT count(*) FROM channel_state WHERE {base} AND fault_since IS NOT NULL"),
            ),
            Check(
                "alarms: = «Обнаружен дым» без повторов в секунду",
                alarms,
                q(f"SELECT count(*) FROM alarms WHERE {base}"),
            ),
            Check(
                "alarm_episodes: тревог в эпизодах = тревог",
                alarms,
                q(f"SELECT sum(n_alarms) FROM alarm_episodes WHERE {base}"),
            ),
            Check(
                "channel_daily: неисправностей = событий неисправности в архиве",
                con.execute(f"SELECT count(*) FROM a WHERE in_catalog AND value_raw IN {faults}").fetchone()[0],
                q(f"SELECT sum(n_fault) FROM channel_daily WHERE {base}"),
            ),
        ]
    return checks
