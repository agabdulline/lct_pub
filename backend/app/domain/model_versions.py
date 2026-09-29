"""Версии событийной модели прогона (этап 15.2, теневой скоринг): какие версии ML-сервиса умеют оценивать дымовые
тревоги и какая из них основная.

Версия — строка, которую ML-сервис пишет в ответ скоринга и backend — в `predictions.model_version`: `baseline-v0`,
`v3`. Основная по умолчанию — `settings.primary_alarm_model`, если она доступна, иначе активная у ML-сервиса; все
остальные доступные версии — теневые. Выбор делается при старте прогона и при смене основной (`PATCH /runs/{id}`),
а не в `create_run`: домен прогонов не ходит в ML-сервис.

Связка (`v3>baseline-v0`) — основная из нескольких версий по порядку: решение тревоги даёт первая версия, у которой
оно есть (своё или унаследованное за сутки), — v3, где оценила; где пропустила — v0 (`triage.decide`). Офлайн на
I полугодии 2026 — P 86.9% / R 78.8% (`ml.v3_bridge.fallback`, #241). Прогнозы пишутся на каждую версию связки, как
у теневых; решение тревоги — ссылка `alarms.prediction_id` на прогноз решившей версии.
"""

from app.core.config import settings
from app.domain.ml_client import MlClient, MlUnavailable
from app.domain.runs import RunError

TASK = "smoke_alarm"
CHAIN_SEP = ">"  # связка версий по порядку: «v3>baseline-v0» — v3, где оценила, иначе v0
# пропуск «одно решение на канал в сутки»: действует прошлое решение по каналу (triage.py, `inherited` в API)
INHERIT_REASON = "decided_within_24h"


def chain_of(version: str | None) -> list[str]:
    """Версии по порядку: у связки — её части, у одной версии — она сама, `None` — пусто."""
    return [v for v in version.split(CHAIN_SEP) if v] if version else []


def is_chain(version: str | None) -> bool:
    return len(chain_of(version)) > 1


def decided_sql(alias: str = "") -> str:
    """SQL-условие «прогноз дал вердикт своей тревоги»: ссылка `alarms.prediction_id` на него, и тревога — его.
    Унаследованное решение и оценка версии, за которую решала другая, — не решение."""
    a = alias
    return f"EXISTS (SELECT 1 FROM alarms da WHERE da.id = {a}alarm_id AND da.prediction_id = {a}id)"


def primary_only(alias: str = "") -> str:
    """SQL-условие «прогноз основной версии своего прогона»: у событийной модели прогона с `primary_model` — только
    её прогнозы, у связки — решения тревог (`decided_sql`), у остальных моделей, базы и прогонов без выбора — все.
    Для мест с одним решением на тревогу или канал: вердикт, карточка канала, метрики."""
    a = alias
    primary = f"(SELECT r.primary_model FROM runs r WHERE r.id = {a}run_id)"
    return (
        f"({a}model <> '{TASK}' OR {a}model_version = coalesce({primary}, {a}model_version) "
        f"OR (position('{CHAIN_SEP}' IN coalesce({primary}, '')) > 0 AND {decided_sql(a)}))"
    )


def version_sql(alias: str = "", param: str = "version") -> str:
    """SQL-условие «прогноз версии `:param`» (`NULL` — любой): у связки — её решения тревог (`decided_sql`)."""
    a, v = alias, f"CAST(:{param} AS text)"
    return (
        f"({v} IS NULL OR (position('{CHAIN_SEP}' IN {v}) = 0 AND {a}model_version = {v}) "
        f"OR (position('{CHAIN_SEP}' IN {v}) > 0 AND {a}model_version = ANY (string_to_array({v}, '{CHAIN_SEP}')) "
        f"AND {decided_sql(a)}))"
    )


def verdict_version_sql(alarm: str) -> str:
    """Версия, чьё решение дало вердикт тревоги: версия прогноза по ссылке `alarms.prediction_id` (своего или
    унаследованного), а без ссылки — основная версия прогона. После смены основной прошлые тревоги остаются с той
    версией, что их решила."""
    return f"""coalesce(
        (SELECT vp.model_version FROM alarms va JOIN predictions vp ON vp.id = va.prediction_id WHERE va.id = {alarm}),
        (SELECT r.primary_model FROM alarms va JOIN runs r ON r.id = va.run_id WHERE va.id = {alarm}))"""


def scores_sql(alarm: str, exclude: str | None = None) -> str:
    """JSON-массив оценок тревоги всеми версиями событийной модели (`ModelScore`): версия, прогноз, статус, оценка,
    порог, тревога модели, причина пропуска и `primary` — версия, чьё решение дало вердикт. `exclude` — прогноз,
    который не нужен (`Prediction.paired` — оценки других версий).

    Пропуск «одно решение на канал в сутки» (`decided_within_24h`) у версии, чьё решение по каналу за сутки до этого
    есть, — `inherited`: прогноз, статус, оценка, порог и тревога — из того решения, как у `Alarm.prediction`. Иначе
    под основной версией с унаследованным решением у теневой стояло «пропуск», хотя её решение тоже действует."""
    skip = f" AND q.id <> {exclude}" if exclude else ""
    return f"""(SELECT coalesce(jsonb_agg(jsonb_build_object(
        'model_version', q.model_version, 'prediction_id', coalesce(l.id, q.id),
        'status', coalesce(l.status, q.status),
        'score', CASE WHEN l.id IS NULL THEN q.score ELSE l.score END,
        'threshold', CASE WHEN l.id IS NULL THEN q.threshold ELSE l.threshold END,
        'alert', CASE WHEN l.id IS NULL THEN q.alert ELSE l.alert END,
        'skip_reason', CASE WHEN l.id IS NULL THEN q.skip_reason END,
        'inherited', l.id IS NOT NULL,
        'primary', q.model_version IS NOT DISTINCT FROM v.version
    ) ORDER BY q.model_version), '[]'::jsonb)
    FROM predictions q CROSS JOIN (SELECT {verdict_version_sql(alarm)} AS version) v
    LEFT JOIN LATERAL (
        SELECT p.id, p.status, p.score, p.threshold, p.alert FROM predictions p
        WHERE q.status = 'skipped' AND q.skip_reason = '{INHERIT_REASON}'
          AND p.run_id IS NOT DISTINCT FROM q.run_id AND p.model = q.model AND p.model_version = q.model_version
          AND p.channel_id = q.channel_id AND p.status = 'scored'
          AND p.decision_time < q.decision_time AND p.decision_time >= q.decision_time - interval '24 hours'
        ORDER BY p.decision_time DESC LIMIT 1
    ) l ON true
    WHERE q.alarm_id = {alarm} AND q.model = '{TASK}'{skip})"""


def disagree_sql(alarm: str) -> str:
    """Версии расходятся: тревогу оценили не меньше двух версий, все — без пропуска (унаследованное решение —
    оценка), а тревога модели у них разная. Пропуск одной из версий — не расхождение (`Alarm.disagree = null`).
    Считается по тем же `scores`, что `disagree()`: фильтр и `counts.disagree` совпадают с флагом тревоги."""
    return f"""coalesce((SELECT count(*) >= 2 AND bool_and(e->>'status' = 'scored')
        AND bool_or((e->>'alert')::boolean) <> bool_and((e->>'alert')::boolean)
    FROM jsonb_array_elements({scores_sql(alarm)}) e), false)"""


def disagree(scores: list[dict]) -> bool | None:
    """`Alarm.disagree`: сравнить нечего — одна версия или у какой-то пропуск — None."""
    if len(scores) < 2 or any(s["status"] != "scored" for s in scores):
        return None
    return len({s["alert"] for s in scores}) > 1


def paired_sql(alias: str = "") -> str:
    """Решение парное: его тревогу оценили без пропуска все версии прогона (основная — все части связки — и теневые)
    — метрики версий считаются на одних решениях. У прогона с одной версией парное любое оценённое решение тревоги."""
    a = alias
    return f"""({a}alarm_id IS NOT NULL AND (SELECT count(DISTINCT q.model_version) FROM predictions q
        WHERE q.alarm_id = {a}alarm_id AND q.model = '{TASK}' AND q.status = 'scored')
        >= (SELECT coalesce(cardinality(string_to_array(r.primary_model, '{CHAIN_SEP}')), 1)
                   + cardinality(r.shadow_models) FROM runs r WHERE r.id = {a}run_id))"""


def other_disagrees_sql(alias: str = "") -> str:
    """Решение этой версии и другой версии на той же тревоге — оба без пропуска, а тревога модели разная
    (`/predictions?disagree=true`)."""
    a = alias
    return f"""({a}status = 'scored' AND {a}alarm_id IS NOT NULL AND EXISTS (SELECT 1 FROM predictions q
        WHERE q.alarm_id = {a}alarm_id AND q.model = '{TASK}' AND q.id <> {a}id AND q.status = 'scored'
          AND q.alert IS DISTINCT FROM {a}alert))"""


def _is_alarm_model(m: dict) -> bool:
    if m.get("family") == "external":
        return False
    if m.get("task"):
        return m["task"] == TASK
    return m.get("name") == TASK or (m.get("family") == "v3" and "daily" not in (m.get("name") or ""))


def version_of(m: dict) -> str:
    return m.get("label") or m.get("version") or m.get("name") or "unknown"


def _ready(models: list[dict]) -> tuple[list[str], str | None]:
    """Версии событийной модели, готовые скорить, и активная у ML-сервиса."""
    ready = [m for m in models if _is_alarm_model(m) and m.get("scoring") == "available"]
    return [version_of(m) for m in ready], next((version_of(m) for m in ready if m.get("active")), None)


def chains(versions: list[str]) -> list[str]:
    """Связки, чьи части все готовы: сейчас одна — из настройки основной, если она связка."""
    configured = settings.primary_alarm_model
    return [configured] if is_chain(configured) and all(v in versions for v in chain_of(configured)) else []


def default_version(models: list[dict]) -> str | None:
    """Основная версия новых прогонов: `settings.primary_alarm_model`, если готова (у связки — все её части); у
    связки с неготовой частью — её последняя часть, если готова (v0 без v3); иначе активная у ML-сервиса."""
    versions, active = _ready(models)
    if not versions:
        return None
    configured = settings.primary_alarm_model
    if configured in versions or configured in chains(versions):
        return configured
    fallback = chain_of(configured)[-1] if chain_of(configured) else None
    return fallback if fallback in versions else active or versions[0]


def shadows_of(primary: str | None, candidates: list[str]) -> list[str]:
    """Теневые версии: кандидаты без основной и частей связки, без повторов и без самих связок."""
    taken = set(chain_of(primary))
    return [v for v in dict.fromkeys(candidates) if v not in taken and not is_chain(v)]


async def alarm_versions(ml: MlClient | None = None) -> tuple[list[str], str | None]:
    """Версии событийной модели, которые можно сделать основной, — готовые и связки из готовых, — и основная
    по умолчанию."""
    models = await (ml or MlClient()).models()
    versions = _ready(models)[0]
    return versions + chains(versions), default_version(models)


async def select_versions(
    requested: str | None = None, ml: MlClient | None = None, shadows: list[str] | None = None
) -> tuple[str | None, list[str]]:
    """Основная и теневые версии нового прогона. ML-сервис недоступен — `(None, [])`: одна активная модель, как до
    15.2. `shadows` — теневые версии явно: `None` — все остальные готовые, `[]` — без теневых. Версии нет среди
    готовых — `RunError(code="unknown_version")`."""
    asked = [v for v in [requested, *(shadows or [])] if v is not None]
    try:
        versions, default = await alarm_versions(ml)
    except MlUnavailable:
        if asked:
            message = f"версии {', '.join(asked)} не проверить: ML-сервис недоступен"
            raise RunError(message, code="ml_unavailable") from None
        return None, []
    if unknown := [v for v in asked if v not in versions]:
        raise RunError(
            f"версий событийной модели {', '.join(unknown)} нет среди готовых: {', '.join(versions) or 'нет'}",
            code="unknown_version",
        )
    if not versions:
        return None, []
    primary = requested or default
    return primary, shadows_of(primary, versions if shadows is None else shadows)
