"""Сценарий из своего журнала (app/domain/uploads.py, app/api/uploads.py): CSV и XLSX в формате журнала СМВУ."""

import csv
import datetime as dt
import io
import json
from pathlib import Path

import duckdb
import pytest

from app.core.config import settings

API = "/api/v1/scenario-uploads"
SAMPLE = Path(__file__).resolve().parents[2] / "data" / "raw" / "журнал_событий_пример.csv"
HEADER = ["ид_события", "ид_канала_данных", "дата", "время", "тревожное", "значение_датчика"]
ROWS = [  # каналы 1001–1003 — справочник тестовой базы; 999999 — вне справочника
    ["11", "1001", "2026-08-01", "10:00:00", "true", "Обнаружен дым"],
    ["12", "1003", "2026-08-01", "10:00:05", "false", "18"],
    ["13", "1002", "2026-08-01", "10:01:00", "false", "0.02"],
    ["13", "1002", "2026-08-01", "10:01:00", "false", "0.02"],  # дубль
    ["14", "999999", "2026-08-01", "10:02:00", "false", "Норма"],
    ["15", "1003", "01.08.2026", "10:03:00", "f", "19"],
    ["16", "1003", "2026-13-01", "10:04:00", "f", "20"],  # дата не распознаётся
]


@pytest.fixture(autouse=True)
def dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "uploads_dir", tmp_path / "uploads")
    monkeypatch.setattr(settings, "emulator_dir", tmp_path / "emulator")
    return tmp_path


def csv_bytes(rows, header=HEADER, delimiter=",", encoding="utf-8") -> bytes:
    out = io.StringIO()
    w = csv.writer(out, delimiter=delimiter, quoting=csv.QUOTE_MINIMAL)
    if header:
        w.writerow(header)
    w.writerows(rows)
    return out.getvalue().encode(encoding)


def upload(client, content: bytes, name: str = "journal.csv"):
    return client.post(API, files={"file": (name, content, "application/octet-stream")})


def mapping_of(result) -> dict:
    return {f["field"]: f["column"] for f in result["fields"]}


def matches_of(result) -> dict:
    return {f["field"]: f["match"] for f in result["fields"]}


def create(client, upload_id: str, **body):
    return client.put(f"{API}/{upload_id}", json={"dry_run": False, **body})


def slice_rows(dirs, scenario_id: str) -> list[tuple]:
    path = dirs / "emulator" / "uploaded" / f"{scenario_id}.parquet"
    return duckdb.sql(
        f"SELECT event_id, channel_id, ts, is_alarm, value FROM '{path}' ORDER BY ts, channel_id"
    ).fetchall()


def warning(result, code: str) -> dict | None:
    return next((w for w in result["warnings"] if w["code"] == code), None)


def test_journal_format(client, dirs):
    r = upload(client, csv_bytes(ROWS))
    assert r.status_code == 201
    u = r.json()
    assert u["format"] == {"kind": "csv", "encoding": "utf-8", "delimiter": ",", "sheet": None, "header": True}
    assert mapping_of(u) == {
        "event_id": 0,
        "channel_id": 1,
        "datetime": None,
        "date": 2,
        "time": 3,
        "is_alarm": 4,
        "value": 5,
    }
    assert set(matches_of(u).values()) == {"exact", None} and u["ready"] and u["rows"] == 7
    assert u["preview"][0] == ROWS[0] and u["columns"][1]["name"] == "ид_канала_данных"
    assert u["stats"]["duplicates"] == 1 and u["stats"]["unknown_channels"] == 1 and u["stats"]["invalid_rows"] == 1
    assert u["stats"]["events"] == 5 and u["stats"]["objects"] == [{"id": 10, "name": "Объект А", "channels": 3}]
    assert warning(u, "unknown_channels")["examples"] == ["999999"]
    assert warning(u, "bad_dates")["count"] == 1 and warning(u, "duplicates")["count"] == 1
    assert u["period"] == {"start": "2026-08-01T10:00:00+03:00", "end": "2026-08-01T10:03:01+03:00"}
    assert client.get(f"{API}/{u['id']}").json() == u

    r = create(client, u["id"], title="Мой журнал")
    assert r.status_code == 201
    s = r.json()
    assert s["scenario_id"] == f"upload-{u['id']}" and s["title"] == "Мой журнал" and s["events"] == 5
    assert slice_rows(dirs, s["scenario_id"]) == [
        (11, 1001, dt.datetime(2026, 8, 1, 10, 0), True, "Обнаружен дым"),
        (12, 1003, dt.datetime(2026, 8, 1, 10, 0, 5), False, "18"),
        (13, 1002, dt.datetime(2026, 8, 1, 10, 1), False, "0.02"),
        (14, 999999, dt.datetime(2026, 8, 1, 10, 2), False, "Норма"),  # вне справочника остаётся — эмулятор пропустит
        (15, 1003, dt.datetime(2026, 8, 1, 10, 3), False, "19"),
    ]
    meta = json.loads((dirs / "emulator" / "uploaded" / f"{s['scenario_id']}.json").read_text(encoding="utf-8"))
    assert meta["id"] == meta["scenario_id"] == s["scenario_id"] and meta["source_file"] == "journal.csv"
    assert meta["period"] == {"start": "2026-08-01T10:00:00", "end": "2026-08-01T10:03:01"}
    assert meta["objects"] == [10] and meta["events"] == 5 and meta["include_gas"] is True
    assert not list((dirs / "emulator" / "uploaded").glob(".*.tmp"))


def test_renamed_and_reordered_columns(client, dirs):
    header = ["Комментарий", "Значение", "Канал", "Время", "Дата", "Тревога", "ID"]
    rows = [["x", r[5], r[1], r[3], r[2], r[4], r[0]] for r in ROWS[:3]]
    u = upload(client, csv_bytes(rows, header)).json()
    assert mapping_of(u) == {
        "event_id": 6,
        "channel_id": 2,
        "datetime": None,
        "date": 4,
        "time": 3,
        "is_alarm": 5,
        "value": 1,
    }
    assert matches_of(u)["channel_id"] == "synonym" and u["ready"]
    s = create(client, u["id"]).json()
    assert [r[:2] for r in slice_rows(dirs, s["scenario_id"])] == [(11, 1001), (12, 1003), (13, 1002)]


def test_without_alarm_column_and_datetime(client, dirs):
    header = ["channel", "datetime", "value"]
    rows = [["1001", "2026-08-01 10:00:00", "Обнаружен дым"], ["1003", "2026-08-01T10:00:05", "18"]]
    u = upload(client, csv_bytes(rows, header)).json()
    assert mapping_of(u)["datetime"] == 1 and mapping_of(u)["is_alarm"] is None and u["ready"]
    assert warning(u, "field_missing")["field"] == "is_alarm"
    s = create(client, u["id"]).json()
    assert [(r[1], r[3], r[0]) for r in slice_rows(dirs, s["scenario_id"])] == [
        (1001, False, None),
        (1003, False, None),
    ]


def test_xlsx(client, dirs):
    import openpyxl

    wb = openpyxl.Workbook()
    wb.active.title = "Пусто"
    ws = wb.create_sheet("Журнал")
    ws.append(HEADER)
    ws.append([11, 1001, dt.date(2026, 8, 1), dt.time(10, 0, 0), True, "Обнаружен дым"])
    ws.append([12, 1003.0, dt.date(2026, 8, 1), dt.time(10, 0, 5), False, 18])
    buf = io.BytesIO()
    wb.save(buf)
    u = upload(client, buf.getvalue(), "journal.xlsx").json()
    assert u["format"]["kind"] == "xlsx" and u["sheets"] == ["Пусто", "Журнал"]
    assert u["format"]["sheet"] == "Журнал" and u["ready"] and u["stats"]["events"] == 2  # первый лист пуст — пропущен
    assert client.put(f"{API}/{u['id']}", json={"sheet": "Пусто", "dry_run": True}).status_code == 400
    assert client.put(f"{API}/{u['id']}", json={"sheet": "Нет такого", "dry_run": True}).status_code == 422
    s = create(client, u["id"]).json()
    assert slice_rows(dirs, s["scenario_id"]) == [
        (11, 1001, dt.datetime(2026, 8, 1, 10, 0), True, "Обнаружен дым"),
        (12, 1003, dt.datetime(2026, 8, 1, 10, 0, 5), False, "18"),
    ]


def test_cp1251_semicolon(client, dirs):
    header = ["Ид события", "Ид канала данных", "Дата", "Время", "Тревожное", "Значение датчика"]
    rows = [[r[0], r[1], r[2], r[3], "да" if r[4] == "true" else "нет", r[5]] for r in ROWS[:2]]
    u = upload(client, csv_bytes(rows, header, ";", "cp1251")).json()
    assert (u["format"]["encoding"], u["format"]["delimiter"]) == ("cp1251", ";") and u["ready"]
    assert matches_of(u)["channel_id"] == "exact"
    s = create(client, u["id"]).json()
    assert [(r[1], r[3], r[4]) for r in slice_rows(dirs, s["scenario_id"])] == [
        (1001, True, "Обнаружен дым"),
        (1003, False, "18"),
    ]


def test_manual_mapping_and_errors(client, dirs, monkeypatch):
    header = ["a", "b", "c", "d"]  # заголовок не опознан: названия не синонимы
    rows = [["1001", "2026-08-01", "10:00:00", "Обнаружен дым"]]
    u = upload(client, csv_bytes(rows, header)).json()
    assert not u["format"]["header"] and warning(u, "no_header") and not u["ready"]
    assert set(u["required_missing"]) == {"channel_id", "value", "date", "time"}
    assert create(client, u["id"]).status_code == 422  # required_missing

    mapping = {"channel_id": 0, "date": 1, "time": 2, "value": 3}
    u = client.put(f"{API}/{u['id']}", json={"mapping": mapping, "dry_run": True}).json()
    assert u["ready"] and matches_of(u)["channel_id"] == "manual" and u["stats"]["events"] == 1
    assert u["stats"]["invalid_rows"] == 1 and warning(u, "invalid_rows")  # строка «a, b, c, d» — канал не число
    assert client.put(f"{API}/{u['id']}", json={"mapping": {"value": 9}, "dry_run": True}).status_code == 422
    u = client.put(f"{API}/{u['id']}", json={"mapping": {"value": None}, "dry_run": True}).json()  # правка одного поля
    assert mapping_of(u)["channel_id"] == 0 and mapping_of(u)["value"] is None and u["required_missing"] == ["value"]
    assert client.get(f"{API}/20260101-abcdef").status_code == 404

    assert upload(client, b"\xd0\xcf\x11\xe0 old excel", "journal.xls").status_code == 415
    monkeypatch.setattr(settings, "upload_max_bytes", 10)
    assert upload(client, csv_bytes(ROWS)).status_code == 413


@pytest.mark.skipif(not SAMPLE.exists(), reason="нет примера журнала организаторов")
def test_organizers_sample(client, dirs):
    u = upload(client, SAMPLE.read_bytes(), SAMPLE.name).json()
    assert u["ready"] and u["rows"] == 169_993 and set(matches_of(u).values()) == {"exact", None}
    assert u["period"] == {"start": "2026-08-01T00:00:00+03:00", "end": "2026-08-01T23:59:59+03:00"}
    assert u["stats"]["duplicates"] == 0 and u["stats"]["invalid_rows"] == 0
    s = create(client, u["id"]).json()
    assert s["events"] == 169_993
    path = dirs / "emulator" / "uploaded" / f"{s['scenario_id']}.parquet"
    assert duckdb.sql(f"SELECT count(*), count(DISTINCT channel_id) FROM '{path}'").fetchone() == (169_993, 1241)
