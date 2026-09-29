"""Правило маршрутизации и чтение журналов CSV вместо 7z. Запуск из ml-service:
`PYTHONPATH=.. uv run pytest ../ml/daily_routing`."""

from ml.daily_routing.compare import decide
from ml.v3_inputs.ingest import archive_command, journal_source


def m(p: float | None, r: float | None) -> dict:
    return {"precision": p, "recall": r, "passes": p is not None and r is not None and p > 0.7 and r > 0.5}


def test_his_model_wins_where_v0_fails():
    assert decide("ИБП", m(None, 0.0), m(0.775, 0.657), "ups")[0] == "ups"


def test_his_model_must_pass_thresholds():
    assert decide("Датчик дыма", m(0.0, 0.0), m(0.691, 0.239), "v3")[0] == "v0"


def test_both_pass_his_needs_both_not_lower():
    assert decide("X", m(0.75, 0.6), m(0.76, 0.6), "v3")[0] == "v3"
    assert decide("X", m(0.75, 0.6), m(0.80, 0.55), "v3")[0] == "v0"


def test_type_without_v0():
    assert decide("КД Дверь", None, m(0.8, 0.6), "v3")[0] == "v3"
    assert decide("КД Дверь", None, m(None, 0.0), "v3")[0] is None


def test_author_advice_keeps_v0():
    assert decide("Состояние насоса", m(0.81, 0.96), None, None)[0] == "v0"


def test_journal_source_prefers_7z_then_csv(tmp_path):
    csv = tmp_path / "ext-journal-2026.csv"
    csv.write_text("x\n")
    assert journal_source(tmp_path, 2026) == csv
    assert archive_command(csv) == ["cat", str(csv)]
    (tmp_path / "ext-journal-2026.7z").write_bytes(b"")
    assert journal_source(tmp_path, 2026).suffix == ".7z"


def test_episodes_from_islands_equal_full_journal():
    """Его эпизоды по событиям неисправности, событиям той же секунды и островам базы — те же, что по всему журналу.

    Острова — как у загрузчика базы: подряд «Неисправен» / «Отключено устройство», конец — первое другое значение,
    острова с одним началом склеиваются. Случаи: «Отключено» внутри острова и в его начале, два острова в одной
    секунде, серия в одну секунду, первое событие канала — неисправность, незакрытый остров."""
    import pandas as pd

    from ml.daily_routing.online import FAULT_VALUES, assemble, his_episodes

    t = pd.Timestamp("2026-01-10")

    def s(sec: int) -> pd.Timestamp:
        return t + pd.Timedelta(seconds=sec)

    fault, off, ok = "Неисправен", "Отключено устройство", "Норма"
    journal = {  # канал 1 — тип v3 (дым), канал 2 — тип вне v3 (ИБП)
        1: [(0, fault), (5, ok), (9, fault), (12, off), (15, fault), (20, ok), (30, ok), (30, fault), (30, ok),
            (30, fault), (40, ok), (50, off), (55, fault), (60, ok), (70, fault)],
        2: [(3, ok), (4, fault), (4, ok), (8, fault), (8, off), (10, ok), (11, fault)],
    }  # fmt: skip
    rows = [(c, i, s(sec), v) for c, evs in journal.items() for i, (sec, v) in enumerate(evs)]
    full = pd.DataFrame(rows, columns=["sensor_id", "event_id", "ts", "value"]).astype({"event_id": "Int64"})
    islands = []
    for c, g in full.groupby("sensor_id"):
        f = g.value.isin(FAULT_VALUES).to_numpy()
        for k in range(len(g)):
            if f[k] and (k == 0 or not f[k - 1]):
                j = k
                while j < len(g) and f[j]:
                    j += 1
                end = g.ts.iloc[j] if j < len(g) else pd.NaT
                islands.append((c, g.ts.iloc[k], end, g.value.iloc[k]))
    islands = pd.DataFrame(islands, columns=["channel_id", "start", "end", "state"])
    islands = islands.groupby(["channel_id", "start"], as_index=False).agg(end=("end", "max"), state=("state", "first"))
    faulty = full[full.value.isin(FAULT_VALUES)]
    keys = set(zip(faulty.sensor_id, faulty.ts, strict=True))
    ev = full[[(c, ts) in keys for c, ts in zip(full.sensor_id, full.ts, strict=True)]]
    first = {c: (g.ts.iloc[0], g.value.iloc[0] in FAULT_VALUES) for c, g in full.groupby("sensor_id")}
    catalog = pd.DataFrame({"ид_канала_данных": [1, 2], "тип_датчика": ["Датчик дыма", "ИБП"]})
    days = pd.DatetimeIndex([t])
    got = his_episodes(assemble(ev, islands, first), catalog, days)
    want = his_episodes(full, catalog, days)
    cols = ["sensor_id", "start", "end", "start_is_observed"]
    assert got[cols].equals(want[cols])
    assert len(want) == 10 and not want.start_is_observed.iloc[0]
