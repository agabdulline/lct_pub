"""Ключевые моменты сценария — только факты журнала, без ожиданий от системы (13.2).

Вход — соединение DuckDB, в котором зарегистрированы таблицы:

- `s` — срез сценария: event_id, channel_id, ts (Москва без пояса), is_alarm, value;
- `ch` — справочник каналов: channel_id, name, sensor_type, subsystem, tag, sub_object_id, object_id (объект 2-го
  уровня);
- `obj` — объекты: id, level, parent_id, name.

Выход `key_moments` — до `LIMIT` моментов по времени:
`{at, until, kind, state, title, text, object_id, object, sub_object_id, subobject, channel_ids, count}`, где
`count` — число событий момента, `until` — конец интервала (`None` у мгновенных). Виды (`kind`):

- `smoke_single`, `smoke_group` — «Обнаружен дым» одного или нескольких датчиков объекта, серии с разрывом до 25 мин;
- `lever` — срывы рычагов;
- `state_wave`, `state_return` — от 5 каналов разом сообщили «Неисправен», «Обесточен», «Отключено устройство» или
  вышли из них; близкие по времени волны объединяются;
- `flapping` — канал меняет состояние от 10 раз за час; считаются только каналы состояний — не больше 8 разных
  значений в срезе; датчики движения, числовые показания и пропуска доступа не считаются;
- `gas_peak` — газ выше порога или «Обнаружен газ»;
- `code_series` — серии тревог каналов без спокойных значений в срезе (коды ошибок агрегата) по подобъекту;
- `access_peak` — самые загруженные часы событий доступа (проезды, считывания пропусков);
- `failure_record` — записи журнала отказов и ремонтов (Azure PdM), близкие по времени — одним моментом;
- `silence` — паузы без событий от 30 мин и от трёх обычных шагов потока (у телеметрии раз в час — от 3 ч);
- `first_event`, `last_event` — первое и последнее событие среза.

Вне соединения модуль ничего не читает — ни файлов, ни настроек: эмулятор вызывает его в потоке запроса.
"""

import bisect
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

SMOKE = "Обнаружен дым"
LEVER = "Рычаг сдернут"
GAS_TYPE, GAS_STATE = "Газовый датчик", "Обнаружен газ"
STATES = ("Неисправен", "Обесточен", "Отключено устройство")
MOTION_TYPE = "Датчик движения"  # переключается при каждом проходе — в частых сменах это шум
# значение события доступа — пропуск (машина, карта), а не состояние (docs/architecture/external-data.md §3.3)
ACCESS_TYPES = ("Датчик проезда", "Считыватель зоны", "Мобильный считыватель")
ACCESS_WORDS = {"Датчик проезда": ("проезд", "проезда", "проездов")}
# запись службы, а не сообщение устройства: is_alarm у неё нет, но это факт журнала (external-data.md §3.2)
FAILURE_TYPES = ("Отказы и ремонты",)
FAILURE_GAP = timedelta(hours=1)

EPISODE_GAP = timedelta(minutes=25)  # как эпизоды тревог backend (backend/config/triage.yaml, gap_minutes)
WAVE_GAP, WAVE_MIN_CHANNELS = timedelta(minutes=5), 5
FLAP_WINDOW, FLAP_MIN, FLAP_TOP = timedelta(hours=1), 10, 5
STATE_VOCAB = 8  # у каналов хакатона — от 2 до 8 разных состояний; больше — метки времени, пропуска, коды
CODE_GAP, CODE_MIN = timedelta(hours=24), 2
ACCESS_WINDOW, ACCESS_MIN, ACCESS_TOP = timedelta(hours=1), 10, 3
GAS_PEAK = 0.2  # единицы газа неизвестны (Q20); в 2026 году 99.9% показаний ниже 0.17
GAS_SERIES = 3  # больше серий пиков на объекте — один момент на весь срез
SILENCE_MIN, SILENCE_TOP = timedelta(minutes=30), 3
SILENCE_STEPS = 3  # пауза — не короче трёх обычных шагов потока
STEP_MIN_GAPS = 10  # шаг потока — по медиане не меньше чем 10 интервалов
LIMIT = 30

# порядок важности, когда моментов больше LIMIT; первое и последнее событие остаются всегда
PRIORITY = {
    "first_event": 0,
    "last_event": 0,
    "smoke_group": 1,
    "state_wave": 2,
    "failure_record": 2,
    "lever": 3,
    "gas_peak": 4,
    "code_series": 4,
    "state_return": 5,
    "smoke_single": 6,
    "flapping": 7,
    "access_peak": 7,
    "silence": 8,
}


@dataclass(frozen=True)
class Row:
    ts: datetime
    channel_id: int
    value: str
    name: str
    sensor_type: str
    sub_object_id: int | None
    object_id: int | None
    is_alarm: bool = False


# ---------- текст


def num(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def plural(n: int, one: str, few: str, many: str) -> str:
    n10, n100 = n % 10, n % 100
    word = one if n10 == 1 and n100 != 11 else few if 2 <= n10 <= 4 and not 12 <= n100 <= 14 else many
    return f"{num(n)} {word}"


def verb(n: int, one: str, many: str) -> str:
    """Согласование глагола с числом: «21 канал сообщил», «22 канала сообщили»."""
    return one if n % 10 == 1 and n % 100 != 11 else many


def duration(delta: timedelta) -> str:
    sec = int(delta.total_seconds())
    if sec < 60:
        return f"{sec} с"
    minutes = round(sec / 60)
    if minutes < 60:
        return f"{minutes} мин"
    h, m = divmod(minutes, 60)
    return f"{h} ч {m} мин" if m else f"{h} ч"


def within(delta: timedelta) -> str:
    return "в одну секунду" if delta < timedelta(seconds=1) else f"за {duration(delta)}"


class Clock:
    """Время в текстах: часы и минуты, а если срез длиннее суток — ещё и дата."""

    def __init__(self, first: datetime, last: datetime):
        self.with_date = first.date() != last.date()

    def at(self, ts: datetime, seconds: bool = False) -> str:
        fmt = "%H:%M:%S" if seconds else "%H:%M"
        return ts.strftime(f"%d.%m {fmt}" if self.with_date else fmt)

    def span(self, a: datetime, b: datetime) -> str:
        if self.at(a) == self.at(b):
            return self.at(a)
        tail = b.strftime("%H:%M") if a.date() == b.date() else self.at(b)
        return f"{self.at(a)}–{tail}"


# ---------- разбор среза


def load(con) -> tuple[list[Row], dict[int, str]]:
    raw = con.execute(
        """
        SELECT s.ts, s.channel_id, s.value, c.name, c.sensor_type, c.sub_object_id, c.object_id, s.is_alarm
        FROM s LEFT JOIN ch AS c USING (channel_id)
        ORDER BY s.ts, s.event_id
        """
    ).fetchall()
    rows = [
        Row(ts, cid, str(value), name or f"канал {cid}", sensor_type or "", sub, obj, bool(alarm))
        for ts, cid, value, name, sensor_type, sub, obj, alarm in raw
    ]
    names = {int(i): n for i, n in con.execute("SELECT id, name FROM obj").fetchall()}
    return rows, names


def series(rows: list[Row], gap: timedelta, key=lambda r: r.object_id) -> list[list[Row]]:
    """Серии строк одного ключа (по умолчанию — объект 2-го уровня) с разрывом не больше gap."""
    open_: dict = {}
    out: list[list[Row]] = []
    for r in rows:
        k = key(r)
        cur = open_.get(k)
        if cur is not None and r.ts - cur[-1].ts <= gap:
            cur.append(r)
        else:
            cur = [r]
            open_[k] = cur
            out.append(cur)
    return out


def merge_close(groups: list[list[Row]], gap: timedelta) -> list[list[list[Row]]]:
    """Склеивает группы, идущие друг за другом с разрывом до gap на тех же объектах — одна серия волн."""
    out: list[list[list[Row]]] = []
    for g in sorted(groups, key=lambda g: g[0].ts):
        objs = {r.object_id for r in g}
        for chain in reversed(out):
            if g[0].ts - chain[-1][-1].ts > gap:
                break
            if objs & {r.object_id for r in chain[-1]}:
                chain.append(g)
                break
        else:
            out.append([g])
    return out


def number(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None


class Builder:
    def __init__(self, rows: list[Row], names: dict[int, str]):
        self.rows, self.names = rows, names
        self.clock = Clock(rows[0].ts, rows[-1].ts)

    def moment(self, kind: str, group: list[Row], title: str, text: str, state: str | None = None) -> dict:
        objects = {r.object_id for r in group}
        subs = {r.sub_object_id for r in group}
        obj = next(iter(objects)) if len(objects) == 1 else None
        sub = next(iter(subs)) if len(subs) == 1 else None
        at, end = group[0].ts, group[-1].ts
        return {
            "at": at,
            "until": end if end != at else None,
            "kind": kind,
            "state": state,
            "title": title,
            "text": text,
            "object_id": obj,
            "object": self.names.get(obj) if obj is not None else None,
            "sub_object_id": sub,
            "subobject": self.names.get(sub) if sub is not None else None,
            "channel_ids": sorted({r.channel_id for r in group}),
            "count": len(group),
        }

    def where(self, group: list[Row]) -> str:
        objs = {r.object_id for r in group}
        if len(objs) == 1:
            return self.names.get(next(iter(objs)), "объект не указан")
        return plural(len(objs), "объект", "объекта", "объектов")

    def place(self, group: list[Row]) -> str:
        """Подобъект, если он у группы один, иначе объект: у агрегата Azure PdM объект — модель, подобъект — агрегат."""
        subs = {r.sub_object_id for r in group}
        sub = next(iter(subs)) if len(subs) == 1 else None
        return self.names[sub] if sub in self.names else self.where(group)

    def breakdown(self, group: list[Row], by=lambda r: r.sub_object_id, label="подобъектам") -> str:
        per = defaultdict(set)
        for r in group:
            per[by(r)].add(r.channel_id)
        parts = sorted(per.items(), key=lambda kv: -len(kv[1]))
        text = "; ".join(f"{self.names.get(k, 'не указан')} — {num(len(v))}" for k, v in parts[:4])
        if len(parts) > 4:
            text += f"; ещё {num(len(parts) - 4)}"
        return f"По {label}: {text}."

    # ---------- детекторы

    def edges(self) -> list[dict]:
        out = []
        for kind, r, word in (("first_event", self.rows[0], "Первое"), ("last_event", self.rows[-1], "Последнее")):
            title = f"{self.clock.at(r.ts, seconds=True)} — {word.lower()} событие среза: «{r.name}», {self.where([r])}"
            # метка вида («Первое событие») стоит рядом с текстом — не повторяем её
            text = f"{r.sensor_type or 'Тип не указан'} «{r.name}» — «{r.value}»."
            out.append(self.moment(kind, [r], title, text))
        return out

    def smoke(self) -> list[dict]:
        out = []
        gap_min = int(EPISODE_GAP.total_seconds() // 60)
        for group in series([r for r in self.rows if r.value == SMOKE], EPISODE_GAP):
            chans = {r.channel_id for r in group}
            span, took = self.clock.span(group[0].ts, group[-1].ts), within(group[-1].ts - group[0].ts)
            hits = plural(len(group), "срабатывание", "срабатывания", "срабатываний")
            if len(chans) == 1:
                r = group[0]
                title = f"{span} — {self.where(group)}: дымовой датчик «{r.name}»"
                if len(group) > 1:
                    title += f", {hits} {took}"
                text = (
                    f"«{SMOKE}» одного датчика, подобъект {self.names.get(r.sub_object_id, 'не указан')}. "
                    f"Другие дымовые датчики объекта в пределах {gap_min} мин не срабатывали."
                )
                out.append(self.moment("smoke_single", group, title, text))
            else:
                sensors = plural(len(chans), "дымовой датчик", "дымовых датчика", "дымовых датчиков")
                title = f"{span} — {self.where(group)}: {sensors}, {hits} {took}"
                first, last = group[0], group[-1]
                text = (
                    f"{self.breakdown(group)} Первое — «{first.name}» в {self.clock.at(first.ts, True)}, "
                    f"последнее — «{last.name}» в {self.clock.at(last.ts, True)}."
                )
                out.append(self.moment("smoke_group", group, title, text))
        return out

    def levers(self) -> list[dict]:
        out = []
        for group in series([r for r in self.rows if r.value == LEVER], EPISODE_GAP):
            names = sorted({r.name for r in group})
            span = self.clock.span(group[0].ts, group[-1].ts)
            pulls = plural(len(group), "срыв рычага", "срыва рычага", "срывов рычага")
            if len(names) == 1:
                title = f"{span} — {self.where(group)}: {pulls} «{names[0]}»"
            else:
                title = f"{span} — {self.where(group)}: {pulls}, {plural(len(names), 'канал', 'канала', 'каналов')}"
            text = f"«{LEVER}»: " + ", ".join(f"«{n}»" for n in names[:6]) + (" и другие" if len(names) > 6 else "")
            out.append(self.moment("lever", group, text=text + ".", title=title))
        return out

    def states(self) -> list[dict]:
        """«Неисправен», «Обесточен», «Отключено устройство» — когда их сообщают или покидают многие каналы разом."""
        prev: dict[int, str] = {}
        entries: list[Row] = []
        exits: list[Row] = []
        left: dict[int, str] = {}  # id строки выхода → состояние, из которого вышли
        for r in self.rows:
            p = prev.get(r.channel_id)
            if r.value in STATES and p != r.value:
                entries.append(r)
            if p in STATES and r.value != p:
                exits.append(r)
                left[id(r)] = p
            prev[r.channel_id] = r.value
        out = []
        for kind, rows in (("state_wave", entries), ("state_return", exits)):
            groups = series(rows, WAVE_GAP, key=lambda r: 0)
            waves = [g for g in groups if len({r.channel_id for r in g}) >= WAVE_MIN_CHANNELS]
            for chain in merge_close(waves, EPISODE_GAP):
                group = sorted((r for g in chain for r in g), key=lambda r: r.ts)
                state_of = (lambda r: r.value) if kind == "state_wave" else (lambda r: left[id(r)])
                per_state = Counter()
                for st, cids in _channels_by(group, state_of).items():
                    per_state[st] = len(cids)
                main, n_main = per_state.most_common(1)[0]
                chans = len({r.channel_id for r in group})
                span, took = self.clock.span(group[0].ts, group[-1].ts), within(group[-1].ts - group[0].ts)
                counted = plural(n_main, "канал", "канала", "каналов")
                if kind == "state_wave":
                    what = f"{counted} {verb(n_main, 'сообщил', 'сообщили')} «{main}»"
                else:
                    what = f"{counted} {verb(n_main, 'вышел', 'вышли')} из «{main}»"
                others = "".join(f", {num(n)} — «{st}»" for st, n in per_state.most_common()[1:])
                title = f"{span} — {self.where(group)}: {what}{others} {took}"
                if len(chain) > 1:
                    title += f", {plural(len(chain), 'волна', 'волны', 'волн')}"
                if kind == "state_wave":
                    types = Counter(r.sensor_type for r in group).most_common(3)
                    extra = " Типы: " + ", ".join(f"{t} — {n}" for t, n in types) + "."
                else:
                    values = Counter(r.value for r in group).most_common(3)
                    extra = " Новые значения: " + ", ".join(f"«{v}» — {num(n)}" for v, n in values) + "."
                by = (lambda r: r.object_id) if len({r.object_id for r in group}) > 1 else (lambda r: r.sub_object_id)
                label = "объектам" if len({r.object_id for r in group}) > 1 else "подобъектам"
                text = f"Всего {plural(chans, 'канал', 'канала', 'каналов')}. {self.breakdown(group, by, label)}{extra}"
                out.append(self.moment(kind, group, title, text, state=main))
        return out

    def flapping(self) -> list[dict]:
        """Каналы состояний, которые меняют состояние от FLAP_MIN раз за час.

        Не считаются датчики движения и газа, числа, пропуска доступа и каналы, у которых в срезе больше STATE_VOCAB
        разных значений: там каждое событие приносит новое значение — это не смена состояния.
        """
        vocab: dict[int, set[str]] = defaultdict(set)
        for r in self.rows:
            if number(r.value) is None:
                vocab[r.channel_id].add(r.value)
        prev: dict[int, str] = {}
        changes: dict[int, list[Row]] = defaultdict(list)
        pairs: dict[int, Counter] = defaultdict(Counter)
        for r in self.rows:
            if r.sensor_type in (MOTION_TYPE, GAS_TYPE, *ACCESS_TYPES) or number(r.value) is not None:
                continue
            if len(vocab[r.channel_id]) > STATE_VOCAB:
                continue
            p = prev.get(r.channel_id)
            if p is not None and p != r.value:
                changes[r.channel_id].append(r)
                pairs[r.channel_id][tuple(sorted((p, r.value)))] += 1
            prev[r.channel_id] = r.value
        found = []
        for cid, rows in changes.items():
            if len(rows) < FLAP_MIN:
                continue
            ts = [r.ts for r in rows]
            top, start = 0, 0
            for i in range(len(ts)):
                j = bisect.bisect_right(ts, ts[i] - FLAP_WINDOW, 0, i + 1)
                if i - j + 1 > top:
                    top, start = i - j + 1, j
            if top >= FLAP_MIN:
                found.append((top, rows[start : start + top], cid))
        # каналы одного объекта с одинаковым окном — один момент
        found.sort(key=lambda f: (f[1][0].object_id or 0, f[1][0].sensor_type, f[1][0].ts))
        merged: list[list[tuple]] = []
        for f in found:
            last = merged[-1][-1] if merged else None
            same = last and (last[1][0].object_id, last[1][0].sensor_type) == (f[1][0].object_id, f[1][0].sensor_type)
            if same and abs(last[1][0].ts - f[1][0].ts) < timedelta(minutes=5):
                merged[-1].append(f)
            else:
                merged.append([f])
        merged.sort(key=lambda m: -max(f[0] for f in m))
        out = []
        for grp in merged[:FLAP_TOP]:
            rows = sorted((r for _, w, _ in grp for r in w), key=lambda r: r.ts)
            top = max(f[0] for f in grp)
            (a, b), _ = pairs[grp[0][2]].most_common(1)[0]
            span, took = self.clock.span(rows[0].ts, rows[-1].ts), within(rows[-1].ts - rows[0].ts)
            names = [f"«{w[0].name}»" for _, w, _ in grp]
            if len(grp) == 1:
                who = f"{names[0]} {verb(1, 'сменил', 'сменили')} состояние {plural(top, 'раз', 'раза', 'раз')}"
            else:
                counted = plural(len(grp), "канал", "канала", "каналов")
                who = f"{counted} сменили состояние до {plural(top, 'раз', 'раза', 'раз')}"
            title = f"{span} — {self.where(rows)}: {who} {took}"
            text = (
                f"{grp[0][1][0].sensor_type}: чаще всего «{a}» ↔ «{b}»."
                + (f" Каналы: {', '.join(names[:6])}." if len(grp) > 1 else "")
                + f" Всего смен в срезе — {num(sum(len(changes[c]) for _, _, c in grp))}."
            )
            out.append(self.moment("flapping", rows, title, text))
        return out

    def gas(self) -> list[dict]:
        peaks = [
            r
            for r in self.rows
            if r.sensor_type == GAS_TYPE and (r.value == GAS_STATE or (number(r.value) or 0) > GAS_PEAK)
        ]
        by_object = defaultdict(list)
        for g in series(peaks, EPISODE_GAP):
            by_object[g[0].object_id].append(g)
        out = []
        for groups in by_object.values():
            chunks = groups
            if len(groups) > GAS_SERIES:
                chunks = [sorted((r for g in groups for r in g), key=lambda r: r.ts)]
            for group in chunks:
                nums = [(number(r.value), r) for r in group if number(r.value) is not None]
                span = self.clock.span(group[0].ts, group[-1].ts)
                if nums:
                    x, top = max(nums, key=lambda t: t[0])
                    title = f"{span} — {self.where(group)}: газ выше {GAS_PEAK}, максимум {x:g} («{top.name}»)"
                else:
                    title = f"{span} — {self.where(group)}: «{GAS_STATE}»"
                if len(groups) > GAS_SERIES:
                    title += f", {plural(len(groups), 'серия', 'серии', 'серий')}"
                chans = {r.channel_id for r in group}
                text = (
                    f"{plural(len(group), 'показание', 'показания', 'показаний')} выше {GAS_PEAK} или «{GAS_STATE}» "
                    f"от {plural(len(chans), 'датчика', 'датчиков', 'датчиков')}; единицы газа в журнале не указаны."
                )
                out.append(self.moment("gas_peak", group, title, text))
        return out

    def step(self) -> timedelta:
        """Обычный шаг потока — медиана интервалов между соседними моментами с событиями; при малом числе
        интервалов сравнивать не с чем — шаг не определён."""
        ts = sorted({r.ts for r in self.rows})
        gaps = sorted(b - a for a, b in zip(ts, ts[1:], strict=False))
        return gaps[len(gaps) // 2] if len(gaps) >= STEP_MIN_GAPS else timedelta(0)

    def silence(self) -> list[dict]:
        """Паузы от SILENCE_MIN и от SILENCE_STEPS обычных шагов: телеметрия пачкой раз в час — не пауза."""
        step = self.step()
        least = max(SILENCE_MIN, SILENCE_STEPS * step)
        pairs = zip(self.rows, self.rows[1:], strict=False)
        gaps = [(b.ts - a.ts, a, b) for a, b in pairs if b.ts - a.ts >= least]
        out = []
        for delta, a, b in sorted(gaps, key=lambda g: -g[0])[:SILENCE_TOP]:
            title = f"{self.clock.span(a.ts, b.ts)} — нет событий {duration(delta)}"
            text = f"Последнее событие перед паузой — «{a.name}», первое после — «{b.name}»."
            if least > SILENCE_MIN:
                text += f" Обычный шаг потока — {duration(step)}."
            m = self.moment("silence", [a, b], title, text)
            m.update(channel_ids=[], count=0, object_id=None, object=None, sub_object_id=None, subobject=None)
            out.append(m)
        return out

    def codes(self) -> list[dict]:
        """Серии тревог каналов, у которых в срезе нет спокойных значений: такой канал сообщает коды, а не состояния
        (коды ошибок агрегата). Дым, рычаги, газ и состояния хакатона — у своих детекторов."""
        calm = {r.channel_id for r in self.rows if not r.is_alarm}
        taken = {SMOKE, LEVER, GAS_STATE, *STATES}
        alarms = [
            r
            for r in self.rows
            if r.is_alarm and r.channel_id not in calm and r.value not in taken and r.sensor_type != GAS_TYPE
        ]
        out, singles = [], []
        for group in series(alarms, CODE_GAP, key=lambda r: r.sub_object_id or r.object_id):
            if len(group) < CODE_MIN:
                singles += group
                continue
            span = self.clock.span(group[0].ts, group[-1].ts)
            took = "одновременно" if group[0].ts == group[-1].ts else within(group[-1].ts - group[0].ts)
            title = f"{span} — {self.place(group)}: {plural(len(group), 'тревога', 'тревоги', 'тревог')}"
            title += f" {self.types(group)} {took}"
            values = Counter(r.value for r in group).most_common()
            names = ", ".join(f"«{n}»" for n in sorted({r.name for r in group})[:3])
            text = f"{names}: " + ", ".join(f"«{v}» — {num(n)}" for v, n in values[:6]) + "."
            out.append(self.moment("code_series", group, title, text))
        if singles:
            # одиночные — одним моментом на срез, с самым тревожным днём
            singles.sort(key=lambda r: r.ts)
            subs = {r.sub_object_id or r.object_id for r in singles}
            where = plural(len(subs), "подобъекте", "подобъектах", "подобъектах")
            span = self.clock.span(singles[0].ts, singles[-1].ts)
            title = f"{span} — одиночные тревоги {self.types(singles)}: {num(len(singles))} на {where}"
            day, n = Counter(r.ts.date() for r in alarms).most_common(1)[0]
            text = (
                f"Одиночные — без другой тревоги того же подобъекта в пределах {duration(CODE_GAP)}. "
                f"Больше всего тревог этого вида за сутки — {day:%d.%m}: {num(n)}."
            )
            out.append(self.moment("code_series", singles, title, text))
        return out

    def failures(self) -> list[dict]:
        """Записи об отказах и ремонтах; записи в пределах FAILURE_GAP друг от друга — один момент."""
        out = []
        for group in series([r for r in self.rows if r.sensor_type in FAILURE_TYPES], FAILURE_GAP, key=lambda r: 0):
            per: dict[str, list[str]] = defaultdict(list)
            for r in group:
                per[self.names.get(r.sub_object_id, r.name)].append(r.value)
            parts = [f"{k} — {', '.join(f'«{v}»' for v in vs)}" for k, vs in per.items()]
            what = plural(len(group), "запись", "записи", "записей")
            title = f"{self.clock.span(group[0].ts, group[-1].ts)} — {group[0].sensor_type}: {what}"
            subs = plural(len(parts), "подобъект", "подобъекта", "подобъектов")
            title += f", {parts[0]}" if len(parts) == 1 else f", {subs}"
            text = "; ".join(parts[:6]) + (f"; ещё {num(len(parts) - 6)}" if len(parts) > 6 else "") + "."
            out.append(self.moment("failure_record", group, title, text))
        return out

    @staticmethod
    def types(group: list[Row]) -> str:
        kinds = sorted({r.sensor_type for r in group})
        return f"типа «{kinds[0]}»" if len(kinds) == 1 else "типов " + ", ".join(f"«{k}»" for k in kinds[:3])

    def access(self) -> list[dict]:
        """Самые загруженные часы событий доступа — окна по часу без пересечений, от ACCESS_MIN событий."""
        rows = [r for r in self.rows if r.sensor_type in ACCESS_TYPES]
        ts = [r.ts for r in rows]
        windows = sorted(
            ((bisect.bisect_left(ts, t + ACCESS_WINDOW) - i, i) for i, t in enumerate(ts)), key=lambda w: (-w[0], w[1])
        )
        chosen: list[tuple[int, int]] = []
        for n, i in windows:
            if n < ACCESS_MIN or len(chosen) == ACCESS_TOP:
                break
            if all(i + n <= j or j + m <= i for m, j in chosen):
                chosen.append((n, i))
        out = []
        for n, i in chosen:
            group = rows[i : i + n]
            types = {r.sensor_type for r in group}
            words = ACCESS_WORDS.get(next(iter(types))) if len(types) == 1 else None
            what = plural(n, *words) if words else plural(n, "событие доступа", "события доступа", "событий доступа")
            span, took = self.clock.span(group[0].ts, group[-1].ts), within(group[-1].ts - group[0].ts)
            title = f"{span} — {self.where(group)}: {what} {took}"
            per = Counter(self.names.get(r.sub_object_id, "не указан") for r in group).most_common()
            parts = "; ".join(f"{k} — {num(v)}" for k, v in per[:4])
            if len(per) > 4:
                parts += f"; ещё {num(len(per) - 4)}"
            passes = len({r.value for r in group})
            text = f"По группам: {parts}. Разных пропусков — {num(passes)}."
            out.append(self.moment("access_peak", group, title, text))
        return out


def _channels_by(group: list[Row], key) -> dict:
    out = defaultdict(set)
    for r in group:
        out[key(r)].add(r.channel_id)
    return out


def by_time(m: dict) -> tuple:
    """По времени; при равном времени первое событие среза — раньше всех, последнее — позже всех."""
    return m["at"], {"first_event": -1, "last_event": 1}.get(m["kind"], 0)


def limit(moments: list[dict], n: int = LIMIT) -> list[dict]:
    """Не больше n моментов: важные виды раньше; лишние одиночные срабатывания дыма — одним моментом."""
    if len(moments) <= n:
        return sorted(moments, key=by_time)
    ranked = sorted(moments, key=lambda m: (PRIORITY[m["kind"]], -len(m["channel_ids"]), -m["count"], m["at"]))
    keep, rest = ranked[: n - 1], ranked[n - 1 :]
    singles = [m for m in rest if m["kind"] == "smoke_single"]
    if not singles:
        return sorted([*keep, rest[0]], key=by_time)
    objs = {m["object_id"] for m in singles}
    words = ("одиночное срабатывание", "одиночных срабатывания", "одиночных срабатываний")
    keep.append(
        {
            "at": min(m["at"] for m in singles),
            "until": max(m["until"] or m["at"] for m in singles),
            "kind": "smoke_single",
            "state": None,
            "title": f"Ещё {plural(len(singles), *words)} дымовых датчиков на "
            f"{plural(len(objs), 'объекте', 'объектах', 'объектах')}",
            "text": "Срабатывания одного датчика, когда другие дымовые датчики объекта в пределах "
            f"{int(EPISODE_GAP.total_seconds() // 60)} мин не срабатывали.",
            "object_id": None,
            "object": None,
            "sub_object_id": None,
            "subobject": None,
            "channel_ids": sorted({c for m in singles for c in m["channel_ids"]}),
            "count": sum(m["count"] for m in singles),
        }
    )
    return sorted(keep, key=by_time)


def key_moments(con) -> list[dict]:
    """Ключевые моменты среза в соединении con (таблицы s, ch, obj) — по времени, не больше LIMIT."""
    rows, names = load(con)
    if not rows:
        return []
    b = Builder(rows, names)
    found = [*b.edges(), *b.smoke(), *b.levers(), *b.states(), *b.flapping(), *b.gas(), *b.codes(), *b.access()]
    return limit([*found, *b.failures(), *b.silence()])


def brief(moments: list[dict]) -> str:
    """Одна строка сводки по моментам — для заголовка просмотра сценария."""
    kinds = Counter(m["kind"] for m in moments)
    labels = {
        "smoke_group": ("группа дымовых срабатываний", "группы дымовых срабатываний", "групп дымовых срабатываний"),
        "smoke_single": ("одиночное срабатывание дыма", "одиночных срабатывания дыма", "одиночных срабатываний дыма"),
        "lever": ("серия срывов рычага", "серии срывов рычага", "серий срывов рычага"),
        "state_wave": ("массовое сообщение состояния", "массовых сообщения состояния", "массовых сообщений состояния"),
        "flapping": ("частая смена состояния", "частые смены состояния", "частых смен состояния"),
        "gas_peak": ("газовый пик", "газовых пика", "газовых пиков"),
        "code_series": ("серия тревожных кодов", "серии тревожных кодов", "серий тревожных кодов"),
        "access_peak": ("пик событий доступа", "пика событий доступа", "пиков событий доступа"),
        "failure_record": ("запись об отказе", "записи об отказах", "записей об отказах"),
        "silence": ("пауза без событий", "паузы без событий", "пауз без событий"),
    }
    parts = [plural(kinds[k], *forms) for k, forms in labels.items() if kinds[k]]
    return "; ".join(parts) if parts else "без срабатываний дыма, массовых смен состояния и пауз"
