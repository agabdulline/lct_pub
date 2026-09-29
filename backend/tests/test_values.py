import duckdb
import pytest

from app.domain.values import is_fault, parse_number, sql_value_num, sql_value_state, value_num, value_state

TEMP = "Датчик температуры"
VALUES = [
    "0.01",
    "-0.32",
    "18",
    " 5 ",
    "1e3",
    ".5",
    "Норма",
    "nan",
    "inf",
    "1_000",
    "0x10",
    "",
    "01.01.1970 03:00:00",
    "-127",
    "-3276",
    "-3000",
    "-2999",
    "Не определено",
    "Неопределен",
    "В норме от +3 до +40",
    "Температура ниже 3ºC",
]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("0.01", 0.01), ("-0.32", -0.32), ("18", 18.0), ("Норма", None), ("nan", None), ("01.01.1970 03:00:00", None)],
)
def test_parse_number(raw, expected):
    assert parse_number(raw) == expected


def test_temperature_error_codes_are_not_measurements():
    assert value_num("-127", TEMP) is None
    assert value_num("-3276", TEMP) is None
    assert value_num("18", TEMP) == 18.0
    assert value_num("-127", "Газовый датчик") == -127.0
    assert value_state("-127", TEMP) == "Нет данных"
    assert value_state("18", TEMP) is None


def test_state_normalization():
    assert value_state("Не определено", TEMP) == "Неопределен"
    assert value_state("25.04.2020 18:53:27", "Состояние охраны") is None
    assert value_state("Норма", None) == "Норма"


def test_fault_states():
    assert is_fault("Неисправен") and is_fault("Отключено устройство")
    assert not is_fault("Неопределен")


@pytest.mark.parametrize("sensor_type", [TEMP, "Газовый датчик", None])
def test_sql_rules_match_python(sensor_type):
    """Архив (DuckDB) и приём потока (Python) разбирают значения одинаково."""
    con = duckdb.connect()
    con.execute("CREATE TABLE v (value_raw VARCHAR, sensor_type VARCHAR)")
    con.executemany("INSERT INTO v VALUES (?, ?)", [(x, sensor_type) for x in VALUES])
    rows = con.execute(f"SELECT value_raw, {sql_value_num()}, {sql_value_state()} FROM v").fetchall()
    for raw, num, state in rows:
        assert num == value_num(raw, sensor_type), raw
        assert state == value_state(raw, sensor_type), raw
