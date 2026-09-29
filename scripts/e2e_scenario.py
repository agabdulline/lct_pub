"""Сквозная проверка сценария эмулятора (docs/PLAN.md 11.1): сценарий → OPC UA → шлюз → приём → триаж → скоринг →
прогнозы → правила заявок.

    make e2e SCENARIO=solitary-fault            # на стенде этого worktree, скорость — предел сценария
    make e2e SCENARIO=ppr ARGS=--interrupt      # перезапуск эмулятора посреди прогона
    make e2e SCENARIO="ext-magdeburg-cable ext-magdeburg-welding"   # подряд: граница прогонов
    make e2e UPLOAD=data/raw/журнал_событий_пример.csv   # свой журнал: загрузка → сценарий → прогон (13.1)
    cd backend && uv run python ../scripts/e2e_scenario.py ppr --speed 30

Запускает сценарий через `/api/v1/emulator/start`, ждёт конца и затишья конвейера (скоринг с дебаунсом, правила)
и проверяет прогон по БД стенда: события прогона построчно против среза, вердикты тревог, прогнозы, исходы, заявки
по правилам и «историю» сценария — то, ради чего он задуман. Несколько сценариев идут подряд, следующий — сразу после
конца предыдущего: так проверяется, что первые события нового сценария не уходят в прошлый прогон
(emulator.md §7, «Прогон»). Отчёт — в консоль и JSON в `data/interim/e2e/`.

`--upload <файл>` — сценарий из своего журнала (13.1): `POST /scenario-uploads` → `PUT dry_run=false` → сценарий
`upload-<id>` в эмуляторе → прогон, события которого сверяются построчно со срезом `uploaded/<id>.parquet`. Удалить
загрузку и сценарий API не даёт: остаются `data/uploads/<id>/`, срез с метаданными и прогон — отчёт их называет.
Код возврата — 1, если не прошла хотя бы одна проверка истории или не сошлись события.

Нужен venv backend (duckdb, SQLAlchemy, psycopg): запускать через `uv run` из `backend/`.
"""

import argparse
import datetime as dt
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
SLICES = ROOT / "data" / "processed" / "emulator"
# эталон «вскрытия под охраной» на окнах сценариев (docs/architecture/access.md, README там же)
ACCESS_SCENARIOS = ROOT / "backend" / "tests" / "fixtures" / "access" / "scenarios.json"
MSK = ZoneInfo("Europe/Moscow")
REPORTS = ROOT / "data" / "interim" / "e2e"
UPLOADED = "uploaded"  # сценарии из своего журнала (13.1) — в этой подпапке срезов


def stand_env(path: Path) -> dict[str, str]:
    env = {"API_PORT": "18000", "POSTGRES_PORT": "15432", "POSTGRES_USER": "lct", "POSTGRES_PASSWORD": "lct"}
    env |= {"POSTGRES_DB": "lct", "COMPOSE_PROJECT_NAME": "lct"}
    if path.exists():
        for line in path.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and not key.startswith("#"):
                env[key.strip()] = value.strip()
    return env


class Api:
    def __init__(self, base: str) -> None:
        self.base = base

    def call(self, method: str, path: str, body: dict | None = None, attempts: int = 3) -> dict:
        """Чтение (`GET`) при 503 повторяется — до `attempts` попыток: прогон 73 оборвался на единичном 503
        `/emulator/state`, когда api на 16 с не получил процессор. Изменения не повторяются."""
        data = None if body is None else json.dumps(body).encode()
        for attempt in range(1, attempts + 1):
            req = urllib.request.Request(self.base + path, data=data, method=method)
            req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                detail = e.read().decode()[:300]
                if method == "GET" and e.code == 503 and attempt < attempts:
                    print(f"  {method} {path}: 503, попытка {attempt} из {attempts} — повтор через {2 * attempt} с")
                    time.sleep(2 * attempt)
                    continue
                raise SystemExit(f"{method} {path}: {e.code} {detail}") from e
        raise AssertionError("unreachable")

    def upload(self, path: str, file: Path) -> dict:
        """`multipart/form-data` с полем `file`, как у браузера: имя файла — UTF-8 как есть."""
        boundary = uuid.uuid4().hex
        head = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{file.name}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        body = head + file.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(self.base + path, data=body, method="POST")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            raise SystemExit(f"POST {path}: {e.code} {e.read().decode()[:300]}") from e


# --- что считаем ---------------------------------------------------------------------------------------------------


def slice_path(scenario: str, slices: Path = SLICES) -> Path:
    """Срез сценария: встроенные — в папке срезов, из своего журнала (`upload-<id>`) — в `uploaded/`."""
    folder = slices / UPLOADED if scenario.startswith("upload-") else slices
    return folder / f"{scenario}.parquet"


def slice_stats(path: Path, smoke_channels: set[int]) -> dict:
    con = duckdb.connect()
    rows, unique = con.execute(
        f"SELECT count(*), count(DISTINCT (channel_id, ts, value)) FROM read_parquet('{path}')"
    ).fetchone()
    smoke = con.execute(
        f"SELECT count(DISTINCT (channel_id, ts)) FROM read_parquet('{path}') "
        "WHERE value = 'Обнаружен дым' AND list_contains($ids, channel_id)",
        {"ids": sorted(smoke_channels)},
    ).fetchone()[0]
    return {"rows": rows, "unique": unique, "smoke_alarms": smoke, "path": str(path)}


def rows_match(conn, path: Path, run_id: int) -> dict:
    """События прогона построчно против среза: (канал, секунда по Москве, значение) — ни потерянных, ни чужих.
    Каналы вне справочника базы шлюз не принимает — их в сверке нет."""
    channels = sorted(c for (c,) in conn.execute(text("SELECT id FROM channels")))
    expected = set(
        duckdb.connect()
        .execute(
            "SELECT DISTINCT channel_id, strftime(ts, '%Y-%m-%d %H:%M:%S'), value "
            f"FROM read_parquet('{path}') WHERE list_contains($ids, channel_id)",
            {"ids": channels},
        )
        .fetchall()
    )
    got = {
        tuple(r)
        for r in conn.execute(
            text(
                "SELECT channel_id, to_char(ts AT TIME ZONE 'Europe/Moscow', 'YYYY-MM-DD HH24:MI:SS'), value_raw "
                "FROM events WHERE run_id = :r UNION "
                "SELECT channel_id, to_char(ts AT TIME ZONE 'Europe/Moscow', 'YYYY-MM-DD HH24:MI:SS'), value_raw "
                "FROM events_raw WHERE run_id = :r"
            ),
            {"r": run_id},
        )
    }
    missing, extra = sorted(expected - got), sorted(got - expected)
    detail = f"в срезе {len(expected)}, в прогоне {len(got)}: не пришло {len(missing)}, чужих {len(extra)}"
    if missing:
        detail += f"; не пришло, первые: {missing[:3]}"
    if extra:
        detail += f"; чужие, первые: {extra[:3]}"
    return check("события прогона = срез построчно", not missing and not extra, detail)


def _msk(value: str | None) -> str | None:
    return dt.datetime.fromisoformat(value).astimezone(MSK).isoformat() if value else None


def _access_rows(api: "Api", run_id: int) -> list[dict]:
    """Инциденты прогона из `/access-incidents` в виде эталона `scenarios.json`, по началу."""
    items = api.call("GET", f"/access-incidents?run={run_id}&status=open,closed,dismissed&size=200")["items"]
    rows = []
    for i in sorted((i for i in items if i["run_id"] == run_id), key=lambda i: i["start_ts"]):
        rows.append(
            {
                "object_id": i["object_id"],
                "sub_object_id": i["sub_object_id"],
                "status": i["status"],
                "start_ts": _msk(i["start_ts"]),
                "qualified_ts": _msk(i["qualified_ts"]),
                "end_ts": _msk(i["end_ts"]),
                "end_reason": i["end_reason"],
                "armed_since": _msk(i["armed_since"]),
                "disarmed_ts": _msk(i["end_ts"]) if i["end_reason"] == "disarmed" else None,
                "openings": i["openings"],
                "hatch_openings": i["hatch_openings"],
                "channels": [{"id": c["id"], "name": c["name"]} for c in i["channels"]],
                "flags": i["flags"],
                "priority": i["priority"],
                "_work_order_id": i["work_order_id"],
            }
        )
    return rows


def access_story(api: "Api", scenario: str, run_id: int, timeout: float = 60) -> list[dict]:
    """Вскрытие под охраной: инциденты прогона — поле в поле как в эталоне `scenarios.json`, заявки
    `access_open_armed` — по одной на инцидент с приоритетом инцидента. Окончательный пересчёт закрытого прогона
    приходит с тактом `runs.tick` — ждём, пока ответ не совпадёт или не перестанет меняться."""
    want = json.loads(ACCESS_SCENARIOS.read_text(encoding="utf-8")).get(scenario)
    if want is None:
        return []
    started, previous, rows = time.monotonic(), None, []
    while time.monotonic() - started < timeout:
        rows = _access_rows(api, run_id)
        plain = [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]
        if plain == want["incidents"] or (plain and plain == previous):
            break
        previous = plain
        time.sleep(5)
    plain = [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]
    diff = [
        f"{w.get('start_ts')}: {k} эталон {w.get(k)!r}, в прогоне {g.get(k)!r}"
        for w, g in zip(want["incidents"], plain, strict=False)
        for k in w
        if w.get(k) != g.get(k)
    ]
    if len(plain) != len(want["incidents"]):
        diff.insert(0, f"инцидентов в эталоне {len(want['incidents'])}, в прогоне {len(plain)}")
    checks = [
        check(
            "вскрытие под охраной = эталон scenarios.json поле в поле",
            not diff,
            "; ".join(diff[:5]) if diff else ", ".join(f"{r['status']} {r['start_ts'][11:19]}" for r in plain),
        )
    ]
    orders = {
        o["id"]: o
        for o in api.call("GET", f"/work-orders?run={run_id}&size=200")["items"]
        if o["rule"] == "access_open_armed"
    }
    # по эталону — заявка на каждый инцидент (не «снято после открытия») с его приоритетом; второй инцидент при
    # открытой заявке подобъекта ссылается на неё — в окнах сценариев такого нет
    expected = sorted(i["priority"] for i in want["incidents"] if i["status"] in ("open", "closed"))
    got = sorted(o["priority"] for o in orders.values())
    linked = all(r["_work_order_id"] in orders for r in rows if r["status"] in ("open", "closed"))
    checks.append(
        check(
            "заявки access_open_armed — на каждый инцидент, приоритет инцидента",
            got == expected and linked,
            f"в эталоне {expected}, в прогоне {got}" + ("" if linked else "; инцидент без заявки"),
        )
    )
    return checks


def one(conn, sql: str, **params):
    return conn.execute(text(sql), params).scalar()


def table(conn, sql: str, **params) -> dict:
    return {str(k): v for k, v in conn.execute(text(sql), params).all()}


# Событийная модель — основная версия прогона (15.2): теневая оценивает те же тревоги, но вердикты, заявки и проверки
# историй — по основной. У связки (`v3>baseline-v0`) — по решившей версии: прогнозы, давшие вердикт своей тревоге
# (`alarms.prediction_id`). Прогон без выбора версий — все прогнозы
PRIMARY = (
    "(model <> 'smoke_alarm' OR CASE WHEN position('>' IN coalesce((SELECT primary_model FROM runs WHERE id = :r), ''))"
    " > 0 THEN EXISTS (SELECT 1 FROM alarms da WHERE da.id = alarm_id AND da.prediction_id = predictions.id) "
    "ELSE model_version IS NOT DISTINCT FROM coalesce((SELECT primary_model FROM runs WHERE id = :r), model_version) "
    "END)"
)


def versions(conn, run_id: int) -> dict:
    """Сводка по версиям событийной модели и расхождения (15.2): решения, тревоги, сбывшиеся; тревоги, где обе
    версии оценили без пропуска, а тревога модели у одной; `decided_by` — сколько тревог решила каждая версия
    (вердикт по её оценке, своей или унаследованной), у связки — кто решал."""
    primary, shadows = conn.execute(
        text("SELECT primary_model, shadow_models FROM runs WHERE id = :r"), {"r": run_id}
    ).one()
    rows = conn.execute(
        text(
            "SELECT model_version, count(*) FILTER (WHERE status = 'scored') AS scored, "
            "count(*) FILTER (WHERE alert) AS alerts, count(*) FILTER (WHERE alert AND outcome = 'fault') AS confirmed "
            "FROM predictions WHERE run_id = :r AND model = 'smoke_alarm' GROUP BY 1 ORDER BY 1"
        ),
        {"r": run_id},
    ).all()
    disagree = one(
        conn,
        "SELECT count(*) FROM (SELECT alarm_id FROM predictions WHERE run_id = :r AND model = 'smoke_alarm' "
        "AND alarm_id IS NOT NULL GROUP BY alarm_id HAVING count(*) >= 2 AND bool_and(status = 'scored') "
        "AND bool_or(alert) <> bool_and(alert)) d",
        r=run_id,
    )
    decided_by = table(
        conn,
        "SELECT p.model_version, count(*) FROM alarms a JOIN predictions p ON p.id = a.prediction_id "
        "WHERE a.run_id = :r AND p.model = 'smoke_alarm' AND p.status = 'scored' GROUP BY 1 ORDER BY 1",
        r=run_id,
    )
    return {
        "primary": primary,
        "shadow": list(shadows or []),
        "by_version": {v: {"scored": sc, "alerts": al, "confirmed": cf} for v, sc, al, cf in rows},
        "decided_by": decided_by,
        "disagree": disagree,
    }


def collect(conn, run_id: int) -> dict:
    r = {"run": run_id}
    r["events"] = {
        "events": one(conn, "SELECT count(*) FROM events WHERE run_id = :r", r=run_id),
        "events_raw": one(conn, "SELECT count(*) FROM events_raw WHERE run_id = :r", r=run_id),
    }
    r["alarms"] = {
        "total": one(conn, "SELECT count(*) FROM alarms WHERE run_id = :r", r=run_id),
        "verdicts": table(
            conn, "SELECT verdict, count(*) FROM alarms WHERE run_id = :r GROUP BY 1 ORDER BY 2 DESC", r=run_id
        ),
        "refined": one(conn, "SELECT count(*) FROM alarms WHERE run_id = :r AND verdict_refined", r=run_id),
    }
    r["episodes"] = table(
        conn, "SELECT kind, count(*) FROM alarm_episodes WHERE run_id = :r GROUP BY 1 ORDER BY 1", r=run_id
    )
    r["predictions"] = {
        "by_model_status": table(
            conn,
            f"SELECT model || ' ' || status, count(*) FROM predictions WHERE run_id = :r AND {PRIMARY} "
            "GROUP BY 1 ORDER BY 1",
            r=run_id,
        ),
        "alerts": table(
            conn,
            f"SELECT model, count(*) FROM predictions WHERE run_id = :r AND alert AND {PRIMARY} GROUP BY 1",
            r=run_id,
        ),
        "skip_reasons": table(
            conn,
            f"SELECT skip_reason, count(*) FROM predictions WHERE run_id = :r AND status = 'skipped' AND {PRIMARY} "
            "GROUP BY 1 ORDER BY 2 DESC",
            r=run_id,
        ),
        "outcomes": table(
            conn,
            "SELECT coalesce(outcome, 'open'), count(*) FROM predictions WHERE run_id = :r AND status = 'scored' "
            f"AND {PRIMARY} GROUP BY 1",
            r=run_id,
        ),
        "versions": table(
            conn, "SELECT model_version, count(*) FROM predictions WHERE run_id = :r GROUP BY 1", r=run_id
        ),
    }
    r["model_versions"] = versions(conn, run_id)
    r["work_orders"] = table(
        conn, "SELECT rule, count(*) FROM work_orders WHERE run_id = :r GROUP BY 1 ORDER BY 1", r=run_id
    )
    r["work_orders_priority"] = table(
        conn,
        "SELECT rule || ' ' || priority, count(*) FROM work_orders WHERE run_id = :r GROUP BY 1 ORDER BY 1",
        r=run_id,
    )
    r["fault_episodes_started"] = one(
        conn,
        "SELECT count(*) FROM fault_episodes WHERE run_id = :r "
        "AND start_ts >= (SELECT sim_start FROM runs WHERE id = :r)",
        r=run_id,
    )
    return r


# --- истории сценариев ---------------------------------------------------------------------------------------------


def check(name: str, ok: bool, detail) -> dict:
    return {"check": name, "ok": bool(ok), "detail": detail}


def model_orders_match_alerts(conn, run_id: int) -> dict:
    """A12: заявка alarm_model_alert — на каждый канал с тревогой основной версии событийной модели (у связки —
    решившей), в проверке тоже; тревоги теневой версии и нерешившей части связки заявок не дают (15.2)."""
    channels = one(
        conn,
        "SELECT count(DISTINCT channel_id) FROM predictions WHERE run_id = :r AND model = 'smoke_alarm' AND alert "
        f"AND {PRIMARY}",
        r=run_id,
    )
    orders = one(
        conn,
        "SELECT count(DISTINCT channel_id) FROM work_orders WHERE run_id = :r AND rule = 'alarm_model_alert'",
        r=run_id,
    )
    return check(
        "заявки alarm_model_alert — на каждый канал с тревогой модели",
        channels == orders,
        f"каналов с тревогой {channels}, с заявкой {orders}",
    )


# Связка версий (15.5b): оценки первой и второй части по каждой тревоге и ссылка вердикта
_CHAIN_PARTS = """
SELECT a.prediction_id, f.id AS first_id, f.status AS first_status, f.skip_reason AS first_reason,
    n.id AS next_id, n.status AS next_status
FROM alarms a JOIN runs r ON r.id = a.run_id
JOIN predictions f ON f.alarm_id = a.id AND f.model = 'smoke_alarm'
    AND f.model_version = split_part(r.primary_model, '>', 1)
LEFT JOIN predictions n ON n.alarm_id = a.id AND n.model = 'smoke_alarm'
    AND n.model_version = split_part(r.primary_model, '>', 2)
WHERE a.run_id = :r
"""


def chain_decisions(conn, run_id: int, require_fallback: bool) -> dict:
    """15.5b: у связки `v3>baseline-v0` вердикт тревоги — от v3, где она оценила; где пропустила не по правилу «одно
    решение в сутки» (там действует её прошлое решение), а v0 оценила, — от v0. `require_fallback` — в сценарии v3
    должна пропускать (01.08.2026: до него месяц без журнала, правило покрытия v3)."""
    own = wrong_own = fallback = wrong_fallback = 0
    reasons: dict[str, int] = {}
    for r in conn.execute(text(_CHAIN_PARTS), {"r": run_id}):
        if r.first_status == "scored":
            own += 1
            wrong_own += r.prediction_id != r.first_id
        elif r.first_reason != "decided_within_24h" and r.next_status == "scored":
            fallback += 1
            wrong_fallback += r.prediction_id != r.next_id
            reasons[r.first_reason] = reasons.get(r.first_reason, 0) + 1
    ok = wrong_own == 0 and wrong_fallback == 0 and (fallback > 0 or not require_fallback)
    return check(
        "связка: решает v3, где оценила; где пропустила — v0"
        + (" (путь «v3 пропустила → v0» пройден)" if require_fallback else ""),
        ok,
        f"v3 решила {own} (ссылка не на неё — {wrong_own}), v0 за пропуск v3 {fallback} "
        f"(ссылка не на v0 — {wrong_fallback}), причины пропуска v3 {reasons}",
    )


def story(conn, scenario: str, run_id: int, report: dict) -> list[dict]:
    verdicts = report["alarms"]["verdicts"]
    total = report["alarms"]["total"]
    if scenario == "ppr":
        ppr = verdicts.get("ppr", 0)
        return [
            check("эпизод плановой проверки распознан", report["episodes"].get("ppr", 0) >= 1, report["episodes"]),
            check("≥ 80% тревог — «плановая проверка»", total and ppr / total >= 0.8, f"{ppr} из {total}"),
            model_orders_match_alerts(conn, run_id),
        ]
    if scenario == "ppr-failures":
        ppr = verdicts.get("ppr", 0)
        scored = report["predictions"]["by_model_status"].get("smoke_alarm scored", 0)
        alerts = report["predictions"]["alerts"].get("smoke_alarm", 0)
        caught = one(
            conn,
            "SELECT count(*) FROM predictions WHERE run_id = :r AND model = 'smoke_alarm' AND alert "
            f"AND outcome = 'fault' AND {PRIMARY}",
            r=run_id,
        )
        return [
            check("эпизод плановой проверки распознан", report["episodes"].get("ppr", 0) >= 1, report["episodes"]),
            check("≥ 80% тревог — «плановая проверка»", total and ppr / total >= 0.8, f"{ppr} из {total}"),
            check(
                "модель отмечает большинство датчиков проверки",
                scored and alerts / scored >= 0.8,
                f"{alerts} тревог модели из {scored} решений",
            ),
            model_orders_match_alerts(conn, run_id),
            check("≥ 80% отмеченных ушли в «Неисправен»", alerts and caught / alerts >= 0.8, f"{caught} из {alerts}"),
        ]
    if scenario == "solitary-fault":
        channel = 93527
        alarm = conn.execute(
            text(
                "SELECT a.ts, a.verdict, p.score, p.threshold, p.alert, p.outcome, p.skip_reason FROM alarms a "
                "LEFT JOIN predictions p ON p.id = a.prediction_id WHERE a.run_id = :r AND a.channel_id = :c "
                "ORDER BY a.ts LIMIT 1"
            ),
            {"r": run_id, "c": channel},
        ).first()
        order = one(
            conn,
            "SELECT count(*) FROM work_orders WHERE run_id = :r AND channel_id = :c AND rule = 'alarm_model_alert'",
            r=run_id,
            c=channel,
        )
        detail = None if alarm is None else {k: str(v) for k, v in alarm._mapping.items()}
        return [
            check("первая тревога 93527 записана", alarm is not None, detail),
            check("вердикт — «вероятная неисправность»", alarm is not None and alarm.verdict == "sensor_fault", detail),
            check("заявка alarm_model_alert на 93527", order >= 1, order),
            check(
                "исход прогноза — fault («Неисправен» в 08:01:55)",
                alarm is not None and alarm.outcome == "fault",
                detail,
            ),
        ]
    if scenario == "sample-2026-08-01":
        gas_events, gas_raw = conn.execute(
            text(
                "SELECT (SELECT count(*) FROM events e JOIN channels c ON c.id = e.channel_id "
                "        WHERE e.run_id = :r AND c.sensor_type = 'Газовый датчик'), "
                "       (SELECT count(*) FROM events_raw e JOIN channels c ON c.id = e.channel_id "
                "        WHERE e.run_id = :r AND c.sensor_type = 'Газовый датчик')"
            ),
            {"r": run_id},
        ).one()
        faults, powered_off = conn.execute(
            text(
                "SELECT (SELECT count(*) FROM fault_episodes WHERE run_id = :r "
                "        AND start_ts >= (SELECT sim_start FROM runs WHERE id = :r)), "
                "       (SELECT count(DISTINCT channel_id) FROM events WHERE run_id = :r AND value_raw = 'Обесточен')"
            ),
            {"r": run_id},
        ).one()
        pending = verdicts.get("pending", 0)
        daily = {k: v for k, v in report["predictions"]["by_model_status"].items() if k.startswith("daily")}
        return [
            check(
                "газ через зону нечувствительности: в рабочей базе меньше, чем в сыром потоке",
                gas_raw > 0 and gas_events < gas_raw,
                f"events {gas_events}, events_raw {gas_raw}",
            ),
            check(
                "эпизоды неисправности открыты, «Обесточен» дошёл",
                faults > 0 and powered_off > 0,
                f"эпизодов неисправности {faults}, каналов «Обесточен» {powered_off}",
            ),
            check("заявки по правилам без ML (справочно)", True, report["work_orders_priority"]),
            check(
                "дымовые тревоги дня получили вердикт",
                total == report["slice"]["smoke_alarms"] and pending == 0,
                f"тревог {total} из {report['slice']['smoke_alarms']} в срезе, вердикты {verdicts}",
            ),
            check(
                "ежедневный прогноз на дату старта (справочно)",
                True,
                f"{daily}, пропуски {report['predictions']['skip_reasons']}",
            ),
        ]
    if scenario == "new-object":
        scored = report["predictions"]["by_model_status"].get("smoke_alarm scored", 0)
        blind, top, threshold = conn.execute(
            text(
                "SELECT count(*) FILTER (WHERE object_history IS false), max(score), max(threshold) FROM predictions "
                f"WHERE run_id = :r AND model = 'smoke_alarm' AND status = 'scored' AND {PRIMARY}"
            ),
            {"r": run_id},
        ).one()
        alerts = report["predictions"]["alerts"].get("smoke_alarm", 0)
        return [
            check("модель оценила тревоги объекта", scored > 0, f"{scored} решений"),
            check(
                "у ≥ 80% оценённых нет истории объекта или шлейфа (object_history = false)",
                scored and blind / scored >= 0.8,
                f"без истории {blind} из {scored}",
            ),
            check(
                "тревог модели нет — слепая зона", alerts == 0, f"{alerts}; макс. оценка {top} при пороге {threshold}"
            ),
            check("заявки по правилам без ML (справочно)", True, report["work_orders_priority"]),
        ]
    if scenario == "power-outage":
        off = one(
            conn,
            "SELECT max(n) FROM (SELECT date_trunc('minute', ts), count(DISTINCT channel_id) n FROM events "
            "WHERE run_id = :r AND value_raw = 'Обесточен' GROUP BY 1) m",
            r=run_id,
        )
        return [
            check("≥ 100 каналов «Обесточен» за минуту", (off or 0) >= 100, off),
            check(
                "неисправности оборудования открыты группой",
                report["fault_episodes_started"] >= 40,
                report["fault_episodes_started"],
            ),
        ]
    if scenario == "ordinary-day":
        daily = sum(v for k, v in report["predictions"]["by_model_status"].items() if k.startswith("daily"))
        return [
            check("ежедневный прогноз в 00:10 сделан", daily > 0, daily),
            check("газ транслируется", report["events"]["events_raw"] > 0, report["events"]),
        ]
    if scenario == "subobject-week":
        days = one(
            conn,
            "SELECT count(DISTINCT (decision_time AT TIME ZONE 'Europe/Moscow')::date) FROM predictions "
            "WHERE run_id = :r AND model = 'daily'",
            r=run_id,
        )
        return [
            check("ежедневный прогноз — каждый день недели", (days or 0) >= 6, days),
            check("плановая проверка в пятницу распознана", report["episodes"].get("ppr", 0) >= 1, report["episodes"]),
        ]
    return []


# --- прогон --------------------------------------------------------------------------------------------------------


def wait_finished(api: Api, timeout: float) -> dict:
    started = time.monotonic()
    last = None
    while time.monotonic() - started < timeout:
        state = api.call("GET", "/emulator/state")
        if state["state"] in ("finished", "stopped"):
            return state
        progress = (state["events_sent"], state["events_total"], state["effective_speed"])
        if progress != last:
            sent = f"{state['events_sent']}/{state['events_total']}"
            print(f"  {state['sim_time'][:19]}  {sent}  ×{state['effective_speed']}")
            last = progress
        time.sleep(5)
    raise SystemExit(f"сценарий не доигран за {timeout:.0f} с")


def wait_quiet(conn_factory, run_id: int, timeout: float = 180) -> float:
    """Конвейер после конца сценария: дебаунс скоринга 5 с, скоринг, правила. Ждём, пока тревоги без вердикта
    не кончатся и счётчики не перестанут меняться."""
    started, previous, stable = time.monotonic(), None, 0
    while time.monotonic() - started < timeout:
        with conn_factory() as conn:
            snapshot = conn.execute(
                text(
                    "SELECT (SELECT count(*) FROM alarms WHERE run_id = :r AND verdict = 'pending'), "
                    "(SELECT count(*) FROM predictions WHERE run_id = :r), "
                    "(SELECT count(*) FROM work_orders WHERE run_id = :r), "
                    "(SELECT count(*) FROM events WHERE run_id = :r) "
                    "+ (SELECT count(*) FROM events_raw WHERE run_id = :r)"
                ),
                {"r": run_id},
            ).one()
        stable = stable + 1 if snapshot == previous and snapshot[0] == 0 else 0
        if stable >= 3:
            return time.monotonic() - started
        previous = snapshot
        time.sleep(5)
    return time.monotonic() - started


def interrupt(api: Api, engine, run_id: int, project: str, wait_s: float = 40, timeout: float = 180) -> dict:
    """Перезапуск эмулятора посреди прогона: шлюз должен закрыть прогон как прерванный (`stopped`,
    `params.stop_reason = interrupted`), а не держать его активным до следующего запуска."""
    time.sleep(wait_s)  # дольше отсрочки шлюза для новых прогонов (30 с)
    print("  перезапуск эмулятора")
    subprocess.run(["docker", "restart", f"{project}-emulator-1"], check=True, capture_output=True)
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT status, finished_at IS NOT NULL AS closed, params->>'stop_reason' AS reason FROM runs "
                    "WHERE id = :r"
                ),
                {"r": run_id},
            ).one()
        if row.status != "running":
            break
        time.sleep(5)
    state = api.call("GET", "/emulator/state")["state"]
    detail = {
        "status": row.status,
        "finished_at": row.closed,
        "stop_reason": row.reason,
        "emulator": state,
        "closed_after_s": round(time.monotonic() - started),
    }
    ok = row.status == "stopped" and row.closed and row.reason == "interrupted"
    return check("прерванный прогон закрыт после перезапуска эмулятора", ok, detail)


def upload_scenario(api: Api, file: Path, timeout: float = 60) -> dict:
    """Свой журнал → сценарий эмулятора (13.1): загрузка с разбором, сценарий по предложенному сопоставлению, ждём
    его в каталоге эмулятора — тот перечитывает папку на каждый `/scenarios`."""
    if not file.is_file():
        raise SystemExit(f"нет файла {file}")
    parsed = api.upload("/scenario-uploads", file)
    stats = parsed["stats"]
    print(
        f"{file.name}: загрузка {parsed['id']}, строк {parsed['rows']}, событий {stats['events']}, каналов "
        f"{stats['channels']} (вне справочника {stats['unknown_channels']}), дублей {stats['duplicates']}, "
        f"предупреждения {[w['code'] for w in parsed['warnings']]}"
    )
    if not parsed["ready"]:
        raise SystemExit(f"сопоставление не готово, не назначены: {parsed['required_missing']}")
    created = api.call("PUT", f"/scenario-uploads/{parsed['id']}", {"dry_run": False})
    sid = created["scenario_id"]
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        found = next((s for s in api.call("GET", "/emulator/scenarios")["items"] if s["id"] == sid), None)
        if found and found["ready"]:
            break
        time.sleep(1)
    else:
        raise SystemExit(f"эмулятор не видит сценарий {sid} за {timeout:.0f} с")
    return {
        "file": file.name,
        "upload_id": parsed["id"],
        "scenario_id": sid,
        "events": created["events"],
        "channels": created["channels"],
        "period": created["period"],
        # удаления загрузки и сценария в API нет (docs/api.md, «Сценарий из своего журнала»)
        "left": [f"data/uploads/{parsed['id']}/", f"data/processed/emulator/{UPLOADED}/{sid}.parquet и .json"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Сквозная проверка сценария эмулятора")
    parser.add_argument("scenario", nargs="*", help="несколько — подряд, следующий сразу после конца предыдущего")
    parser.add_argument("--upload", type=Path, help="свой журнал CSV или XLSX: загрузить, проиграть, сверить (13.1)")
    parser.add_argument("--slices", type=Path, default=SLICES, help="папка срезов эмулятора стенда")
    parser.add_argument("--speed", type=float, help="по умолчанию — предел сценария")
    parser.add_argument("--timeout", type=float, default=3600, help="на проигрывание, с")
    parser.add_argument("--interrupt", action="store_true", help="перезапустить эмулятор посреди прогона")
    parser.add_argument("--run", type=int, help="не запускать, а проверить готовый прогон сценария")
    parser.add_argument("--force", action="store_true", help="запустить, даже если эмулятор ведёт чужой прогон")
    parser.add_argument("--env", type=Path, default=ROOT / "infra" / ".env", help="infra/.env стенда: порты, проект")
    args = parser.parse_args()

    env = stand_env(args.env)
    api = Api(f"http://localhost:{env['API_PORT']}/api/v1")
    db_url = (
        f"postgresql+psycopg://{env['POSTGRES_USER']}:{env['POSTGRES_PASSWORD']}@localhost:{env['POSTGRES_PORT']}/"
        f"{env['POSTGRES_DB']}"
    )
    engine = create_engine(db_url)

    if (args.upload is None) == (not args.scenario):
        raise SystemExit("нужны имена сценариев или --upload <файл>, не то и другое")
    if args.upload is not None and (args.run is not None or args.interrupt):
        raise SystemExit("--upload — без --run и --interrupt")
    busy = api.call("GET", "/emulator/state")
    if args.run is None and busy["state"] in ("running", "paused") and not args.force:
        # общий стенд: чужой прогон не останавливаем
        raise SystemExit(f"эмулятор занят: {busy['scenario']}, прогон {busy['run_id']}, {busy['state']} (--force)")
    uploaded = upload_scenario(api, args.upload) if args.upload is not None else None
    if uploaded is not None:
        args.scenario = [uploaded["scenario_id"]]

    scenarios = {s["id"]: s for s in api.call("GET", "/emulator/scenarios")["items"]}
    for name in args.scenario:
        if name not in scenarios:
            raise SystemExit(f"нет сценария {name}; есть: {', '.join(scenarios)}")
        if not scenarios[name]["ready"]:
            raise SystemExit(f"срез {name} не подготовлен: make emulator-slices S={name}")
    if len(args.scenario) > 1 and (args.run is not None or args.interrupt):
        raise SystemExit("--run и --interrupt — для одного сценария")
    with engine.connect() as conn:
        smoke = {c for (c,) in conn.execute(text("SELECT id FROM channels WHERE sensor_type = 'Датчик дыма'"))}
    expected = {name: slice_stats(slice_path(name, args.slices), smoke) for name in args.scenario}
    speeds = {name: args.speed or scenarios[name]["max_speed"] for name in args.scenario}

    if args.run is not None:
        name = args.scenario[0]
        return report_run(api, engine, name, expected[name], args.run, speeds[name], None, 0.0, 0.0)

    played = []
    for name in args.scenario:
        sc = scenarios[name]
        print(f"{name}: {sc['title']}, {sc['period_start'][:16]} — {sc['period_end'][:16]}, ×{speeds[name]:g}")
        t0 = time.monotonic()
        run_id = api.call("POST", "/emulator/start", {"scenario": name, "speed": speeds[name]})["run_id"]
        print(f"  прогон {run_id}")
        if args.interrupt:
            result = interrupt(api, engine, run_id, env["COMPOSE_PROJECT_NAME"])
            print(f"  {'✓' if result['ok'] else '✗'} {result['check']}: {result['detail']}")
            REPORTS.mkdir(parents=True, exist_ok=True)
            out = REPORTS / f"{name}-run{run_id}-interrupt.json"
            report = {"scenario": name, "run": run_id, "story": [result]}
            out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
            return 0 if result["ok"] else 1
        state = wait_finished(api, args.timeout)
        played.append((name, run_id, state, time.monotonic() - t0))
    codes = []
    for name, run_id, state, seconds in played:
        settle = wait_quiet(engine.connect, run_id)
        codes.append(
            report_run(api, engine, name, expected[name], run_id, speeds[name], state, seconds, settle, uploaded)
        )
    return max(codes)


def report_run(
    api: Api,
    engine,
    scenario: str,
    expected: dict,
    run_id: int,
    speed,
    state,
    played: float,
    settle: float,
    uploaded: dict | None = None,
) -> int:
    run = api.call("GET", f"/runs/{run_id}")
    if state is None:  # проверка готового прогона: отправлено столько, сколько приняла база плюс дубли среза
        state = {"state": run["status"], "events_sent": expected["rows"] if run["status"] == "finished" else None}
    with engine.connect() as conn:
        report = collect(conn, run_id)
        report["slice"] = expected
        report["story"] = story(conn, scenario, run_id, report)
        if ">" in (report["model_versions"]["primary"] or ""):
            fallback = scenario == "sample-2026-08-01"
            report["story"].append(chain_decisions(conn, run_id, require_fallback=fallback))
        rows = rows_match(conn, Path(expected["path"]), run_id)
    report["story"] += access_story(api, scenario, run_id)
    report.update(
        {
            "scenario": scenario,
            "speed": speed,
            "state": state["state"],
            "run_status": run["status"],
            "played_s": round(played, 1),
            "settle_s": round(settle, 1),
            "slice": expected,
            "sent": state["events_sent"],
            "accepted": run["events"],
            "live": run.get("live"),
            "checked_at": dt.datetime.now().isoformat(timespec="seconds"),
        }
    )
    # у записанного прогона (--run) счётчика эмулятора нет: у доигранного — весь срез, у остановленного — неизвестно
    sent_ok = report["sent"] is None or report["sent"] == expected["rows"]
    events_ok = report["accepted"] == expected["unique"] and sent_ok
    sent = "неизвестно (записанный прогон не доигран)" if report["sent"] is None else report["sent"]
    report["story"].insert(
        0,
        check(
            "все события среза приняты",
            events_ok,
            f"в срезе {expected['rows']} ({expected['unique']} уникальных), отправлено {sent}, "
            f"принято {report['accepted']}",
        ),
    )

    report["story"].insert(1, rows)
    # потери, которые видит сама система: шлюз сверяет принятые события со счётчиком эмулятора `EventsSent`
    lost = run.get("events_lost")
    report["story"].insert(
        2,
        check(
            "шлюз принял всё, что отправил эмулятор (счётчик EventsSent)",
            not lost,
            f"потеряно {lost}, окна {run.get('loss_windows')}" if lost else "потерь нет",
        ),
    )
    if uploaded is not None:
        report["upload"] = uploaded
        report["story"].insert(
            2,
            check(
                "срез сценария = разбор загрузки",
                uploaded["events"] == expected["rows"],
                f"событий в разборе {uploaded['events']}, в срезе {expected['rows']}",
            ),
        )

    REPORTS.mkdir(parents=True, exist_ok=True)
    out = REPORTS / f"{scenario}-run{run_id}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))

    print(f"\nпрогон {run_id}: {report['run_status']}, проигран за {played:.0f} с, затишье через {settle:.0f} с")
    alarms = report["alarms"]
    print(f"  тревоги: {alarms['total']} (в срезе {expected['smoke_alarms']}), вердикты {alarms['verdicts']}")
    print(f"  эпизоды: {report['episodes']}")
    print(f"  прогнозы: {report['predictions']['by_model_status']}, с тревогой {report['predictions']['alerts']}")
    mv = report["model_versions"]
    if mv["shadow"] or ">" in (mv["primary"] or ""):
        head = f"  версии: основная {mv['primary']}, теневые {mv['shadow']}, решили {mv['decided_by']}"
        print(f"{head}; {mv['by_version']}; расходятся {mv['disagree']}")
    print(f"  пропуски: {report['predictions']['skip_reasons']}, исходы {report['predictions']['outcomes']}")
    print(f"  заявки: {report['work_orders_priority']}")
    for c in report["story"]:
        print(f"  {'✓' if c['ok'] else '✗'} {c['check']}: {c['detail']}")
    if uploaded is not None:
        print(f"  осталось (удаления в API нет): прогон {run_id}, {', '.join(uploaded['left'])}")
    print(f"  JSON: {out.relative_to(ROOT)}")
    return 0 if all(c["ok"] for c in report["story"]) else 1


if __name__ == "__main__":
    sys.exit(main())
