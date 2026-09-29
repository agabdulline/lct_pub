"""Простой api в логе (прогон 73): сторож цикла событий различает синхронный код и процесс без процессора; отказ
эмулятора пишется с причиной и временем; `make e2e` переживает единичный 503 чтения."""

import asyncio
import logging

import httpx
import pytest

from app.core.loop_watch import cause, watch_loop


class Script:
    """Часы по сценарию: значения по очереди, потом последнее — время стоит, простоев больше нет."""

    def __init__(self, *values: float) -> None:
        self.values = list(values)

    def __call__(self) -> float:
        return self.values.pop(0) if len(self.values) > 1 else self.values[0]


@pytest.mark.anyio
async def test_loop_watch_tells_busy_loop_from_starved_process(caplog):
    """Часы и процессорное время — по сценарию, не настоящие: на общей VM под нагрузкой полного прогона тест с
    настоящими паузами ловил чужие простои (#251). Первый простой 3 с — процесс занят 2.9 с (синхронный код),
    второй 3 с — процессор не расходуется (процессу его не дали), дальше время стоит."""
    caplog.set_level(logging.WARNING, logger="api.loop")
    clock, cpu = Script(0.0, 3.0, 3.0, 6.0), Script(0.0, 2.9, 2.9, 2.9)
    watch = asyncio.create_task(watch_loop(interval=0, threshold=2.0, clock=clock, cpu=cpu))
    for _ in range(10):
        await asyncio.sleep(0)
    watch.cancel()
    messages = [r.getMessage() for r in caplog.records if r.name == "api.loop"]
    assert messages == [
        "цикл событий api простоял 3.0 с, процессор процесса за простой 2.9 с — синхронный код в цикле",
        "цикл событий api простоял 3.0 с, процессор процесса за простой 0.0 с — процесс не получал процессор",
    ]


def test_stall_cause_threshold():
    assert cause(lag=4.0, busy=2.0) == "синхронный код в цикле"  # половина простоя на процессоре — код
    assert cause(lag=4.0, busy=1.9) == "процесс не получал процессор"


@pytest.mark.anyio
async def test_emulator_failure_logged_with_cause(caplog, monkeypatch):
    from app.api import emulator

    def stalled(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("", request=request)

    monkeypatch.setattr(emulator, "transport", httpx.MockTransport(stalled))
    caplog.set_level(logging.WARNING, logger="api.emulator")
    with pytest.raises(emulator.EmulatorError) as e:
        await emulator._call("GET", "/state")
    assert (e.value.status, e.value.code) == (503, "emulator_unavailable")
    [message] = [r.getMessage() for r in caplog.records if r.name == "api.emulator"]
    assert message.startswith("эмулятор GET /state: ReadTimeout за ") and "(таймаут 10 с)" in message


def test_e2e_retries_single_503_of_reads(monkeypatch):
    import io
    import sys
    import urllib.error
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    import e2e_scenario

    answers = [503, 200]
    calls = []

    def urlopen(req, timeout):
        calls.append(req.get_method())
        code = answers.pop(0)
        if code != 200:
            raise urllib.error.HTTPError(req.full_url, code, "Service Unavailable", {}, io.BytesIO(b"{}"))
        return io.BytesIO(b'{"state": "playing"}')

    monkeypatch.setattr(e2e_scenario.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(e2e_scenario.time, "sleep", lambda s: None)
    api = e2e_scenario.Api("http://api")
    assert api.call("GET", "/emulator/state") == {"state": "playing"} and calls == ["GET", "GET"]
    answers[:] = [503]
    with pytest.raises(SystemExit):  # изменение не повторяется
        api.call("POST", "/emulator/start", {"scenario": "ppr"})
    assert calls[-1] == "POST" and len(calls) == 3
