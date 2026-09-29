import hashlib
import io
import json
import zipfile

import duckdb
import pytest

from app.external import vast2017
from app.external.prepare import prepare, prepared_dir
from app.external.registry import Dataset, DatasetFile, channel_id, load_registry, object_id

HEADER = "Timestamp,car-id,car-type,gate-name"


def _trip(car, cls, day, stops):
    return [f"{day} {t},{car},{cls},{gate}" for t, gate in stops]


def _source() -> str:
    rows = []
    # Рейнджеры: база → закрытые ворота → посты за воротами; много визитов, чтобы доля не-рейнджеров была мала
    for i in range(12):
        car = f"2015050{i % 9 + 1}000000-{100 + i}"
        rows += _trip(
            car,
            "2P",
            f"2015-05-0{i % 9 + 1}",
            [
                ("08:00:00", "ranger-base"),
                ("08:10:00", "gate0"),
                ("08:20:00", "ranger-stop1"),
                ("08:30:00", "ranger-stop3"),
                ("08:40:00", "gate0"),
                ("08:50:00", "ranger-base"),
            ],
        )
    # Обычная машина — пост 0 на общей дороге, кемпинг
    rows += _trip(
        "20150510000000-1",
        "1",
        "2015-05-10",
        [
            ("10:00:00", "entrance0"),
            ("10:05:00", "general-gate1"),
            ("10:10:00", "ranger-stop0"),
            ("10:20:00", "camping2"),
            ("12:00:00", "entrance0"),
        ],
    )
    # Ночной грузовик через закрытые ворота к посту 3 — нарушение
    rows += _trip(
        "20150511020000-2",
        "4",
        "2015-05-11",
        [
            ("02:00:00", "entrance3"),
            ("02:10:00", "gate6"),
            ("02:20:00", "ranger-stop3"),
            ("02:40:00", "gate6"),
            ("02:50:00", "entrance3"),
        ],
    )
    # Мотоцикл на посту 1 в обход ворот — нарушение без записи на воротах
    rows += _trip(
        "20150512100000-3",
        "1",
        "2015-05-12",
        [
            ("10:00:00", "entrance1"),
            ("10:30:00", "ranger-stop1"),
            ("11:00:00", "entrance1"),
        ],
    )
    # Эталон: ночёвка грузовика вне кемпинга в дату из списка и машина с превышением скорости
    rows += _trip("20150627220000-4", "4", "2015-06-27", [("22:00:00", "entrance2"), ("22:30:00", "general-gate2")])
    rows += _trip("20150627220000-4", "4", "2015-06-28", [("08:00:00", "entrance2")])
    rows += _trip("20151320031302-558", "1", "2015-07-20", [("15:13:02", "entrance0"), ("15:20:00", "entrance1")])
    return "\n".join([HEADER, *rows]) + "\n"


@pytest.fixture
def prepared(tmp_path):
    raw = tmp_path / "external" / "vast2017" / "raw"
    raw.mkdir(parents=True)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(vast2017.CSV_NAME, _source())
    data = buf.getvalue()
    (raw / vast2017.SOURCE_FILE).write_bytes(data)
    ds = Dataset(
        "vast2017",
        2,
        "t",
        "t",
        "unauthorized_access",
        "u",
        "l",
        True,
        (DatasetFile(vast2017.SOURCE_FILE, "http://unused", hashlib.sha256(data).hexdigest()),),
        {},
    )
    report = prepare(ds, tmp_path / "external", memory_limit="512MB")
    return report, prepared_dir(tmp_path / "external", ds)


def _q(out, name, sql):
    con = duckdb.connect()
    con.execute(f"CREATE VIEW t AS SELECT * FROM read_parquet('{out / name}')")
    return con.execute(sql).fetchall()


def test_catalog_and_journal(prepared):
    report, out = prepared
    assert report["details"]["open_ranger_stops"] == ["ranger-stop0"]
    closed_gates, ranger_zone = object_id(2, 15), object_id(2, 16)
    channels = dict(_q(out, "channels.parquet", "SELECT tag, object_id FROM t"))
    assert channels["gate6"] == closed_gates
    assert channels["ranger-stop1"] == ranger_zone and channels["ranger-stop3"] == ranger_zone
    assert channels["ranger-base"] == ranger_zone
    assert channels["ranger-stop0"] == object_id(2, 14)
    assert {r[0] for r in _q(out, "channels.parquet", "SELECT sensor_type FROM t")} == {vast2017.SENSOR_TYPE}
    ids = dict(_q(out, "channels.parquet", "SELECT tag, id FROM t"))
    assert ids["gate6"] == channel_id(2, 1506) and ids["ranger-base"] == channel_id(2, 1699)

    # Событие проезда: значение — билет машины, класс — в справочнике пропусков
    events = _q(out, "events.parquet", "SELECT event_id, value, is_alarm FROM t ORDER BY event_id")
    assert len(events) == _source().count("\n") - 1
    assert [e[0] for e in events] == list(range(1, len(events) + 1))
    assert not any(e[2] for e in events)
    classes = dict(_q(out, "credentials.parquet", "SELECT credential_id, json_extract_string(meta, '$.class') FROM t"))
    assert classes["20150511020000-2"] == "4"

    permits = _q(out, "permits.parquet", "SELECT credential_id, object_id FROM t")
    rangers = {c for c, cls in classes.items() if cls == "2P"}
    assert {c for c, _ in permits} == rangers
    assert {o for _, o in permits} == {closed_gates, ranger_zone}
    assert len(permits) == 2 * len(rangers)


def test_labels(prepared):
    report, out = prepared
    labels = _q(
        out,
        "labels.parquet",
        "SELECT label, json_extract_string(meta, '$.subtype'), json_extract_string(meta, '$.credential_id'), "
        "channel_id, end_ts - start_ts FROM t ORDER BY start_ts",
    )
    violations = [r for r in labels if r[0] == "violation"]
    assert [(r[1], r[2]) for r in violations] == [
        ("closed_gate_pass", "20150511020000-2"),
        ("closed_gate_pass", "20150511020000-2"),
        ("closed_gate_pass", "20150511020000-2"),
        ("gate_bypass", "20150512100000-3"),
    ]
    assert all(r[3] is not None for r in violations)
    suspicious = sorted((r[1], r[2]) for r in labels if r[0] == "suspicious")
    assert suspicious == [("overnight_truck", "20150627220000-4"), ("speeding", "20151320031302-558")]
    assert all(r[3] is None for r in labels if r[0] == "suspicious")
    assert report["details"]["labels"]["violation/closed_gate_pass"] == {"labels": 3, "credentials": 1}
    assert json.loads(json.dumps(report["rows"]))["permits"] == 24


def test_registry_entry():
    ds = load_registry()["vast2017"]
    assert ds.k == 2 and ds.synthetic
    assert ds.files[0].name == vast2017.SOURCE_FILE
