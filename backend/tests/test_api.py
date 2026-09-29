from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_live():
    assert client.get("/api/v1/health/live").json() == {"status": "ok"}


def test_health_lists_components():
    body = client.get("/api/v1/health").json()
    assert body["status"] in {"ok", "degraded"}
    assert {"db", "redis", "ml_service"} <= body["components"].keys()


def test_clock_falls_back_to_real_time(seeded):
    """Часы выбирают прогон — нужна база; без стенда тест пропускается, как остальные тесты с БД."""
    body = client.get("/api/v1/clock").json()
    assert body["mode"] in {"real", "stream"}
    assert body["now"]


def test_openapi_available():
    assert client.get("/api/v1/openapi.json").status_code == 200
