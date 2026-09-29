"""Сценарий из своего журнала (docs/api.md, «Сценарий из своего журнала»; PLAN 13.1).

Файл журнала СМВУ в CSV или XLSX — как `журнал_событий_пример.csv` — становится сценарием эмулятора. Колонки могут
идти в любом порядке и называться иначе: поля журнала находятся по названиям и синонимам, недостающее назначается
вручную. Файл один раз переводится в «сырые ячейки» Parquet (все колонки — текст), дальше каждый разбор под
сопоставление — SQL в DuckDB: и сотни мегабайт перечитываются за секунды.

Поля: `channel_id`, `value` и время — обязательны; время — одной колонкой (`datetime`) или двумя (`date` + `time`).
`event_id` и `is_alarm` — нет: без флага тревоги события считаются нетревожными (предупреждение). Время без пояса —
Москва. Срез для эмулятора — в схеме встроенных (`event_id`, `channel_id`, `ts`, `is_alarm`, `value`), без дублей,
по времени; каналы вне справочника остаются в срезе, эмулятор их не передаёт — узлов нет (договорённость с lct-39).
"""

import codecs
import csv
import datetime as dt
import json
import os
import re
import secrets
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

import duckdb

from app.core.config import settings

PREVIEW_ROWS = 20
SAMPLES = 5
EXAMPLES = 5
SNIFF_BYTES = 256 * 1024
SPEED = 60  # скорость эмулятора по умолчанию — для оценки длительности прогона
_MSK = dt.timezone(dt.timedelta(hours=3))

FIELDS: list[tuple[str, str]] = [
    ("event_id", "Ид события"),
    ("channel_id", "Канал"),
    ("datetime", "Дата и время"),
    ("date", "Дата"),
    ("time", "Время"),
    ("is_alarm", "Тревожное"),
    ("value", "Значение"),
]
LABELS = dict(FIELDS)
# Колонки журнала СМВУ в примере организаторов — по ним и совпадение exact, и порядок файла без заголовка
JOURNAL = ["event_id", "channel_id", "date", "time", "is_alarm", "value"]
JOURNAL_NAMES = {
    "event_id": "ид_события",
    "channel_id": "ид_канала_данных",
    "date": "дата",
    "time": "время",
    "is_alarm": "тревожное",
    "value": "значение_датчика",
}
SYNONYMS = {
    "event_id": ["ид_события", "id_события", "ид", "id", "event", "event_id", "номер_события", "№"],
    "channel_id": ["ид_канала_данных", "ид_канала", "id_канала", "канал", "channel", "channel_id", "ид_канала_датчика"],
    "datetime": [
        "дата_время",
        "дата_и_время",
        "датавремя",
        "datetime",
        "date_time",
        "timestamp",
        "ts",
        "время_события",
    ],
    "date": ["дата", "date", "день", "дата_события"],
    "time": ["время", "time"],
    "is_alarm": ["тревожное", "тревога", "тревожный", "флаг_тревоги", "is_alarm", "alarm"],
    "value": ["значение_датчика", "значение", "value", "показание", "состояние", "значение_канала"],
}
_TRUE = ("t", "true", "1", "1.0", "да", "yes", "y", "истина", "д")
_FALSE = ("f", "false", "0", "0.0", "нет", "no", "n", "ложь", "н", "")
_DT_FORMATS = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%d %H:%M",
    "%d.%m.%Y %H:%M:%S",
    "%d.%m.%Y %H:%M:%S.%f",
    "%d.%m.%Y %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%d/%m/%Y %H:%M:%S",
    "%Y-%m-%d",  # полночь: дата без времени, в том числе ячейка Excel
    "%d.%m.%Y",
]


class UploadError(ValueError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _q(path) -> str:
    """Путь литералом SQL: DuckDB не принимает параметры в COPY и CREATE VIEW."""
    return "'" + str(path).replace("'", "''") + "'"


def _duck(work: Path) -> duckdb.DuckDBPyConnection:
    """DuckDB с пределом памяти: разбор идёт в процессе api, у которого на проде лимит 1 ГБ; лишнее — на диск."""
    return duckdb.connect(
        config={
            "memory_limit": settings.upload_duckdb_memory,
            "threads": 2,
            "temp_directory": str(work / "duckdb.tmp"),
        }
    )


def _norm(name: str) -> str:
    return re.sub(r"[\s\-]+", "_", str(name).strip().strip("\"'").lower().replace("ё", "е")).strip("_")


_SYNONYM_INDEX = {_norm(s): f for f, names in SYNONYMS.items() for s in names}


def upload_dir(upload_id: str) -> Path:
    if not re.fullmatch(r"\d{8}-[0-9a-f]{6}", upload_id):
        raise UploadError(404, "upload_not_found", f"загрузки {upload_id} нет")
    return settings.uploads_dir / upload_id


def new_upload_id() -> str:
    return f"{dt.date.today():%Y%m%d}-{secrets.token_hex(3)}"


def scenario_id(upload_id: str) -> str:
    return f"upload-{upload_id}"


# --- формат файла ---------------------------------------------------------------------------------------------------


@dataclass
class FileFormat:
    kind: str  # csv | xlsx
    encoding: str | None
    delimiter: str | None
    sheet: str | None
    header: bool


def kind_of(file_name: str, head: bytes) -> str:
    """CSV или XLSX — по расширению и сигнатуре; старый XLS и прочее не принимаем."""
    ext = Path(file_name).suffix.lower()
    if head.startswith(b"PK\x03\x04") or ext == ".xlsx":
        if not head.startswith(b"PK\x03\x04"):
            raise UploadError(400, "bad_file", "файл с расширением .xlsx — не книга Excel")
        return "xlsx"
    if ext in (".xls",) or head.startswith(b"\xd0\xcf\x11\xe0"):
        raise UploadError(
            415, "unsupported_media_type", "старый формат XLS не поддерживается — сохраните как XLSX или CSV"
        )
    if ext not in ("", ".csv", ".txt", ".tsv"):
        raise UploadError(415, "unsupported_media_type", f"ожидается CSV или XLSX, получен {ext}")
    return "csv"


def _encoding(raw: bytes) -> str:
    if raw.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    try:
        codecs.getincrementaldecoder("utf-8")().decode(raw, final=False)
        return "utf-8"
    except UnicodeDecodeError:
        return "cp1251"


def _delimiter(lines: list[str]) -> str:
    """Разделитель, при котором строки дают одинаковое число колонок и их больше одной."""
    best, best_score = ",", (-1, -1)
    for d in (",", ";", "\t"):
        counts = [len(r) for r in csv.reader(lines, delimiter=d) if r]
        if not counts:
            continue
        mode = max(set(counts), key=counts.count)
        score = (counts.count(mode) if mode > 1 else 0, mode)
        if score > best_score:
            best, best_score = d, score
    return best


def _header_matches(row: list[str]) -> int:
    return sum(1 for c in row if _norm(c) in _SYNONYM_INDEX)


# --- сырые ячейки ---------------------------------------------------------------------------------------------------


def _cell_str(v) -> str:
    """Ячейка XLSX строкой: дата и время — ISO, целое число — без «.0»."""
    if v is None:
        return ""
    if isinstance(v, dt.datetime):  # дата в Excel приходит временем в полночь — отдельная колонка даты
        return v.date().isoformat() if v.time() == dt.time() else v.isoformat(sep=" ", timespec="seconds")
    if isinstance(v, dt.date | dt.time):
        return v.isoformat()
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else format(v, "g")
    return str(v).strip()


def _xlsx_to_csv(path: Path, sheet: str | None, out: Path) -> tuple[str, list[str]]:
    """Лист книги → CSV строками ячеек. Лист не задан — первый непустой."""
    import openpyxl

    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as e:  # noqa: BLE001 — любая ошибка чтения книги — «файл не читается»
        raise UploadError(400, "bad_file", f"книга Excel не читается: {e}") from e
    try:
        sheets = list(wb.sheetnames)
        for name in [sheet] if sheet is not None else sheets:
            with out.open("w", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                written = 0
                for row in wb[name].iter_rows(values_only=True):
                    cells = [_cell_str(v) for v in row]
                    while cells and cells[-1] == "":
                        cells.pop()
                    if cells:
                        w.writerow(cells)
                        written += 1
            if written:
                return name, sheets
    finally:
        wb.close()
    raise UploadError(400, "bad_file", f"лист «{sheet}» пуст" if sheet else "в книге нет листа с данными")


def _transcode(path: Path, encoding: str, out: Path) -> None:
    with path.open("r", encoding=encoding, newline="") as src, out.open("w", encoding="utf-8", newline="") as dst:
        shutil.copyfileobj(src, dst, 1 << 20)


def prepare(path: Path, file_name: str, work: Path, sheet: str | None = None) -> tuple[FileFormat, list[str] | None]:
    """Файл → `cells.parquet` (n, c0…ck — текст) в папке загрузки; возвращает формат и листы XLSX."""
    head = path.open("rb").read(SNIFF_BYTES)
    kind = kind_of(file_name, head[:8])
    sheets = None
    text_csv = path
    encoding: str | None = None
    if kind == "xlsx":
        text_csv = work / "sheet.csv"
        try:
            sheet, sheets = _xlsx_to_csv(path, sheet, text_csv)
        except UploadError:
            text_csv.unlink(missing_ok=True)
            raise
        head = text_csv.open("rb").read(SNIFF_BYTES)
        delimiter = ","
    else:
        encoding = _encoding(head)
        if encoding != "utf-8":
            text_csv = work / "utf8.csv"
            _transcode(path, encoding, text_csv)
            head = text_csv.open("rb").read(SNIFF_BYTES)
        sample = head.decode("utf-8", errors="ignore")
        lines = sample.splitlines()[: -1 if len(head) == SNIFF_BYTES else None][:200]
        delimiter = _delimiter(lines)
    sample_rows = [r for r in csv.reader(head.decode("utf-8", errors="ignore").splitlines()[:5], delimiter=delimiter)]
    first = next((r for r in sample_rows if any(c.strip() for c in r)), [])
    header = _header_matches(first) >= 2
    width = max((len(r) for r in sample_rows), default=0)
    if width == 0:
        raise UploadError(400, "bad_file", "в файле нет строк")
    names = [f"c{i}" for i in range(width)]
    con = _duck(work)
    try:
        columns_sql = ", ".join(f"'{c}': 'VARCHAR'" for c in names)
        delim_sql = "'\t'" if delimiter == "\t" else _q(delimiter)
        con.execute(
            f"""
            COPY (
                SELECT row_number() OVER () AS n, * FROM read_csv({_q(text_csv)}, delim = {delim_sql}, quote = '"',
                    escape = '"', header = {str(header).lower()}, columns = {{{columns_sql}}},
                    null_padding = true, strict_mode = false, auto_detect = false)
            ) TO {_q(work / "cells.parquet")} (FORMAT parquet)
            """
        )
    except duckdb.Error as e:
        raise UploadError(400, "bad_file", f"файл не разбирается как CSV: {e}") from e
    finally:
        con.close()
        if text_csv != path:
            text_csv.unlink(missing_ok=True)
    columns = [c.strip() or f"Колонка {i + 1}" for i, c in enumerate(first)] if header else []
    columns += [f"Колонка {i + 1}" for i in range(len(columns), width)]
    fmt = FileFormat(kind, encoding, delimiter if kind == "csv" else None, sheet, header)
    (work / "columns.json").write_text(json.dumps(columns, ensure_ascii=False), encoding="utf-8")
    return fmt, sheets


def columns_of(work: Path) -> list[str]:
    return json.loads((work / "columns.json").read_text(encoding="utf-8"))


# --- сопоставление --------------------------------------------------------------------------------------------------


def suggest(columns: list[str], header: bool) -> dict[str, tuple[int | None, str | None]]:
    """Поле → (колонка, как нашли: exact | synonym | guess). Без заголовка и с 6 колонками — порядок журнала."""
    found: dict[str, tuple[int | None, str | None]] = {f: (None, None) for f, _ in FIELDS}
    if header:
        for i, name in enumerate(columns):
            n = _norm(name)
            field = _SYNONYM_INDEX.get(n)
            if field is None or found[field][0] is not None:
                continue
            found[field] = (i, "exact" if n == JOURNAL_NAMES.get(field) else "synonym")
    elif len(columns) == len(JOURNAL):
        for i, field in enumerate(JOURNAL):
            found[field] = (i, "guess")
    if found["datetime"][0] is not None and found["date"][0] is not None and found["time"][0] is not None:
        found["datetime"] = (None, None)  # есть отдельные дата и время — как в журнале СМВУ
    return found


def required_missing(mapping: dict[str, int | None]) -> list[str]:
    missing = [f for f in ("channel_id", "value") if mapping.get(f) is None]
    if mapping.get("datetime") is None:
        missing += [f for f in ("date", "time") if mapping.get(f) is None]
    return missing


def _required(field: str, mapping: dict[str, int | None]) -> bool:
    if field in ("channel_id", "value"):
        return True
    if field in ("date", "time"):
        return mapping.get("datetime") is None
    return False


# --- разбор под сопоставление ---------------------------------------------------------------------------------------


def _norm_sql(mapping: dict[str, int | None], columns: list[str], header: bool, cells: Path) -> str:
    def col(field: str) -> str | None:
        i = mapping.get(field)
        return None if i is None else f"nullif(trim(c{int(i)}), '')"

    ch, val, eid, alarm = col("channel_id"), col("value"), col("event_id"), col("is_alarm")
    if mapping.get("datetime") is not None:
        raw_ts = col("datetime")
    else:
        raw_ts = f"({col('date')} || ' ' || {col('time')})"
    formats = "[" + ", ".join(f"'{f}'" for f in _DT_FORMATS) + "]"
    ts = (
        f"CASE WHEN regexp_matches({raw_ts}, '(Z|[+-][0-9]{{2}}:?[0-9]{{2}})$') "
        f"THEN timezone('Europe/Moscow', TRY_CAST({raw_ts} AS TIMESTAMPTZ)) "
        f"ELSE try_strptime({raw_ts}, {formats}) END"
    )
    true_set = ", ".join(f"'{v}'" for v in _TRUE)
    false_set = ", ".join(f"'{v}'" for v in _FALSE)
    alarm_sql = (
        "false"
        if alarm is None
        else f"CASE WHEN lower(coalesce({alarm}, '')) IN ({true_set}) THEN true "
        f"WHEN lower(coalesce({alarm}, '')) IN ({false_set}) THEN false END"
    )
    # целое: «120473» или «120473.0» (так число приходит из Excel)
    int_of = (
        "coalesce(TRY_CAST({x} AS BIGINT), "
        "CASE WHEN regexp_matches({x}, '^[0-9]+\\.0+$') THEN TRY_CAST(split_part({x}, '.', 1) AS BIGINT) END)"
    )
    header_row = "false"
    if header and mapping.get("channel_id") is not None:  # строки-заголовки внутри файла (data-format.md §3)
        name = columns[int(mapping["channel_id"])].replace("'", "''")
        header_row = f"coalesce({ch} = '{name}', false)"
    sql = f"""
    SELECT n,
        {int_of.format(x=eid) if eid else "CAST(NULL AS BIGINT)"} AS event_id,
        CAST({int_of.format(x=ch)} AS INTEGER) AS channel_id,
        CAST({ts} AS TIMESTAMP) AS ts,
        {alarm_sql} AS alarm,
        {val} AS value,
        {raw_ts} AS raw_ts,
        {alarm if alarm else "NULL"} AS raw_alarm
    FROM read_parquet({{cells}}) WHERE NOT {header_row}
    """
    return sql.replace("{cells}", _q(cells))


@dataclass
class Catalog:
    """Справочник каналов: канал → (название, объект уровня 2, название объекта)."""

    channels: dict[int, tuple[str, int, str]]

    def write(self, path: Path) -> None:
        with path.open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            for cid, (name, obj, obj_name) in self.channels.items():
                w.writerow([cid, name, obj, obj_name])


def _fmt_ts(ts: dt.datetime) -> str:
    return ts.strftime("%d.%m.%Y %H:%M")


def _duration(seconds: float) -> str:
    minutes = seconds / SPEED / 60
    if minutes < 90:
        return f"{max(1, round(minutes))} мин"
    hours = minutes / 60
    return f"{hours:.1f} ч".replace(".0 ", " ").replace(".", ",") if hours < 10 else f"{round(hours)} ч"


def analyze(work: Path, mapping: dict[str, int | None], header: bool, catalog: Catalog) -> dict:
    """Разбор сырых ячеек под сопоставление: статистика, предупреждения, превью."""
    columns = columns_of(work)
    cells = str(work / "cells.parquet")
    con = _duck(work)
    try:
        rows_total = con.execute("SELECT count(*) FROM read_parquet(?)", [cells]).fetchone()[0]
        cols_sql = ", ".join(f"coalesce(c{i}, '')" for i in range(len(columns)))
        preview = con.execute(
            f"SELECT {cols_sql} FROM read_parquet(?) ORDER BY n LIMIT {PREVIEW_ROWS}", [cells]
        ).fetchall()
        samples = [
            [
                r[0]
                for r in con.execute(
                    f"SELECT DISTINCT c{i} FROM (SELECT c{i} FROM read_parquet(?) "
                    f"WHERE nullif(trim(c{i}), '') IS NOT NULL ORDER BY n LIMIT 200) LIMIT {SAMPLES}",
                    [cells],
                ).fetchall()
            ]
            for i in range(len(columns))
        ]
        stats = {
            "events": 0,
            "channels": 0,
            "unknown_channels": 0,
            "duplicates": 0,
            "invalid_rows": 0,
            "objects": [],
        }
        warnings: list[dict] = []
        period = None
        missing = required_missing(mapping)
        if not header:
            warnings.append(
                {
                    "code": "no_header",
                    "message": "В файле нет строки заголовка — колонки сопоставлены по порядку журнала СМВУ, проверьте"
                    if any(v is not None for v in mapping.values())
                    else "В файле нет строки заголовка — назначьте колонки вручную",
                }
            )
        for field in missing:
            warnings.append(
                {
                    "code": "field_missing",
                    "field": field,
                    "message": f"Не нашли колонку «{LABELS[field]}» — назначьте вручную",
                }
            )
        if mapping.get("is_alarm") is None:
            warnings.append(
                {
                    "code": "field_missing",
                    "field": "is_alarm",
                    "message": "Нет колонки «Тревожное» — все события будут без флага тревоги",
                }
            )
        if not missing:
            catalog_path = work / "catalog.csv"
            con.execute(
                "CREATE TABLE cat (channel_id INTEGER, channel_name VARCHAR, object_id INTEGER, object_name VARCHAR)"
            )
            if catalog.channels:
                catalog.write(catalog_path)
                con.execute(
                    f"INSERT INTO cat SELECT * FROM read_csv({_q(catalog_path)}, header = false, delim = ',', "
                    "quote = '\"', escape = '\"', auto_detect = false, columns = {'channel_id': 'INTEGER', "
                    "'channel_name': 'VARCHAR', 'object_id': 'INTEGER', 'object_name': 'VARCHAR'})"
                )
            catalog_path.unlink(missing_ok=True)
            con.execute(f"CREATE VIEW norm AS {_norm_sql(mapping, columns, header, work / 'cells.parquet')}")
            con.execute(
                "CREATE VIEW valid AS SELECT * FROM norm "
                "WHERE channel_id IS NOT NULL AND ts IS NOT NULL AND value IS NOT NULL"
            )
            a = con.execute(
                """
                SELECT count(*) AS rows, count(*) FILTER (WHERE channel_id IS NULL) AS bad_channel,
                    count(*) FILTER (WHERE ts IS NULL AND channel_id IS NOT NULL) AS bad_ts,
                    count(*) FILTER (WHERE value IS NULL AND channel_id IS NOT NULL AND ts IS NOT NULL) AS bad_value,
                    count(*) FILTER (WHERE alarm IS NULL) AS bad_alarm
                FROM norm
                """
            ).fetchone()
            v = con.execute(
                """
                SELECT count(*), (SELECT count(*) FROM (SELECT DISTINCT channel_id, event_id, ts, value FROM valid)),
                    min(ts), max(ts), count(DISTINCT channel_id)
                FROM valid
                """
            ).fetchone()
            unknown = con.execute(
                "SELECT channel_id, count(*) FROM valid WHERE channel_id NOT IN (SELECT channel_id FROM cat) "
                "GROUP BY 1 ORDER BY 2 DESC, 1"
            ).fetchall()
            objects = con.execute(
                "SELECT c.object_id, c.object_name, count(DISTINCT v.channel_id) FROM valid v "
                "JOIN cat c USING (channel_id) GROUP BY 1, 2 ORDER BY 3 DESC, 1"
            ).fetchall()
            rows, bad_channel, bad_ts, bad_value, bad_alarm = a
            n_valid, n_distinct, start, end, n_channels = v
            stats.update(
                events=n_distinct,
                channels=n_channels,
                unknown_channels=len(unknown),
                duplicates=n_valid - n_distinct,
                invalid_rows=rows - n_valid,
                objects=[{"id": o, "name": name, "channels": c} for o, name, c in objects],
            )
            if start is not None:
                end_excl = end + dt.timedelta(seconds=1)
                period = {"start": start.replace(tzinfo=_MSK), "end": end_excl.replace(tzinfo=_MSK)}
                warnings.append(
                    {
                        "code": "period",
                        "message": f"Период {_fmt_ts(start)} — {_fmt_ts(end)}: при ×{SPEED} прогон займёт "
                        f"{_duration((end_excl - start).total_seconds())}",
                    }
                )
            if unknown:
                lost = sum(c for _, c in unknown)
                warnings.append(
                    {
                        "code": "unknown_channels",
                        "count": len(unknown),
                        "message": f"{len(unknown)} каналов нет в справочнике — их события ({lost}) не будут переданы",
                        "examples": [str(c) for c, _ in unknown[:EXAMPLES]],
                    }
                )
            if bad_ts:
                examples = con.execute(
                    f"SELECT DISTINCT raw_ts FROM norm WHERE ts IS NULL AND channel_id IS NOT NULL LIMIT {EXAMPLES}"
                ).fetchall()
                warnings.append(
                    {
                        "code": "bad_dates",
                        "count": bad_ts,
                        "message": f"{bad_ts} строк с нераспознанной датой — они будут пропущены",
                        "examples": [str(e[0]) for e in examples],
                    }
                )
            if bad_channel or bad_value:
                warnings.append(
                    {
                        "code": "invalid_rows",
                        "count": bad_channel + bad_value,
                        "message": f"{bad_channel + bad_value} строк без канала или значения — они будут пропущены",
                    }
                )
            if mapping.get("is_alarm") is not None and bad_alarm:
                examples = con.execute(
                    f"SELECT DISTINCT raw_alarm FROM norm WHERE alarm IS NULL LIMIT {EXAMPLES}"
                ).fetchall()
                warnings.append(
                    {
                        "code": "bad_alarm",
                        "count": bad_alarm,
                        "message": f"{bad_alarm} значений «Тревожное» не распознаны — считаем их нетревожными",
                        "examples": [str(e[0]) for e in examples],
                    }
                )
            if stats["duplicates"]:
                warnings.append(
                    {
                        "code": "duplicates",
                        "count": stats["duplicates"],
                        "message": f"Дубли: {stats['duplicates']} строк — будут убраны",
                    }
                )
    finally:
        con.close()
    return {
        "rows": rows_total,
        "columns": [{"index": i, "name": name, "samples": samples[i]} for i, name in enumerate(columns)],
        "preview": [list(r) for r in preview],
        "required_missing": missing,
        "ready": not missing,
        "stats": stats,
        "warnings": warnings,
        "period": period,
    }


def _atomic_write(target: Path, write) -> None:
    tmp = target.with_name(f".{target.name}.tmp")
    write(tmp)
    os.replace(tmp, target)


def build_slice(work: Path, mapping: dict[str, int | None], header: bool, out_dir: Path, sid: str) -> None:
    """Срез в схеме эмулятора: без дублей (канал, ид события, время, значение), по времени."""
    columns = columns_of(work)
    out_dir.mkdir(parents=True, exist_ok=True)
    con = _duck(work)
    try:
        con.execute(f"CREATE VIEW norm AS {_norm_sql(mapping, columns, header, work / 'cells.parquet')}")

        def write(tmp: Path) -> None:
            con.execute(
                f"""
                COPY (
                    SELECT event_id, channel_id, ts, coalesce(alarm, false) AS is_alarm, value FROM (
                        SELECT DISTINCT ON (channel_id, event_id, ts, value) * FROM norm
                        WHERE channel_id IS NOT NULL AND ts IS NOT NULL AND value IS NOT NULL
                        ORDER BY channel_id, event_id, ts, value, n)
                    ORDER BY ts, event_id NULLS FIRST, channel_id
                ) TO {_q(tmp)} (FORMAT parquet)
                """
            )

        _atomic_write(out_dir / f"{sid}.parquet", write)
    finally:
        con.close()


def write_meta(out_dir: Path, sid: str, meta: dict) -> None:
    _atomic_write(
        out_dir / f"{sid}.json",
        lambda tmp: tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=1, default=str), encoding="utf-8"),
    )


# --- состояние загрузки ---------------------------------------------------------------------------------------------


@dataclass
class UploadState:
    id: str
    file_name: str
    stored_name: str
    size_bytes: int
    format: dict
    sheets: list[str] | None
    mapping: dict[str, int | None]
    matches: dict[str, str | None]
    uploaded_at: str

    def save(self, work: Path) -> None:
        (work / "upload.json").write_text(json.dumps(asdict(self), ensure_ascii=False, indent=1), encoding="utf-8")

    @classmethod
    def load(cls, work: Path) -> "UploadState":
        path = work / "upload.json"
        if not path.exists():
            raise UploadError(404, "upload_not_found", f"загрузки {work.name} нет")
        return cls(**json.loads(path.read_text(encoding="utf-8")))


def response(state: UploadState, analysis: dict) -> dict:
    fields = [
        {
            "field": f,
            "label": label,
            "required": _required(f, state.mapping),
            "column": state.mapping.get(f),
            "match": state.matches.get(f),
        }
        for f, label in FIELDS
    ]
    stem = Path(state.file_name).stem
    period = analysis["period"]
    name = f"{stem} — {period['start']:%d.%m.%Y}" if period else stem
    return {
        "id": state.id,
        "file_name": state.file_name,
        "size_bytes": state.size_bytes,
        "format": state.format,
        "sheets": state.sheets,
        "fields": fields,
        "name_suggestion": name,
        **analysis,
    }
