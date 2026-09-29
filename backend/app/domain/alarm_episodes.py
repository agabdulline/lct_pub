"""Эпизоды дымовых тревог на объекте: плановая проверка, группа, одиночная (docs/research/data-findings.md §3).

Эпизод — тревоги объекта уровня 2 с разрывом не больше `gap`. Вид:
- `ppr` — плановая проверка: не меньше `min_channels` датчиков, будний день и «Рычаг сдернут» на объекте рядом
  (или, при `signature: lever_or_hours`, рычаг либо старт в рабочие часы);
- `solitary` — один датчик; `group` — остальное.

Правила — `backend/config/triage.yaml`. Одни и те же в двух видах: Python (приём потока) и SQL для DuckDB
(загрузчик базы); тест проверяет совпадение.
"""

import datetime as dt
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from sqlalchemy import text

CONFIG = Path(__file__).resolve().parents[2] / "config" / "triage.yaml"
MSK = ZoneInfo("Europe/Moscow")


@dataclass(frozen=True)
class EpisodeRules:
    alarm_sensor_type: str
    alarm_value: str
    gap: dt.timedelta
    lever_value: str
    lever_window: dt.timedelta
    ppr_min_channels: int
    ppr_weekdays: frozenset[int]
    ppr_lever_or_hours: bool
    ppr_hours: tuple[int, int]  # [с, до)

    @classmethod
    def from_dict(cls, d: dict) -> "EpisodeRules":
        return cls(
            alarm_sensor_type=d["alarm"]["sensor_type"],
            alarm_value=d["alarm"]["value"],
            gap=dt.timedelta(minutes=d["episodes"]["gap_minutes"]),
            lever_value=d["episodes"]["lever_value"],
            lever_window=dt.timedelta(minutes=d["episodes"]["lever_window_minutes"]),
            ppr_min_channels=d["ppr"]["min_channels"],
            ppr_weekdays=frozenset(d["ppr"]["weekdays"]),
            ppr_lever_or_hours=d["ppr"]["signature"] == "lever_or_hours",
            ppr_hours=tuple(d["ppr"]["start_hours"]),
        )


@cache
def rules(path: Path = CONFIG) -> EpisodeRules:
    return EpisodeRules.from_dict(yaml.safe_load(path.read_text(encoding="utf-8")))


def classify(n_channels: int, start_ts: dt.datetime, has_lever: bool, r: EpisodeRules) -> str:
    """Вид эпизода. Меняется по мере прихода тревог: одиночная → группа → плановая проверка."""
    local = start_ts.astimezone(MSK)
    in_hours = r.ppr_lever_or_hours and r.ppr_hours[0] <= local.hour < r.ppr_hours[1]
    if n_channels >= r.ppr_min_channels and local.isoweekday() in r.ppr_weekdays and (has_lever or in_hours):
        return "ppr"
    return "solitary" if n_channels == 1 else "group"


@dataclass
class AlarmChange:
    """Что изменилось при приёме тревоги — для живой ленты."""

    alarm_id: int
    channel_id: int
    object_id: int
    ts: dt.datetime
    episode_id: int
    kind: str
    verdict: str
    refined_alarm_ids: list[int]  # тревоги эпизода, вердикт которых сменился: до ППР или «нет оценки» → реакция


_RUN = "run_id IS NOT DISTINCT FROM :run"


async def _has_lever(session, run_id, lever_channels: list[int], start: dt.datetime, end: dt.datetime, r) -> bool:
    if not lever_channels:
        return False
    found = await session.scalar(
        text(
            "SELECT EXISTS (SELECT 1 FROM run_events(:run) WHERE channel_id = ANY(:channels) "
            "AND value_raw = :lever AND ts BETWEEN :from AND :to)"
        ),
        {
            "run": run_id,
            "channels": lever_channels,
            "lever": r.lever_value,
            "from": start - r.lever_window,
            "to": end + r.lever_window,
        },
    )
    return bool(found)


async def _refine_to_ppr(session, run_id, episode_id: int) -> list[int]:
    """Эпизод распознан как плановая проверка: вердикт всех его тревог — `ppr`, пометка «уточнён» (A6)."""
    rows = await session.execute(
        text(
            f"UPDATE alarms SET verdict = 'ppr', verdict_refined = true WHERE {_RUN} AND episode_id = :ep "
            "AND verdict <> 'ppr' RETURNING id"
        ),
        {"run": run_id, "ep": episode_id},
    )
    return [r[0] for r in rows]


async def register_alarm(
    session, run_id: int | None, channel_id: int, object_id: int, ts: dt.datetime, lever_channels: list[int]
) -> AlarmChange | None:
    """Принять дымовую тревогу: запись в ленту, продление или создание эпизода объекта, пересмотр вида.

    Тревоги приходят по порядку времени; эпизод продлевается, если последняя тревога объекта была не раньше
    `ts − gap`. Повтор той же тревоги возвращает None."""
    r = rules()
    alarm_id = await session.scalar(
        text(
            "INSERT INTO alarms (run_id, channel_id, object_id, ts, verdict) VALUES (:run, :ch, :obj, :ts, 'pending') "
            "ON CONFLICT DO NOTHING RETURNING id"
        ),
        {"run": run_id, "ch": channel_id, "obj": object_id, "ts": ts},
    )
    if alarm_id is None:
        return None

    episode = (
        await session.execute(
            text(
                f"SELECT id, start_ts, end_ts, channel_ids, n_alarms, kind, has_lever FROM alarm_episodes "
                f"WHERE {_RUN} AND object_id = :obj AND end_ts >= :since AND start_ts <= :ts "
                f"ORDER BY end_ts DESC LIMIT 1 FOR UPDATE"
            ),
            {"run": run_id, "obj": object_id, "since": ts - r.gap, "ts": ts},
        )
    ).first()

    if episode is None:
        start, end, channels, n_alarms, old_kind = ts, ts, [channel_id], 1, None
        has_lever = await _has_lever(session, run_id, lever_channels, start, end, r)
    else:
        start, end = episode.start_ts, max(episode.end_ts, ts)
        channels = sorted(set(episode.channel_ids) | {channel_id})
        n_alarms, old_kind = episode.n_alarms + 1, episode.kind
        has_lever = episode.has_lever or await _has_lever(session, run_id, lever_channels, start, end, r)
    kind = classify(len(channels), start, has_lever, r)

    values = {
        "run": run_id,
        "obj": object_id,
        "start": start,
        "end": end,
        "n_ch": len(channels),
        "n": n_alarms,
        "kind": kind,
        "lever": has_lever,
        "channels": channels,
    }
    if episode is None:
        episode_id = await session.scalar(
            text(
                "INSERT INTO alarm_episodes (run_id, object_id, start_ts, end_ts, n_channels, n_alarms, kind, "
                "has_lever, channel_ids) VALUES (:run, :obj, :start, :end, :n_ch, :n, :kind, :lever, :channels) "
                "RETURNING id"
            ),
            values,
        )
    else:
        episode_id = episode.id
        await session.execute(
            text(
                "UPDATE alarm_episodes SET end_ts = :end, n_channels = :n_ch, n_alarms = :n, kind = :kind, "
                "has_lever = :lever, channel_ids = :channels WHERE id = :id"
            ),
            {**values, "id": episode_id},
        )

    verdict = "ppr" if kind == "ppr" else "pending"
    await session.execute(
        text("UPDATE alarms SET episode_id = :ep, verdict = :verdict WHERE id = :id"),
        {"ep": episode_id, "verdict": verdict, "id": alarm_id},
    )
    refined = []
    if kind == "ppr" and old_kind != "ppr":
        refined = await _refine_to_ppr(session, run_id, episode_id)
    elif kind == "group" and old_kind == "solitary":
        refined = await _group_needs_reaction(session, run_id, episode_id)
    return AlarmChange(alarm_id, channel_id, object_id, ts, episode_id, kind, verdict, refined)


async def _group_needs_reaction(session, run_id, episode_id: int) -> list[int]:
    """Одиночная тревога без оценки стала частью группы — групповой эпизод вне ППР требует реакции (A6)."""
    rows = await session.execute(
        text(
            f"UPDATE alarms SET verdict = 'reaction' WHERE {_RUN} AND episode_id = :ep AND verdict = 'no_score' "
            "RETURNING id"
        ),
        {"run": run_id, "ep": episode_id},
    )
    return [r[0] for r in rows]


async def register_lever(session, run_id: int | None, object_id: int, ts: dt.datetime) -> list[tuple[int, list[int]]]:
    """«Рычаг сдернут» на объекте: отметить эпизоды рядом и пересмотреть их вид. Возвращает (эпизод, уточнённые
    тревоги) для эпизодов, ставших плановой проверкой."""
    r = rules()
    rows = (
        await session.execute(
            text(
                f"UPDATE alarm_episodes SET has_lever = true WHERE {_RUN} AND object_id = :obj AND NOT has_lever "
                f"AND start_ts - :w <= :ts AND end_ts + :w >= :ts RETURNING id, n_channels, start_ts, kind"
            ),
            {"run": run_id, "obj": object_id, "ts": ts, "w": r.lever_window},
        )
    ).all()
    changed = []
    for episode_id, n_channels, start, kind in rows:
        new_kind = classify(n_channels, start, True, r)
        if new_kind != kind:
            await session.execute(
                text("UPDATE alarm_episodes SET kind = :kind WHERE id = :id"), {"kind": new_kind, "id": episode_id}
            )
            if new_kind == "ppr":
                changed.append((episode_id, await _refine_to_ppr(session, run_id, episode_id)))
    return changed


def sql_kind(n_channels: str, start_ts: str, has_lever: str, r: EpisodeRules) -> str:
    """Выражение DuckDB, равное `classify()`; `start_ts` — TIMESTAMPTZ."""
    local = f"timezone('Europe/Moscow', {start_ts})"
    weekdays = ", ".join(str(d) for d in sorted(r.ppr_weekdays))
    in_hours = (
        f"hour({local}) >= {r.ppr_hours[0]} AND hour({local}) < {r.ppr_hours[1]}" if r.ppr_lever_or_hours else "false"
    )
    return (
        f"CASE WHEN {n_channels} >= {r.ppr_min_channels} AND isodow({local}) IN ({weekdays}) "
        f"AND ({has_lever} OR {in_hours}) THEN 'ppr' "
        f"WHEN {n_channels} = 1 THEN 'solitary' ELSE 'group' END"
    )
