"""Объекты и граф (docs/api.md, «Объекты и каналы», «Граф объектов»)."""

from fastapi import APIRouter

from app.api.deps import RunContext, RunCtx, Session
from app.api.errors import ApiError
from app.db.models import HACKATHON
from app.domain.views import ChannelView, aggregate, by_sensor_type, by_sub_object, channel_views, object_tree
from app.schemas.api import Graph, ObjectDetail, ObjectNode

router = APIRouter(tags=["объекты"])

NO_GROUP = "без группы"


async def scope_views(session, ctx: RunContext) -> dict[int, ChannelView]:
    """Каналы с состоянием в представлении экрана. «Все данные» (`?run=all`): хакатон — по базе на конец его данных,
    каждый набор — по своему прогону-основе на его конец; времена источников не смешиваются."""
    if not ctx.all_data:
        return await channel_views(session, ctx.run_id, ctx.now, ctx.source)
    views = await channel_views(session, None, ctx.now, HACKATHON)
    for run in ctx.dataset_runs:
        dataset = (run.params or {}).get("dataset")
        if dataset:
            views |= await channel_views(session, run.id, run.sim_now or run.sim_end or run.sim_start, dataset)
    return views


def _tree_source(ctx: RunContext) -> str | None:
    """Справочник экрана: «Все данные» — все наборы, иначе только набор прогона."""
    return None if ctx.all_data else ctx.source


def _channels_under(tree, groups, object_id: int) -> list:
    return [c for sub in tree.sub_objects_under(object_id) for c in groups.get(sub, [])]


@router.get("/objects", response_model=list[ObjectNode])
async def objects(ctx: RunCtx, session: Session) -> list[dict]:
    """Дерево район → объекты → подобъекты с агрегатами: каналы, тревоги, риск, неисправности, статус."""
    tree = await object_tree(session, _tree_source(ctx))
    groups = by_sub_object(await scope_views(session, ctx))

    def node(object_id: int) -> dict:
        o = tree.objects[object_id]
        return {
            **o,
            **aggregate(_channels_under(tree, groups, object_id)).as_dict(),
            "children": [node(c) for c in tree.children.get(object_id, [])],
        }

    if ctx.all_data:  # «Все данные»: корень каждого набора — хакатон первым
        roots = [o["id"] for o in tree.objects.values() if o["parent_id"] is None]
        return [node(r) for r in sorted(roots, key=lambda r: (tree.objects[r]["source"] != HACKATHON, r))]
    return [node(tree.district_id)] if tree.district_id else []


@router.get("/objects/{object_id}", response_model=ObjectDetail)
async def object_detail(object_id: int, ctx: RunCtx, session: Session) -> dict:
    tree = await object_tree(session, _tree_source(ctx))
    if object_id not in tree.objects:
        raise ApiError(404, "object_not_found", f"объект {object_id} не найден")
    groups = by_sub_object(await scope_views(session, ctx))
    channels = _channels_under(tree, groups, object_id)
    subs = sorted(tree.sub_objects_under(object_id) - {object_id}, key=lambda i: tree.objects[i]["name"])
    return {
        **tree.objects[object_id],
        **aggregate(channels).as_dict(),
        "path": [{"id": p["id"], "name": p["name"], "level": p["level"]} for p in tree.path(object_id)],
        "sub_objects": [{**tree.objects[s], **aggregate(groups.get(s, [])).as_dict()} for s in subs],
        "by_sensor_type": by_sensor_type(channels),
    }


def _node(kind: str, node_id: str, label: str, parent: str | None, agg, source: str, picket: int | None = None) -> dict:
    a = agg.as_dict()
    return {
        "id": node_id,
        "kind": kind,
        "label": label,
        "parent": parent,
        "subsystem": None,
        "sensor_type": None,
        "status": a["status"],
        "risk": a["max_risk"],
        "active_alarms": a["active_alarms"],
        "picket_number": picket,
        "channels": a["channels"],
        "at_risk": a["at_risk"],
        "in_fault": a["in_fault"],
        "source": source,
    }


@router.get("/graph", response_model=Graph)
async def graph(ctx: RunCtx, session: Session) -> dict:
    """Обзор: район, объекты, подобъекты с агрегатами; рёбра иерархии. Каналы — в `/graph/{sub_object_id}`."""
    tree = await object_tree(session, _tree_source(ctx))
    groups = by_sub_object(await scope_views(session, ctx))
    nodes, edges = [], []
    for o in tree.objects.values():
        node_id = f"{o['level']}:{o['id']}"
        parent = tree.objects.get(o["parent_id"]) if o["parent_id"] else None
        parent_id = f"{parent['level']}:{parent['id']}" if parent else None
        nodes.append(
            _node(
                o["level"],
                node_id,
                o["name"],
                parent_id,
                aggregate(_channels_under(tree, groups, o["id"])),
                o["source"],
            )
        )
        if parent_id:
            edges.append({"source": parent_id, "target": node_id})
    return {"nodes": nodes, "edges": edges}


@router.get("/graph/{sub_object_id}", response_model=Graph)
async def sub_object_graph(sub_object_id: int, ctx: RunCtx, session: Session) -> dict:
    """Раскрытый подобъект: группы по тегу и каналы; рёбра подобъект → группа → канал."""
    tree = await object_tree(session, _tree_source(ctx))
    sub = tree.objects.get(sub_object_id)
    if sub is None or sub["level"] != "sub_object":
        raise ApiError(404, "sub_object_not_found", f"подобъект {sub_object_id} не найден")
    channels = by_sub_object(await scope_views(session, ctx)).get(sub_object_id, [])
    by_group: dict[str, list] = {}
    for c in sorted(channels, key=lambda c: (c.picket_number is None, c.picket_number or 0, c.name)):
        by_group.setdefault(c.tag_group or NO_GROUP, []).append(c)
    sub_id = f"sub_object:{sub_object_id}"
    nodes, edges = [], []
    for group, members in sorted(by_group.items()):
        group_id = f"tag_group:{sub_object_id}:{group}"
        pickets = [c.picket_number for c in members if c.picket_number is not None]
        picket = round(sum(pickets) / len(pickets)) if pickets else None
        nodes.append(_node("tag_group", group_id, group, sub_id, aggregate(members), sub["source"], picket))
        edges.append({"source": sub_id, "target": group_id})
        for c in members:
            nodes.append(
                {
                    "id": f"channel:{c.id}",
                    "kind": "channel",
                    "label": c.name,
                    "parent": group_id,
                    "subsystem": c.subsystem,
                    "sensor_type": c.sensor_type,
                    "status": c.status,
                    "risk": c.risk,
                    "active_alarms": 1 if c.status in ("alarm", "ppr") else 0,
                    "picket_number": c.picket_number,
                    "channels": None,
                    "at_risk": None,
                    "in_fault": None,
                    "source": c.source,
                }
            )
            edges.append({"source": group_id, "target": f"channel:{c.id}"})
    return {"nodes": nodes, "edges": edges}
