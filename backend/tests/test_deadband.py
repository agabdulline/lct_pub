import math
import random

from app.domain.deadband import Deadband


def test_rules():
    d = Deadband()
    assert d.keep(1, 0.01)
    assert not d.keep(1, 0.02)  # дрожание на шаг
    assert not d.keep(1, 0.03)  # ровно 0.02 — не больше порога
    assert d.keep(1, 0.04)
    assert d.keep(1, None)  # текстовое состояние пишется всегда
    assert d.keep(1, 0.04)  # и сбрасывает опорное значение
    assert d.keep(2, 0.04)  # у каждого канала своё опорное значение


def test_batch_equals_stream():
    """Пакетный вариант загрузчика и поточный вариант приёма дают одно и то же, в том числе между пакетами."""
    rng = random.Random(7)
    rows = []
    for channel_id in (5, 6, 9):
        x = 0.0
        for _ in range(500):
            x = round(x + rng.choice([-0.01, 0, 0.01, 0.03]), 2)
            rows.append((channel_id, None if rng.random() < 0.02 else x))

    stream = Deadband()
    expected = [i for i, (c, v) in enumerate(rows) if stream.keep(c, v)]

    batch = Deadband()
    kept: list[int] = []
    half = len(rows) // 2 + 17  # граница пакета внутри канала
    for offset, part in ((0, rows[:half]), (half, rows[half:])):
        channels = [c for c, _ in part]
        values = [math.nan if v is None else v for _, v in part]
        kept += [offset + i for i in batch.keep_sorted(channels, values)]
    assert kept == expected
