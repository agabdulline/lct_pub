"""Офлайн-метрики для страницы модели: формат блоков, PR-кривая, привязка offline.json к артефакту."""

import json

import numpy as np
from fastapi.testclient import TestClient

from app.main import app
from app.models import _offline
from ml.baseline.offline import block, pr_curve

BLOCK_KEYS = set("decisions positives alerts tp fp fn tn precision recall f1 ap lead_time".split())


def test_block_matches_live_metrics_format():
    y = np.array([1, 0, 1, 0, 0])
    b = block(y, np.array([0.9, 0.8, 0.2, 0.1, 0.1]), 0.5, lead_s=np.array([60, 0, 120, 0, 0]))
    assert set(b) == BLOCK_KEYS
    assert (b["tp"], b["fp"], b["fn"], b["tn"], b["alerts"]) == (1, 1, 1, 2, 2)
    assert b["precision"] == 0.5 and b["recall"] == 0.5 and b["lead_time"]["p50_s"] == 60.0
    quiet = block(y, np.zeros(5), 0.5)  # ни одной тревоги — Precision не определена
    assert quiet["precision"] is None and quiet["recall"] == 0.0 and quiet["f1"] is None and quiet["lead_time"] is None


def test_pr_curve_sorted_by_recall_with_unique_thresholds():
    rng = np.random.default_rng(0)
    y = rng.integers(0, 2, 2000)
    curve = pr_curve(y, y * 0.3 + rng.random(2000), n=50)
    assert 20 <= len(curve) <= 50
    assert [p["recall"] for p in curve] == sorted(p["recall"] for p in curve)
    assert len({p["threshold"] for p in curve}) == len(curve)
    assert curve[-1]["recall"] == 1.0


def test_offline_ignored_for_other_artifact(tmp_path):
    (tmp_path / "offline.json").write_text(json.dumps({"sha256": "abc", "test": {}}), encoding="utf-8")
    assert _offline(tmp_path / "m.cbm", "abc") == {"test": {}}
    assert _offline(tmp_path / "m.cbm", "other") is None
    assert _offline(tmp_path / "missing" / "m.cbm", "abc") is None


def test_models_expose_offline_metrics():
    with TestClient(app) as client:
        models = {m["name"]: m for m in client.get("/v1/models").json()["models"]}
    for name in ("smoke_alarm", "daily", "alarm_all_d7"):  # alarm_all_d7 — событийная v3, отчёт того же формата
        off = models[name]["offline"]
        assert off["model"] == models[name]["task"] and off["threshold"] == models[name]["threshold"]
        for key in ("test", "seen", "unseen"):
            assert set(off[key]) == BLOCK_KEYS
        assert len(off["pr_curve"]) >= 40 and {q["quarter"] for q in off["by_quarter"]} >= {"2025Q3", "2026Q1"}
        assert set(off["rule"]) == BLOCK_KEYS | {"name"} and off["rule"]["ap"] is None
    assert models["smoke_alarm"]["offline"]["by_type"] is None
    assert len(models["daily"]["offline"]["by_type"]) == 8
    kinds = models["smoke_alarm"]["offline"]["by_episode_kind"]
    assert {k["kind"] for k in kinds} == {"ppr", "group", "solitary"}
    assert sum(k["decisions"] for k in kinds) == models["smoke_alarm"]["offline"]["test"]["decisions"]
    assert models["daily"]["offline"]["by_episode_kind"] is None
