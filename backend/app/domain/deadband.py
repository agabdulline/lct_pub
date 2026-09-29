"""Зона нечувствительности для газовых показаний (D25, docs/architecture/data.md §2).

Числовое показание пишется в `events`, только если отличается от последнего записанного больше чем на 0.02.
Текстовое состояние (`Обнаружен газ`, `Неисправен` …) пишется всегда и сбрасывает опорное значение: первое
число после смены состояния тоже пишется — так видно, что канал вернулся к показаниям.
Одно правило для загрузчика базы (пакетно) и приёма потока (по событию).
"""

GAS_DEADBAND = 0.02


def hackathon_route(sensor_type: str) -> tuple[str, float | None]:
    """Маршрут канала хакатона (D31): газ — сырой поток с зоной нечувствительности, движение — сырой поток,
    остальное — значимые события. Загрузчик пишет его в справочник (`channels.stream`, `channels.deadband`)."""
    from app.domain.values import GAS_SENSOR, MOTION_SENSOR

    if sensor_type == GAS_SENSOR:
        return "raw", GAS_DEADBAND
    if sensor_type == MOTION_SENSOR:
        return "raw", None
    return "significant", None


_EPS = 1e-9  # показания квантованы шагом 0.01: разница ровно в два шага не должна проходить из-за ошибки float


class Deadband:
    """Опорное значение по каналам. `keep()` — пропустить ли показание в `events`."""

    def __init__(self, width: float = GAS_DEADBAND, reference: dict[int, float | None] | None = None) -> None:
        self.width = width + _EPS
        self.reference: dict[int, float | None] = reference if reference is not None else {}

    def keep(self, channel_id: int, value_num: float | None) -> bool:
        if value_num is None:
            self.reference[channel_id] = None
            return True
        ref = self.reference.get(channel_id)
        if ref is None or abs(value_num - ref) > self.width:
            self.reference[channel_id] = value_num
            return True
        return False

    def keep_sorted(self, channel_ids: list[int], values: list[float]) -> list[int]:
        """Пакетный вариант `keep()` для строк, упорядоченных по каналу и времени; NaN — текстовое состояние.
        Возвращает номера строк, которые пишутся. Опорные значения переходят между пакетами."""
        width = self.width
        reference = self.reference
        kept: list[int] = []
        last_channel = None
        ref = None
        for i, (channel_id, x) in enumerate(zip(channel_ids, values, strict=True)):
            if channel_id != last_channel:
                if last_channel is not None:
                    reference[last_channel] = ref
                last_channel = channel_id
                ref = reference.get(channel_id)
            if x != x:  # NaN
                ref = None
                kept.append(i)
            elif ref is None or abs(x - ref) > width:
                ref = x
                kept.append(i)
        if last_channel is not None:
            reference[last_channel] = ref
        return kept
