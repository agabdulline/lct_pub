"""Реестр моделей: все .cbm из ml/models/<версия>/ загружаются на старте (docs/architecture/ml-service.md §2).

Семейства: `v3` — модели ML-разработчика (PR #114, код признаков — ml/v3 через ml/v3_bridge), `baseline` — базовая
модель команды (ml/baseline), `external` — модели внешних наборов. Скорит модель с задачей в config.json (`model`);
у v3 без config.json (ежедневная — ждёт кода входов, Q2) скоринга нет. Активная модель задачи — первая со скорингом:
её берёт запрос без `model_version`.
Офлайн-метрики для страницы модели — offline.json рядом с моделью (ml.baseline.offline); берутся, только если
посчитаны для этого же артефакта (sha256).

Ежедневный прогноз по типам (15.4): `daily_routing.json` — какая модель ведёт какой тип датчика. `each_channel` —
отдельные модели типов ML-разработчика; скорят только те, у кого есть маршруты, — через адаптер
`ml.daily_routing.online`. Ансамбль ИБП своего .cbm не имеет — его запись строится по `config.json` ансамбля.
"""

import hashlib
import importlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from catboost import CatBoostClassifier

log = logging.getLogger(__name__)


@dataclass
class LoadedModel:
    name: str
    version: str
    path: Path
    sha256: str
    model: CatBoostClassifier | None  # у внешних моделей без CatBoost (нормы, правила) — None, артефакт в artifact
    config: dict = field(default_factory=dict)
    offline: dict | None = None
    artifact: object | None = None
    active: bool = False  # первая модель со скорингом своей задачи — её берёт запрос без model_version
    routes: list[str] = field(default_factory=list)  # типы датчиков, которые модель ведёт в ежедневном прогнозе

    @property
    def family(self) -> str:
        return self.config.get("family", "v3")

    @property
    def label(self) -> str:
        """baseline-v0 для базовой модели, v3 для моделей ML-разработчика, у внешних — label из config."""
        if "label" in self.config:
            return self.config["label"]
        return f"{self.family}-{self.config['version']}" if "version" in self.config else self.version

    @property
    def model_id(self) -> str:
        """Id модели в ответе ежедневного прогноза и в журнале: label, у отдельных моделей типов — имя."""
        return self.name if self.family == "each_channel" else self.label

    @property
    def task(self) -> str | None:
        """Задача модели: smoke_alarm, daily или задача внешней модели (magdeburg_fire, vast_gate…). У отдельной
        модели типа задачи нет, пока таблица маршрутизации не дала ей типы."""
        return self.config.get("model") or ("daily" if self.routes else None)

    @property
    def scoring(self) -> bool:
        return self.task is not None and self.family in ("baseline", "external", "v3", "each_channel", "access")

    @property
    def threshold(self) -> float | None:
        return self.config.get("threshold")

    @property
    def seen_channels(self) -> set[int]:
        return set(self.config.get("seen_channels", []))

    def describe(self) -> dict:
        m = self.model
        if m is None:  # артефакт не CatBoost: деревьев и важности нет, признаки — из config
            names = self.config.get("features", [])
            tree = {"trees": None, "features": len(names), "features_used": len(names), "categorical": []}
        else:
            importance = m.get_feature_importance()
            tree = {
                "trees": m.tree_count_,
                "features": len(m.feature_names_),
                "features_used": int((importance > 0).sum()),
                "categorical": [m.feature_names_[i] for i in m.get_cat_feature_indices()],
            }
        return {
            "name": self.name,
            "task": self.task,
            "active": self.active,
            "version": self.version,
            "label": self.label,
            "family": self.family,
            "file": self.path.name,
            "sha256": self.sha256,
            **tree,
            "threshold": self.threshold,
            "scoring": "available" if self.scoring else "waiting_feature_code",
            "periods": self.config.get("periods"),
            "metrics": self.config.get("metrics"),
            # внешние данные (docs/architecture/external-data.md): название задачи, набор, синтетика, ритм, проверка
            "title": self.config.get("title"),
            "dataset": self.config.get("dataset"),
            "synthetic": self.config.get("synthetic"),
            "trigger": self.config.get("trigger"),
            "eval_scheme": self.config.get("eval_scheme"),
            # цель и исход — для worker (backend.md): канал цели, горизонт, как закрывать исход по разметке набора
            **{k: self.config.get(k) for k in EXTERNAL_OUTCOME},
            "offline": self.offline,
            "routes": self.routes,
            # модель доступа (lct-8f): автор, офлайн-итог с интервалами и ориентиром-правилом, ограничения для «Моделей»
            **{k: self.config[k] for k in ACCESS_PASSTHROUGH if k in self.config},
        }


ACCESS_PASSTHROUGH = ("author", "offline_metrics", "limitations", "trained_on")  # из config.json как есть
EXTERNAL_OUTCOME = ("target", "target_tag", "horizon", "outcome", "label_key", "positive_labels", "ignore_labels",
                    "history_minutes", "decision_lag_s", "unseen_windows")  # fmt: skip


def load_all(model_dir: Path) -> list[LoadedModel]:
    loaded = []
    for path in sorted(model_dir.glob("**/*.cbm")):  # внешние — ml/models/external/<набор>-<задача>/
        model = CatBoostClassifier()
        model.load_model(str(path))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        cfg_path = path.parent / "config.json"
        config = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
        if config and config.get("sha256") not in (None, digest):
            raise RuntimeError(f"{path}: sha256 не совпадает с config.json — модель и конфиг из разных версий")
        loaded.append(LoadedModel(path.stem, path.parent.name, path, digest, model, config, _offline(path, digest)))
    loaded += [_artifact_model(cfg) for cfg in sorted(model_dir.glob("external/*/config.json")) if _has_artifact(cfg)]
    tasks: set[str] = set()
    for m in loaded:  # порядок — как у путей: baseline-v0 раньше v3, поэтому без model_version скорит v0
        if m.scoring and m.task not in tasks:
            m.active = True
            tasks.add(m.task)
    return loaded


def _has_artifact(cfg_path: Path) -> bool:
    artifact = json.loads(cfg_path.read_text(encoding="utf-8")).get("artifact")
    return bool(artifact) and not artifact.endswith(".cbm")


def _artifact_model(cfg_path: Path) -> LoadedModel:
    """Внешняя модель без CatBoost: артефакт из config.artifact читает module.load_artifact того же кода, что обучал."""
    config = json.loads(cfg_path.read_text(encoding="utf-8"))
    path = cfg_path.parent / config["artifact"]
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if config.get("sha256") not in (None, digest):
        raise RuntimeError(f"{path}: sha256 не совпадает с config.json — модель и конфиг из разных версий")
    artifact = importlib.import_module(config["module"]).load_artifact(path)
    return LoadedModel(path.stem, cfg_path.parent.name, path, digest, None, config, _offline(path, digest), artifact)


def _offline(path: Path, digest: str) -> dict | None:
    """Офлайн-метрики модели без sha256 — или None, если файла нет или он посчитан для другого артефакта."""
    off_path = path.parent / "offline.json"
    if not off_path.exists():
        return None
    offline = json.loads(off_path.read_text(encoding="utf-8"))
    if offline.pop("sha256", None) != digest:
        log.warning("%s: offline.json посчитан для другой модели — офлайн-метрики не отдаём", path)
        return None
    return offline


def load_compare(model_dir: Path, loaded: list[LoadedModel]) -> dict[str, dict]:
    """Сравнение версий задачи на общих решениях проверки — `compare_<задача>.json` (ml.v3_bridge.offline).

    Отдаётся, только если sha256 всех версий в файле совпадают с загруженными моделями: после переобучения любой из
    них старое сравнение не покажется.
    """
    out = {}
    for path in sorted(model_dir.glob("compare_*.json")):
        compare = json.loads(path.read_text(encoding="utf-8"))
        versions = compare.pop("sha256", {})
        sha = {m.label: m.sha256 for m in loaded if m.task == compare["model"]}  # label у задач общий: baseline-v0
        if not versions or any(sha.get(v) != digest for v, digest in versions.items()):
            log.warning("%s: сравнение посчитано для других моделей — не отдаём", path)
            continue
        out[compare["model"]] = compare
    return out


@dataclass
class Routing:
    """Таблица маршрутизации ежедневного прогноза: `ml/models/daily_routing.json` (ml.daily_routing.compare)."""

    version: str
    sha256: str
    routes: dict[str, str]  # тип датчика → id модели
    config: dict

    def describe(self) -> dict:
        c = self.config
        return {
            "version": self.version,
            "sha256": self.sha256,
            "routes": c["routes"],
            "unrouted": c.get("unrouted", []),
            "rule": c.get("rule"),
            "report": c.get("report"),
            "test": c.get("test"),
            "offline": c.get("offline"),
        }


def load_routing(model_dir: Path, loaded: list[LoadedModel]) -> Routing | None:
    """Маршруты — моделям; ансамбль без своего .cbm добавляется записью; адаптеры отдельных моделей — к записям.

    Модель из маршрута, которой нет среди загруженных, — ошибка старта: иначе тип молча остался бы без прогноза."""
    path = model_dir / "daily_routing.json"
    if not path.exists():
        return None
    from ml.daily_routing.online import ADAPTERS

    raw = path.read_bytes()
    cfg = json.loads(raw)
    for model_id, spec in cfg["models"].items():
        folder = (model_dir / spec["config"]).parent
        if spec.get("adapter") == "ensemble":
            cfg_path = model_dir / spec["config"]
            config = json.loads(cfg_path.read_text(encoding="utf-8"))
            digest = hashlib.sha256(cfg_path.read_bytes()).hexdigest()
            loaded.append(LoadedModel(model_id, folder.name, cfg_path, digest, None, config, None))
        if spec.get("adapter"):
            model = next((m for m in loaded if m.model_id == model_id), None)
            if model is None:
                raise RuntimeError(f"daily_routing.json: модели {model_id} нет среди загруженных")
            model.artifact = ADAPTERS[spec["adapter"]](folder)
    # у событийной и ежедневной v0 label общий (baseline-v0): маршруты — только ежедневным и отдельным моделям типов
    by_id = {m.model_id: m for m in loaded if m.config.get("model") == "daily" or m.family == "each_channel"}
    routes = {}
    for r in cfg["routes"]:
        model = by_id.get(r["model"])
        if model is None:
            raise RuntimeError(f"daily_routing.json: модели {r['model']} нет среди загруженных")
        model.routes.append(r["sensor_type"])
        routes[r["sensor_type"]] = r["model"]
    return Routing(cfg["version"], hashlib.sha256(raw).hexdigest(), routes, cfg)
