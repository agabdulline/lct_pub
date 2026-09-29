import datetime as dt

from fastapi import APIRouter

from app.api.deps import RunCtx
from app.core import clock
from app.domain.runs import ACTIVE_STATUSES
from app.schemas.api import Clock

router = APIRouter(tags=["служебное"])


@router.get("/clock", response_model=Clock)
async def get_clock(ctx: RunCtx) -> dict:
    """Время выбранного прогона: у идущего — `SimulatedTime` эмулятора, если часы уже его (у только что созданного —
    ещё прошлого прогона), иначе — где прогон стоит; без прогонов — реальное время (D10)."""
    now = await clock.current()
    run = ctx.run
    if run is None:
        return {"mode": "real", "now": dt.datetime.now(dt.UTC)} if now["mode"] == "stream" else now
    if run.status in ACTIVE_STATUSES and now["mode"] == "stream" and now["run_id"] == run.id:
        return now
    return {"mode": "stream", "now": run.sim_now or run.sim_start}
