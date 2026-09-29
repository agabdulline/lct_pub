"""Синтетический журнал для теста v3 — данных хакатона в тестах нет.

Два объекта, полгода 2025 года. Объект 1 — 12 дымовых каналов на двух шлейфах (теги `1-1.1.1.k.` и `1-1.1.2.k.`)
с пикетами ПК10…ПК21, у одного название без пикета; объект 2 — 4 канала, появились в журнале 1 февраля. Каждый день
«Норма»; случайные тревоги, часть — с «Неисправен» через часы; плановые проверки объекта 1 (тревоги всех каналов за
пару минут); «Неопределен». Каждая причина пропуска v3 — хотя бы раз: ранняя тревога нового канала, неисправность
в ту же секунду, «Отключен» в ту же секунду после тревоги, тревога через 3 ч после неисправности. 10.03 — день без
данных журнала (`coverage_gap`). Генератор детерминирован (seed).
"""

import numpy as np
import pandas as pd

START, END = pd.Timestamp("2025-01-01"), pd.Timestamp("2025-07-01")
GAP = pd.Timestamp("2025-03-10")
NEW_OBJECT = pd.Timestamp("2025-02-01")  # каналы объекта 2 появились в журнале позже
SMOKE, FAULT, NORMAL = "Обнаружен дым", "Неисправен", "Норма"


def channels() -> pd.DataFrame:
    """Справочник в колонках таблицы channels."""
    rows = []
    for k in range(12):
        loop, pos = divmod(k, 6)
        name = f"ДД ПК{10 + k}" if k != 11 else "ДД венткамера"
        rows.append((101 + k, 1, f"1-1.1.{loop + 1}.{pos + 1}.", name, 10 + k if k != 11 else None))
    rows += [(201 + k, 2, f"2-1.1.1.{k + 1}.", f"ДД ПК{5 + 2 * k}", 5 + 2 * k) for k in range(4)]
    ch = pd.DataFrame(rows, columns=["channel_id", "object_id", "tag", "name", "picket_number"])
    return ch.assign(system_type="Пожарная охрана", sensor_type="Датчик дыма")


def events(seed: int = 7) -> pd.DataFrame:
    """События (channel_id, event_id, ts — наивное московское, value), отсортированы как в сервисе."""
    rng = np.random.default_rng(seed)
    ch = channels()
    rows = []
    days = pd.date_range(START, END - pd.Timedelta(days=1))
    for c in ch.channel_id:
        for d in days:
            if d == GAP or (c > 200 and d < NEW_OBJECT):
                continue
            rows.append((c, d + pd.Timedelta(hours=6, seconds=int(rng.integers(0, 3600))), NORMAL))
            if rng.random() < 0.04:  # тревога; у трети — неисправность через 1–20 ч, потом норма
                t = d + pd.Timedelta(hours=int(rng.integers(8, 20)), seconds=int(rng.integers(0, 3600)))
                rows.append((c, t, SMOKE))
                if rng.random() < 0.35:
                    f = t + pd.Timedelta(hours=float(rng.uniform(0.2, 20)))
                    rows += [(c, f.floor("s"), FAULT), (c, (f + pd.Timedelta(hours=2)).floor("s"), NORMAL)]
                elif rng.random() < 0.3:  # повторная тревога в те же сутки
                    rows.append((c, t + pd.Timedelta(minutes=int(rng.integers(5, 300))), SMOKE))
            if rng.random() < 0.01:
                rows.append((c, d + pd.Timedelta(hours=23), "Неопределен"))
    for d in pd.date_range(START + pd.Timedelta(days=20), END, freq="21D"):  # плановые проверки объекта 1
        t0 = d + pd.Timedelta(hours=10)
        for k, c in enumerate(range(101, 113)):
            rows.append((c, t0 + pd.Timedelta(seconds=7 * k), SMOKE))
            rows.append((c, t0 + pd.Timedelta(seconds=7 * k + 120), NORMAL))
    at = pd.Timestamp
    rows += [(201, at("2025-02-03 10:00:00"), SMOKE)]  # history_lt_7d: канал в журнале третий день
    rows += [(103, at("2025-05-20 12:00:00"), SMOKE), (103, at("2025-05-20 12:00:00"), FAULT)]  # fault_same_second
    rows += [(104, at("2025-06-02 11:00:00"), SMOKE), (104, at("2025-06-02 11:00:00"), "Отключен")]  # state_not_normal
    rows += [(105, at("2025-04-15 08:00:00"), FAULT), (105, at("2025-04-15 09:00:00"), NORMAL),
             (105, at("2025-04-15 11:00:00"), SMOKE)]  # fault_recent_24h  # fmt: skip
    ev = pd.DataFrame(rows, columns=["channel_id", "ts", "value"])
    ev["ts"] = ev["ts"].astype("datetime64[ns]")
    ev = ev.sort_values(["channel_id", "ts"], kind="stable").reset_index(drop=True)
    ev["event_id"] = np.arange(len(ev), dtype="int64")
    return ev[["channel_id", "event_id", "ts", "value"]]


def coverage() -> set[pd.Timestamp]:
    return set(pd.date_range(START, END - pd.Timedelta(days=1))) - {GAP}
