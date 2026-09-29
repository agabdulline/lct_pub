"""Снимки ответов для контрактных тестов backend совпадают с тем, что сервис отвечает сейчас (app.contract_fixtures).

Упал — поменялся контракт или модель: пересоберите снимки `python -m app.contract_fixtures` и закоммитьте вместе с
правкой, чтобы backend проверял worker и схемы на настоящих ответах.
"""

import pytest

from app.contract_fixtures import OUT, render, snapshots


@pytest.fixture(scope="module")
def current() -> dict[str, dict]:
    return snapshots()


def test_fixtures_are_up_to_date(current):
    stale = [n for n, p in current.items() if not (OUT / f"{n}.json").is_file() or (OUT / f"{n}.json").read_text(
        encoding="utf-8") != render(p)]  # fmt: skip
    assert not stale, f"снимки устарели: {stale} — python -m app.contract_fixtures"


def test_fixtures_cover_families_and_outcomes(current):
    families = {m["family"] for m in current["models"]["models"]}
    assert {"baseline", "v3", "external"} <= families
    names = [n for n in current if n.startswith("external_score_") and not n.endswith("_request")]
    scored = [r for n in names for r in current[n]["results"]]
    assert {True, False} <= {r["alert"] for r in scored if r["status"] == "scored"}
    reasons = {r["skip_reason"] for r in scored if r["status"] == "skipped"}
    assert {"unknown_target", "future_event", "short_history"} <= reasons


def test_alarm_versions_share_format(current):
    """Теневой скоринг: одна пачка тревог — один формат ответа у baseline-v0 и v3, у v3 — свои правила пропуска."""
    v3, v0 = current["alarm_score_v3"], current["alarm_score_baseline"]
    assert v3["model"] == "v3" and v0["model"] == "baseline-v0"
    assert [set(r) for r in v3["results"]] == [set(r) for r in v0["results"]]
    assert {r["status"] for r in v3["results"]} == {"scored", "skipped"}
    assert {"baseline-v0", "v3"} == set(current["models_compare"]["versions"])
