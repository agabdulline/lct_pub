"""Решения моделей на истории — строки `predictions` базы (`run_id = NULL`) для режима «Вся история».

Строки считает ML-код офлайн — тем же кодом, что эталоны сравнения моделей, с которым путь сервиса совпадает:
`ml.history.smoke` (событийная, связка v3 → v0, #241) и `ml.history.daily` (ежедневная по таблице маршрутизации,
#250). Здесь — только запись готового файла parquet: в образе backend нет CatBoost, а считать это пакетно дешевле,
чем через сервис по дню.

`python -m app.cli history predictions --file data/interim/history/smoke_alarm.parquet [...]`:
- строки модели файла за его период (`decision_time` от первого до последнего) заменяются целиком — повторный запуск
  даёт то же;
- у событийной `alarm_id` — базовая тревога того же канала и секунды (`alarms`, `run_id NULL`); строка с `decides` —
  решение связки: на неё ставится `alarms.prediction_id`, как у живого прогона (`model_versions.decided_sql`). Вердикт
  тревоги не меняется.

Чтение файла — DuckDB, запись — COPY во временную таблицу и один INSERT … SELECT: ~1.3 млн строк ежедневной —
минуты, память — на пачку COPY.
"""

import time
from pathlib import Path

import duckdb
import psycopg

COLUMNS = {  # столбец файла → тип временной таблицы
    "model": "text",
    "model_version": "text",
    "channel_id": "integer",
    "alarm_ts": "timestamptz",
    "decision_time": "timestamptz",
    "horizon_start": "timestamptz",
    "horizon_end": "timestamptz",
    "status": "text",
    "skip_reason": "text",
    "score": "double precision",
    "threshold": "double precision",
    "alert": "boolean",
    "outcome": "text",
    "outcome_at": "timestamptz",
    "outcome_source": "text",
    "decides": "boolean",
}
BATCH = 50_000


def _copy(cur, path: Path) -> int:
    con = duckdb.connect()
    have = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{path}')").fetchall()}
    cols = [f'"{c}"' if c in have else f"NULL AS {c}" for c in COLUMNS]
    res = con.execute(f"SELECT {', '.join(cols)} FROM read_parquet('{path}')")
    n = 0
    with cur.copy(f"COPY hp ({', '.join(COLUMNS)}) FROM STDIN") as copy:
        while batch := res.fetchmany(BATCH):
            for row in batch:
                copy.write_row(row)
            n += len(batch)
    return n


def write(conninfo: str, files: list[Path]) -> dict:
    """Заменить строки истории моделей из файлов. Одна транзакция на всё: при ошибке база как была."""
    started = time.perf_counter()
    report: dict = {"files": [str(f) for f in files]}
    with psycopg.connect(conninfo) as conn, conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE hp (" + ", ".join(f"{c} {t}" for c, t in COLUMNS.items()) + ") ON COMMIT DROP")
        report["rows"] = sum(_copy(cur, f) for f in files)
        cur.execute("SELECT model, min(decision_time), max(decision_time) FROM hp GROUP BY model")
        spans = cur.fetchall()
        for model, lo, hi in spans:
            span = {"model": model, "lo": lo, "hi": hi}
            cur.execute(
                "UPDATE alarms a SET prediction_id = NULL FROM predictions p WHERE a.prediction_id = p.id "
                "AND p.run_id IS NULL AND p.model = %(model)s AND p.decision_time BETWEEN %(lo)s AND %(hi)s",
                span,
            )
            cur.execute(
                "DELETE FROM predictions WHERE run_id IS NULL AND model = %(model)s "
                "AND decision_time BETWEEN %(lo)s AND %(hi)s",
                span,
            )
            report.setdefault("replaced", {})[model] = cur.rowcount
        cur.execute(
            """
            INSERT INTO predictions (run_id, model, model_version, channel_id, alarm_id, decision_time, horizon_start,
                horizon_end, status, skip_reason, score, threshold, alert, outcome, outcome_at, outcome_source)
            SELECT NULL, h.model, h.model_version, h.channel_id, a.id, h.decision_time, h.horizon_start, h.horizon_end,
                h.status, h.skip_reason, h.score, h.threshold, h.alert, h.outcome, h.outcome_at, h.outcome_source
            FROM hp h
            LEFT JOIN alarms a ON h.alarm_ts IS NOT NULL AND a.run_id IS NULL AND a.channel_id = h.channel_id
                AND a.ts = h.alarm_ts
            ON CONFLICT DO NOTHING
            """
        )
        report["inserted"] = cur.rowcount
        cur.execute("SELECT count(*) FROM hp h WHERE h.alarm_ts IS NOT NULL")
        with_alarm = cur.fetchone()[0]
        cur.execute(
            "SELECT count(*) FROM predictions p JOIN hp h ON h.model = p.model AND h.model_version = p.model_version "
            "AND h.channel_id = p.channel_id AND h.decision_time = p.decision_time "
            "WHERE p.run_id IS NULL AND h.alarm_ts IS NOT NULL AND p.alarm_id IS NULL"
        )
        missing = cur.fetchone()[0]
        if missing:
            raise RuntimeError(f"базовой тревоги нет у {missing} из {with_alarm} строк — база загружена не целиком?")
        cur.execute(
            """
            UPDATE alarms a SET prediction_id = p.id
            FROM predictions p JOIN hp h ON h.model = p.model AND h.model_version = p.model_version
                AND h.channel_id = p.channel_id AND h.decision_time = p.decision_time
            WHERE p.run_id IS NULL AND h.decides AND a.id = p.alarm_id
            """
        )
        report["decided_alarms"] = cur.rowcount
        conn.commit()
    report["seconds"] = round(time.perf_counter() - started, 1)
    return report
