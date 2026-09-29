import hashlib
import http.server
import json
import threading
from datetime import datetime, timedelta
from functools import partial

import duckdb
import pytest

from app.external import magdeburg
from app.external.download import download, verify
from app.external.prepare import prepare, prepared_dir
from app.external.registry import (
    Dataset,
    DatasetFile,
    channel_id,
    channel_range,
    load_registry,
    object_id,
    object_range,
)

HEADER = (
    "Date,Sensor_ID,CO2_Room,scenario_label,progress_label,experiment_number,H2_Room,PM05_Room,PM100_Room,PM10_Room,"
    "PM25_Room,PM40_Room,PM_Room_Typical_Size,PM_Total_Room,VOC_Room_RAW,Valid_Experiment,fire,nuisance,"
    "Temperature_Room,Humidity_Room,CO_Room,anomaly_label,anomaly_scenario"
)
NODES = ["sensornode0006", "sensornode0012"]
T0 = datetime(2023, 7, 10, 9, 30, 0)


def _row(t, node, scenario="Background", progress="Outgasing", number="0.0", fire="0.0", anomaly="0.0", exp=""):
    nuisance = "1.0" if anomaly == "1.0" and fire == "0.0" else "0.0"
    stamp = t.strftime("%Y-%m-%d %H:%M:%S") + "+02:00"
    return (
        f"{stamp},{node},900.0,{scenario},{progress},{number},0.1,20.0,0.0,3.0,0.0,0.0,0.5,25.0,-0.0,1.0,"
        f"{fire},{nuisance},28.2,47.0,0.54,{anomaly},{exp}"
    )


def _source() -> str:
    """Фон, опыт 1 (дерево), одиночная строка опыта 2 вплотную к опыту 3 того же вида, опыт 4 из двух видов,
    строки аномалии без номера опыта."""
    lines, t = [HEADER], T0
    plan = (
        [("bg", {})] * 3
        + [("exp", {"scenario": "Wood", "progress": "Smoldering", "number": "1.0", "fire": "1.0", "exp": "1.0"})] * 3
        + [("bg", {})] * 2
        + [("one", {"scenario": "Cable", "progress": "Short_Cicuit", "fire": "1.0", "exp": "2.0"})]
        + [("exp", {"scenario": "Cable", "progress": "Outgasing", "number": "1.0", "fire": "1.0", "exp": "3.0"})] * 5
        + [("bg", {})] * 2
        + [("exp", {"scenario": "Green_Waste", "number": "1.0", "exp": "4.0"})] * 2
        + [("exp", {"scenario": "Ethanol", "number": "2.0", "exp": "4.0"})] * 2
        + [("unk", {})] * 2
        + [("bg", {})]
    )
    for kind, kw in plan:
        nodes = NODES[:1] if kind == "one" else NODES
        for node in nodes:
            if kind == "bg":
                lines.append(_row(t, node))
            elif kind == "unk":
                lines.append(_row(t, node, anomaly="1.0"))
            else:
                lines.append(_row(t, node, anomaly="1.0", **kw))
        t += timedelta(seconds=10 if kind != "one" else 3)
    return "\n".join(lines) + "\n"


@pytest.fixture
def dataset_and_dir(tmp_path):
    raw = tmp_path / "external" / "magdeburg" / "raw"
    raw.mkdir(parents=True)
    data = _source().encode()
    (raw / magdeburg.SOURCE_FILE).write_bytes(data)
    ds = Dataset(
        key="magdeburg",
        k=1,
        title="test",
        short="test",
        direction="fire_risk",
        url="https://example.org",
        license="CC BY 4.0",
        synthetic=False,
        files=(DatasetFile(magdeburg.SOURCE_FILE, "http://unused", hashlib.sha256(data).hexdigest()),),
        meta={},
    )
    return ds, tmp_path / "external"


def _read(path, sql="SELECT * FROM t"):
    con = duckdb.connect()
    con.execute(f"CREATE VIEW t AS SELECT * FROM read_parquet('{path}')")
    return con.execute(sql).fetchall()


def test_prepare_magdeburg(dataset_and_dir):
    ds, ext = dataset_and_dir
    report = prepare(ds, ext, memory_limit="512MB")
    out = prepared_dir(ext, ds)
    source_rows = _source().count("\n") - 1

    assert report["rows"]["objects"] == 2 + len(NODES)
    assert report["rows"]["channels"] == len(NODES) * (len(magdeburg.MEASURES) + 1)
    assert report["rows"]["events"] == source_rows * len(magdeburg.MEASURES)
    assert json.loads((out / "report.json").read_text())["dataset"] == "magdeburg"

    o_lo, o_hi = object_range(1)
    assert all(o_lo <= r[0] < o_hi for r in _read(out / "objects.parquet"))
    c_lo, c_hi = channel_range(1)
    channels = _read(out / "channels.parquet", "SELECT id, object_id, sensor_type, tag, tag_group, unit FROM t")
    assert all(c_lo <= r[0] < c_hi for r in channels)
    target = [r for r in channels if r[2] == magdeburg.TARGET_TYPE]
    assert [r[0] for r in target] == [channel_id(1, 600), channel_id(1, 1200)]
    co2 = next(r for r in channels if r[0] == channel_id(1, 601))
    assert co2[1:] == (object_id(1, 106), "Датчик CO₂", "CO2_Room", "sensornode0006", "ppm")

    # Время: +02:00 → московское без пояса, значения — строкой как в источнике, event_id по (время, узел, показание)
    first = _read(out / "events.parquet", "SELECT event_id, channel_id, ts, is_alarm, value FROM t ORDER BY event_id")
    assert first[0] == (1, channel_id(1, 601), T0 + timedelta(hours=1), False, "900.0")
    assert {r[4] for r in first if r[1] == channel_id(1, 604)} == {"-0.0"}
    assert [r[0] for r in first] == list(range(1, len(first) + 1))

    labels = _read(
        out / "labels.parquet",
        "SELECT object_id, channel_id, start_ts, end_ts, label, meta FROM t ORDER BY start_ts",
    )
    assert [(r[4], json.loads(r[5]).get("subtype")) for r in labels] == [
        ("fire", "Wood"),
        ("fire", "Cable"),
        ("nuisance", "Green_Waste"),
        ("nuisance", "Ethanol"),
        ("unknown", None),
    ]
    assert all(r[0] == object_id(1, 2) and r[1] is None for r in labels)
    wood = labels[0]
    assert wood[2] == T0 + timedelta(hours=1, seconds=30)
    assert wood[3] - wood[2] == timedelta(seconds=30)  # 3 записи по 10 с
    cable = json.loads(labels[1][5])
    assert cable["experiment"] == 3 and cable["merged_experiments"] == [2]
    assert report["details"]["labels"]["merged"] == [{"experiment": 2, "into": 3, "rows": 1}]
    assert report["details"]["labels"]["unknown_rows"] == 2 * len(NODES)


def test_prepare_replaces_previous_output(dataset_and_dir):
    ds, ext = dataset_and_dir
    prepare(ds, ext, memory_limit="512MB")
    stale = prepared_dir(ext, ds) / "stale.parquet"
    stale.write_bytes(b"x")
    prepare(ds, ext, memory_limit="512MB")
    assert not stale.exists()


def test_verify_rejects_changed_file(dataset_and_dir):
    ds, ext = dataset_and_dir
    (ext / "magdeburg" / "raw" / magdeburg.SOURCE_FILE).write_text(HEADER + "\n")
    with pytest.raises(SystemExit, match="sha256"):
        verify(ds, ext)


def test_download_checks_sha256(tmp_path, monkeypatch):
    # httpx берёт прокси из окружения: запрос к локальному серверу через чужой прокси не дойдёт
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    served = tmp_path / "served"
    served.mkdir()
    (served / "a.csv").write_bytes(b"payload")
    handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(served))
    handler.log_message = lambda *a, **k: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/a.csv"
    try:
        good = hashlib.sha256(b"payload").hexdigest()
        ds = Dataset("demo", 9, "t", "t", "x", "u", "l", True, (DatasetFile("a.csv", url, good),), {})
        assert download(ds, tmp_path / "ext")[0]["status"] == "downloaded"
        assert download(ds, tmp_path / "ext")[0]["status"] == "present"

        bad = Dataset("bad", 8, "t", "t", "x", "u", "l", True, (DatasetFile("a.csv", url, "0" * 64),), {})
        with pytest.raises(SystemExit, match="sha256"):
            download(bad, tmp_path / "ext")
        assert not (tmp_path / "ext" / "bad" / "raw" / "a.csv").exists()
    finally:
        server.shutdown()
        server.server_close()


def test_registry_and_id_ranges():
    registry = load_registry()
    assert registry["magdeburg"].k == 1
    assert registry["magdeburg"].files[0].sha256 == ("b431b611a44e4282510bd53158aaa71512554481433d8f3a90f9475c17134839")
    assert object_id(1, 1) == 9_010_001 and channel_id(1, 601) == 901_000_601
    # Диапазоны не пересекаются с хакатоном (объекты до 5 966, каналы до 337 554) и между наборами
    assert object_range(1)[0] > 5_966 and channel_range(1)[0] > 337_554
    assert object_range(1)[1] == object_range(2)[0] and channel_range(1)[1] == channel_range(2)[0]
    assert channel_range(9)[1] < 2**31
    with pytest.raises(ValueError):
        object_id(1, 10_000)


@pytest.mark.parametrize(
    ("attr", "value", "message"),
    [
        ("TARGET_TYPE", "Датчик дыма", "типы датчиков хакатона"),
        ("SYSTEM_TYPE", "Пожарная охрана", "подсистема хакатона"),
        ("SYSTEM_TYPE", "Пожарный мониторинг", "без пометки"),
    ],
)
def test_prepare_rejects_hackathon_catalog_names(dataset_and_dir, monkeypatch, attr, value, message):
    # Имена хакатона включили бы на внешних каналах его правила: газ, эпизоды оборудования, ежедневную модель
    ds, ext = dataset_and_dir
    monkeypatch.setattr(magdeburg, attr, value)
    with pytest.raises(SystemExit, match=message):
        prepare(ds, ext, memory_limit="512MB")
