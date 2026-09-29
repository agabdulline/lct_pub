"""Разбор значений датчиков — одни правила для архива, загрузчика и приёма событий (docs/data-format.md §5, §8).

Правила есть в двух видах: функции Python (приём потока) и выражения SQL для DuckDB (сборка архива и загрузка).
Тест `tests/test_values.py` проверяет, что оба вида дают одинаковый результат.
"""

import math
import re

FAULT_STATES = frozenset({"Неисправен", "Отключено устройство"})
SMOKE_SENSOR = "Датчик дыма"
SMOKE_ALARM_VALUE = "Обнаружен дым"
LEVER_PULLED_VALUE = "Рычаг сдернут"
GAS_SENSOR = "Газовый датчик"
MOTION_SENSOR = "Датчик движения"
TEMPERATURE_SENSOR = "Датчик температуры"
NO_DATA_STATE = "Нет данных"
# Событийные каналы внешних наборов: значение — факт в свой момент (код ошибки, запись ТО или отказа, проезд,
# считывание пропуска), а не состояние до следующего значения. Тревогу канала они не держат (status.py)
EVENT_SENSOR_TYPES = frozenset(
    {"Код ошибки", "Журнал ТО", "Отказы и ремонты", "Датчик проезда", "Считыватель зоны", "Мобильный считыватель"}
)

# Коды «нет данных» у температуры: −127 — DS18B20 «датчик не отвечает», ≤ −3000 — заглушка 16-битного значения
_TEMPERATURE_NO_DATA = -127.0
_TEMPERATURE_STUB_MAX = -3000.0
_STATE_ALIASES = {"Не определено": "Неопределен"}

NUMBER_PATTERN = r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?"
DATE_PATTERN = r"\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}:\d{2}"  # мусор в «Состояние охраны»: даты вместо состояний
_NUMBER = re.compile(NUMBER_PATTERN)
_DATE = re.compile(DATE_PATTERN)


def parse_number(value_raw: str) -> float | None:
    """Число из `значение_датчика` или None для текстового состояния."""
    s = value_raw.strip()
    if not _NUMBER.fullmatch(s):
        return None
    x = float(s)
    return x if math.isfinite(x) else None


def _is_temperature_code(x: float, sensor_type: str | None) -> bool:
    return sensor_type == TEMPERATURE_SENSOR and (x == _TEMPERATURE_NO_DATA or x <= _TEMPERATURE_STUB_MAX)


def value_num(value_raw: str, sensor_type: str | None) -> float | None:
    """Числовое показание для итогов и графиков. Коды ошибок температуры — не измерения, возвращается None."""
    x = parse_number(value_raw)
    return None if x is None or _is_temperature_code(x, sensor_type) else x


def value_state(value_raw: str, sensor_type: str | None) -> str | None:
    """Нормализованное текстовое состояние; None — у числовых показаний и мусорных дат."""
    x = parse_number(value_raw)
    if x is not None:
        return NO_DATA_STATE if _is_temperature_code(x, sensor_type) else None
    if _DATE.fullmatch(value_raw.strip()):
        return None
    return _STATE_ALIASES.get(value_raw, value_raw)


def is_fault(value_raw: str) -> bool:
    return value_raw in FAULT_STATES


def _sql_str(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _sql_number(value: str) -> tuple[str, str]:
    """(условие «значение — конечное число», само число) для DuckDB."""
    num = f"TRY_CAST(trim({value}) AS DOUBLE)"
    return f"(regexp_full_match(trim({value}), {_sql_str(NUMBER_PATTERN)}) AND isfinite({num}))", num


def _sql_temperature_code(num: str, sensor_type: str) -> str:
    return (
        f"({sensor_type} IS NOT DISTINCT FROM {_sql_str(TEMPERATURE_SENSOR)} "
        f"AND ({num} = {_TEMPERATURE_NO_DATA} OR {num} <= {_TEMPERATURE_STUB_MAX}))"
    )


def sql_value_num(value: str = "value_raw", sensor_type: str = "sensor_type") -> str:
    """Выражение DuckDB, равное `value_num()`."""
    is_num, num = _sql_number(value)
    return f"CASE WHEN {is_num} AND NOT {_sql_temperature_code(num, sensor_type)} THEN {num} END"


def sql_value_state(value: str = "value_raw", sensor_type: str = "sensor_type") -> str:
    """Выражение DuckDB, равное `value_state()`."""
    is_num, num = _sql_number(value)
    aliases = " ".join(f"WHEN {value} = {_sql_str(k)} THEN {_sql_str(v)}" for k, v in _STATE_ALIASES.items())
    return (
        f"CASE WHEN {is_num} THEN "
        f"CASE WHEN {_sql_temperature_code(num, sensor_type)} THEN {_sql_str(NO_DATA_STATE)} END "
        f"WHEN regexp_full_match(trim({value}), {_sql_str(DATE_PATTERN)}) THEN NULL "
        f"{aliases} ELSE {value} END"
    )
