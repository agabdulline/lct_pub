"""Внешние наборы: `GET /datasets` (docs/api.md, «Внешние наборы»). Реестр — backend/config/external_datasets.yaml,
загрузка — `python -m app.cli load-external` (external-data.md §4.2)."""

from fastapi import APIRouter
from sqlalchemy import text

from app.api.deps import Session
from app.external.registry import load_registry
from app.schemas.datasets import DatasetList

router = APIRouter(tags=["внешние наборы"])

# Последняя основа каждого набора и размер справочника по источнику
_LOADED = """
SELECT DISTINCT ON (params->>'dataset') params->>'dataset' AS key, id, sim_start, sim_end,
    CAST(params->'rows'->>'events' AS bigint) AS events
FROM runs WHERE kind = 'dataset'
ORDER BY params->>'dataset', id DESC
"""
_CATALOG = """
SELECT source, (SELECT count(*) FROM objects o WHERE o.source = s.source) AS objects,
    (SELECT count(*) FROM channels c WHERE c.source = s.source) AS channels
FROM unnest(CAST(:keys AS text[])) AS s(source)
"""


@router.get("/datasets", response_model=DatasetList)
async def datasets(session: Session) -> dict:
    """Реестр внешних наборов по номеру `k` и что из них загружено в базу: основа, период журнала, объём."""
    registry = sorted(load_registry().values(), key=lambda d: d.k)
    loaded = {r.key: r for r in await session.execute(text(_LOADED))}
    catalog = {r.source: r for r in await session.execute(text(_CATALOG), {"keys": [d.key for d in registry]})}
    items = []
    for d in registry:
        base = loaded.get(d.key)
        items.append(
            {
                "key": d.key,
                "title": d.title,
                "short": d.short,
                "direction": d.direction,
                "description": " ".join(str(d.meta.get("description", "")).split()),
                "url": d.url,
                "doi": d.meta.get("doi"),
                "license": d.license,
                "license_url": d.meta.get("license_url"),
                "synthetic": d.synthetic,
                "base_run_id": base.id if base else None,
                "period_start": base.sim_start if base else None,
                "period_end": base.sim_end if base else None,
                "objects": catalog[d.key].objects,
                "channels": catalog[d.key].channels,
                "events": base.events if base else None,
            }
        )
    return {"items": items}
