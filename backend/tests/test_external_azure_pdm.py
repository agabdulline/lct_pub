import hashlib
import json

import duckdb
import pytest

from app.external import azure_pdm
from app.external.prepare import prepare, prepared_dir
from app.external.registry import Dataset, DatasetFile, channel_id, load_registry, object_id

SOURCES = {
    "PdM_machines.csv": '"machineID","model","age"\n1,"model3",18\n2,"model4",7\n',
    "PdM_telemetry.csv": (
        '"datetime","machineID","volt","rotate","pressure","vibration"\n'
        "2015-01-01 06:00:00,1,176.217853015625,418.504078221616,113.077935462083,45.0876857639276\n"
        "2015-01-01 06:00:00,2,170.0,400.0,100.0,40.0\n"
        "2015-01-01 07:00:00,1,162.87922289706,402.747489565395,95.4605253823187,43.4139726834815\n"
        "2015-01-01 07:00:00,2,171.0,401.0,101.0,41.0\n"
    ),
    "PdM_errors.csv": '"datetime","machineID","errorID"\n2015-01-01 07:00:00,1,"error1"\n',
    "PdM_maint.csv": '"datetime","machineID","comp"\n2014-06-01 06:00:00,1,"comp2"\n2015-01-01 07:00:00,2,"comp4"\n',
    "PdM_failures.csv": '"datetime","machineID","failure"\n2015-01-01 07:00:00,2,"comp4"\n',
}


@pytest.fixture
def prepared(tmp_path):
    raw = tmp_path / "external" / "azure_pdm" / "raw"
    raw.mkdir(parents=True)
    files = []
    for name, text in SOURCES.items():
        (raw / name).write_text(text)
        files.append(DatasetFile(name, "http://unused", hashlib.sha256(text.encode()).hexdigest()))
    ds = Dataset("azure_pdm", 4, "t", "t", "wear", "u", "MIT", True, tuple(files), {})
    report = prepare(ds, tmp_path / "external", memory_limit="512MB")
    return report, prepared_dir(tmp_path / "external", ds)


def _q(out, name, sql):
    con = duckdb.connect()
    con.execute(f"CREATE VIEW t AS SELECT * FROM read_parquet('{out / name}')")
    return con.execute(sql).fetchall()


def test_catalog(prepared):
    report, out = prepared
    assert report["rows"]["objects"] == 1 + 2 + 2  # корень, модели 3 и 4, два агрегата
    assert report["rows"]["channels"] == 2 * 8
    machine = _q(out, "objects.parquet", f"SELECT parent_id, level, name, meta FROM t WHERE id = {object_id(4, 101)}")[
        0
    ]
    assert machine[:3] == (object_id(4, 13), 3, "Агрегат 001")
    assert json.loads(machine[3]) == {"machine_id": 1, "model": "model3", "age_years": 18}
    types = dict(_q(out, "channels.parquet", "SELECT id, sensor_type FROM t"))
    assert types[channel_id(4, 100)] == azure_pdm.TARGET_TYPE
    assert types[channel_id(4, 101)] == "Напряжение" and types[channel_id(4, 107)] == "Отказы и ремонты"


def test_journal_and_labels(prepared):
    report, out = prepared
    events = _q(out, "events.parquet", "SELECT event_id, channel_id, ts, is_alarm, value FROM t ORDER BY event_id")
    assert len(events) == 4 * 4 + 1 + 2 + 1
    assert [e[0] for e in events] == list(range(1, len(events) + 1))
    # История ТО раньше телеметрии — тоже в журнале; значения — как в источнике
    assert events[0][1:] == (channel_id(4, 106), events[0][2], False, "comp2")
    assert str(events[0][2]) == "2014-06-01 06:00:00"
    volt = [e[4] for e in events if e[1] == channel_id(4, 101)]
    assert volt == ["176.217853015625", "162.87922289706"]
    # Ошибку сообщает устройство — тревожная; ТО и отказ — записи журнала
    alarms = {(e[1], e[4]) for e in events if e[3]}
    assert alarms == {(channel_id(4, 105), "error1")}
    assert (channel_id(4, 207), "comp4") in {(e[1], e[4]) for e in events}

    labels = _q(out, "labels.parquet", "SELECT object_id, channel_id, start_ts, end_ts, label, meta FROM t")
    assert len(labels) == 1
    object_id_, channel, start, end, label, meta = labels[0]
    assert (object_id_, channel, label) == (object_id(4, 102), None, "failure")
    assert (end - start).total_seconds() == 3600
    assert json.loads(meta) == {"subtype": "comp4", "machine_id": 2}
    assert report["details"]["failures_with_maintenance_record"] == 1


def test_registry_entry():
    ds = load_registry()["azure_pdm"]
    assert ds.k == 4 and ds.synthetic and ds.license == "MIT"
    assert {f.name for f in ds.files} == set(azure_pdm.FILES.values())
    assert all("ebdc49e7ef15808b75660921d7cfdc0c661f78e9" in f.url for f in ds.files)
