"""HTTP-слой для импорта плейлиста из папки и ресинка (app/playlists/folder_sync.py).

Отдельный модуль, а не часть web/api.py: эти маршруты используют свои задачи
и хранилище превью, и так файл API не растёт ещё на тысячу строк.

Маршруты:
  POST /api/web/playlists/folder-scan                 — запуск сборки (job)
  GET  /api/web/playlists/folder-jobs/{job_id}         — опрос прогресса
  POST /api/web/playlists/{id}/resync/preview          — запуск проверки (job)
  POST /api/web/playlists/{id}/resync/confirm          — применить превью

Превью → подтверждение: удаление делается только в confirm, и только для
link_id, которые клиент явно отметил из списка remove.
"""

from __future__ import annotations

import asyncio
import logging
import time
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.bootstrap import AppContext
from app.playlists.folder_sync import (
    FolderSyncError,
    validate_folder,
    JobRegistry,
    PlanStore,
    apply_resync_plan,
    build_resync_plan,
    scan_folder_to_playlist,
)
from app.repositories.playlists import PlaylistRepository
from app.web.deps import csrf_protected, current_session, get_ctx
from app.web.sessions import SessionRecord

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/web", tags=["web"])

# Состояние живёт в памяти процесса: после перезапуска прогресс сбрасывается,
# но сами плейлисты в БД остаются.
JOBS = JobRegistry()
PLANS = PlanStore()
# Держим ссылки на фоновые задачи, иначе asyncio может их собрать раньше времени.
_TASKS: set[asyncio.Task] = set()

# Код ошибки → HTTP-статус.
_STATUS = {
    "library_stale": 409,
    "forbidden": 403,
    "not_found": 404,
    "plan_not_found": 404,
}


def _error(exc: FolderSyncError) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "code": exc.code, "detail": exc.message},
        status_code=_STATUS.get(exc.code, 400),
    )


def _spawn(job, coro) -> None:
    """Запускает корутину задачи и переводит job в done/error."""

    async def runner() -> None:
        try:
            await coro
        except FolderSyncError as exc:
            job.state, job.error_code, job.error = "error", exc.code, exc.message
        except Exception as exc:  # noqa: BLE001 - задача не должна молча умереть
            logger.exception("задача %s упала", job.kind)
            job.state, job.error_code, job.error = "error", "internal", str(exc)
        finally:
            if job.state == "running":
                job.state = "done"
            job.finished_at = time.monotonic()

    task = asyncio.create_task(runner(), name=f"folder-{job.kind}-{job.id}")
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


def _user_flags(session: SessionRecord) -> tuple[int, bool]:
    return session.user.id, session.user.role == "admin"


# ----------------------------------------------------------------------
# Сценарий 1: папка → плейлист
# ----------------------------------------------------------------------
@router.post("/playlists/folder-scan")
async def start_folder_scan(
    request: Request,
    ctx: AppContext = Depends(get_ctx),
    session: SessionRecord = Depends(csrf_protected),
) -> JSONResponse:
    """Body: {"folder": "/music/Artist1", "name": "опционально"}."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if not isinstance(body, dict):
        body = {}
    folder = str(body.get("folder") or "")
    name = body.get("name")
    user_id, _ = _user_flags(session)

    # Проверку пути делаем сразу, чтобы клиент увидел ошибку без опроса.
    try:
        validate_folder(folder, ctx.settings.music_dir)
    except FolderSyncError as exc:
        return _error(exc)

    job = JOBS.new("scan")
    _spawn(job, _run_scan(ctx, job, folder, user_id, name))
    return JSONResponse({"ok": True, "data": job.as_dict()}, status_code=202)


async def _run_scan(ctx: AppContext, job, folder: str, user_id: int, name) -> None:
    result = await scan_folder_to_playlist(
        db=ctx.music_db,
        playlists=ctx.playlists,
        music_root=ctx.settings.music_dir,
        folder=folder,
        owner_id=user_id,
        name=name if isinstance(name, str) else None,
        job=job,
    )
    job.result = result.as_dict()
    job.done = job.total


# ----------------------------------------------------------------------
# Сценарий 2: ресинк
# ----------------------------------------------------------------------
@router.post("/playlists/{playlist_id}/resync/preview")
async def start_resync_preview(
    playlist_id: int,
    ctx: AppContext = Depends(get_ctx),
    session: SessionRecord = Depends(csrf_protected),
) -> JSONResponse:
    user_id, is_admin = _user_flags(session)
    job = JOBS.new("resync")
    _spawn(job, _run_preview(ctx, job, playlist_id, user_id, is_admin))
    return JSONResponse({"ok": True, "data": job.as_dict()}, status_code=202)


async def _run_preview(ctx, job, playlist_id: int, user_id: int, is_admin: bool) -> None:
    plan = await build_resync_plan(
        db=ctx.music_db,
        playlists=ctx.playlists,
        music_root=ctx.settings.music_dir,
        playlist_id=playlist_id,
        user_id=user_id,
        is_admin=is_admin,
        job=job,
    )
    token = PLANS.put(plan)
    job.result = {"token": token, "plan": plan.as_dict()}


@router.post("/playlists/{playlist_id}/resync/confirm")
async def confirm_resync(
    playlist_id: int,
    request: Request,
    ctx: AppContext = Depends(get_ctx),
    session: SessionRecord = Depends(csrf_protected),
) -> JSONResponse:
    """Body: {"token": "...", "remove_link_ids": [..]}.

    Перемещения и добавления применяются, удаляются только отмеченные.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if not isinstance(body, dict):
        body = {}
    user_id, is_admin = _user_flags(session)
    try:
        plan = PLANS.take(str(body.get("token") or ""))
        if plan.playlist_id != playlist_id:
            raise FolderSyncError("plan_not_found", "Превью относится к другому плейлисту")
        playlist = await ctx.playlists.get(playlist_id)
        if playlist is None or not PlaylistRepository.can_edit(
            playlist, user_id, is_admin=is_admin
        ):
            raise FolderSyncError("forbidden", "Нет прав на редактирование плейлиста")
        raw_ids = body.get("remove_link_ids") or []
        if not isinstance(raw_ids, list):
            raw_ids = []
        remove_ids = [int(x) for x in raw_ids if str(x).lstrip("-").isdigit()]
        result = await apply_resync_plan(
            db=ctx.music_db,
            playlists=ctx.playlists,
            plan=plan,
            remove_link_ids=remove_ids,
        )
    except FolderSyncError as exc:
        return _error(exc)
    return JSONResponse({"ok": True, "data": result})


@router.get("/playlists/folder-jobs/{job_id}")
async def folder_job_status(
    job_id: str,
    session: SessionRecord = Depends(current_session),
) -> JSONResponse:
    """Опрос прогресса для обеих задач (сборка и превью ресинка)."""
    job = JOBS.get(job_id)
    if job is None:
        return JSONResponse({"ok": False, "detail": "Задача не найдена"}, status_code=404)
    return JSONResponse({"ok": True, "data": job.as_dict()})


@router.get("/folder-synced-playlists")
async def folder_synced_playlists(
    ctx: AppContext = Depends(get_ctx),
    session: SessionRecord = Depends(current_session),
) -> JSONResponse:
    """Плейлисты текущего пользователя, созданные из папки (для блока ресинка)."""
    rows = await ctx.music_db.fetch_all(
        "SELECT id, name, origin_folder_path, synced_at FROM playlists "
        "WHERE origin_folder_path IS NOT NULL AND owner_id = ? ORDER BY name",
        (session.user.id,),
    )
    items = [
        {
            "id": int(r["id"]),
            "name": r["name"],
            "origin_folder_path": r["origin_folder_path"],
            "synced_at": r["synced_at"],
        }
        for r in rows
    ]
    return JSONResponse({"ok": True, "data": {"items": items}})
