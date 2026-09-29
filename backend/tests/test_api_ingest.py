CSV = """"ид_события","ид_канала_данных","дата","время","тревожное","значение_датчика"
4524243389,1003,"2026-08-01","03:09:27",false,"28"
4524243390,1001,"2026-08-01","03:10:00",true,"Обнаружен дым"
"ид_события","ид_канала_данных","дата","время","тревожное","значение_датчика"
4524243391,9999,"2026-08-01","03:11:00",false,"Норма"
"""


def test_ingest_json(client):
    body = [
        {"channel_id": 1002, "ts": "2026-05-01T12:00:00+03:00", "value": 0.05},
        {"channel_id": 1001, "ts": "2026-05-01T12:00:01+03:00", "value": "Обнаружен дым", "is_alarm": True},
    ]
    r = client.post("/api/v1/ingest/events", json=body)
    assert r.status_code == 202, r.text
    assert r.json() == {"accepted": 2, "duplicates": 0, "unknown_channels": 0, "alarms": 1, "alarms_enqueued": 0}
    assert client.post("/api/v1/ingest/events", json=body).json()["duplicates"] == 2


def test_ingest_csv_like_sample_journal(client):
    r = client.post("/api/v1/ingest/events", content=CSV.encode(), headers={"Content-Type": "text/csv"})
    assert r.status_code == 202, r.text
    assert r.json()["accepted"] == 2 and r.json()["unknown_channels"] == 1 and r.json()["alarms"] == 1


def test_ingest_errors_have_common_shape(client):
    r = client.post("/api/v1/ingest/events", json=[{"channel_id": "x"}])
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
    r = client.post("/api/v1/ingest/events?run=777", json=[])
    assert r.status_code == 404 and r.json()["error"] == {"code": "run_not_found", "message": "прогон 777 не найден"}
    r = client.post("/api/v1/ingest/events", content=b"1,2,3", headers={"Content-Type": "text/csv"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_csv"
    r = client.post("/api/v1/ingest/events", content=b"x", headers={"Content-Type": "application/xml"})
    assert r.status_code == 415
