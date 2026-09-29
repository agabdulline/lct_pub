"""Решения событийной v3 по дымовым тревогам — код ML-разработчика как есть (`ml/v3/src`, PR #114).

Их код считает признаки по всей истории сразу (`build_alarm_rows` + `build_extended`). Здесь то же для пачки тревог
одного объекта, без пересчёта лишнего: все признаки v3 живут внутри объекта (соседи, шлейф, пикеты, история решений —
по `object_key`), поэтому достаточно истории объекта.

- `build_alarm_rows` — по всей истории объекта: решения и их исходы нужны признакам истории (до 10 лет) и цепочке
  «одно решение на канал за 24 ч». Каналы независимы друг от друга, поэтому `AlarmRows` пересчитывает только каналы
  с новыми событиями или с решением, окно исхода которого ещё не закрылось;
- признаки соседей и своей последовательности — только для решений тревог и прошлых решений тех же каналов
  за PEER_DAYS: столько смотрят `past_*_isolated_rate_*` (до 90 суток). Изолированность прошлого решения зависит от
  доступности соседей за 90 суток до него, поэтому события соседей нужны ещё на PEER_DAYS раньше;
- `add_history` — с историей всех решений объекта: исход прошлого решения виден, только когда закрылось его окно 24 ч.

Совпадение с полным расчётом их кода проверяют `ml-service/tests/test_v3.py` (срез объектов) и `ml.v3_bridge.offline`
(все тревоги I полугодия 2026 по порядку, пачками, как их присылает worker). Входы: события дымовых каналов в формате
v3 (sensor_id, event_id, ts — наивное московское, value), дни с данными журнала (`coverage`), справочник в колонках
исходного CSV. Решение по тревоге — в момент T = время тревоги + 1 с, события в T и позже не видны.
"""

import threading

import pandas as pd

from ml.v3.src.event_model import build_alarm_rows
from ml.v3.src.neighbors import add_history, add_own_sequence, add_peer_features, attach_metadata, metadata

SECOND = pd.Timedelta(seconds=1)
DAY = pd.Timedelta(days=1)
PEER_DAYS = pd.Timedelta(days=91)  # past_*_isolated_rate — до 90 суток, доступность соседа — 90 суток; плюс запас
HISTORY_START = "2022-01-01"  # события v3 — с 2022 года (smoke_events_2022_2026)
FAULT_WASHOUT_HOURS = 24  # их отгруженные решения — alarm_rows_washout24
SMOKE = "Обнаружен дым"
FAULT = "Неисправен"
ALLOWED = ("Норма", "Дыма нет", SMOKE)  # состояние канала перед решением (их правило допустимости)
KEY = ["sensor_id", "decision_time"]
NOT_FEATURES = {"decision_time", "sensor_id", "target", "next_onset", "lead_hours"}

# справочник сервиса (таблица channels) → колонки исходного CSV, которые читает их metadata()
CATALOG_COLUMNS = {
    "channel_id": "ид_канала_данных",
    "system_type": "тип_инж_системы",
    "sensor_type": "тип_датчика",
    "tag": "тег_инженерной_системы",
    "name": "название_датчика",
    "object_id": "ид_объект",
}


def registry(channels: pd.DataFrame) -> pd.DataFrame:
    """Справочник v3 (их metadata) из справочника сервиса. Соседи v3 — внутри объекта, так что хватает каналов
    объекта: группы объекта, шлейфа и пикетов получаются те же, что по всему справочнику."""
    raw = channels.rename(columns=CATALOG_COLUMNS)[list(CATALOG_COLUMNS.values())].copy()
    raw["ид_канала_данных"] = raw["ид_канала_данных"].astype("int64")
    raw["ид_объект"] = raw["ид_объект"].astype("int64")  # object_key — str(int), как у целого столбца в их CSV
    return metadata(raw)


def frame(events: pd.DataFrame) -> pd.DataFrame:
    """События сервиса (channel_id, event_id, ts, value) → формат v3 (sensor_id, event_id, ts, value)."""
    out = events.rename(columns={"channel_id": "sensor_id"})[["sensor_id", "event_id", "ts", "value"]].copy()
    out["sensor_id"] = out["sensor_id"].astype("int64")
    out["event_id"] = out["event_id"].astype("int64")
    return out


def day_number(ts: pd.Timestamp) -> int:
    return int(pd.Timestamp(ts).to_datetime64().astype("datetime64[D]").astype("int64"))


def days(coverage) -> set[int]:
    """Дни с данными журнала как номера суток — так их сравнивает build_alarm_rows."""
    return set(pd.DatetimeIndex(coverage).to_numpy().astype("datetime64[D]").astype("int64").tolist())


def alarm_rows(events: pd.DataFrame, coverage, until: pd.Timestamp) -> pd.DataFrame:
    """Все решения их кода по событиям до until (не включая); исходы — только по фактам до until."""
    ev = events[events["ts"] < until]
    return build_alarm_rows(
        ev, pd.DatetimeIndex(coverage), label_end_exclusive=until, fault_washout_hours=FAULT_WASHOUT_HOURS
    )


class AlarmRows:
    """Решения `build_alarm_rows` по каналам одного объекта с пересчётом только изменившихся каналов.

    Решения канала зависят только от его событий и дней покрытия. Посчитанное до момента L верно и для более
    позднего until, если у канала не было событий в [L, until), не изменились его события до L и ни одно его решение
    не закрыло окно исхода в (L, until]: исход решения их код ставит, только когда окно закрылось до label_end, до
    этого — NaN. Иначе канал пересчитывается целиком. Запрос назад во времени или новые дни покрытия до L — полный
    пересчёт.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.rows: pd.DataFrame | None = None
        self.until: pd.Timestamp | None = None
        self.counts: pd.Series | None = None  # событий до until по каналам
        self.covered: set[int] = set()  # дни покрытия до дня until включительно
        self.recomputed = 0  # сколько каналов пересчитано последним вызовом — для замеров

    def get(self, events: pd.DataFrame, coverage, until: pd.Timestamp) -> pd.DataFrame:
        with self.lock:
            ev = events[events["ts"] < until]
            cov = days(coverage)
            full = (
                self.rows is None
                or until < self.until
                or {d for d in cov if d <= day_number(self.until)} != self.covered
            )
            if full:
                rows = alarm_rows(ev, coverage, until)
                self.recomputed = int(ev["sensor_id"].nunique())
            else:
                before = ev[ev["ts"] < self.until].groupby("sensor_id").size()
                idx = before.index.union(self.counts.index)
                moved = before.reindex(idx, fill_value=0) != self.counts.reindex(idx, fill_value=0)
                fresh = set(ev.loc[ev["ts"] >= self.until, "sensor_id"])
                closing = set()
                if len(self.rows):  # без решений их код отдаёт пустой кадр с колонками object
                    closes = self.rows["decision_time"] + DAY
                    closing = set(self.rows.loc[(closes > self.until) & (closes <= until), "sensor_id"])
                changed = set(idx[moved.to_numpy()]) | fresh | closing
                kept = self.rows[~self.rows["sensor_id"].isin(changed)]
                part = alarm_rows(ev[ev["sensor_id"].isin(changed)], coverage, until) if changed else kept.iloc[:0]
                parts = [p for p in (kept, part) if len(p)]
                # тот же порядок, что у build_alarm_rows: по времени решения, затем по каналу
                rows = (
                    pd.concat(parts, ignore_index=True)
                    .sort_values(["decision_time", "sensor_id"])
                    .reset_index(drop=True)
                    if parts
                    else kept.iloc[:0]
                )
                self.recomputed = len(changed)
            self.rows, self.until = rows, until
            self.counts = ev.groupby("sensor_id").size()
            self.covered = {d for d in cov if d <= day_number(until)}
            return rows


def decide(
    events: pd.DataFrame,
    reg: pd.DataFrame,
    alarms: list[tuple[int, pd.Timestamp]],
    rows: pd.DataFrame,
) -> pd.DataFrame:
    """Признаки v3 по тревогам alarms [(канал, время тревоги)] одного объекта.

    rows — все решения объекта до max(alarms) + 1 с (`alarm_rows` или `AlarmRows.get`). Возвращает строки допустимых
    решений: sensor_id, alarm_ts, decision_time и признаки v3. Тревоги без решения (не прошли правила допустимости v3)
    в ответ не попадают — причину даёт `skip_reason`.
    """
    until = max(ts for _, ts in alarms) + SECOND
    ev = events[events["ts"] < until]
    want = pd.DataFrame([(int(c), ts + SECOND) for c, ts in alarms], columns=KEY).drop_duplicates()
    mine = rows.merge(want, on=KEY) if len(rows) else rows
    if mine.empty:
        return pd.DataFrame(columns=[*KEY, "alarm_ts"])
    lo = mine["decision_time"].min() - PEER_DAYS
    context = rows[
        rows["sensor_id"].isin(mine["sensor_id"])
        & (rows["decision_time"] >= lo)
        & (rows["decision_time"] <= mine["decision_time"].max())
    ]
    f = add_peer_features(context, ev[ev["ts"] >= lo - PEER_DAYS], reg)
    f = add_own_sequence(f, ev[ev["sensor_id"].isin(context["sensor_id"])])
    f = add_history(f, history=attach_metadata(rows, reg))
    f = f.merge(want, on=KEY)
    # как в их build_extended: все числовые — float32; copy() собирает ~230 колонок в общие блоки — без него
    # добавление alarm_ts на раздробленном кадре даёт PerformanceWarning pandas
    f = f.astype({c: "float32" for c in f.columns if c not in NOT_FEATURES and f[c].dtype.kind in "fiu"}).copy()
    return f.assign(alarm_ts=f["decision_time"] - SECOND)


def skip_reason(events: pd.DataFrame, coverage, sensor_id: int, alarm_ts: pd.Timestamp) -> str:
    """Почему по тревоге нет решения v3 — правила их build_alarm_rows по порядку. Словарь причин — как у v0 плюс
    `coverage_gap` (за 8 суток до решения есть день без данных журнала) и `state_not_normal` (последнее состояние канала
    перед решением — не «Норма», «Дыма нет» или «Обнаружен дым»)."""
    alarm_ts = pd.Timestamp(alarm_ts)
    own = events[events["sensor_id"] == sensor_id]
    own = own.drop_duplicates(["sensor_id", "event_id", "ts", "value"]).sort_values(["ts", "event_id"], kind="stable")
    t = alarm_ts + SECOND
    ts, values = own["ts"], own["value"]
    if not ((ts == alarm_ts) & (values == SMOKE)).any():
        return "alarm_not_found"
    covered = days(coverage)
    if not all(day_number(t) - k in covered for k in range(8)):
        return "coverage_gap"
    if t - ts.iloc[0] < pd.Timedelta(days=7):
        return "history_lt_7d"
    if ((ts == alarm_ts) & (values == FAULT)).any():
        return "fault_same_second"
    before = own[ts < t]
    if before["value"].iloc[-1] not in ALLOWED:
        return "state_not_normal"
    faults = before.loc[before["value"] == FAULT, "ts"]
    if len(faults) and t - faults.iloc[-1] <= pd.Timedelta(hours=FAULT_WASHOUT_HOURS):
        return "fault_recent_24h"
    return "decided_within_24h"
