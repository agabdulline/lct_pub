"""Данные сценария до запуска (13.2): сводка и сигналы среза."""

import errno
from pathlib import Path

import pytest
from conftest import write_slice

from app import scenario_data
from app.player import load_slice
from app.scenario_data import DataError, ScenarioData, bucket_seconds
from app.scenarios import parse_scenario

# объект 7 «объект Гамма»: подобъект 5961 — дым 287267 и рычаг 287268, подобъект 5962 — температура 120298 и газ 300001
ROWS = [
    (10, 287267, "2026-03-28 05:56:40", True, "Обнаружен дым"),
    (11, 287268, "2026-03-28 05:56:40", True, "Рычаг сдернут"),
    (12, 120298, "2026-03-28 05:56:40", False, "18"),
    (13, 999999, "2026-03-28 05:57:00", False, "Норма"),  # вне справочника — не передаётся
    (14, 287267, "2026-03-28 05:58:02", False, "Дыма нет"),
    (15, 300001, "2026-03-28 05:59:00", False, "0.01"),
    (16, 120298, "2026-03-28 06:30:00", False, "Неисправен"),
]


@pytest.fixture
def slice_(tmp_path):
    scenario = parse_scenario(
        "t", {"title": "Т", "period": {"start": "2026-03-28T05:00:00", "end": "2026-03-28T07:00:00"}, "objects": [7]}
    )
    path = tmp_path / "t.parquet"
    write_slice(path, ROWS)
    return scenario, path


def test_summary(catalog, slice_, monkeypatch):
    monkeypatch.setattr(scenario_data, "key_moments", lambda con: [])
    s = ScenarioData(catalog).summary(*slice_)
    assert (s["scenario_id"], s["period_start"], s["period_end"]) == (
        "t",
        "2026-03-28T05:00:00+03:00",
        "2026-03-28T07:00:00+03:00",
    )
    assert (s["events"], s["alarm_events"], s["channels"]) == (6, 2, 4)
    assert (s["unknown_channels"], s["unknown_events"]) == (1, 1)
    assert s["objects"] == [
        {
            "id": 7,
            "name": "объект Гамма",
            "channels": 4,
            "events": 6,
            "sub_objects": [
                {"id": 5961, "name": "объект Гамма ОС", "channels": 2, "events": 3},
                {"id": 5962, "name": "объект Гамма ПС", "channels": 2, "events": 3},
            ],
        }
    ]
    assert s["by_type"][0] == {"sensor_type": "Датчик дыма", "channels": 1, "events": 2}
    assert {r["subsystem"]: r["events"] for r in s["by_subsystem"]} == {
        "Пожарная охрана": 3,
        "Температурная подсистема": 2,
        "Газовая подсистема": 1,
    }
    assert s["by_value"][0] == {"value": "числовые показания", "events": 2, "alarm_events": 0, "numeric": True}
    assert {r["value"] for r in s["by_value"]} == {
        "числовые показания",
        "Обнаружен дым",
        "Рычаг сдернут",
        "Дыма нет",
        "Неисправен",
    }
    density = s["density"]
    assert density["bucket_seconds"] == 60 and len(density["items"]) == 120  # 2 ч по минуте
    assert density["items"][56] == {"ts": "2026-03-28T05:56:00+03:00", "events": 3, "alarm_events": 2}
    assert sum(i["events"] for i in density["items"]) == 6
    assert density["peak_minute_events"] == 3 and density["avg_per_second"] == round(6 / 7200, 3)


def test_values_tail_and_buckets(catalog, tmp_path, monkeypatch):
    monkeypatch.setattr(scenario_data, "TOP_VALUES", 2)
    scenario = parse_scenario(
        "v", {"title": "V", "period": {"start": "2026-03-28T05:00:00", "end": "2026-03-29T05:00:00"}}
    )
    rows = [(i, 287267, f"2026-03-28 05:0{i}:00", False, v) for i, v in enumerate(["А", "А", "А", "Б", "Б", "В", "Г"])]
    write_slice(tmp_path / "v.parquet", rows)
    s = ScenarioData(catalog).summary(scenario, tmp_path / "v.parquet")
    assert [(r["value"], r["events"]) for r in s["by_value"]] == [("А", 3), ("Б", 2), ("прочие", 2)]
    assert s["by_value"][-1]["values"] == 2
    assert s["density"]["bucket_seconds"] == 600 and len(s["density"]["items"]) == 144  # сутки
    assert bucket_seconds(3600) == 60 and bucket_seconds(7 * 86400) == 3600 and bucket_seconds(400 * 86400) == 172800


def test_moments_from_module(catalog, slice_, monkeypatch):
    def fake(con):
        # модулю видны срез s и справочник ch, obj
        n = con.execute(
            "SELECT count(*) FROM s JOIN ch USING (channel_id) JOIN obj ON obj.id = ch.object_id"
        ).fetchone()
        at = con.execute("SELECT min(ts), max(ts) FROM s WHERE is_alarm").fetchone()
        return [
            {
                "at": at[0],
                "until": at[1],
                "kind": "smoke_group",
                "state": None,
                "title": "05:56 — 2 канала",
                "text": f"{n[0]} событий в срезе",
                "object_id": 7,
                "object": "объект Гамма",
                "sub_object_id": 5961,
                "subobject": "объект Гамма ОС",
                "channel_ids": [287267, 287268],
                "count": 2,
            },
            {"at": at[0], "kind": "smoke_single", "title": "дым", "channel_ids": [287267], "count": 1},
        ]

    monkeypatch.setattr(scenario_data, "key_moments", fake)
    # модуль подменён, а ключ сводки на диске — по коду модуля: без диска
    moments = ScenarioData(catalog, disk=False).summary(*slice_)["moments"]
    assert moments[0] == {
        "ts": "2026-03-28T05:56:40+03:00",
        "until": "2026-03-28T05:56:40+03:00",
        "kind": "smoke_group",
        "state": None,
        "title": "05:56 — 2 канала",
        "detail": "6 событий в срезе",
        "object_id": 7,
        "object_name": "объект Гамма",
        "sub_object_id": 5961,
        "sub_object_name": "объект Гамма ОС",
        "channel_id": None,
        "channel_ids": [287267, 287268],
        "events": 2,
    }
    assert moments[1]["channel_id"] == 287267 and moments[1]["detail"] is None

    def broken(con):
        raise RuntimeError("ошибка модуля")

    monkeypatch.setattr(scenario_data, "key_moments", broken)
    assert ScenarioData(catalog, disk=False).summary(*slice_)["moments"] == []  # модуль упал — сводка без моментов


def test_moments_of_real_module(catalog, slice_):
    """Модуль lct-50 на срезе теста: первое и последнее событие есть всегда."""
    kinds = {m["kind"] for m in ScenarioData(catalog).summary(*slice_)["moments"]}
    assert {"first_event", "last_event"} <= kinds


def test_events_pages_in_transmission_order(catalog, slice_):
    data = ScenarioData(catalog)
    page = data.events(*slice_, limit=4)
    assert page["total"] == 6 and page["next_cursor"] == "4"
    assert page["items"][0] == {
        "seq": 1,
        "ts": "2026-03-28T05:56:40+03:00",
        "event_id": 10,
        "channel_id": 287267,
        "channel_name": "ДД ПК12",
        "sensor_type": "Датчик дыма",
        "object_id": 7,
        "object_name": "объект Гамма",
        "sub_object_id": 5961,
        "sub_object_name": "объект Гамма ОС",
        "value": "Обнаружен дым",
        "is_alarm": True,
    }
    rest = data.events(*slice_, cursor=page["next_cursor"], limit=4)
    assert [i["seq"] for i in rest["items"]] == [5, 6] and rest["next_cursor"] is None
    # seq — порядок, в котором передаст проигрыватель
    played = load_slice(slice_[1], set(catalog.channels))
    assert [i["channel_id"] for i in page["items"] + rest["items"]] == played.channel


@pytest.mark.parametrize(
    ("filters", "seqs"),
    [
        ({"sensor_type": ["Датчик дыма,Ручной извещатель"]}, [1, 2, 4]),  # через запятую
        ({"sensor_type": ["Датчик дыма", "Ручной извещатель"]}, [1, 2, 4]),  # повтором параметра
        ({"value": ["числовые показания"]}, [3, 5]),
        ({"value": ["числовые показания,Неисправен"]}, [3, 5, 6]),
        ({"alarm": False}, [3, 4, 5, 6]),
        ({"object_id": ["5962"]}, [3, 5, 6]),  # подобъект
        ({"object_id": ["7"]}, [1, 2, 3, 4, 5, 6]),  # объект — с вложенными
        ({"object_id": ["5773"]}, [1, 2, 3, 4, 5, 6]),  # район
        ({"channel_id": ["287267,120298"]}, [1, 3, 4, 6]),
        ({"q": "пк88"}, [3, 6]),  # «Темп. ВШ ПК88,5», без учёта регистра
        ({"from_": "2026-03-28T05:58:02", "to": "2026-03-28T06:30:00"}, [4, 5]),  # to не включается
        ({"from_": "2026-03-28T02:58:02+00:00"}, [4, 5, 6]),  # с поясом
        ({"sensor_type": ["Датчик дыма"], "alarm": True}, [1]),
    ],
)
def test_events_filters(catalog, slice_, filters, seqs):
    page = ScenarioData(catalog).events(*slice_, **filters)
    assert [i["seq"] for i in page["items"]] == seqs and page["total"] == len(seqs)


def test_events_errors_and_file_change(catalog, slice_, tmp_path):
    data = ScenarioData(catalog)
    for kwargs, code in [({"cursor": "abc"}, "bad_cursor"), ({"from_": "вчера"}, "validation_error")]:
        with pytest.raises(DataError) as e:
            data.events(*slice_, **kwargs)
        assert (e.value.status, e.value.code) == (422, code)
    with pytest.raises(DataError) as e:
        data.events(slice_[0], Path(tmp_path / "нет.parquet"))
    assert (e.value.status, e.value.code) == (409, "slice_not_ready")
    assert data.events(*slice_, limit=5000)["total"] == 6  # limit ограничен 1000

    assert data.summary(*slice_)["events"] == 6
    write_slice(slice_[1], ROWS[:2])  # срез пересобрали — сводка и сигналы по новому файлу
    assert data.summary(*slice_)["events"] == 2
    assert data.events(*slice_)["total"] == 2


def test_summary_on_disk(catalog, slice_, monkeypatch):
    """Сводка — на диск рядом со срезом; следующий процесс берёт её оттуда, не считая. Срез изменился — пересчёт."""
    scenario, path = slice_
    first = ScenarioData(catalog).summary(scenario, path)
    assert scenario_data.summary_file(path).is_file()

    def must_not_compute(self, *args):
        raise AssertionError("сводка должна прийти с диска")

    monkeypatch.setattr(ScenarioData, "compute_summary", must_not_compute)
    assert ScenarioData(catalog).summary(scenario, path) == first
    monkeypatch.undo()

    write_slice(path, ROWS[:3])
    assert ScenarioData(catalog).summary(scenario, path)["events"] == 3  # ключ — файл среза: сводка пересчитана


def test_summary_read_only_slices(catalog, slice_):
    """Срезы только на чтение (контейнер): сводка отдаётся, на диск не пишется."""
    scenario, path = slice_
    path.parent.chmod(0o555)
    try:
        assert ScenarioData(catalog).summary(scenario, path)["events"] == 6
        assert not scenario_data.summary_file(path).exists()
    finally:
        path.parent.chmod(0o755)


def test_summary_read_only_mount(catalog, slice_, monkeypatch):
    """Каталог, смонтированный только на чтение (uploaded/ в контейнере эмулятора): и запись, и удаление временного
    файла — EROFS, а не «нет прав» и «нет файла», как после chmod выше. Сводка отдаётся без кэша на диске, повторно —
    из памяти (загрузки 26–27.09 на стенде `lct` отдавали 500)."""
    scenario, path = slice_

    def read_only(real):
        def call(self, *args, **kwargs):
            if self.parent == path.parent:
                raise OSError(errno.EROFS, "Read-only file system", str(self))
            return real(self, *args, **kwargs)

        return call

    monkeypatch.setattr(Path, "write_text", read_only(Path.write_text))
    monkeypatch.setattr(Path, "unlink", read_only(Path.unlink))
    data = ScenarioData(catalog)
    assert data.summary(scenario, path)["events"] == 6
    assert not scenario_data.summary_file(path).exists()

    def must_not_compute(self, *args):
        raise AssertionError("повторно — из памяти")

    monkeypatch.setattr(ScenarioData, "compute_summary", must_not_compute)
    assert data.summary(scenario, path)["events"] == 6


def test_summary_worker_process(catalog, slice_):
    """Сводка в отдельном процессе — та же, что на месте; ошибка среза приходит с кодом."""
    scenario, path = slice_
    worker = scenario_data.SummaryWorker(catalog)
    try:
        assert worker.summary(scenario, path) == ScenarioData(catalog, disk=False).compute_summary(scenario, path)
        with pytest.raises(DataError) as e:
            worker.summary(scenario, path.with_name("нет.parquet"))
        assert e.value.code == "slice_not_ready"
    finally:
        worker.close()
