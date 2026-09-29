"""Сценарий из своего журнала: загрузка CSV или XLSX, сопоставление колонок, срез для эмулятора (docs/api.md,
«Сценарий из своего журнала»; разбор — app/domain/uploads.py)."""

import datetime as dt
import json
import shutil
from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import text

from app.api.deps import Session
from app.api.errors import ApiError
from app.core.config import settings
from app.domain import uploads as U
from app.schemas.api import ScenarioCreated, ScenarioUpload, ScenarioUploadUpdate

router = APIRouter(tags=["сценарии из журнала"])

CHUNK = 1 << 20


async def _catalog(session) -> U.Catalog:
    rows = await session.execute(
        text(
            "SELECT c.id, c.name, coalesce(so.parent_id, so.id) AS object_id, coalesce(o.name, so.name) AS object_name "
            "FROM channels c JOIN objects so ON so.id = c.object_id LEFT JOIN objects o ON o.id = so.parent_id "
            # известные эмулятору — справочник хакатона: адресное пространство строится из него (этап 14)
            "WHERE c.source = 'hackathon'"
        )
    )
    return U.Catalog({r.id: (r.name, r.object_id, r.object_name) for r in rows})


def _error(e: U.UploadError) -> ApiError:
    return ApiError(e.status, e.code, e.message)


def _work(upload_id: str) -> Path:
    try:
        work = U.upload_dir(upload_id)
    except U.UploadError as e:
        raise _error(e) from e
    if not work.exists():
        raise ApiError(404, "upload_not_found", f"загрузки {upload_id} нет")
    return work


async def _analyze(session, work: Path, state: U.UploadState) -> dict:
    analysis = await run_in_threadpool(U.analyze, work, state.mapping, state.format["header"], await _catalog(session))
    result = U.response(state, analysis)
    (work / "analysis.json").write_text(json.dumps(result, ensure_ascii=False, default=str), encoding="utf-8")
    return result


def _now() -> str:
    return dt.datetime.now(dt.UTC).astimezone(U._MSK).isoformat(timespec="seconds")


@router.post("/scenario-uploads", status_code=201, response_model=ScenarioUpload)
async def create_upload(request: Request, file: UploadFile, session: Session) -> dict:
    """Журнал СМВУ в CSV или XLSX до 300 МБ: формат, колонки, предложенное сопоставление, предупреждения, превью."""
    limit = settings.upload_max_bytes
    if int(request.headers.get("content-length") or 0) > limit + CHUNK:
        raise ApiError(413, "too_large", f"файл больше {limit // CHUNK} МБ")
    upload_id = U.new_upload_id()
    work = settings.uploads_dir / upload_id
    work.mkdir(parents=True)
    file_name = Path(file.filename or "journal.csv").name
    target = work / f"source{Path(file_name).suffix.lower()[:8]}"
    size = 0
    try:
        with target.open("wb") as out:
            while chunk := await file.read(CHUNK):
                size += len(chunk)
                if size > limit:
                    raise ApiError(413, "too_large", f"файл больше {limit // CHUNK} МБ")
                out.write(chunk)
        if size == 0:
            raise ApiError(400, "bad_file", "файл пустой")
        fmt, sheets = await run_in_threadpool(U.prepare, target, file_name, work)
    except U.UploadError as e:
        shutil.rmtree(work, ignore_errors=True)
        raise _error(e) from e
    except ApiError:
        shutil.rmtree(work, ignore_errors=True)
        raise
    suggested = U.suggest(U.columns_of(work), fmt.header)
    state = U.UploadState(
        id=upload_id,
        file_name=file_name,
        stored_name=target.name,
        size_bytes=size,
        format=asdict(fmt),
        sheets=sheets,
        mapping={f: c for f, (c, _) in suggested.items()},
        matches={f: m for f, (_, m) in suggested.items()},
        uploaded_at=_now(),
    )
    state.save(work)
    return await _analyze(session, work, state)


@router.get("/scenario-uploads/{upload_id}", response_model=ScenarioUpload)
async def get_upload(upload_id: str) -> dict:
    """Последний разбор загрузки."""
    work = _work(upload_id)
    return json.loads((work / "analysis.json").read_text(encoding="utf-8"))


@router.put("/scenario-uploads/{upload_id}", response_model=ScenarioUpload | ScenarioCreated)
async def update_upload(upload_id: str, body: ScenarioUploadUpdate, response: Response, session: Session) -> dict:
    """Сопоставление колонок и лист XLSX. `dry_run: true` — пересчитанный разбор; иначе — сценарий эмулятора:
    срез `uploaded/<scenario_id>.parquet` и метаданные `.json` (201)."""
    work = _work(upload_id)
    try:
        state = U.UploadState.load(work)
    except U.UploadError as e:
        raise _error(e) from e
    if body.sheet is not None and state.format["kind"] == "xlsx" and body.sheet != state.format["sheet"]:
        if body.sheet not in (state.sheets or []):
            raise ApiError(422, "bad_sheet", f"листа «{body.sheet}» в книге нет")
        try:
            fmt, _ = await run_in_threadpool(U.prepare, work / state.stored_name, state.file_name, work, body.sheet)
        except U.UploadError as e:
            raise _error(e) from e
        suggested = U.suggest(U.columns_of(work), fmt.header)
        state.format = asdict(fmt)
        state.mapping = {f: c for f, (c, _) in suggested.items()}
        state.matches = {f: m for f, (_, m) in suggested.items()}
    if body.mapping is not None:
        width = len(U.columns_of(work))
        for column in body.mapping.values():
            if column is not None and not 0 <= column < width:
                raise ApiError(422, "bad_mapping", f"колонки {column} в файле нет: колонок {width}")
        for field, column in body.mapping.items():  # только переданные поля, остальные — как были
            if column != state.mapping.get(field):
                state.matches[field] = "manual" if column is not None else None
            state.mapping[field] = column
    state.save(work)
    result = await _analyze(session, work, state)
    if body.dry_run:
        return result
    if not result["ready"]:
        labels = ", ".join(f"«{U.LABELS[f]}»" for f in result["required_missing"])
        raise ApiError(422, "required_missing", f"не назначены колонки: {labels}")
    sid = U.scenario_id(upload_id)
    out_dir = settings.emulator_dir / "uploaded"
    await run_in_threadpool(U.build_slice, work, state.mapping, state.format["header"], out_dir, sid)
    period = result["period"]
    title = (body.title or "").strip() or result["name_suggestion"]
    naive = (
        {k: period[k].replace(tzinfo=None).isoformat(timespec="seconds") for k in ("start", "end")} if period else None
    )
    meta = {
        "scenario_id": sid,
        "id": sid,
        "title": title,
        "description": None,
        "period": naive,
        "objects": [o["id"] for o in result["stats"]["objects"]],
        "events": result["stats"]["events"],
        "channels": result["stats"]["channels"],
        "warnings": result["warnings"],
        "include_gas": True,
        "source": {
            "file_name": state.file_name,
            "size_bytes": state.size_bytes,
            "format": state.format,
            "uploaded_at": state.uploaded_at,
        },
        "source_file": state.file_name,
        "uploaded_at": state.uploaded_at,
        "mapping": state.mapping,
        "created_at": _now(),
    }
    await run_in_threadpool(U.write_meta, out_dir, sid, meta)
    response.status_code = 201
    return {
        "scenario_id": sid,
        "title": title,
        "period": period,
        "events": result["stats"]["events"],
        "channels": result["stats"]["channels"],
        "objects": result["stats"]["objects"],
        "warnings": result["warnings"],
    }
