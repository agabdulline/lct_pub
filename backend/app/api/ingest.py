"""`POST /ingest/events` — пакетный приём событий тем же кодом, что у шлюза (docs/api.md, «Приём данных»)."""

import csv
import datetime as dt
import io

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from app.api.errors import ApiError
from app.db.models import Run
from app.db.session import SessionLocal
from app.domain.ingest import RawEvent, ingest_events

router = APIRouter(tags=["приём данных"])

MAX_EVENTS = 200_000
_TRUE = {"t", "true", "1"}
_FALSE = {"f", "false", "0"}
HEADER_EVENT_ID = "ид_события"


class IngestEvent(BaseModel):
    channel_id: int
    ts: dt.datetime = Field(description="ISO 8601; без часового пояса — Europe/Moscow")
    value: str | float = Field(description="значение_датчика как в журнале: число или состояние")
    is_alarm: bool = False
    event_id: int | None = None


class IngestResponse(BaseModel):
    accepted: int
    duplicates: int
    unknown_channels: int
    alarms: int
    alarms_enqueued: int


def _value(v: str | float) -> str:
    return v if isinstance(v, str) else format(v, "g")


def parse_csv(body: str) -> list[RawEvent]:
    """6 колонок журнала: ид_события, ид_канала_данных, дата, время, тревожное, значение_датчика. Заголовки
    внутри файла пропускаются, кавычки и `true` / `false` — как в примере журнала 2026 года."""
    events = []
    for n, row in enumerate(csv.reader(io.StringIO(body)), start=1):
        if not row or row[0] == HEADER_EVENT_ID:
            continue
        if len(row) != 6:
            raise ApiError(400, "bad_csv", f"строка {n}: ожидалось 6 колонок, получено {len(row)}")
        event_id, channel_id, date, time, alarm, value = (x.strip() for x in row)
        if alarm.lower() not in _TRUE | _FALSE:
            raise ApiError(400, "bad_csv", f"строка {n}: тревожное = {alarm!r}")
        try:
            ts = dt.datetime.fromisoformat(f"{date}T{time}")
            events.append(
                RawEvent(int(channel_id), ts, value, alarm.lower() in _TRUE, int(event_id) if event_id else None)
            )
        except ValueError as e:
            raise ApiError(400, "bad_csv", f"строка {n}: {e}") from e
    return events


@router.post("/ingest/events", status_code=202, response_model=IngestResponse)
async def ingest(
    request: Request,
    run: int | None = Query(None, description="прогон; без параметра — базовая история"),
) -> IngestResponse:
    """Пакет событий: JSON-массив `[{channel_id, ts, value, is_alarm, event_id?}]` или CSV из 6 колонок журнала
    (`Content-Type: text/csv`). Повторная доставка не создаёт дублей."""
    content_type = request.headers.get("content-type", "application/json").split(";")[0].strip()
    body = await request.body()
    if content_type in ("text/csv", "text/plain"):
        events = parse_csv(body.decode("utf-8-sig"))
    elif content_type == "application/json":
        try:
            items = TypeAdapter(list[IngestEvent]).validate_json(body)
        except ValidationError as e:
            first = e.errors()[0]
            raise ApiError(422, "validation_error", f"{'.'.join(map(str, first['loc']))}: {first['msg']}") from e
        events = [RawEvent(i.channel_id, i.ts, _value(i.value), i.is_alarm, i.event_id) for i in items]
    else:
        raise ApiError(415, "unsupported_media_type", "ожидается application/json или text/csv")
    if len(events) > MAX_EVENTS:
        raise ApiError(413, "too_large", f"не больше {MAX_EVENTS} событий за запрос")

    if run is not None:
        async with SessionLocal() as session:
            if await session.get(Run, run) is None:
                raise ApiError(404, "run_not_found", f"прогон {run} не найден")
    result = await ingest_events(run, events)
    return IngestResponse(
        accepted=result.accepted,
        duplicates=result.duplicates,
        unknown_channels=result.unknown_channels,
        alarms=result.alarms,
        alarms_enqueued=result.alarms_enqueued,
    )
