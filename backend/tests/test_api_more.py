"""Журнал прогнозов, метрики, модели, дашборд и заявки (docs/api.md v0.2) на минимальном справочнике."""

import psycopg
import pytest

from app.loader import pg_conninfo

API = "/api/v1"


@pytest.fixture
def pg(seeded):
    with psycopg.connect(pg_conninfo(seeded.render_as_string(hide_password=False)), autocommit=True) as conn:
        yield conn


@pytest.fixture
def journal(client, pg):
    """Две тревоги дыма и два прогноза: TP (неисправность через 2 ч) и TN; «сейчас» базы — 03.05 12:00."""
    client.post(
        f"{API}/ingest/events",
        json=[
            {"channel_id": 1001, "ts": "2026-05-01T12:00:00+03:00", "value": "Обнаружен дым", "is_alarm": True},
            {"channel_id": 1001, "ts": "2026-05-01T14:00:00+03:00", "value": "Неисправен", "is_alarm": True},
            {"channel_id": 1003, "ts": "2026-05-03T12:00:00+03:00", "value": "18"},
        ],
    )
    alarm = pg.execute("SELECT id FROM alarms").fetchone()[0]
    pg.execute(
        """
        INSERT INTO predictions (run_id, model, model_version, channel_id, alarm_id, decision_time, horizon_start,
            horizon_end, status, score, threshold, alert, seen_channel, outcome, outcome_at, factors, object_history)
        VALUES
          (NULL, 'smoke_alarm', 'baseline-v0', 1001, %(a)s, '2026-05-01 12:00:01+03', '2026-05-01 12:00:01+03',
           '2026-05-02 12:00:01+03', 'scored', 0.9, 0.43, true, true, 'fault', '2026-05-01 14:00:00+03',
           '[{"feature": "loop_past_rate", "value": 0.2, "contribution": 0.3}]', true),
          (NULL, 'smoke_alarm', 'baseline-v0', 1003, NULL, '2026-05-01 12:00:01+03', '2026-05-01 12:00:01+03',
           '2026-05-02 12:00:01+03', 'scored', 0.1, 0.43, false, false, 'no_fault', '2026-05-02 12:00:01+03', NULL,
           false)
        """,
        {"a": alarm},
    )
    pg.execute("UPDATE alarms SET verdict = 'sensor_fault', prediction_id = (SELECT min(id) FROM predictions)")
    return client


def test_outcome_filter_skips_decisions_without_score(journal, pg):
    """«Ожидается» и другие исходы — только у оценённых решений: «без оценки» в фильтр исхода не попадает."""
    pg.execute(
        """
        INSERT INTO predictions (run_id, model, model_version, channel_id, decision_time, horizon_start, horizon_end,
            status, skip_reason, seen_channel)
        VALUES (NULL, 'daily', 'baseline-v0', 1003, '2026-05-03 00:00:00+03', '2026-05-04 00:00:00+03',
            '2026-06-02 00:00:00+03', 'skipped', 'no_coverage', false)
        """
    )
    assert journal.get(f"{API}/predictions", params={"status": "skipped"}).json()["total"] == 1
    for outcome in ("open", "fault", "no_fault", "open,fault,no_fault"):
        items = journal.get(f"{API}/predictions", params={"outcome": outcome}).json()["items"]
        assert all(i["status"] == "scored" for i in items), outcome


def test_predictions_and_metrics(journal):
    page = journal.get(f"{API}/predictions").json()
    assert page["total"] == 2 and page["items"][0]["channel_name"] in ("ДД ПК1", "ТЕМП ПК3")
    assert journal.get(f"{API}/predictions", params={"alert": "true"}).json()["total"] == 1
    assert journal.get(f"{API}/predictions", params={"outcome": "open"}).json()["total"] == 0
    assert journal.get(f"{API}/predictions", params={"object_id": 100, "seen_channel": "false"}).json()["total"] == 1
    one = journal.get(f"{API}/predictions/{page['items'][0]['id']}").json()
    assert one["model"] == "smoke_alarm" and one["object_name"] == "Объект А"

    m = journal.get(f"{API}/metrics", params={"model": "smoke_alarm"}).json()
    assert (m["all"]["tp"], m["all"]["tn"], m["all"]["precision"], m["all"]["recall"]) == (1, 1, 1.0, 1.0)
    assert m["seen"]["tp"] == 1 and m["unseen"]["tn"] == 1 and m["open"] == 0
    assert m["all"]["lead_time"]["p50_s"] == pytest.approx(2 * 3600 - 1) and "from" in m
    assert journal.get(f"{API}/metrics").status_code == 422  # model обязателен

    alarm = journal.get(f"{API}/alarms").json()["items"][-1]
    assert alarm["prediction"]["alert"] is True and alarm["prediction"]["inherited"] is False
    assert alarm["prediction"]["object_history"] is True

    # «модель не обучена на этом объекте»: у решения, у канала и фильтр журнала
    assert journal.get(f"{API}/predictions", params={"object_history": "false"}).json()["total"] == 1
    assert journal.get(f"{API}/channels/1001").json()["object_history"] is True
    assert journal.get(f"{API}/channels/1002").json()["object_history"] is None  # решений нет — неизвестно


def test_journal_outcome_filter_as_metrics(journal, pg):
    """Фильтр исхода журнала — как карточка метрик: отказ уже начался, но горизонт не истёк к «сейчас» — решение
    открыто, в «отказ был» не попадает (PdM прогона 53: исходы по разметке за концом прогона)."""
    pg.execute(
        """
        INSERT INTO predictions (run_id, model, model_version, channel_id, decision_time, horizon_start, horizon_end,
            status, score, threshold, alert, seen_channel, outcome, outcome_at, object_history)
        VALUES (NULL, 'smoke_alarm', 'baseline-v0', 1003, '2026-05-03 11:00:00+03', '2026-05-03 11:00:00+03',
            '2026-05-04 11:00:00+03', 'scored', 0.9, 0.43, true, false, 'fault', '2026-05-03 11:30:00+03', false)
        """
    )
    m = journal.get(f"{API}/metrics", params={"model": "smoke_alarm"}).json()
    assert (m["open"], m["open_fault"], m["all"]["tp"]) == (1, 1, 1)

    def total(outcome: str) -> int:
        return journal.get(f"{API}/predictions", params={"outcome": outcome}).json()["total"]

    assert (total("open"), total("fault"), total("no_fault"), total("fault,open")) == (1, 1, 1, 2)


def test_models_from_ml_service(client, monkeypatch):
    async def fake_models(self):
        return [
            {
                "name": "smoke_alarm",
                "version": "baseline-v0",
                "family": "baseline",
                "sha256": "abc",
                "features": 38,
                "threshold": 0.431,
                "scoring": "available",
                "periods": {"train": ["2023-01-01", "2025-01-01"], "test": ["2026-01-01", "2026-07-01"]},
                "metrics": {
                    "test": {
                        "precision": 0.85,
                        "recall": 0.82,
                        "f1": 0.83,
                        "average_precision": 0.86,
                        "tp": 442,
                        "fp": 78,
                        "decisions": 8258,
                        "positives": 542,
                    }
                },
            },
            {
                "name": "alarm_all_d7",
                "version": "v3",
                "family": "v3",
                "features": 227,
                "threshold": None,
                "scoring": "waiting_feature_code",
            },
        ]

    monkeypatch.setattr("app.domain.ml_client.MlClient.models", fake_models)
    items = client.get(f"{API}/models").json()["items"]
    assert [(i["model"], i["family"], i["active"]) for i in items] == [
        ("smoke_alarm", "baseline", True),
        ("smoke_alarm", "v3", False),
    ]
    assert items[0]["offline_metrics"]["period"] == "2026-01-01 — 2026-07-01" and items[1]["offline_metrics"] is None


def test_dashboard(journal):
    s = journal.get(f"{API}/dashboard/summary").json()
    assert s["channels_total"] == 3 and s["channels_in_fault"] == 1
    assert len(s["alarms_by_hour"]) == 24 and s["alarms_24h"] == 0  # тревога была двое суток назад
    assert s["live"]["smoke_alarm"] == {
        "precision": 1.0,
        "recall": 1.0,
        "decisions": 2,
        "positives": 1,
        "history": 0,
        "alerts": 1,
        "alerts_confirmed": 1,
        "open": 0,
    }
    r = journal.get(f"{API}/dashboard/risk").json()
    assert r["top"] == [] and r["by_object"][0]["object_id"] == 10 and r["by_object"][0]["in_fault"] == 1


def test_work_orders_lifecycle(client):
    body = {"channel_id": 1001, "priority": "high", "title": "Проверить ДД ПК1", "due_date": "2026-05-02"}
    created = client.post(f"{API}/work-orders", json=body)
    assert created.status_code == 201, created.text
    order = created.json()
    assert order["rule"] == "manual" and order["status"] == "new" and order["object_name"] == "Объект А"
    assert client.post(f"{API}/work-orders", json=body).json()["error"]["code"] == "duplicate"

    patched = client.patch(f"{API}/work-orders/{order['id']}", json={"status": "in_progress", "assignee": "Иванов"})
    assert patched.status_code == 200 and patched.json()["assignee"] == "Иванов"
    done = client.patch(f"{API}/work-orders/{order['id']}", json={"status": "done", "comment": "заменён"}).json()
    assert done["closed_at"] is not None and [h["status"] for h in done["history"]] == ["new", "in_progress", "done"]
    assert done["rule_description"] and done["prediction"] is None
    again = client.patch(f"{API}/work-orders/{order['id']}", json={"status": "in_progress"})
    assert again.status_code == 409 and again.json()["error"]["code"] == "invalid_transition"

    page = client.get(f"{API}/work-orders", params={"status": "done", "object_id": 100}).json()
    assert page["total"] == 1
    assert client.get(f"{API}/work-orders", params={"status": "new,in_progress"}).json()["total"] == 0
    csv_text = client.get(f"{API}/work-orders/export.csv").content.decode("utf-8")
    assert csv_text.startswith("﻿id;created_at;status") and "Проверить ДД ПК1" in csv_text
    assert client.get(f"{API}/work-orders/999").json()["error"]["code"] == "work_order_not_found"


def test_models_pass_offline_report(client, monkeypatch):
    """Офлайн-отчёты базовых моделей (ml/models/*/offline.json) проходят схему `/models` без преобразований."""
    import json
    from pathlib import Path

    models_dir = Path(__file__).resolve().parents[2] / "ml" / "models"
    reports = {p.parent.name: json.loads(p.read_text()) for p in models_dir.glob("*/offline.json")}
    assert {"baseline-v0", "baseline-v0-daily"} <= reports.keys()

    async def fake_models(self):
        return [
            {
                "name": "smoke_alarm",
                "version": "baseline-v0",
                "family": "baseline",
                "features": 38,
                "threshold": 0.431,
                "scoring": "available",
                "offline": reports["baseline-v0"],
            },
            {
                "name": "daily",
                "version": "baseline-v0",
                "family": "baseline",
                "features": 40,
                "threshold": 0.639,
                "scoring": "available",
                "offline": reports["baseline-v0-daily"],
            },
        ]

    monkeypatch.setattr("app.domain.ml_client.MlClient.models", fake_models)
    r = client.get(f"{API}/models")
    assert r.status_code == 200, r.text
    smoke, daily = r.json()["items"]
    assert smoke["offline_metrics"]["period"] == "I полугодие 2026" and smoke["offline"]["period"]["from"]
    assert smoke["offline"]["by_type"] is None and smoke["offline"]["rule"]["name"]
    assert daily["model"] == "daily" and len(daily["sensor_types"]) == len(daily["offline"]["by_type"])


def test_models_pass_external_report(client, monkeypatch):
    """Внешняя модель (этап 14, ml/models/external/*/offline.json) проходит `/models` своим семейством, а не как
    событийная модель хакатона."""
    import json
    from pathlib import Path

    model_dir = Path(__file__).resolve().parents[2] / "ml" / "models" / "external" / "magdeburg-fire"
    report = json.loads((model_dir / "offline.json").read_text())

    async def fake_models(self):
        return [
            {
                "name": "magdeburg_fire",
                "version": "magdeburg-fire",
                "label": "magdeburg-fire-v0",
                "family": "external",
                "features_used": 128,
                "threshold": 0.5,
                "scoring": "available",
                "dataset": "magdeburg",
                "synthetic": False,
                "trigger": "every_60s",
                "eval_scheme": "leave-one-experiment-out",
                "offline": report,
            }
        ]

    monkeypatch.setattr("app.domain.ml_client.MlClient.models", fake_models)
    r = client.get(f"{API}/models")
    assert r.status_code == 200, r.text
    (m,) = r.json()["items"]
    assert (m["model"], m["family"], m["dataset"], m["trigger"], m["active"]) == (
        "magdeburg_fire",
        "external",
        "magdeburg",
        "every_60s",
        True,
    )
    assert m["offline"]["seen"] is None and m["offline"]["by_class"][0]["class"] == "fire"
    assert m["offline_metrics"]["decisions"] == report["test"]["decisions"] and m["offline"]["by_experiment"]
