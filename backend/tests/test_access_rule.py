"""Правило «вскрытие под охраной» (docs/architecture/access.md §3): движок на синтетическом потоке подобъекта.

Сверка с эталоном на журнале — `python -m app.cli access backfill --check` (нужен архив): итоги 2025 и I полугодия
2026 (`tests/fixtures/access/journal_2025_2026h1.json`) и окна сценариев (`scenarios.json`)."""

import datetime as dt

import pytest

from app.domain.access import MSK, Event, SubObject, evaluate, finish_run, mark_repeated, off_hours, rules

R = rules()
GUARD, DOOR, HATCH = 1, 2, 3
SUB = SubObject(100, 10, "объект А ОС", GUARD, {DOOR: ("КД дверь ПК1", False), HATCH: ("КД люк ПК2", True)})


def t(s: str) -> dt.datetime:
    """Четверг 16.04.2026, по Москве; `+Nd` в конце — через N суток."""
    day = 0
    if "+" in s:
        s, extra = s.split("+")
        day = int(extra.removesuffix("d"))
    return dt.datetime.fromisoformat(f"2026-04-16T{s}").replace(tzinfo=MSK) + dt.timedelta(days=day)


def ev(ts: str, channel: int, value: str, alarm: bool = False) -> Event:
    return Event(t(ts), channel, value, alarm)


ARMED = ev("08:00:00", GUARD, R.armed)


def run(events, until="23:59:59", burst=None, since=None):
    res = evaluate(SUB, events, burst or (lambda c, ts: 1), t(until), R, since=t(since) if since else None)
    return res.incidents, res.skipped


def test_disarm_within_grace_dismisses_otherwise_incident():
    # снятие через 86 с — «снято после открытия»; через 17 мин — инцидент с 5-й минуты, закрыт снятием
    incidents, _ = run(
        [
            ARMED,
            ev("10:00:00", DOOR, R.opened, True),
            ev("10:01:26", GUARD, R.disarmed),
            ev("11:00:00", GUARD, R.armed),
            ev("22:17:10", HATCH, R.opened, True),
            ev("22:18:00", HATCH, R.closed),
            ev("22:20:30", HATCH, R.opened, True),
            ev("22:34:00", GUARD, R.disarmed),
        ]
    )
    dismissed, closed = incidents
    assert (dismissed.status, dismissed.end_reason, dismissed.priority) == ("dismissed", "disarmed", None)
    assert dismissed.end_ts == t("10:01:26") and dismissed.qualified_ts is None
    assert (closed.status, closed.qualified_ts, closed.end_ts) == ("closed", t("22:22:10"), t("22:34:00"))
    assert (closed.armed_since, len(closed.openings), closed.hatch_openings) == (t("11:00:00"), 2, 2)
    assert closed.priority == "high"  # люк и ночь
    assert closed.openings[0].closed_ts == t("22:18:00") and closed.openings[1].closed_ts is None


def test_disarm_exactly_at_grace_still_dismisses():
    incidents, _ = run([ARMED, ev("10:00:00", DOOR, R.opened), ev("10:05:00", GUARD, R.disarmed)])
    assert incidents[0].status == "dismissed"
    incidents, _ = run([ARMED, ev("10:00:00", DOOR, R.opened), ev("10:05:01", GUARD, R.disarmed)])
    assert (incidents[0].status, incidents[0].qualified_ts) == ("closed", t("10:05:00"))


def test_same_second_rules():
    # открытие в секунду постановки на охрану — охрана ещё не стояла
    incidents, _ = run([ev("09:00:00", DOOR, R.opened), ev("09:00:00", GUARD, R.armed)])
    assert incidents == []
    # открытие в секунду снятия: охрана стояла, снятие в ту же секунду — «снято после открытия»
    incidents, _ = run([ARMED, ev("10:00:00", DOOR, R.opened), ev("10:00:00", GUARD, R.disarmed)])
    assert incidents[0].status == "dismissed"
    # секунда с «На охране» и «Снято с охраны» — повтор передачи: состояние не меняется, снятием не считается
    incidents, _ = run(
        [
            ARMED,
            ev("10:00:00", DOOR, R.opened),
            ev("10:01:00", GUARD, R.disarmed),
            ev("10:01:00", GUARD, R.armed),
            ev("10:30:00", DOOR, R.closed),
        ]
    )
    assert (incidents[0].status, incidents[0].end_reason, incidents[0].armed_since) == (
        "closed",
        "quiet",
        t("08:00:00"),
    )


def test_same_second_close_then_open_on_open_door():
    # дверь открыта с 10:00:00; в 10:00:02 «Норма» и «Не замкнут» — закрыли и снова открыли (прогон 55, 06:53:24):
    # закрытие достаётся прежнему открытию, дверь открыта до 10:20, и тишина её ждёт; порядок во входе не важен
    incidents, _ = run(
        [
            ARMED,
            ev("10:00:00", DOOR, R.opened, True),
            ev("10:00:02", DOOR, R.opened, True),
            ev("10:00:02", DOOR, R.closed),
            ev("10:20:00", DOOR, R.closed),
        ]
    )
    (inc,) = incidents
    assert [o.closed_ts for o in inc.openings] == [t("10:00:02"), t("10:20:00")]
    assert (inc.status, inc.end_reason, inc.end_ts) == ("closed", "quiet", t("10:20:00"))
    # у закрытой двери — открыли и закрыли в ту же секунду
    incidents, _ = run([ARMED, ev("11:00:00", DOOR, R.closed), ev("11:00:00", DOOR, R.opened, True)])
    (inc,) = incidents
    assert inc.openings[0].closed_ts == t("11:00:00")


def test_quiet_end_and_opening_at_its_second_continues():
    # тишина — max(последнее открытие + 10 мин, последнее закрытие); открытие ровно в этот момент — тот же инцидент
    incidents, _ = run(
        [
            ARMED,
            ev("10:00:00", DOOR, R.opened),
            ev("10:12:00", DOOR, R.closed),
            ev("10:12:00", DOOR, R.opened),
            ev("10:13:00", DOOR, R.closed),
        ]
    )
    assert len(incidents) == 1
    assert (incidents[0].end_reason, incidents[0].end_ts, len(incidents[0].openings)) == ("quiet", t("10:22:00"), 2)
    # открытая дверь держит инцидент открытым
    incidents, _ = run([ARMED, ev("10:00:00", DOOR, R.opened)], until="12:00:00")
    assert (incidents[0].status, incidents[0].end_ts) == ("open", None)


def test_long_armed_and_burst_are_skipped_but_long_armed_joins_open_incident():
    long_armed = ev("10:00:00+8d", DOOR, R.opened)
    incidents, skipped = run([ARMED, long_armed], until="12:00:00+8d")
    assert incidents == [] and [s for s, _ in skipped] == ["long_armed"]
    # волна переопроса — не открытие
    incidents, skipped = run([ARMED, ev("10:00:00", DOOR, R.opened)], burst=lambda c, ts: 5)
    assert incidents == [] and [s for s, _ in skipped] == ["burst"]
    # в идущий инцидент входят открытия и после 7 суток охраны
    incidents, _ = run(
        [ARMED, ev("07:59:00+7d", DOOR, R.opened), ev("08:01:00+7d", HATCH, R.opened)], until="09:00:00+7d"
    )
    assert len(incidents[0].openings) == 2


def test_watermark_and_run_end():
    events = [ARMED, ev("10:00:00", DOOR, R.opened)]
    incidents, _ = run(events, until="10:04:59")
    assert incidents[0].status == "candidate"  # start + 5 мин позже водяного знака
    incidents, _ = run(events, until="10:05:00")
    assert (incidents[0].status, incidents[0].qualified_ts) == ("open", t("10:05:00"))
    finish_run(incidents, t("10:30:00"))
    assert (incidents[0].status, incidents[0].end_reason, incidents[0].end_ts) == ("closed", "run_end", t("10:30:00"))
    # открытия раньше старта прогона не рассматриваются, состояние охраны — из базы
    incidents, _ = run([ARMED, ev("09:00:00", DOOR, R.opened)], since="09:30:00")
    assert incidents == []


def test_flags_priority_and_repeated():
    assert off_hours(t("22:00:00"), R) and off_hours(t("05:59:59"), R) and not off_hours(t("06:00:00"), R)
    assert off_hours(t("12:00:00+2d"), R)  # суббота
    incidents, _ = run(
        [
            ARMED,
            ev("07:00:00", GUARD, R.faulty),
            ev("12:00:00", DOOR, R.opened),
            ev("12:00:00", GUARD, R.healthy),  # в ту же секунду — после открытия
        ]
    )
    inc = incidents[0]
    assert (inc.guard_faulty, inc.off_hours, inc.priority) == (True, False, "medium")
    mark_repeated(incidents, [t("12:00:00") - dt.timedelta(days=d) for d in (1, 6)], R)
    assert inc.repeated
    mark_repeated(incidents, [t("12:00:00") - dt.timedelta(days=d) for d in (1, 7)], R)
    assert not inc.repeated  # (start − 7 сут, start]: ровно 7 суток назад — вне окна


@pytest.mark.parametrize("value", ["16.04.2026 16:58:23", "01.01.1970 03:00:00", "Норма", "Неопределен"])
def test_other_guard_values_do_not_change_state(value):
    incidents, _ = run([ARMED, ev("09:00:00", GUARD, value), ev("10:00:00", DOOR, R.opened)])
    assert incidents[0].armed_since == t("08:00:00")
