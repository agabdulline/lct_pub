"""Триаж дымовых тревог: скоринг событийной моделью и вердикт ленты (допущение A6, docs/product/v1-scope.md).

Модель оценивает все тревоги объекта, ещё не получившие решения, одним запросом. Вердикт по приоритету:

1. `ppr` — тревога в эпизоде плановой проверки (модель всё равно оценивает, прогноз пишется в журнал);
2. `sensor_fault` — решение модели выше порога;
3. `reaction` — решение модели ниже порога, или решения нет, но эпизод групповой;
4. `no_score` — решения нет у одиночной тревоги: правила допустимости не пройдены или модель недоступна.

Тревоге, пропущенной правилом «одно решение на канал в сутки» (`decided_within_24h`), показывается последнее решение
по каналу за сутки — `prediction.inherited` в API.

Теневой скоринг (этап 15.2): у прогона с `runs.primary_model` тревогу оценивает каждая версия — основная и теневые
(`runs.shadow_models`), прогноз пишется на каждую версию. Вердикт, ссылка `alarms.prediction_id` и заявки — только от
основной версии. Без `primary_model` — как раньше: одна активная модель ML-сервиса.

Связка версий (`v3>baseline-v0`, `model_versions.chain_of`) — основная из нескольких версий по порядку: на шаге
основной оценивают все её части, решение тревоги (`decide`) — первой версии, у которой оно есть: своё или
унаследованное за сутки; v3 пропустила по другой причине — решает v0. Заявки — от решившей версии.
"""

import datetime as dt
import logging
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.expression import bindparam

from app.domain.feed import alarms_by_ids
from app.domain.ml_client import MlClient, MlUnavailable
from app.domain.model_versions import INHERIT_REASON, chain_of, shadows_of
from app.domain.notify import Notification
from app.domain.records import predictions_where

log = logging.getLogger("triage")

MODEL = "smoke_alarm"
HORIZON = dt.timedelta(hours=24)
DECISION_DELAY = dt.timedelta(seconds=1)  # решение — через 1 с после тревоги, когда пришли все записи этой секунды
ML_UNAVAILABLE = "ml_unavailable"
_RUN = "run_id IS NOT DISTINCT FROM :run"


def verdict(episode_kind: str | None, decision: dict | None) -> str:
    """decision — своё или унаследованное решение `{status, alert}`; None — решения нет."""
    if episode_kind == "ppr":
        return "ppr"
    if decision is not None and decision["status"] == "scored":
        return "sensor_fault" if decision["alert"] else "reaction"
    return "reaction" if episode_kind == "group" else "no_score"


@dataclass
class TriageResult:
    alarms: int = 0
    scored: int = 0
    skipped: int = 0
    model_version: str | None = None
    prediction_ids: list[int] = field(default_factory=list)  # решения тревог основной версии (связки) — по ним заявки
    shadow: dict[str, int] = field(default_factory=dict)  # теневая версия → записано прогнозов
    notifications: list[Notification] = field(default_factory=list)


_PENDING = f"""
SELECT a.id, a.channel_id, a.ts, e.kind,
    (SELECT count(DISTINCT n.channel_id) FROM alarms n
     WHERE n.{_RUN} AND n.object_id = a.object_id AND n.channel_id <> a.channel_id
       AND n.ts >= a.ts - interval '1 hour' AND n.ts < a.ts) AS neighbors
FROM alarms a LEFT JOIN alarm_episodes e ON e.id = a.episode_id
WHERE a.{_RUN} AND a.object_id = :object
  AND NOT EXISTS (
      SELECT 1 FROM predictions p
      WHERE p.alarm_id = a.id AND (CAST(:version AS text) IS NULL OR p.model_version = :version)
  )
ORDER BY a.ts, a.id
"""

_INSERT = text(
    f"""
    INSERT INTO predictions (run_id, model, model_version, channel_id, alarm_id, decision_time, horizon_start,
        horizon_end, status, skip_reason, score, threshold, alert, seen_channel, object_history, context, factors)
    VALUES (:run, '{MODEL}', :version, :channel, :alarm, :decision, :decision, :horizon_end, :status, :skip_reason,
        :score, :threshold, :alert, :seen, :object_history, :context, :factors)
    ON CONFLICT (run_id, model, model_version, channel_id, decision_time) DO UPDATE SET alarm_id = EXCLUDED.alarm_id
    RETURNING id
    """
).bindparams(bindparam("context", type_=JSONB), bindparam("factors", type_=JSONB))

_LAST_DECISION = f"""
SELECT id, status, alert FROM predictions
WHERE {_RUN} AND model = '{MODEL}' AND channel_id = :channel AND status = 'scored'
  AND (CAST(:version AS text) IS NULL OR model_version = :version)
  AND decision_time < :decision AND decision_time >= :decision - interval '24 hours'
ORDER BY decision_time DESC LIMIT 1
"""


_DECIDE = f"""
SELECT a.id, a.channel_id, a.verdict, a.prediction_id, e.kind,
    p.id AS pid, p.model_version, p.status, p.alert, p.skip_reason, p.decision_time
FROM alarms a
LEFT JOIN alarm_episodes e ON e.id = a.episode_id
LEFT JOIN predictions p ON p.alarm_id = a.id AND p.model = '{MODEL}'
WHERE a.id = ANY(:ids)
ORDER BY a.ts, a.id, p.id
"""


@dataclass
class Decisions:
    changes: list[dict] = field(default_factory=list)  # {alarm_id, from, to} — тревоги с новым вердиктом или решением
    transitions: Counter = field(default_factory=Counter)  # «было→стало»: n
    decided: list[int] = field(default_factory=list)  # свои решения тревог (прогнозы), не унаследованные
    no_decision: int = 0  # тревог без прогноза ни одной версии основной


@dataclass
class _Resolved:
    alarm: object  # строка `_DECIDE`: вердикт, ссылка, вид эпизода
    decision: dict | None = None  # {status, alert} — решение, давшее вердикт; None — ни одна версия не решила
    shown: int | None = None  # прогноз для ссылки `alarms.prediction_id`; None — прогнозов версий нет
    own: int | None = None  # свой прогноз-решение (не унаследованное) — по нему заявки


async def _resolve(
    session: AsyncSession, run_id: int | None, alarm_ids: list[int], chain: list[str | None]
) -> dict[int, _Resolved]:
    """Решение каждой тревоги по версиям `chain` по порядку: первой версии, у которой оно есть, — своё (оценила) или
    унаследованное (пропуск «одно решение на канал в сутки» при решении этой версии за сутки до этого). Пропуск по
    другой причине — решает следующая версия; не решила ни одна — ссылка на прогноз последней. `None` в `chain` —
    любая версия (прогон без выбора версий). Ничего не пишет."""
    rows: dict[int, list] = {}
    if alarm_ids:
        for r in await session.execute(text(_DECIDE), {"ids": alarm_ids}):
            rows.setdefault(r.id, []).append(r)
    out = {}
    for alarm_id, preds in rows.items():
        a = out[alarm_id] = _Resolved(preds[0])
        own = {p.model_version: p for p in preds if p.pid is not None}
        for version in chain:
            p = own.get(version) if version is not None else max(own.values(), key=lambda q: q.pid, default=None)
            if p is None:
                continue
            a.shown = p.pid
            if p.status == "scored":
                a.decision, a.own = {"status": p.status, "alert": p.alert}, p.pid
                break
            if p.skip_reason == INHERIT_REASON:
                params = {"run": run_id, "channel": a.alarm.channel_id, "decision": p.decision_time, "version": version}
                last = (await session.execute(text(_LAST_DECISION), params)).first()
                if last is not None:
                    a.decision, a.shown = {"status": last.status, "alert": last.alert}, last.id
                    break
    return out


async def decide(session: AsyncSession, run_id: int | None, alarm_ids: list[int], chain: list[str | None]) -> Decisions:
    """Вердикты тревог по решению основной версии, у связки — первой её версии с решением (`_resolve`). Не решила ни
    одна — вердикт без модели. Плановая проверка важнее модели. Коммит — на вызывающем."""
    out = Decisions()
    touched = []
    for alarm_id, r in (await _resolve(session, run_id, alarm_ids, chain)).items():
        a, decision, shown = r.alarm, r.decision, r.shown
        if r.own is not None:
            out.decided.append(r.own)
        if shown is None:
            out.no_decision += 1
            continue
        touched.append(alarm_id)
        new = "ppr" if a.verdict == "ppr" else verdict(a.kind, decision)
        if (new, shown) == (a.verdict, a.prediction_id):
            continue
        # Вид эпизода мог смениться, пока шёл скоринг: плановую проверку вердикт модели не перетирает
        await session.execute(
            text(
                "UPDATE alarms SET prediction_id = :prediction, "
                "verdict = CASE WHEN verdict = 'ppr' THEN 'ppr' ELSE :verdict END WHERE id = :id"
            ),
            {"verdict": new, "prediction": shown, "id": alarm_id},
        )
        out.changes.append({"alarm_id": alarm_id, "from": a.verdict, "to": new})
        if new != a.verdict:
            out.transitions[f"{a.verdict}→{new}"] += 1
    await reconcile_verdicts(session, touched)
    return out


_RECONCILE_PPR = """
UPDATE alarms a SET verdict = 'ppr', verdict_refined = true
FROM alarm_episodes e
WHERE a.id = ANY(:ids) AND e.id = a.episode_id AND e.kind = 'ppr' AND a.verdict <> 'ppr'
RETURNING a.id
"""

_RECONCILE_GROUP = """
UPDATE alarms a SET verdict = 'reaction'
FROM alarm_episodes e
WHERE a.id = ANY(:ids) AND e.id = a.episode_id AND e.kind = 'group' AND a.verdict = 'no_score'
RETURNING a.id
"""

_RECONCILE_FLAG = """
UPDATE alarms SET verdict_refined = false WHERE id = ANY(:ids) AND verdict_refined AND verdict <> 'ppr'
RETURNING id
"""


async def reconcile_verdicts(session: AsyncSession, alarm_ids: list[int]) -> list[int]:
    """Привести вердикты к актуальному виду эпизода (A6): ППР важнее решения модели, групповая тревога без решения
    требует реакции, «уточнён» — только вместе с ППР. Возвращает тревоги, чей вердикт изменился."""
    if not alarm_ids:
        return []
    changed = []
    for sql in (_RECONCILE_PPR, _RECONCILE_GROUP, _RECONCILE_FLAG):
        changed += [r[0] for r in await session.execute(text(sql), {"ids": alarm_ids})]
    return changed


def _ts(value) -> dt.datetime | None:
    return dt.datetime.fromisoformat(value) if isinstance(value, str) else value


async def run_versions(session: AsyncSession, run_id: int | None) -> tuple[str | None, list[str]]:
    """Основная и теневые версии событийной модели прогона; у базы и прогона без выбора — (None, [])."""
    if run_id is None:
        return None, []
    row = (
        await session.execute(text("SELECT primary_model, shadow_models FROM runs WHERE id = :r"), {"r": run_id})
    ).first()
    if row is None or row.primary_model is None:
        return None, []
    return row.primary_model, shadows_of(row.primary_model, row.shadow_models or [])


async def score_object(
    session: AsyncSession, run_id: int | None, object_id: int, ml: MlClient | None = None
) -> TriageResult:
    """Оценить тревоги объекта без решения: основной версией — с вердиктами, теневыми — только прогнозы. Коммит —
    на вызывающем. Задача скоринга делает это в два шага с коммитом между ними (`score_primary`, `score_shadows`):
    вердикт не ждёт теневых версий."""
    ml = ml or MlClient()
    result = await score_primary(session, run_id, object_id, ml)
    shadow = await score_shadows(session, run_id, object_id, ml)
    result.shadow = shadow.shadow
    # `alarm.scored` основной версии заменяет второе, с оценками всех версий: одна транзакция — одно событие
    scored = {n.data["id"] for n in shadow.notifications if n.type == "alarm.scored"}
    result.notifications = [
        n for n in result.notifications if not (n.type == "alarm.scored" and n.data["id"] in scored)
    ] + shadow.notifications
    return result


async def score_primary(
    session: AsyncSession, run_id: int | None, object_id: int, ml: MlClient | None = None
) -> TriageResult:
    """Тревоги объекта без прогноза основной версии (у прогона без выбора версий — без прогноза вовсе): прогнозы,
    вердикты по решению (`decide`), `alarm.scored`. У связки части оценивают по порядку, следующая — только тревоги
    без решения предыдущих: вердикт ждёт v0 лишь там, где v3 пропустила; остальное v0 оценит на шаге теневых.
    `prediction_ids` — свои решения тревог, по ним заявки. Коммит — на вызывающем."""
    ml = ml or MlClient()
    primary, _ = await run_versions(session, run_id)
    chain = chain_of(primary) or [None]
    result, alarm_ids = TriageResult(), []
    for i, version in enumerate(chain):
        part, part_alarms = await _score_version(session, run_id, object_id, version, ml, undecided_by=chain[:i])
        result.alarms = max(result.alarms, part.alarms)
        result.scored += part.scored
        result.skipped += part.skipped
        if part.alarms:
            result.model_version = primary if len(chain) > 1 else part.model_version
        result.notifications += part.notifications
        alarm_ids += [a for a in part_alarms if a not in alarm_ids]
    result.prediction_ids = (await decide(session, run_id, alarm_ids, chain)).decided
    await _scored_notifications(session, run_id, alarm_ids, result)
    return result


async def score_shadows(
    session: AsyncSession, run_id: int | None, object_id: int, ml: MlClient | None = None
) -> TriageResult:
    """Тревоги объекта без прогноза теневых версий и следующих частей связки (там, где решила первая — для
    сравнения версий на одних решениях): только прогнозы; `alarm.scored` — с оценками всех версий (`Alarm.scores`),
    вердикт не меняется. Коммит — на вызывающем."""
    ml = ml or MlClient()
    primary, shadows = await run_versions(session, run_id)
    result, alarm_ids = TriageResult(), []
    for version in [*chain_of(primary)[1:], *shadows]:
        shadow, shadow_alarms = await _score_version(session, run_id, object_id, version, ml)
        if shadow.prediction_ids:
            result.shadow[version] = len(shadow.prediction_ids)
            result.notifications += shadow.notifications
            alarm_ids += [a for a in shadow_alarms if a not in alarm_ids]
    await _scored_notifications(session, run_id, alarm_ids, result)
    return result


async def _scored_notifications(session: AsyncSession, run_id: int | None, alarm_ids: list[int], result) -> None:
    if not alarm_ids:
        return
    now = await session.scalar(text("SELECT max(ts) FROM alarms WHERE id = ANY(:ids)"), {"ids": alarm_ids})
    for alarm in await alarms_by_ids(session, run_id, alarm_ids, now):
        result.notifications.append(Notification(run_id, "alarm.scored", alarm))


async def _score_version(
    session: AsyncSession,
    run_id: int | None,
    object_id: int,
    version: str | None,
    ml: MlClient,
    undecided_by: list[str | None] | None = None,
) -> tuple[TriageResult, list[int]]:
    """Тревоги объекта без прогноза этой версии (`None` — любой) — одним запросом к ML-сервису: только прогнозы,
    вердикты — `decide`. `undecided_by` — предыдущие части связки: тревоги с их решением не запрашиваются. Возвращает
    итог и оценённые тревоги."""
    result = TriageResult()
    params = {"run": run_id, "object": object_id, "version": version}
    pending = (await session.execute(text(_PENDING), params)).all()
    if undecided_by and pending:
        resolved = await _resolve(session, run_id, [a.id for a in pending], undecided_by)
        pending = [a for a in pending if a.id not in resolved or resolved[a.id].decision is None]
    if not pending:
        return result, []
    result.alarms = len(pending)
    try:
        response = await ml.score_alarms(run_id, [(a.channel_id, a.ts) for a in pending], model_version=version)
        results = response["results"]
        response_version = response.get("model") or "unknown"
        if version is not None and response_version != version:
            # ML-сервис без выбора версии оценил бы активной моделью, и прогноз v0 записался бы как v3
            raise MlUnavailable(f"ответила версия {response_version}, а не {version}")
    except MlUnavailable as e:
        log.warning(
            "объект %s, версия %s: скоринг недоступен (%s), %d тревог без оценки",
            object_id,
            version or "активная",
            e.reason,
            len(pending),
        )
        results = [{"status": "skipped", "skip_reason": ML_UNAVAILABLE}] * len(pending)
        response_version = "unavailable"
    result.model_version = version or response_version

    for alarm, r in zip(pending, results, strict=True):
        decision_time = _ts(r.get("decision_time")) or alarm.ts + DECISION_DELAY
        prediction_id = await session.scalar(
            _INSERT,
            {
                "run": run_id,
                # версия прогона — ровно та, что запрошена: по ней ищутся неоценённые тревоги
                "version": version or r.get("model") or response_version,
                "channel": alarm.channel_id,
                "alarm": alarm.id,
                "decision": decision_time,
                "horizon_end": decision_time + HORIZON,
                "status": r["status"],
                "skip_reason": r.get("skip_reason"),
                "score": r.get("score"),
                "threshold": r.get("threshold"),
                "alert": r.get("alert"),
                "seen": r.get("seen_channel"),
                "object_history": r.get("object_history"),
                "context": {"alarm_kind": alarm.kind, "neighbors_alarmed_1h": alarm.neighbors},
                "factors": r.get("factors"),
            },
        )
        result.prediction_ids.append(prediction_id)
        if r["status"] == "scored":
            result.scored += 1
        else:
            result.skipped += 1
    for p in await predictions_where(session, "p.id = ANY(:ids)", {"ids": result.prediction_ids}, "ORDER BY p.id"):
        result.notifications.append(Notification(run_id, "prediction.created", p))
    return result, [a.id for a in pending]
