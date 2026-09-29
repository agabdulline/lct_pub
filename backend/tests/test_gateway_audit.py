"""Сверка принятых событий со счётчиком источника (app/gateway/audit.py): потери прогона и их окна."""

from datetime import UTC, datetime, timedelta

from app.domain.ingest import RawEvent
from app.gateway.audit import EventAudit
from app.gateway.service import RunTimeline

SIM = datetime(2026, 8, 1, 3, 0, tzinfo=UTC)


def w(ms: int) -> datetime:
    """Метка записи у источника, строго растёт."""
    return datetime(2026, 9, 28, 1, 0, tzinfo=UTC) + timedelta(milliseconds=ms)


class Source:
    """Шаги эмулятора по 100 мс: события шага, `RunId`, `EventsSent`, `SimulatedTime` (×60 — минута за шаг)."""

    def __init__(self, run: int = 7) -> None:
        self.run, self.ms, self.sent, self.step_no = run, 0, 0, 0
        self.timeline, self.audit = RunTimeline(), EventAudit()

    def step(self, events: int = 0, lose: int = 0, deliver: bool = True) -> list[RawEvent]:
        """Шаг: `events` событий, из них `lose` не доходят до шлюза; `deliver = False` — не доходит ничего."""
        out = []
        for k in range(events):
            self.ms += 1
            event = RawEvent(1000 + k, SIM, str(self.step_no), False, written=w(self.ms))
            out.append(event)
            if deliver and k >= lose:
                self.audit.received(self.run, event)
        self.sent += events
        self.ms += 1
        self.timeline.note(self.run, w(self.ms))
        self.ms += 1
        self.audit.sent(self.sent, w(self.ms))
        self.ms += 1
        self.audit.clock(SIM + timedelta(minutes=self.step_no), w(self.ms))
        self.step_no += 1
        self.ms = self.step_no * 100
        return out

    def settle(self, steps: int = 12) -> list[int]:
        """Ещё `steps` пустых шагов (больше секунды) — отсчёты становятся сверяемыми."""
        for _ in range(steps):
            self.step()
        return self.audit.check(self.timeline)


def test_no_loss_when_every_event_arrives():
    src = Source()
    for _ in range(5):
        src.step(events=3)
    assert src.settle() == [] and src.audit.runs[7].lost == 0


def test_lost_events_and_their_window():
    """Шаг 2 — 5 событий, до шлюза дошли 3: потеряно 2, окно — между отсчётами шагов 1 и 2 (минуты 1–2)."""
    src = Source()
    src.step(events=3)
    src.step(events=3)
    src.step(events=5, lose=2)
    src.step(events=3)
    assert src.settle() == [7]
    loss = src.audit.runs[7]
    assert loss.lost == 2
    assert loss.as_json() == [
        {
            "start_ts": (SIM + timedelta(minutes=1)).isoformat(),
            "end_ts": (SIM + timedelta(minutes=2)).isoformat(),
            "events": 2,
        }
    ]
    assert src.settle() == []  # потери не растут — второй раз не сообщаем


def test_resubscription_initial_values_are_not_counted_twice():
    """Переподписка: шаги 1–2 не дошли совсем, затем начальные значения новой подписки — последние записи каналов,
    в том числе уже принятые. Потеряно ровно 6 событий двух шагов; повтор старых записей потерь не прячет."""
    src = Source()
    first = src.step(events=3)
    src.step(events=3, deliver=False)
    lost_step = src.step(events=3, deliver=False)
    assert src.settle() == [7] and src.audit.runs[7].lost == 6
    for event in first + lost_step:  # начальные значения: старые метки — уже сверены
        src.audit.received(7, event)
    src.step(events=2)
    assert src.settle() == [] and src.audit.runs[7].lost == 6


def test_gateway_started_mid_run_counts_from_its_first_sample():
    """Шлюз подключился посреди прогона: источник уже отправил 40 событий, шлюз их не видел — это не потери."""
    src = Source()
    src.sent = 40
    src.step(events=2)
    src.step(events=2)
    assert src.settle() == [] and src.audit.runs[7].lost == 0
    src.step(events=4, lose=1)
    assert src.settle() == [7] and src.audit.runs[7].lost == 1


def test_adjacent_loss_windows_merge():
    src = Source()
    src.step(events=2)
    src.step(events=4, lose=1)
    src.step(events=4, lose=3)
    src.step(events=2)
    src.settle()
    windows = src.audit.runs[7].windows
    assert [(w["start_ts"], w["end_ts"], w["events"]) for w in windows] == [(SIM, SIM + timedelta(minutes=2), 4)]


def test_runs_are_counted_separately():
    """Счётчик источника — на прогон: новый прогон начинает с нуля, события — своему прогону по метке записи."""
    src = Source(run=7)
    src.step(events=3)
    src.settle(3)
    src.run, src.sent = 8, 0
    src.step(events=2)
    src.step(events=3, lose=1)
    assert src.settle() == [8]
    assert (src.audit.runs[7].lost, src.audit.runs[8].lost) == (0, 1)
