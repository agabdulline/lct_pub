"""Модель доступа «свой или нужна реакция» (lct-8f): артефакт против офлайн-эталона и эндпоинт `/v1/access/score`."""

from pathlib import Path
from unittest.mock import patch

import pandas as pd
from fastapi.testclient import TestClient

from app import history
from app.contract_fixtures import ACCESS_REQUEST, access_world
from app.main import app
from app.models import load_all
from ml.access.features import FEATURES

MODELS = Path(__file__).resolve().parents[2] / "ml" / "models"


def test_model_reproduces_reference_scores():
    """Оценки модели на признаках проверки 2026 (`test_2026.csv`, `%.17g`) — побитово как у обучения."""
    model = next(m for m in load_all(MODELS) if m.family == "access")
    assert (model.task, model.label, model.config["features"]) == ("access_open", "access-open-v0", FEATURES)
    ref = pd.read_csv(MODELS / "access-open" / "test_2026.csv", float_precision="round_trip")
    got = model.model.predict_proba(ref[FEATURES])[:, 1]
    assert len(ref) == 215 and (got == ref["score"].to_numpy()).all()


def test_access_score_endpoint():
    """Точки — как присылает backend; история — подменённая, решение — по порогу модели."""
    with patch.object(history, "access_inputs", lambda run, points: access_world()), TestClient(app) as client:
        r = client.post("/v1/access/score", json=ACCESS_REQUEST)
        assert r.status_code == 200, r.text
        body = r.json()
        models = client.get("/v1/models").json()["models"]
    access = next(m for m in models if m["family"] == "access")
    assert access["task"] == "access_open" and access["author"] == "команда" and len(access["limitations"]) == 4
    day, night = body["results"]
    assert body["model"] == "access-open-v0" and day["status"] == night["status"] == "scored"
    assert not day["alert"] and night["alert"]  # своя дверь днём — «вероятно свой», люк ночью — «нужна реакция»
    assert night["start_ts"] == "2026-03-02T23:30:00+03:00"


def test_access_score_without_points():
    with TestClient(app) as client:
        body = client.post("/v1/access/score", json={"run_id": None, "points": []}).json()
    assert body["results"] == []
