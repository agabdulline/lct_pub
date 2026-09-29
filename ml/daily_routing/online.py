"""Отдельные модели типов в сервисе: входы из рабочей базы тем же кодом, что эталон, признаки и оценки (AGENTS.md §5).

Эталон (`ml.daily_routing.reference`) строит входы сборщиками ML-разработчика по журналу:
- суточные итоги и эпизоды «Неисправен» типов вне ежедневной v3 — `ml.each_channel.prepare` (DAILY_SQL, EPISODES_SQL)
  по событиям с 2021 года;
- эпизоды типов ежедневной v3 — `ml.v3_inputs.prepare_episodes.reconstruct` по событиям с 2019 года.

Здесь те же функции получают события представления прогона — база до его старта и события прогона (`view_parts`), из
`events` и `events_raw` (движение хранится только сырым потоком), всё — строго до даты решения T:
- свои каналы моделей (ИБП и ручной извещатель: ~210 каналов, ~170 тыс. событий с 2021 года) — все события;
- эпизоды остальных каналов нужны признакам объекта, группы тегов и родителя. Все их события за 400 суток не читаются.
  Эпизод — серия «Неисправен» внутри острова неисправности рабочей базы (`fault_episodes`: подряд «Неисправен» и
  «Отключено устройство»). Читаются события этих двух значений и события в ту же секунду, что они. Остальное дают
  границы островов: событие перед островом — «предыдущее значение есть», конец острова — первое другое значение.
  Из этой последовательности его код строит те же эпизоды, что из полного журнала. Исключение — движение вне окна
  сырого потока (90 суток): его событий в базе нет, эпизод = остров.

Модели:
- ансамбль ИБП: 0.75 × old + 0.25 × mean_3d(new); mean_3d — по двум предыдущим оценённым суткам канала (до 120
  назад);
- ручной извещатель: календарь, порог месяца — по решениям [M − 120 сут, M − 30 сут) с созревшей меткой, тревоги —
  при готовности (1 500 положительных в окне), как `ml.each_channel.hard_channel_study.monthly_scores`.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
from catboost import CatBoostClassifier
from sqlalchemy import text

from ml.each_channel.features import build_features, normalize_inputs
from ml.each_channel.prepare import EPISODES_SQL, daily_and_episodes
from ml.v3.src.baseline_core import SENSOR_FAMILIES
from ml.v3_inputs.prepare_episodes import reconstruct

MODELS = Path(__file__).resolve().parents[1] / "models" / "each-channel"
FAULT_VALUES = ["Неисправен", "Отключено устройство"]  # значения островов рабочей базы (загрузчик, приём)
V3_START = pd.Timestamp("2019-01-01")  # журналы набора ежедневной v3
EACH_START = pd.Timestamp("2021-01-01")  # журналы сборщика отдельных моделей
HISTORY_DAYS = 400  # окна признаков: сетка build_features и эпизоды build_daily — 400 суток до T
LOOKBACK_DAYS = 120  # mean_3d: предыдущие оценённые сутки канала ищутся не дальше (при 30 — расхождения в январе)
MATURITY_DAYS = 30  # порог извещателя: метка решения созревает через 30 суток (окно решений — из config)
_EPS = pd.Timedelta(microseconds=1)


def _local(ts: pd.Series, tz: str) -> pd.Series:
    return pd.to_datetime(ts, utc=True).dt.tz_convert(tz).dt.tz_localize(None).astype("datetime64[ns]")


def view_parts(conn, run_id) -> tuple[list[str], dict]:
    """Части представления прогона (`run_events(p)`, миграция 0010) с постоянными границами: база до старта прогона и
    события самого прогона. Через функцию представления TimescaleDB не отсекает чанки при планировании: выборка по
    значению за 400 суток — 17 с вместо 2, события той же секунды — 61 с вместо 3."""
    if run_id is None:
        return ["run_id IS NULL"], {}
    row = conn.execute(
        text("SELECT sim_start, run_base(:vrun) AS base FROM runs WHERE id = :vrun"), {"vrun": run_id}
    ).one_or_none()
    if row is None:  # прогона нет — представление отдаёт всю базу
        return ["run_id IS NULL"], {}
    base = "run_id IS NULL" if row.base is None else "run_id = :vbase"
    return [f"{base} AND ts < :vstart", "run_id = :vrun"], {"vstart": row.sim_start, "vbase": row.base, "vrun": run_id}


def _events(conn, run_id, sql_filter: str, params: dict, tz: str, raw: bool = True) -> pd.DataFrame:
    """События представления прогона (значимые и сырой поток) — в раскладке его сборщика."""
    parts, view = view_parts(conn, run_id)
    frames = []
    for table in ("events", "events_raw") if raw else ("events",):
        for part in parts:
            frames.append(
                pd.read_sql(
                    text(
                        f"SELECT channel_id AS sensor_id, event_id, ts, is_alarm AS alarm, value_raw AS value "
                        f"FROM {table} WHERE ({part}) AND {sql_filter}"
                    ),
                    conn,
                    params={**view, **params},
                )
            )
    ev = pd.concat(frames, ignore_index=True)
    ev["ts"] = _local(ev.ts, tz)
    ev["sensor_id"] = ev.sensor_id.astype("int64")
    ev["event_id"] = ev.event_id.astype("Int64")
    return ev.drop_duplicates(["sensor_id", "event_id", "ts", "value"], ignore_index=True)


# ---------- входы


def catalog(conn) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Справочники хакатона в раскладке CSV организаторов: каналы (как `channels.csv` сборщика) и объекты."""
    channels = pd.read_sql(
        text(
            "SELECT id AS ид_канала_данных, object_id AS ид_объект, sensor_type AS тип_датчика, "
            "tag AS тег_инженерной_системы, name AS название_датчика FROM channels WHERE source = 'hackathon' "
            "ORDER BY id"
        ),
        conn,
    )
    objects = pd.read_sql(
        text(
            "SELECT id AS object_id, level, parent_id, kind AS object_kind FROM objects "
            "WHERE source = 'hackathon' ORDER BY id"
        ),
        conn,
    )
    return channels, objects


def own_inputs(conn, run_id, channel_ids: list[int], until: pd.Timestamp, tz: str):
    """Суточные итоги и эпизоды своих каналов — его SQL по всем их событиям с 2021 года до T."""
    ev = _events(
        conn,
        run_id,
        "channel_id = ANY(:ids) AND ts >= :lo AND ts < :until",
        {"ids": channel_ids, "lo": EACH_START.tz_localize(tz), "until": until.tz_localize(tz)},
        tz,
        raw=False,
    )
    con = duckdb.connect()
    con.register("events", ev)
    daily, episodes = daily_and_episodes(con)
    con.close()
    return daily, episodes


@dataclass
class SequenceCache:
    """Прочитанные события неисправности и той же секунды — на прогон: следующие сутки дочитывают только новое.

    События до `until` уже не меняются (база до старта прогона постоянна, события прогона приходят по порядку;
    последняя секунда перечитывается). `lo` — с какого времени прочитаны события всех каналов, `early` — каналы,
    у которых прочитаны и более ранние события (острова, начатые до окна), и с какого времени."""

    until: pd.Timestamp | None = None
    lo: pd.Timestamp | None = None
    early: dict[int, pd.Timestamp] = field(default_factory=dict)
    ev: pd.DataFrame | None = None


def _faults_with_neighbours(conn, run_id, lo, until, tz: str, ids: list[int] | None = None) -> pd.DataFrame:
    """События значений неисправности в [lo, until) (у каналов ids или у всех) и события в те же секунды."""
    where = "ts >= :flo AND ts < :fhi AND value_raw = ANY(:faults)"
    params = {"flo": pd.Timestamp(lo).tz_localize(tz), "fhi": pd.Timestamp(until).tz_localize(tz)}
    params["faults"] = FAULT_VALUES
    if ids is not None:
        where += " AND channel_id = ANY(:ids)"
        params["ids"] = ids
    ev = _events(conn, run_id, where, params, tz)
    return pd.concat([ev, same_second(conn, run_id, ev, lo, until, tz, ids)], ignore_index=True)


def fault_sequence(
    conn, run_id, since: pd.Timestamp, until: pd.Timestamp, tz: str, cache: SequenceCache | None = None
) -> pd.DataFrame:
    """События, из которых его код строит эпизоды «Неисправен» всех каналов, пересекающие [since, until).

    Настоящие: значения «Неисправен» / «Отключено устройство» и события в ту же секунду. Добавленные по островам
    рабочей базы: «до» — за 1 мкс до начала первого острова канала (если это не первое событие канала), «после» — в
    конце острова (если в эту секунду события другого значения нет), начало острова — если его событий в базе нет
    (движение вне окна сырого потока). cache — прочитанное прошлыми запросами того же прогона."""
    loc = {"lo": since.tz_localize(tz), "until": until.tz_localize(tz)}
    islands = pd.read_sql(
        text(
            "SELECT channel_id, start_ts, end_ts, state FROM run_fault_episodes(:run) "
            "WHERE start_ts < :until AND (end_ts IS NULL OR end_ts >= :lo)"
        ),
        conn,
        params={"run": run_id, **loc},
    )
    islands["start"], islands["end"] = _local(islands.start_ts, tz), _local(islands.end_ts, tz)
    islands.loc[islands.end >= until, "end"] = pd.NaT  # на момент T эпизод не закончен
    cache = cache if cache is not None else SequenceCache()
    if cache.ev is None or cache.lo > since or cache.until > until:  # первый запрос или назад во времени
        cache.until, cache.lo, cache.early, cache.ev = since, since, {}, None
    fresh = _faults_with_neighbours(conn, run_id, cache.until - pd.Timedelta(seconds=1), until, tz)
    parts = [fresh] if cache.ev is None else [cache.ev, fresh]
    # острова, начатые до окна: их события — по каналам, с начала острова (если ещё не прочитаны)
    early = islands[islands.start < cache.lo].groupby("channel_id").start.min()
    need = early[[cache.early.get(c, cache.lo) > t for c, t in early.items()]]
    if len(need):
        parts.append(_faults_with_neighbours(conn, run_id, need.min(), cache.lo, tz, need.index.tolist()))
        cache.early |= need.to_dict()
    cache.ev = pd.concat(parts, ignore_index=True).drop_duplicates(
        ["sensor_id", "event_id", "ts", "value"], ignore_index=True
    )
    cache.until = until
    # в последовательность — только окно и острова, начатые до него: старые серии без своих островов не нужны
    ev = cache.ev
    from_ = islands[islands.start < since].groupby("channel_id").start.min().reindex(ev.sensor_id).to_numpy()
    ev = ev[(ev.ts >= since).to_numpy() | (ev.ts.to_numpy() >= from_)]
    return assemble(ev, islands, first_events(conn, run_id, islands, since, tz))


def assemble(ev: pd.DataFrame, islands: pd.DataFrame, first: dict[int, tuple]) -> pd.DataFrame:
    """Последовательность для его кода: события неисправности и той же секунды + добавленные по островам (см.
    fault_sequence). islands — channel_id, start, end (NaT — не закончен на T), state; first — первое событие канала."""
    ev = ev.drop_duplicates(["sensor_id", "event_id", "ts", "value"], ignore_index=True)
    at = set(zip(ev.sensor_id, ev.ts, strict=True))
    other = ev[~ev.value.isin(FAULT_VALUES)]
    at_other = set(zip(other.sensor_id, other.ts, strict=True))
    add = []
    # «до» — только у первого острова канала: у следующих предыдущее значение — конец прошлого острова, а метка за 1 мкс
    # до начала встала бы раньше этого конца, если острова в одной секунде
    head = ~islands.sort_values(["channel_id", "start"]).duplicated("channel_id").sort_index()
    for r, is_head in zip(islands.itertuples(index=False), head, strict=True):
        if is_head and first.get(r.channel_id) != (r.start, True):
            add.append((r.channel_id, -1, r.start - _EPS, "__before__"))
        if (r.channel_id, r.start) not in at:
            add.append((r.channel_id, -1, r.start, r.state))
        if not pd.isna(r.end) and (r.channel_id, r.end) not in at_other:
            add.append((r.channel_id, -1, r.end, "__after__"))
    synthetic = pd.DataFrame(add, columns=["sensor_id", "event_id", "ts", "value"])
    synthetic = synthetic.astype({"sensor_id": "int64", "event_id": "Int64", "ts": "datetime64[ns]"})
    seq = pd.concat([ev.drop(columns="alarm", errors="ignore"), synthetic], ignore_index=True)
    return seq.drop_duplicates(["sensor_id", "event_id", "ts", "value"], ignore_index=True)


def same_second(conn, run_id, ev: pd.DataFrame, lo, until, tz: str, ids: list[int] | None = None) -> pd.DataFrame:
    """События других значений в те же секунды, что события ev, — порядок внутри секунды решает, где серия."""
    keys = ev[["sensor_id", "ts"]].drop_duplicates()
    if keys.empty:
        return ev.iloc[:0]
    where = (
        "(channel_id, ts) IN (SELECT * FROM unnest(CAST(:kc AS int[]), CAST(:kt AS timestamptz[]))) "
        "AND ts >= :klo AND ts < :khi AND value_raw <> ALL(:faults)"
    )
    params = {
        "kc": keys.sensor_id.tolist(),
        "kt": [t.tz_localize(tz) for t in keys.ts],
        "klo": pd.Timestamp(lo).tz_localize(tz),
        "khi": pd.Timestamp(until).tz_localize(tz),
        "faults": FAULT_VALUES,
    }
    if ids is not None:
        where += " AND channel_id = ANY(:ids)"
        params["ids"] = ids
    return _events(conn, run_id, where, params, tz)


def first_events(conn, run_id, islands: pd.DataFrame, since: pd.Timestamp, tz: str) -> dict[int, tuple]:
    """Первое событие канала в данных сборщика (v3 — с 2019, остальные типы — с 2021): (время, значение неисправности
    ли). У острова, который с него начинается, предыдущего значения нет. Ищется только у каналов, чьи данные начинаются
    не раньше since."""
    if islands.empty:
        return {}
    days = pd.read_sql(
        text(
            "SELECT d.channel_id, c.sensor_type, min(d.date) AS v3, min(d.date) FILTER (WHERE d.date >= :each) AS each "
            "FROM run_channel_daily(:run) d JOIN channels c ON c.id = d.channel_id "
            "WHERE d.date >= :v3start AND d.channel_id = ANY(:ids) GROUP BY d.channel_id, c.sensor_type"
        ),
        conn,
        params={
            "run": run_id,
            "each": EACH_START.date(),
            "v3start": V3_START.date(),
            "ids": islands.channel_id.unique().tolist(),
        },
    )
    days["first"] = pd.to_datetime(np.where(days.sensor_type.isin(list(SENSOR_FAMILIES)), days.v3, days.each))
    fresh = days[days["first"] >= since - pd.Timedelta(days=1)].set_index("channel_id")["first"]
    # важно только для острова, который начинается в день первого события канала, — таких единицы
    starts = islands.assign(first=islands.channel_id.map(fresh))
    fresh = starts.loc[starts.start.dt.normalize().eq(starts["first"]), ["channel_id", "first"]].drop_duplicates()
    out = {}
    for r in fresh.itertuples(index=False):
        day = _events(
            conn,
            run_id,
            "channel_id = :c AND ts >= :lo AND ts < :hi",
            {
                "c": int(r.channel_id),
                "lo": r.first.tz_localize(tz),
                "hi": (r.first + pd.Timedelta(days=1)).tz_localize(tz),
            },
            tz,
        )
        if len(day):
            head = day.sort_values(["ts", "event_id", "value"]).iloc[0]
            out[int(r.channel_id)] = (head.ts, head.value in FAULT_VALUES)
    return out


def his_episodes(seq: pd.DataFrame, channels: pd.DataFrame, observed: pd.DatetimeIndex) -> pd.DataFrame:
    """Эпизоды его кодом: типы ежедневной v3 — reconstruct (v3_inputs), остальные — EPISODES_SQL (each_channel)."""
    v3_ids = set(channels.loc[channels["тип_датчика"].isin(list(SENSOR_FAMILIES)), "ид_канала_данных"])
    in_v3 = seq.sensor_id.isin(v3_ids)
    boundary = pd.DataFrame(
        {c: pd.Series(dtype=t) for c, t in (("sensor_id", "int64"), ("date", "datetime64[ns]"))}
        | {f"{p}_{c}": pd.Series(dtype=t) for p in ("first", "last")
           for c, t in (("event_id", "Int64"), ("ts", "datetime64[ns]"), ("value", "object"))}
    )  # fmt: skip
    v3_eps, _ = reconstruct(boundary, seq[in_v3], observed)
    con = duckdb.connect()
    con.register("events", seq[~in_v3])
    each_eps = con.execute(EPISODES_SQL.format(events="events")).df()
    con.close()
    cols = ["sensor_id", "start", "end", "start_is_observed", "coverage_uncertain"]
    eps = pd.concat([v3_eps[cols], each_eps[cols]], ignore_index=True)
    for c in ("start", "end"):
        eps[c] = pd.to_datetime(eps[c]).astype("datetime64[ns]")
    return eps.sort_values(["sensor_id", "start"], kind="stable", ignore_index=True)


@dataclass
class Inputs:
    """Входы признаков на дату T — в раскладке `ml.each_channel.features.load_inputs`."""

    channels: pd.DataFrame
    daily: pd.DataFrame
    episodes: pd.DataFrame
    objects: pd.DataFrame
    until: pd.Timestamp
    own: list[int] = field(default_factory=list)


def inputs(conn, run_id, until, own_types: list[str], observed, tz: str, cache: SequenceCache | None = None) -> Inputs:
    """Всё, что нужно признакам отдельных моделей на дату until (строго до неё); cache — на прогон (SequenceCache)."""
    until = pd.Timestamp(until).normalize()
    raw_channels, objects = catalog(conn)
    own = raw_channels.loc[raw_channels["тип_датчика"].isin(own_types), "ид_канала_данных"].tolist()
    daily, own_eps = own_inputs(conn, run_id, own, until, tz)
    since = until - pd.Timedelta(days=HISTORY_DAYS)
    seq = fault_sequence(conn, run_id, since, until, tz, cache)
    ctx = his_episodes(seq[~seq.sensor_id.isin(own)], raw_channels, pd.DatetimeIndex(sorted(observed)))
    own_eps = own_eps.astype({"start": "datetime64[ns]", "end": "datetime64[ns]"})
    episodes = pd.concat([ctx, own_eps[ctx.columns]], ignore_index=True)
    channels, daily, episodes = normalize_inputs(raw_channels, daily, episodes)
    return Inputs(channels, daily, episodes, objects, until, own)


# ---------- модели


def load_cbm(folder: Path, artifact: str, sha: str, features: list[str]) -> CatBoostClassifier:
    import hashlib

    path = folder / artifact
    if hashlib.sha256(path.read_bytes()).hexdigest() != sha:
        raise ValueError(f"SHA256 не совпал: {path}")
    model = CatBoostClassifier().load_model(str(path))
    if model.feature_names_ != features:
        raise ValueError(f"Признаки модели не совпали с конфигом: {path}")
    return model


class Ensemble:
    """Ансамбль ИБП: `ml/models/each-channel/ups/config.json`."""

    def __init__(self, folder: Path = MODELS / "ups"):
        self.config = json.loads((folder / "config.json").read_text(encoding="utf-8"))
        self.parts = []
        for comp in self.config["components"]:
            cfg = json.loads((folder / comp["config"]).read_text(encoding="utf-8"))
            model = load_cbm(folder, comp["path"], comp["sha256"], cfg["features"])
            self.parts.append((comp, cfg, model))
        self.type = self.config["type"]
        self.threshold = self.config["threshold"]

    def score(self, x: Inputs, only: list[int] | None = None, key=None) -> pd.DataFrame:
        """Решения на дату x.until: channel_id, status, score, alert, признаки (для сверки). key — не нужен:
        порог ансамбля постоянный."""
        from ml.each_channel.six_channel_study import add_hierarchy_features, transform_scores

        ids = x.channels.loc[x.channels.sensor_type.eq(self.type), "channel_id"].tolist()
        if only is not None:
            ids = [c for c in ids if c in set(only)]
        dates = pd.date_range(x.until - pd.Timedelta(days=LOOKBACK_DAYS), x.until)
        f = build_features(x.daily, x.episodes, x.channels, dates, ids, labels=False)
        f = add_hierarchy_features(f, x.episodes, x.channels, x.objects)
        scored = f[f.status.eq("scored")].sort_values(["date", "channel_id"], kind="stable").copy()
        scored["score"] = 0.0
        for comp, cfg, model in self.parts:
            raw = model.predict_proba(scored[cfg["features"]])[:, 1] if len(scored) else np.array([])
            value = transform_scores(scored, raw, "mean_3d") if comp.get("postprocess") == "mean_3d" else raw
            scored[f"score_{cfg['version']}"] = value
            scored["score"] += comp["weight"] * value
        cols = ["channel_id", "score", *[f"score_{cfg['version']}" for _, cfg, _ in self.parts]]
        out = f[f.date.eq(x.until)].merge(scored.loc[scored.date.eq(x.until), cols], on="channel_id", how="left")
        out["threshold"] = self.threshold
        out["alert"] = out.score.ge(self.threshold)
        return out


class ManualAlarm:
    """Ручной извещатель: `ml/models/each-channel/manual-alarm/config.json`, порог месяца — причинный."""

    def __init__(self, folder: Path = MODELS / "manual-alarm"):
        from ml.each_channel.calendar_study import calendar_features

        self.config = json.loads((folder / "config.json").read_text(encoding="utf-8"))
        cfg = self.config
        self.model = load_cbm(folder, cfg["artifact"], cfg["sha256"], cfg["features"])
        self.calendar, sources = calendar_features(folder / "calendar")
        self.calendar_sha = {s["url"]: s["sha256"] for s in sources}
        self.type = cfg["type"]
        self.window = cfg["threshold_policy"]["calibration_window_days"]
        self.ready_minimum = cfg["readiness"]["minimum_matured_positive_rows"]
        self._months: dict[tuple, dict] = {}

    def _frame(self, x: Inputs, dates, ids, labels: bool) -> pd.DataFrame:
        f = build_features(x.daily, x.episodes, x.channels, dates, ids, labels=labels)
        f = f.sort_values(["date", "channel_id"], kind="stable")
        f = f.merge(self.calendar, on="date", how="left", validate="many_to_one")
        scored = f.status.eq("scored")
        f["score"] = np.nan
        if scored.any():
            f.loc[scored, "score"] = self.model.predict_proba(f.loc[scored, self.config["features"]])[:, 1]
        return f

    def month(self, x: Inputs, key) -> dict:
        """Порог и готовность месяца T: решения [M − 120 сут, M − 30 сут) с созревшей меткой его сборщика."""
        from ml.each_channel.hard_channel_study import matured_calibration
        from ml.each_channel.train import choose_threshold

        start = x.until.to_period("M").start_time
        cache_key = (key, start)
        if cache_key not in self._months:
            ids = x.channels.loc[x.channels.sensor_type.eq(self.type), "channel_id"].tolist()
            lo = start - pd.Timedelta(days=MATURITY_DAYS + self.window)
            dates = pd.date_range(lo, start - pd.Timedelta(days=MATURITY_DAYS + 1))
            f = self._frame(x, dates, ids, labels=True)
            f = f[f.status.eq("scored") & f.target.notna()]
            cal = matured_calibration(f, start, self.window)
            positives = int(cal.target.sum())
            self._months[cache_key] = {
                "month": str(start.date()),
                "threshold": choose_threshold(cal.target, cal.score),
                "matured_positives": positives,
                "ready": positives >= self.ready_minimum,
            }
        return self._months[cache_key]

    def score(self, x: Inputs, only: list[int] | None = None, key=None) -> pd.DataFrame:
        ids = x.channels.loc[x.channels.sensor_type.eq(self.type), "channel_id"].tolist()
        if only is not None:
            ids = [c for c in ids if c in set(only)]
        month = self.month(x, key)
        out = self._frame(x, [x.until], ids, labels=False)
        out["threshold"], out["ready"] = month["threshold"], month["ready"]
        out["alert"] = out.ready & out.score.ge(out.threshold)
        return out


ADAPTERS = {"ensemble": Ensemble, "manual_alarm": ManualAlarm}  # адаптеры daily_routing.json → класс
