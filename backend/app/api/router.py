from fastapi import APIRouter

from app.api import (
    access,
    alarms,
    channels,
    clock,
    dashboard,
    datasets,
    emulator,
    equipment,
    health,
    ingest,
    objects,
    predictions,
    runs,
    stream,
    uploads,
    work_orders,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(clock.router)
api_router.include_router(ingest.router)
api_router.include_router(runs.router)
api_router.include_router(datasets.router)
api_router.include_router(emulator.router)
api_router.include_router(objects.router)
api_router.include_router(channels.router)
api_router.include_router(alarms.router)
api_router.include_router(equipment.router)
api_router.include_router(access.router)
api_router.include_router(stream.router)
api_router.include_router(predictions.router)
api_router.include_router(dashboard.router)
api_router.include_router(work_orders.router)
api_router.include_router(uploads.router)
