"""Внешние наборы данных (этап 14, docs/architecture/external-data.md): реестр и что из него загружено."""

import datetime as dt
from typing import Literal

from pydantic import BaseModel


class DatasetOut(BaseModel):
    key: str
    title: str
    short: str
    direction: Literal["fire_risk", "unauthorized_access", "wear"]  # направление ТЗ
    description: str
    url: str
    doi: str | None
    license: str
    license_url: str | None
    synthetic: bool
    # что загружено в базу (`load-external`): прогон-основа, границы журнала, объём
    base_run_id: int | None
    period_start: dt.datetime | None
    period_end: dt.datetime | None
    objects: int
    channels: int
    events: int | None


class DatasetList(BaseModel):
    items: list[DatasetOut]
