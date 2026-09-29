"""Сверка принятых событий со счётчиком источника: сколько событий прогона шлюз потерял и в каких окнах.

Эмулятор каждый шаг пишет `EventsSent` — сколько событий прогона отправлено до этой метки записи (emulator.md §7).
Шлюз считает принятые события прогона по той же метке записи и сверяет. Разница — потери: событие, которое источник
отправил, а подписка не донесла (обрыв и переподписка, переполнение очереди). Без сверки такой прогон закрывался
`finished` молча: прогон 60 стенда — 3 600 событий, видно было только в `make e2e --run`.

- Отсчёт счётчика сверяется, когда его прогон по метке записи известен и после него пришёл пульс `RunId` хотя бы на
  `SAFETY` позже: события, записанные раньше отсчёта, приходят в том же или более раннем уведомлении.
- Событие считается один раз по (канал, метка записи): начальные значения новой подписки после переподключения —
  те же записи, их не считать дважды. Событие с меткой не позже сверенного отсчёта больше не считается.
- Шлюз, запущенный посреди прогона, не знает, что было до него: первый отсчёт прогона — точка отсчёта (`offset`).
- Окно потерь — симуляционное время между соседними отсчётами, где разница выросла; соседние окна сливаются.
"""

import bisect
import datetime as dt
from dataclasses import dataclass, field

from app.domain.ingest import RawEvent

SAFETY = dt.timedelta(seconds=1)
CLOCK_SAMPLES = 50_000  # SimulatedTime каждые 0.1 с — на полтора часа реального времени


@dataclass
class RunLoss:
    offset: int | None = None  # отправлено до того, как шлюз начал следить за прогоном
    checked_until: dt.datetime | None = None  # метка записи последнего сверенного отсчёта
    checked_sim: dt.datetime | None = None  # симуляционное время на ней
    received: int = 0  # принято событий с меткой не позже `checked_until`
    pending: set[tuple[int, dt.datetime]] = field(default_factory=set)  # (канал, метка) после неё
    lost: int = 0
    windows: list[dict] = field(default_factory=list)  # {"start_ts", "end_ts", "events"} — симуляционное время

    def as_json(self) -> list[dict]:
        def iso(t: dt.datetime | None) -> str | None:
            return t.isoformat() if t else None

        return [
            {"start_ts": iso(w["start_ts"]), "end_ts": iso(w["end_ts"]), "events": w["events"]} for w in self.windows
        ]


class EventAudit:
    def __init__(self) -> None:
        self.runs: dict[int, RunLoss] = {}
        self.samples: list[tuple[dt.datetime, int]] = []  # отсчёты `EventsSent` (метка, значение), ещё не сверены
        self._clock: list[tuple[dt.datetime, dt.datetime]] = []  # (метка записи, SimulatedTime)

    def received(self, run_id: int, event: RawEvent) -> None:
        if event.written is None:  # источник без меток записи — сверять не с чем
            return
        loss = self.runs.setdefault(run_id, RunLoss())
        if loss.checked_until is not None and event.written <= loss.checked_until:
            return
        loss.pending.add((event.channel_id, event.written))

    def sent(self, value, written: dt.datetime | None) -> None:
        if written is not None and value is not None:
            self.samples.append((written, int(value)))

    def clock(self, value: dt.datetime, written: dt.datetime | None) -> None:
        if written is None:
            return
        self._clock.append((written, value))
        if len(self._clock) > 2 * CLOCK_SAMPLES:
            del self._clock[:-CLOCK_SAMPLES]

    def sim_at(self, written: dt.datetime) -> dt.datetime | None:
        """Симуляционное время шага, в котором записан отсчёт: эмулятор пишет `SimulatedTime` после `EventsSent`
        того же шага — первое записанное не раньше метки; если его ещё нет — последнее до неё."""
        i = bisect.bisect_left(self._clock, written, key=lambda c: c[0])
        if i < len(self._clock):
            return self._clock[i][1]
        return self._clock[i - 1][1] if i else None

    def check(self, timeline) -> list[int]:
        """Сверить отсчёты, которые уже можно сверить (`RunTimeline`); прогоны, у которых выросли потери."""
        if timeline.seen_until is None:
            return []
        horizon = timeline.seen_until - SAFETY
        changed: set[int] = set()
        rest = []
        for written, sent in self.samples:
            if written > horizon or not timeline.known(written):
                rest.append((written, sent))
                continue
            run_id = timeline.run_at(written)
            if run_id is None:
                continue
            loss = self.runs.setdefault(run_id, RunLoss())
            if loss.checked_until is not None and written <= loss.checked_until:
                continue
            upto = {p for p in loss.pending if p[1] <= written}
            received = loss.received + len(upto)
            if loss.offset is None:
                loss.offset = sent - received
            lost = max(sent - received - loss.offset, 0)
            sim = self.sim_at(written)
            if lost > loss.lost:
                last = loss.windows[-1] if loss.windows else None
                if last is not None and last["end_ts"] == loss.checked_sim:
                    last["end_ts"], last["events"] = sim, last["events"] + lost - loss.lost
                else:
                    loss.windows.append({"start_ts": loss.checked_sim, "end_ts": sim, "events": lost - loss.lost})
                loss.lost = lost
                changed.add(run_id)
            loss.pending -= upto
            loss.received, loss.checked_until, loss.checked_sim = received, written, sim
        self.samples = rest
        return sorted(changed)
