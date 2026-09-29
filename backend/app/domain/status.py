"""Статус канала — токены интерфейса (`docs/architecture/frontend.md` §2): ok, risk, fault, alarm, ppr, unknown.

Порядок: неисправность → тревога (плановая проверка, если тревога в эпизоде ППР) → риск по модели → неопределён →
норма. Неисправность важнее флага тревоги: у большинства каналов с флагом «тревожное» текущее значение —
«Отключено устройство» или «Неисправен».
"""

from app.domain.values import EVENT_SENSOR_TYPES, FAULT_STATES, SMOKE_ALARM_VALUE, SMOKE_SENSOR

UNDEFINED_STATES = frozenset({"Неопределен", "Не определено"})
STATUSES = ("ok", "risk", "fault", "alarm", "ppr", "unknown")


def in_alarm(sensor_type: str, value_raw: str, is_alarm: bool) -> bool:
    """Значение тревожное. «Обнаружен дым» — тревога независимо от флага, как в ленте."""
    return is_alarm or (sensor_type == SMOKE_SENSOR and value_raw == SMOKE_ALARM_VALUE)


def holds_alarm(sensor_type: str, value_raw: str, is_alarm: bool) -> bool:
    """Канал сейчас в тревоге — по последнему значению. Событийный канал (`EVENT_SENSOR_TYPES`: коды ошибок, записи,
    проезды) тревогу не держит: событие с флагом тревоги — факт в свой момент. Иначе последний код ошибки держал
    агрегат Azure PdM красным до конца прогона (прогон 53: все 100 агрегатов в тревоге с 2015 года)."""
    return sensor_type not in EVENT_SENSOR_TYPES and in_alarm(sensor_type, value_raw, is_alarm)


def channel_status(
    sensor_type: str,
    value_raw: str | None,
    is_alarm: bool | None,
    alarm_kind: str | None = None,
    risk_alert: bool = False,
) -> str:
    """`alarm_kind` — вид эпизода последней дымовой тревоги канала, `risk_alert` — ежедневная модель выше порога."""
    if value_raw is None:
        return "unknown"
    if value_raw in FAULT_STATES:
        return "fault"
    if holds_alarm(sensor_type, value_raw, bool(is_alarm)):
        return "ppr" if alarm_kind == "ppr" else "alarm"
    if risk_alert:
        return "risk"
    if value_raw in UNDEFINED_STATES:
        return "unknown"
    return "ok"


def event_status(sensor_type: str, value_raw: str | None, is_alarm: bool | None) -> str:
    """Статус события в истории канала. У канала состояний — статус канала после события; у событийного — само
    событие: код ошибки с флагом тревоги — тревожное в свой момент."""
    if sensor_type in EVENT_SENSOR_TYPES and value_raw is not None and in_alarm(sensor_type, value_raw, bool(is_alarm)):
        return "alarm"
    return channel_status(sensor_type, value_raw, is_alarm)
