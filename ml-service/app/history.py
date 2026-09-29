"""История для признаков из рабочей базы — через представление прогона run_events (docs/architecture/data.md).

Кэш в памяти по (прогон, объект) с догрузкой новых событий (D4); /v1/cache/reload сбрасывает кэш прогона. Для v3
здесь же дни с данными журнала по прогону и решения их кода по объекту (`AlarmRows` — пересчёт только изменившихся
каналов).
"""

import threading

import pandas as pd
from sqlalchemy import create_engine, text

from app.config import settings
from ml.v3_bridge.alarm import AlarmRows

SMOKE = "Датчик дыма"

_engine = None
_lock = threading.Lock()
_cache: dict[tuple, pd.DataFrame] = {}
_coverage: dict[int | None, set[pd.Timestamp]] = {}  # дни базовой части прогона — не меняются
_v3_rows: dict[tuple, AlarmRows] = {}
_sequences: dict = {}  # прогон → SequenceCache отдельных моделей типов (ml.daily_routing.online)
V3_RUNS = 2  # решения v3 держим для двух последних прогонов: на крупном объекте это ~10 МБ, память стенда общая


def engine():
    global _engine
    if _engine is None:
        _engine = create_engine(settings.database_url, pool_pre_ping=True, connect_args={"connect_timeout": 3})
    return _engine


def _local(ts: pd.Series) -> pd.Series:
    return ts.dt.tz_convert(settings.timezone).dt.tz_localize(None).astype("datetime64[ns]")


def objects_of(channel_ids: list[int]) -> dict[int, int]:
    sql = text("SELECT id, object_id FROM channels WHERE id = ANY(:ids)")
    with engine().connect() as conn:
        return {int(r.id): int(r.object_id) for r in conn.execute(sql, {"ids": channel_ids})}


def smoke_channels(object_id: int) -> pd.DataFrame:
    sql = text(
        "SELECT id AS channel_id, object_id, system_type, sensor_type, tag, name, picket_number FROM channels "
        "WHERE object_id = :obj AND sensor_type = :smoke"
    )
    with engine().connect() as conn:
        return pd.read_sql(sql, conn, params={"obj": object_id, "smoke": SMOKE})


def smoke_events(run_id: int | None, object_id: int, channel_ids: list[int], since, until) -> pd.DataFrame:
    """События дымовых каналов объекта в представлении прогона с since включительно, время — наивное московское.

    Догрузка перечитывает и последнюю секунду кэша: события той же секунды могли прийти позже (дубли убираются).
    """
    key = (run_id, object_id)
    until = pd.Timestamp(until)
    with _lock:
        cached = _cache.get(key)
    start = pd.Timestamp(since) if cached is None or cached.empty else cached["ts"].max()
    if cached is not None and not cached.empty and start > until:
        return cached[cached["ts"] <= until]
    sql = text(
        "SELECT channel_id, coalesce(event_id, -1) AS event_id, ts, value_raw AS value FROM run_events(:run) "
        "WHERE channel_id = ANY(:ids) AND ts >= :start AND ts <= :until"
    )
    tz = settings.timezone
    params = {"run": run_id, "ids": channel_ids, "start": start.tz_localize(tz), "until": until.tz_localize(tz)}
    with engine().connect() as conn:
        fresh = pd.read_sql(sql, conn, params=params)
    fresh["ts"] = _local(pd.to_datetime(fresh["ts"], utc=True))
    merged = fresh if cached is None else pd.concat([cached, fresh], ignore_index=True)
    merged = merged.drop_duplicates(["channel_id", "event_id", "ts", "value"])
    merged = merged.sort_values(["channel_id", "ts", "event_id"], kind="stable").reset_index(drop=True)
    with _lock:
        _cache[key] = merged
    return merged[merged["ts"] <= until]


def coverage(run_id: int | None) -> set[pd.Timestamp]:
    """Дни с данными журнала в представлении прогона — правило v3 «за 8 суток до решения нет дня без данных».

    Как `global_coverage` бандла v3: день, в который есть хоть одно событие любого канала. Базовая часть — суточные
    итоги до дня старта (на полной базе 2 004 дня, пропуски 7–8.04.2024 и 1.06.2026 — как в архиве), читается один
    раз на прогон; дни самого прогона — на каждый запрос, их мало.
    """
    with _lock:
        base = _coverage.get(run_id)
    with engine().connect() as conn:
        if base is None:
            rows = conn.execute(
                text("SELECT DISTINCT date FROM run_channel_daily(:run) WHERE run_id IS NULL OR run_id <> :run"),
                {"run": run_id},
            )
            base = {pd.Timestamp(r[0]) for r in rows}
            with _lock:
                _coverage[run_id] = base
        if run_id is None:
            return set(base)
        rows = conn.execute(text("SELECT DISTINCT date FROM channel_daily WHERE run_id = :run"), {"run": run_id})
        return base | {pd.Timestamp(r[0]) for r in rows}


def v3_rows(run_id: int | None, object_id: int) -> AlarmRows:
    """Решения кода v3 по объекту в представлении прогона — пересчитываются только изменившиеся каналы.
    Кэш других прогонов, кроме V3_RUNS последних, сбрасывается."""
    with _lock:
        key = (run_id, object_id)
        if key not in _v3_rows:
            runs = list(dict.fromkeys(k[0] for k in _v3_rows))  # в порядке первого запроса
            if run_id not in runs and len(runs) >= V3_RUNS:
                for k in [k for k in _v3_rows if k[0] in runs[: len(runs) - V3_RUNS + 1]]:
                    del _v3_rows[k]
            _v3_rows[key] = AlarmRows()
        return _v3_rows[key]


def reload(run_id: int | None) -> int:
    with _lock:
        keys = [k for k in _cache if k[0] == run_id]
        for k in keys:
            del _cache[k]
        for k in [k for k in _v3_rows if k[0] == run_id]:
            del _v3_rows[k]
        _coverage.pop(run_id, None)
        _sequences.pop(run_id, None)
    return len(keys)


def known_channels(channel_ids: list[int]) -> set[int]:
    with engine().connect() as conn:
        rows = conn.execute(text("SELECT id FROM channels WHERE id = ANY(:ids)"), {"ids": channel_ids})
        return {r[0] for r in rows}


def daily_inputs(run_id: int | None, date, types: list[str], channel_ids: list[int] | None, history_days: int):
    """Каналы, дневные итоги и эпизоды «Неисправен» для ежедневной модели — из представления прогона.

    Каналы — все каналы нужных типов на объектах запрошенных каналов: по ним считаются признаки объекта, как при
    обучении. Дневные итоги — только запрошенных каналов.
    """
    day = pd.Timestamp(date).normalize()
    since = day - pd.Timedelta(days=history_days)
    tz = settings.timezone
    with engine().connect() as conn:
        ch = pd.read_sql(
            text("SELECT id AS channel_id, object_id, sensor_type FROM channels WHERE sensor_type = ANY(:t)"),
            conn,
            params={"t": types},
        )
        wanted = ch["channel_id"].tolist()
        if channel_ids is not None:
            wanted = ch.loc[ch["channel_id"].isin(channel_ids), "channel_id"].tolist()
            ch = ch[ch["object_id"].isin(ch.loc[ch["channel_id"].isin(wanted), "object_id"])]
        ids = ch["channel_id"].tolist()
        daily = pd.read_sql(
            text(
                "SELECT channel_id, date, n_events, n_alarm, n_fault, n_num, mean, max "
                "FROM run_channel_daily(:run) WHERE channel_id = ANY(:ids) AND date >= :since AND date < :day"
            ),
            conn,
            params={"run": run_id, "ids": wanted, "since": since.date(), "day": day.date()},
        )
        eps = pd.read_sql(
            text(
                "SELECT channel_id, start_ts, end_ts FROM run_fault_episodes(:run) "
                "WHERE state = 'Неисправен' AND channel_id = ANY(:ids) AND start_ts < :day "
                "AND (end_ts IS NULL OR end_ts >= :since)"
            ),
            conn,
            params={"run": run_id, "ids": ids, "day": day.tz_localize(tz), "since": since.tz_localize(tz)},
        )
    for col in ("start_ts", "end_ts"):
        eps[col] = _local(pd.to_datetime(eps[col], utc=True))
    return ch, daily, eps.rename(columns={"start_ts": "start", "end_ts": "end"})


def routed_inputs(run_id: int | None, date, types: list[str]):
    """Входы отдельных моделей типов (`ml.daily_routing.online`) из представления прогона — строго до даты: их каналы
    целиком, эпизоды остальных каналов — по островам неисправности. События неисправности кэшируются на прогон:
    первый запрос ~15 с, следующие сутки — ~5 с."""
    from ml.daily_routing.online import SequenceCache, inputs

    observed = coverage(run_id)
    with _lock:
        if run_id not in _sequences:
            for k in list(_sequences)[: max(0, len(_sequences) - V3_RUNS + 1)]:  # как у v3: два последних прогона
                del _sequences[k]
            _sequences[run_id] = SequenceCache()
        cache = _sequences[run_id]
    with engine().connect() as conn:
        return inputs(conn, run_id, pd.Timestamp(date), types, observed, settings.timezone, cache)


def external_channels(dataset: str) -> pd.DataFrame:
    """Каналы внешнего набора (docs/architecture/external-data.md): channel_id, object_id — узел,
    measure — имя сигнала (у внешних каналов оно в channels.tag)."""
    sql = text("SELECT id AS channel_id, object_id, tag AS measure FROM channels WHERE source = :dataset")
    with engine().connect() as conn:
        return pd.read_sql(sql, conn, params={"dataset": dataset})


def external_events(run_id: int | None, channel_ids: list[int], since, until) -> pd.DataFrame:
    """События каналов внешнего набора в [since, until) из представления прогона; время — UTC."""
    sql = text(
        "SELECT channel_id, ts, value_raw AS value FROM run_events(:run) "
        "WHERE channel_id = ANY(:ids) AND ts >= :since AND ts < :until"
    )
    params = {"run": run_id, "ids": channel_ids, "since": pd.Timestamp(since), "until": pd.Timestamp(until)}
    with engine().connect() as conn:
        ev = pd.read_sql(sql, conn, params=params)
    ev["ts"] = pd.to_datetime(ev["ts"], utc=True)
    return ev


def external_passes(run_id: int | None, channel_ids: list[int], values: list[str], since, until) -> pd.DataFrame:
    """Проезды (события со значением-пропуском) каналов набора с заданными значениями в [since, until); время — UTC."""
    sql = text(
        "SELECT channel_id, ts, value_raw AS value FROM run_events(:run) "
        "WHERE channel_id = ANY(:ids) AND value_raw = ANY(:values) AND ts >= :since AND ts < :until"
    )
    params = {"run": run_id, "ids": channel_ids, "values": values, "since": pd.Timestamp(since),
              "until": pd.Timestamp(until)}  # fmt: skip
    with engine().connect() as conn:
        ev = pd.read_sql(sql, conn, params=params)
    ev["ts"] = pd.to_datetime(ev["ts"], utc=True)
    return ev


def external_classes(dataset: str, credential_ids: list[str]) -> dict[str, str]:
    """Класс пропуска (тип машины, отдел) из справочника набора — external_credentials.meta.class."""
    sql = text(
        "SELECT credential_id, meta->>'class' AS class FROM external_credentials "
        "WHERE dataset = :dataset AND credential_id = ANY(:ids)"
    )
    with engine().connect() as conn:
        rows = conn.execute(sql, {"dataset": dataset, "ids": credential_ids}).fetchall()
    return {c: k for c, k in rows if k is not None}


def external_permits(dataset: str) -> pd.DataFrame:
    """Допуски набора: пропуск → объект и окно [valid_from, valid_to) (external-data.md §3.3)."""
    sql = text("SELECT credential_id, object_id, valid_from, valid_to FROM external_permits WHERE dataset = :dataset")
    with engine().connect() as conn:
        return pd.read_sql(sql, conn, params={"dataset": dataset})


def external_units(dataset: str) -> pd.DataFrame:
    """Паспорт объектов набора из objects.meta: model, age_years (агрегаты Azure PdM)."""
    sql = text(
        "SELECT id AS object_id, meta->>'model' AS model, (meta->>'age_years')::float AS age_years FROM objects "
        "WHERE source = :dataset AND meta ? 'model'"
    )
    with engine().connect() as conn:
        return pd.read_sql(sql, conn, params={"dataset": dataset})
