"""JSON API веб-интерфейса: полное управление сервером.

Все изменяющие методы требуют CSRF-токен и, где нужно, роль администратора.
Отличие от Subsonic-слоя: здесь ответы — обычный JSON с HTTP-статусами,
а не конверт `subsonic-response`.

Группы:
  /api/web/session        — кто я, выход
  /api/web/albums|search  — библиотека
  /api/web/playlists      — плейлисты
  /api/web/users          — управление пользователями (admin)
  /api/web/server         — состояние сервера (admin)
  /api/web/account        — свой пароль и свои сессии
"""

from __future__ import annotations

import asyncio
import json

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from app.bootstrap import AppContext, apply_runtime_settings
from app.repositories.playlists import PlaylistError, PlaylistRepository
from app.repositories.users import UserError
from app.utils.timeutil import utcnow_iso
from app.web.deps import (
    admin_csrf,
    csrf_protected,
    current_session,
    get_ctx,
    require_admin,
)
from app.web.sessions import SessionRecord

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/web", tags=["web"])


def _scan_error_fields(row) -> dict[str, Any]:
    """Поля ошибок скана для JSON: полное число и сохранённые тексты."""
    total, items = _scan_errors(row)
    return {"errors_total": total, "errors": items}


def _scan_errors(row) -> tuple[int, list[dict[str, str]]]:
    """Возвращает (полное число ошибок, записи «файл / причина»).

    errors_total и errors_json появились в схеме v3. Если счётчика нет,
    считаем по files_failed — старые записи истории не должны врать. Записи
    до v3.1 хранили склеенную строку «путь: причина», поэтому понимаем оба
    формата: разбирать старую строку лучше здесь, чем в интерфейсе.
    """
    keys = row.keys()
    total = int(row["errors_total"] or 0) if "errors_total" in keys else 0
    if not total:
        total = int(row["files_failed"] or 0)
    raw = row["errors_json"] if "errors_json" in keys else None
    if not raw:
        return total, []
    try:
        items = json.loads(raw)
    except (TypeError, ValueError):
        return total, []
    if not isinstance(items, list):
        return total, []

    result: list[dict[str, str]] = []
    for item in items:
        if isinstance(item, dict):
            result.append({
                "path": str(item.get("path") or ""),
                "reason": str(item.get("reason") or ""),
            })
        else:
            text = str(item)
            # Старый формат: «/music/a.mp3: FLACNoHeaderError: ...».
            path, sep, reason = text.partition(": ")
            if sep:
                result.append({"path": path, "reason": reason})
            else:
                result.append({"path": text, "reason": ""})
    return total, result


def _ok(payload: Any = None, *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        {"ok": True, "data": payload}, status_code=status_code
    )


def _fail(detail: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "detail": detail}, status_code=status_code)


async def _json_body(request: Request) -> dict:
    """Тело запроса как объект. Пустое или битое тело — пустой объект."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    return body if isinstance(body, dict) else {}


def _user_json(user) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "is_active": user.is_active,
        "email": user.email,
        "display_name": user.display_name,
        "created_at": user.created_at,
        "last_login_at": user.last_login_at,
    }


def _version() -> str:
    from app import __version__

    return __version__


# ----------------------------------------------------------------------
# Сессия
# ----------------------------------------------------------------------
@router.get("/session")
async def whoami(
    session: SessionRecord = Depends(current_session),
) -> JSONResponse:
    return _ok(
        {
            "username": session.user.username,
            "role": session.user.role,
            "display_name": session.user.display_name,
            "csrf": session.csrf_token,
            "session_id": session.id,
        }
    )


@router.post("/logout")
async def logout(
    request: Request,
    ctx: AppContext = Depends(get_ctx),
    session: SessionRecord = Depends(csrf_protected),
) -> JSONResponse:
    from app.web.sessions import SESSION_COOKIE

    token = request.cookies.get(SESSION_COOKIE)
    if token:
        await ctx.sessions.revoke_token(token)
    response = _ok({"logged_out": True})
    response.delete_cookie(SESSION_COOKIE)
    return response


# ----------------------------------------------------------------------
# Библиотека
# ----------------------------------------------------------------------
@router.get("/stats")
async def stats(
    ctx: AppContext = Depends(get_ctx),
    session: SessionRecord = Depends(current_session),
) -> JSONResponse:
    library = await ctx.tracks.library_stats()
    return _ok(
        {
            "tracks": library.get("tracks", 0),
            "albums": await ctx.library.count_albums(),
            "artists": len(await ctx.library.list_artists()),
            "users": await ctx.users.count_users(),
        }
    )


@router.get("/albums")
async def albums(
    ctx: AppContext = Depends(get_ctx),
    session: SessionRecord = Depends(current_session),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> JSONResponse:
    rows = await ctx.library.list_albums(limit=limit, offset=offset)
    return _ok(
        {
            "items": [
                {
                    "id": album.id,
                    "name": album.name,
                    "artist": album.artist_name,
                    "year": album.year,
                    "song_count": album.song_count,
                }
                for album in rows
            ],
            # Сколько всего: нужно, чтобы интерфейс знал, есть ли следующая
            # страница, и не тянул сразу все 9000 альбомов.
            "total": await ctx.library.count_albums(),
            "limit": limit,
            "offset": offset,
        }
    )


@router.get("/albums/{album_id}")
async def album_detail(
    album_id: int,
    ctx: AppContext = Depends(get_ctx),
    session: SessionRecord = Depends(current_session),
) -> JSONResponse:
    album = await ctx.library.get_album(album_id)
    if album is None:
        raise HTTPException(status_code=404, detail="Album not found")
    tracks = await ctx.library.album_tracks(album_id)
    return _ok(
        {
            "id": album.id,
            "name": album.name,
            "artist": album.artist_name,
            "year": album.year,
            "tracks": [
                {
                    "id": track.id,
                    "title": track.title,
                    "artist": track.artist,
                    "album": track.album,
                    "duration": track.duration_sec,
                    "track_number": track.track_number,
                }
                for track in tracks
            ],
        }
    )


@router.get("/search")
async def search(
    ctx: AppContext = Depends(get_ctx),
    session: SessionRecord = Depends(current_session),
    q: str = Query("", max_length=200),
) -> JSONResponse:
    text = q.strip()
    if not text:
        return _ok({"tracks": [], "albums": [], "artists": []})
    tracks = await ctx.tracks.search(text, limit=50)
    albums = await ctx.library.search_albums(text, limit=20)
    artists = await ctx.library.search_artists(text, limit=20)
    return _ok(
        {
            "tracks": [
                {
                    "id": track.id,
                    "title": track.title,
                    "artist": track.artist,
                    "album": track.album,
                    "duration": track.duration_sec,
                }
                for track in tracks
            ],
            "albums": [
                {"id": album.id, "name": album.name, "artist": album.artist_name}
                for album in albums
            ],
            "artists": [{"id": a.id, "name": a.name} for a in artists],
        }
    )


@router.get("/recently-played")
async def recently_played(
    ctx: AppContext = Depends(get_ctx),
    session: SessionRecord = Depends(current_session),
    limit: int = Query(20, ge=1, le=100),
) -> JSONResponse:
    """Последние прослушанные треки — для главной страницы.

    История личная: у каждого пользователя своя, поэтому фильтр по `user_id`.
    Если пользователь ещё ничего не слушал, вернётся пустой список —
    интерфейс показывает пустое состояние, а не чужие треки.
    """
    rows = await ctx.tracks.recently_played(session.user.id, limit=limit)
    return _ok({"items": [_track_json(track) for track in rows]})


@router.get("/recently-added")
async def recently_added(
    ctx: AppContext = Depends(get_ctx),
    session: SessionRecord = Depends(current_session),
    limit: int = Query(20, ge=1, le=100),
) -> JSONResponse:
    """Последние добавленные в библиотеку треки."""
    rows = await ctx.tracks.recently_added(limit=limit)
    return _ok({
        "items": [_track_json(track) for track in rows],
        "albums": [
            {
                "id": album.id,
                "name": album.name,
                "artist": album.artist_name,
                "song_count": album.song_count,
            }
            for album in await ctx.library.list_albums(sort="newest", limit=12)
        ],
    })


@router.post("/play-event")
async def record_play_event(
    request: Request,
    ctx: AppContext = Depends(get_ctx),
    session: SessionRecord = Depends(csrf_protected),
) -> JSONResponse:
    """Отметка о старте воспроизведения из веб-плеера.

    Subsonic-клиенты пишут историю сами через `scrobble`, а браузер — нет.
    Без этого «Недавно прослушанные» показывал бы только то, что играло
    в Arpeggi, и не замечал плеер сайта.
    """
    body = await _json_body(request)
    try:
        track_id = int(body.get("track_id"))
    except (TypeError, ValueError):
        return _fail("track_id обязателен")

    track = await ctx.tracks.get_by_id(track_id)
    if track is None or track.missing:
        raise HTTPException(status_code=404, detail="Track not found")

    await ctx.tracks.record_play(
        track_id, session.user.id, client="web",
        played_ms=int(body.get("played_ms") or 0),
    )
    return _ok({"track_id": track_id})


# Фоновые задачи сканирования: держим ссылку, иначе asyncio может собрать её раньше.
_SCAN_TASKS: set[asyncio.Task] = set()


async def _run_scan(ctx: AppContext, mode: str) -> None:
    """Фоновый проход. Результат виден через /scan/status, а не в HTTP-ответе."""
    state = ctx.scan_state
    try:
        await ctx.scanner.scan(mode=mode)
        await state.finish(status="completed")
    except Exception as exc:  # noqa: BLE001 - причину показываем в UI
        logger.exception("скан из веб-интерфейса не удался")
        await state.fail(str(exc))


@router.post("/scan")
async def trigger_scan(
    ctx: AppContext = Depends(get_ctx),
    session: SessionRecord = Depends(admin_csrf),
    mode: str = Query("incremental"),
) -> JSONResponse:
    """Запускает скан в фоне и сразу отвечает 202.

    Раньше ответ ждал конца скана, и интерфейс не видел прогресса, пока
    не обновят страницу. Теперь клиент опрашивает GET /scan/status.
    """
    if mode not in ("full", "incremental"):
        raise HTTPException(
            status_code=400, detail="mode must be full or incremental"
        )
    state = ctx.scan_state
    if not await state.begin(mode):
        # Уже идёт: не запускаем второй проход, отдаём текущее состояние.
        return _fail("Сканирование уже выполняется", 409)
    task = asyncio.create_task(_run_scan(ctx, mode), name=f"scan-{mode}")
    _SCAN_TASKS.add(task)
    task.add_done_callback(_SCAN_TASKS.discard)
    return _ok({"started": True, "mode": mode, **state.as_dict()}, status_code=202)


@router.get("/scan/status")
async def scan_status(
    ctx: AppContext = Depends(get_ctx),
    session: SessionRecord = Depends(current_session),
) -> JSONResponse:
    """Текущее состояние и прогресс. Опрашивается, пока идёт скан.

    Плюс последний завершённый проход из БД: после перезапуска контейнера
    состояние в памяти пустое, а история должна остаться.
    """
    payload = {
        "current": ctx.scan_state.as_dict(),
        "covers": ctx.cover_pass.as_dict(),
        "watcher": None,
        "last_run": None,
    }
    if ctx.watcher is not None:
        payload["watcher"] = {
            "state": ctx.watcher.state,
            "pending": ctx.watcher.pending,
        }
    row = await ctx.music_db.fetch_one(
        "SELECT * FROM scan_runs ORDER BY id DESC LIMIT 1"
    )
    if row is not None:
        payload["last_run"] = {
            "run_id": int(row["id"]),
            "mode": str(row["mode"]),
            "status": str(row["status"]),
            "started_at": str(row["started_at"]),
            "finished_at": row["finished_at"],
            "seen": int(row["files_seen"] or 0),
            "added": int(row["files_added"] or 0),
            "updated": int(row["files_updated"] or 0),
            "removed": int(row["files_removed"] or 0),
            "failed": int(row["files_failed"] or 0),
            "error": row["error"],
            **_scan_error_fields(row),
        }
    return _ok(payload)


@router.get("/scan/history")
async def scan_history(
    ctx: AppContext = Depends(get_ctx),
    session: SessionRecord = Depends(current_session),
    limit: int = Query(20, ge=1, le=100),
) -> JSONResponse:
    """Последние проходы: что и когда сканировалось."""
    rows = await ctx.music_db.fetch_all(
        "SELECT * FROM scan_runs ORDER BY id DESC LIMIT ?", (limit,)
    )
    return _ok({
        "items": [
            {
                "run_id": int(row["id"]),
                "mode": str(row["mode"]),
                "status": str(row["status"]),
                "started_at": str(row["started_at"]),
                "finished_at": row["finished_at"],
                "seen": int(row["files_seen"] or 0),
                "added": int(row["files_added"] or 0),
                "updated": int(row["files_updated"] or 0),
                "removed": int(row["files_removed"] or 0),
                "failed": int(row["files_failed"] or 0),
                "error": row["error"],
                **_scan_error_fields(row),
            }
            for row in rows
        ]
    })


# ----------------------------------------------------------------------
# Плейлисты
# ----------------------------------------------------------------------
@router.get("/playlists")
async def list_playlists(
    ctx: AppContext = Depends(get_ctx),
    session: SessionRecord = Depends(current_session),
) -> JSONResponse:
    rows = await ctx.playlists.list_visible(session.user.id)
    return _ok(
        {
            "items": [
                {
                    "id": playlist.id,
                    "name": playlist.name,
                    "song_count": playlist.song_count,
                    "is_public": playlist.is_public,
                    "is_smart": playlist.is_smart,
                    "owner_id": playlist.owner_id,
                }
                for playlist in rows
            ]
        }
    )


@router.post("/playlists")
async def create_playlist(
    request: Request,
    ctx: AppContext = Depends(get_ctx),
    session: SessionRecord = Depends(csrf_protected),
) -> JSONResponse:
    body = await _json_body(request)
    name = str(body.get("name") or "").strip()
    if not name:
        return _fail("Название обязательно")
    try:
        playlist = await ctx.playlists.create(
            session.user.id,
            name,
            comment=body.get("comment"),
            is_public=bool(body.get("is_public", False)),
            smart_rules=body.get("smart_rules"),
        )
    except PlaylistError as exc:
        return _fail(str(exc))
    return _ok({"id": playlist.id, "name": playlist.name}, status_code=201)


@router.delete("/playlists/{playlist_id}")
async def delete_playlist(
    playlist_id: int,
    ctx: AppContext = Depends(get_ctx),
    session: SessionRecord = Depends(csrf_protected),
) -> JSONResponse:
    playlist = await ctx.playlists.get(playlist_id)
    if playlist is None:
        raise HTTPException(status_code=404, detail="Playlist not found")
    if not PlaylistRepository.can_edit(
        playlist, session.user.id, is_admin=session.is_admin
    ):
        raise HTTPException(status_code=403, detail="Not your playlist")
    await ctx.playlists.delete(playlist_id)
    return _ok({"deleted": playlist_id})


@router.post("/playlists/purge")
async def purge_playlists(
    request: Request,
    ctx: AppContext = Depends(get_ctx),
    session: SessionRecord = Depends(csrf_protected),
) -> JSONResponse:
    """Удаляет все плейлисты пользователя одним запросом.

    Что именно удаляется, задаёт тело:
      {"scope": "mine" | "empty" | "all"}

      * mine  — только свои (по умолчанию);
      * empty — только свои с нулём треков (чистка «фантомов» от импорта);
      * all   — все, включая чужие; только для администратора.

    Чужие плейлисты не-админ удалить не может даже через scope="all":
    это удаление данных другого пользователя, а не «своей уборки».
    Возвращает, сколько удалено и сколько пропущено.
    """
    body = await _json_body(request)
    scope = str(body.get("scope") or "mine")
    if scope not in ("mine", "empty", "all"):
        return _fail("scope должен быть mine, empty или all")
    if scope == "all" and not session.is_admin:
        return _fail("Удалять чужие плейлисты может только администратор", 403)

    visible = await ctx.playlists.list_visible(session.user.id)
    targets = []
    skipped = 0
    for playlist in visible:
        # Чужой приватный плейлист не виден вовсе, поэтому проверяем владельца:
        # удалять можно своё, либо что угодно при scope="all" от админа.
        own = playlist.owner_id == session.user.id
        if not own and scope != "all":
            skipped += 1
            continue
        if scope == "empty" and playlist.song_count != 0:
            skipped += 1
            continue
        targets.append(playlist)

    deleted_ids: list[int] = []
    errors: list[str] = []
    for playlist in targets:
        try:
            await ctx.playlists.delete(playlist.id)
            deleted_ids.append(playlist.id)
        except Exception as exc:  # noqa: BLE001 - показываем причину, не падаем
            logger.exception("не удалось удалить плейлист %d", playlist.id)
            errors.append(f"«{playlist.name}»: {exc}")

    logger.info(
        "массовое удаление плейлистов: scope=%s, удалено=%d, пропущено=%d",
        scope, len(deleted_ids), skipped,
    )
    return _ok({
        "scope": scope,
        "deleted": len(deleted_ids),
        "deleted_ids": deleted_ids,
        "skipped": skipped,
        "errors": errors,
    })


@router.post("/playlists/{playlist_id}/public")
async def toggle_playlist_public(
    playlist_id: int,
    request: Request,
    ctx: AppContext = Depends(get_ctx),
    session: SessionRecord = Depends(csrf_protected),
) -> JSONResponse:
    playlist = await ctx.playlists.get(playlist_id)
    if playlist is None:
        raise HTTPException(status_code=404, detail="Playlist not found")
    if not PlaylistRepository.can_edit(
        playlist, session.user.id, is_admin=session.is_admin
    ):
        raise HTTPException(status_code=403, detail="Not your playlist")
    body = await _json_body(request)
    is_public = bool(body.get("is_public", not playlist.is_public))
    await ctx.playlists.set_public(playlist_id, is_public)
    return _ok({"id": playlist_id, "is_public": is_public})


# ----------------------------------------------------------------------
# Пользователи (только admin)
# ----------------------------------------------------------------------
@router.get("/users")
async def list_users(
    ctx: AppContext = Depends(get_ctx),
    session: SessionRecord = Depends(require_admin),
) -> JSONResponse:
    users = await ctx.users.list_users()
    return _ok({"items": [_user_json(user) for user in users]})


@router.post("/users")
async def create_user(
    request: Request,
    ctx: AppContext = Depends(get_ctx),
    session: SessionRecord = Depends(admin_csrf),
) -> JSONResponse:
    body = await _json_body(request)
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    role = str(body.get("role") or "user")
    if not username or not password:
        return _fail("Имя и пароль обязательны")
    if role not in ("admin", "user"):
        return _fail("role должен быть admin или user")
    try:
        user = await ctx.users.create_user(
            username,
            password,
            role=role,
            email=body.get("email"),
            display_name=body.get("display_name"),
        )
    except UserError as exc:
        return _fail(str(exc))
    return _ok(_user_json(user), status_code=201)


@router.post("/users/{user_id}/password")
async def reset_user_password(
    user_id: int,
    request: Request,
    ctx: AppContext = Depends(get_ctx),
    session: SessionRecord = Depends(admin_csrf),
) -> JSONResponse:
    body = await _json_body(request)
    password = str(body.get("password") or "")
    if not password:
        return _fail("Пароль обязателен")
    try:
        await ctx.users.reset_password(user_id, password)
    except UserError as exc:
        return _fail(str(exc), 404)
    # Смена пароля администратором — повод разлогинить пользователя везде.
    revoked = await ctx.sessions.revoke_all_for_user(user_id)
    return _ok({"password_changed": True, "sessions_revoked": revoked})


@router.post("/users/{user_id}/active")
async def set_user_active(
    user_id: int,
    request: Request,
    ctx: AppContext = Depends(get_ctx),
    session: SessionRecord = Depends(admin_csrf),
) -> JSONResponse:
    body = await _json_body(request)
    is_active = bool(body.get("is_active", True))
    try:
        user = await ctx.users.set_active(user_id, is_active)
    except UserError as exc:
        return _fail(str(exc))
    if not is_active:
        await ctx.sessions.revoke_all_for_user(user_id)
    return _ok(_user_json(user))


@router.post("/users/{user_id}/role")
async def set_user_role(
    user_id: int,
    request: Request,
    ctx: AppContext = Depends(get_ctx),
    session: SessionRecord = Depends(admin_csrf),
) -> JSONResponse:
    body = await _json_body(request)
    role = str(body.get("role") or "")
    if role not in ("admin", "user"):
        return _fail("role должен быть admin или user")
    try:
        user = await ctx.users.set_role(user_id, role)
    except UserError as exc:
        return _fail(str(exc))
    return _ok(_user_json(user))


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: int,
    ctx: AppContext = Depends(get_ctx),
    session: SessionRecord = Depends(admin_csrf),
) -> JSONResponse:
    if user_id == session.user.id:
        return _fail("Нельзя удалить собственную учётную запись")
    try:
        await ctx.users.delete_user(user_id)
    except UserError as exc:
        return _fail(str(exc))
    await ctx.sessions.revoke_all_for_user(user_id)
    return _ok({"deleted": user_id})


# ----------------------------------------------------------------------
# Сервер (только admin)
# ----------------------------------------------------------------------
@router.get("/server")
async def server_info(
    ctx: AppContext = Depends(get_ctx),
    session: SessionRecord = Depends(require_admin),
) -> JSONResponse:
    settings = ctx.settings
    library = await ctx.tracks.library_stats()
    covers = await ctx.covers.stats()
    return _ok(
        {
            "version": _version(),
            "music_dir": str(settings.music_dir),
            "music_dir_exists": settings.music_dir.exists(),
            "music_db": str(settings.music_db),
            "users_db": str(settings.users_db),
            "data_dir": str(settings.data_dir),
            "secret_from_file": settings.secret_key is None,
            "secret_file": str(settings.secret_key_file),
            "schema": {
                "users": await ctx.users_db.get_user_version(),
                "music": await ctx.music_db.get_user_version(),
            },
            "watch_enabled": settings.watch_enabled,
            "watch_polling": settings.watch_polling,
            "scan_interval_sec": settings.scan_interval_sec,
            "max_transcodes": settings.max_transcodes,
            "scan_concurrency": settings.scan_concurrency,
            "log_level": settings.log_level,
            "cookie_secure": settings.cookie_secure,
            "subsonic_api_version": settings.subsonic_api_version,
            "tracks": library.get("tracks", 0),
            "missing": library.get("missing", 0),
            "covers": covers,
        }
    )


@router.post("/covers/purge")
async def purge_covers(
    ctx: AppContext = Depends(get_ctx),
    session: SessionRecord = Depends(admin_csrf),
) -> JSONResponse:
    """Чистит кеш обложек: записи в БД и файлы на диске.

    Логика живёт в репозитории, чтобы CLI (`covers --purge`) делал ровно то
    же самое, а не свою копию.
    """
    removed = await ctx.covers.purge_all()
    return _ok({"files_removed": removed})


@router.post("/account/password")
async def change_own_password(
    request: Request,
    ctx: AppContext = Depends(get_ctx),
    session: SessionRecord = Depends(csrf_protected),
) -> JSONResponse:
    body = await _json_body(request)
    current = str(body.get("current") or "")
    new = str(body.get("next") or "")
    if not current or not new:
        return _fail("Текущий и новый пароль обязательны")
    try:
        await ctx.users.change_password(session.user.id, current, new)
    except UserError as exc:
        return _fail(str(exc))
    # Выходим на всех устройствах, кроме текущего.
    await ctx.sessions.revoke_others(session.user.id, session.id)
    return _ok({"password_changed": True})


@router.get("/account/sessions")
async def my_sessions(
    ctx: AppContext = Depends(get_ctx),
    session: SessionRecord = Depends(current_session),
) -> JSONResponse:
    items = await ctx.sessions.list_for_user(session.user.id)
    return _ok({"items": items, "current_id": session.id})


@router.delete("/account/sessions/{session_id}")
async def revoke_my_session(
    session_id: int,
    ctx: AppContext = Depends(get_ctx),
    session: SessionRecord = Depends(csrf_protected),
) -> JSONResponse:
    """Отзывает сессию — только свою."""
    mine = await ctx.sessions.list_for_user(session.user.id)
    if not any(item["id"] == session_id for item in mine):
        raise HTTPException(status_code=404, detail="Session not found")
    await ctx.sessions.revoke_by_id(session_id)
    return _ok({"revoked": session_id})


# ----------------------------------------------------------------------
# Плейлисты: состав, добавление/удаление треков, переупорядочивание
# ----------------------------------------------------------------------
def _track_json(track) -> dict:
    return {
        "id": track.id,
        "title": track.title,
        "artist": track.artist,
        "album": track.album,
        "duration": track.duration_sec,
        "track_number": track.track_number,
    }


def _check_playlist_access(playlist, session: SessionRecord, *, edit: bool) -> None:
    if playlist is None:
        raise HTTPException(status_code=404, detail="Playlist not found")
    if edit:
        allowed = PlaylistRepository.can_edit(
            playlist, session.user.id, is_admin=session.is_admin
        )
    else:
        allowed = PlaylistRepository.can_view(playlist, session.user.id)
    if not allowed:
        # Для чужого приватного плейлиста отдаём 404, а не 403:
        # не подтверждаем его существование.
        if not edit:
            raise HTTPException(status_code=404, detail="Playlist not found")
        raise HTTPException(status_code=403, detail="Not your playlist")


@router.get("/playlists/{playlist_id}")
async def playlist_detail(
    playlist_id: int,
    ctx: AppContext = Depends(get_ctx),
    session: SessionRecord = Depends(current_session),
) -> JSONResponse:
    playlist = await ctx.playlists.get(playlist_id)
    _check_playlist_access(playlist, session, edit=False)
    tracks = await ctx.playlists.tracks(playlist)
    return _ok(
        {
            "id": playlist.id,
            "name": playlist.name,
            "is_public": playlist.is_public,
            "is_smart": playlist.is_smart,
            "can_edit": PlaylistRepository.can_edit(
                playlist, session.user.id, is_admin=session.is_admin
            ),
            "tracks": [_track_json(track) for track in tracks],
        }
    )


@router.post("/playlists/{playlist_id}/tracks")
async def add_playlist_tracks(
    playlist_id: int,
    request: Request,
    ctx: AppContext = Depends(get_ctx),
    session: SessionRecord = Depends(csrf_protected),
) -> JSONResponse:
    playlist = await ctx.playlists.get(playlist_id)
    _check_playlist_access(playlist, session, edit=True)
    if playlist.is_smart:
        return _fail("Смарт-плейлист формируется правилами, треки добавлять нельзя")
    body = await _json_body(request)
    raw_ids = body.get("track_ids") or []
    if not isinstance(raw_ids, list):
        return _fail("track_ids должен быть списком")
    try:
        track_ids = [int(value) for value in raw_ids]
    except (TypeError, ValueError):
        return _fail("track_ids содержит нечисловые значения")
    added = await ctx.playlists.update(playlist_id, append_track_ids=track_ids)
    return _ok({"id": playlist_id, "song_count": added.song_count})


@router.delete("/playlists/{playlist_id}/tracks/{track_id}")
async def remove_playlist_track(
    playlist_id: int,
    track_id: int,
    ctx: AppContext = Depends(get_ctx),
    session: SessionRecord = Depends(csrf_protected),
) -> JSONResponse:
    playlist = await ctx.playlists.get(playlist_id)
    _check_playlist_access(playlist, session, edit=True)
    if playlist.is_smart:
        return _fail("Из смарт-плейлиста треки удаляются правилами")
    removed = await ctx.playlists.remove_track(playlist_id, track_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Track not in playlist")
    return _ok({"removed": track_id})


@router.post("/playlists/{playlist_id}/rename")
async def rename_playlist(
    playlist_id: int,
    request: Request,
    ctx: AppContext = Depends(get_ctx),
    session: SessionRecord = Depends(csrf_protected),
) -> JSONResponse:
    playlist = await ctx.playlists.get(playlist_id)
    _check_playlist_access(playlist, session, edit=True)
    body = await _json_body(request)
    name = str(body.get("name") or "").strip()
    if not name:
        return _fail("Название обязательно")
    try:
        updated = await ctx.playlists.update(playlist_id, name=name)
    except PlaylistError as exc:
        return _fail(str(exc))
    return _ok({"id": updated.id, "name": updated.name})


# ----------------------------------------------------------------------
# Избранное и оценки текущего пользователя
# ----------------------------------------------------------------------
@router.get("/starred")
async def starred_tracks(
    ctx: AppContext = Depends(get_ctx),
    session: SessionRecord = Depends(current_session),
) -> JSONResponse:
    rows = await ctx.music_db.fetch_all(
        "SELECT t.* FROM track_stars s JOIN tracks t ON t.id = s.track_id "
        "WHERE s.user_id = ? AND t.missing = 0 ORDER BY s.starred_at DESC",
        (session.user.id,),
    )
    from app.repositories.tracks import _row_to_track

    return _ok({"items": [_track_json(_row_to_track(row)) for row in rows]})


@router.post("/tracks/{track_id}/star")
async def set_star(
    track_id: int,
    request: Request,
    ctx: AppContext = Depends(get_ctx),
    session: SessionRecord = Depends(csrf_protected),
) -> JSONResponse:
    track = await ctx.tracks.get_by_id(track_id)
    if track is None or track.missing:
        raise HTTPException(status_code=404, detail="Track not found")
    body = await _json_body(request)
    starred = bool(body.get("starred", True))
    if starred:
        await ctx.music_db.execute(
            "INSERT OR IGNORE INTO track_stars (user_id, track_id, starred_at) "
            "VALUES (?, ?, ?)",
            (session.user.id, track_id, utcnow_iso()),
        )
    else:
        await ctx.music_db.execute(
            "DELETE FROM track_stars WHERE user_id = ? AND track_id = ?",
            (session.user.id, track_id),
        )
    return _ok({"id": track_id, "starred": starred})


@router.get("/starred-ids")
async def starred_ids(
    ctx: AppContext = Depends(get_ctx),
    session: SessionRecord = Depends(current_session),
) -> JSONResponse:
    """Компактный список id избранного — для подсветки звёздочек в UI."""
    rows = await ctx.music_db.fetch_all(
        "SELECT track_id FROM track_stars WHERE user_id = ?", (session.user.id,)
    )
    return _ok({"ids": [int(row["track_id"]) for row in rows]})


# ----------------------------------------------------------------------
# Исполнители (для вида «Исполнители»)
# ----------------------------------------------------------------------
@router.get("/artists")
async def list_artists(
    ctx: AppContext = Depends(get_ctx),
    session: SessionRecord = Depends(current_session),
    limit: int = Query(60, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> JSONResponse:
    """Исполнители страницей. Пустые (без альбомов) отфильтрованы в SQL."""
    rows = await ctx.library.list_artists(limit=limit, offset=offset)
    # Обложки исполнителей в схеме нет, аватар берёт картинку первого
    # альбома. Раньше это был отдельный запрос на каждого исполнителя —
    # на 5000 записей выходило 5000 обращений; теперь один запрос на страницу.
    album_map = await ctx.library.artist_album_map([artist.id for artist in rows])
    return _ok(
        {
            "items": [
                {
                    "id": artist.id,
                    "name": artist.name,
                    "album_count": artist.album_count,
                    "album_id": album_map.get(artist.id),
                }
                for artist in rows
            ],
            "total": await ctx.library.count_artists(),
            "limit": limit,
            "offset": offset,
        }
    )


@router.get("/artists/{artist_id}")
async def artist_detail(
    artist_id: int,
    ctx: AppContext = Depends(get_ctx),
    session: SessionRecord = Depends(current_session),
) -> JSONResponse:
    artist = await ctx.library.get_artist(artist_id)
    if artist is None:
        raise HTTPException(status_code=404, detail="Artist not found")
    albums = await ctx.library.artist_albums(artist_id)
    return _ok(
        {
            "id": artist.id,
            "name": artist.name,
            "albums": [
                {
                    "id": album.id, "name": album.name,
                    "year": album.year, "song_count": album.song_count,
                }
                for album in albums
            ],
        }
    )


# ----------------------------------------------------------------------
# Настройки сервера (только admin)
# ----------------------------------------------------------------------
@router.get("/settings")
async def get_settings(
    ctx: AppContext = Depends(get_ctx),
    session: SessionRecord = Depends(require_admin),
) -> JSONResponse:
    """Все изменяемые настройки с описаниями — для страницы настроек."""
    return _ok(
        {
            "items": await ctx.settings_repo.describe(),
            # Параметры, которые нельзя менять из интерфейса. Показываем их
            # только для чтения, чтобы всё из .env было видно в одном месте.
            "readonly": _readonly_settings(ctx),
        }
    )


def _readonly_settings(ctx: AppContext) -> list[dict[str, Any]]:
    """Только для чтения: что задаётся при создании контейнера.

    Эти значения нельзя менять из интерфейса, потому что они определяют, куда
    смонтированы тома, — их задаёт `docker compose` в `docker-compose.yml`.
    Показываем их здесь, чтобы всё было видно в одном месте.
    """
    settings = ctx.settings
    return [
        {"key": "MUSIC_DIR", "label": "Каталог музыки",
         "value": str(settings.music_dir),
         "help": "Куда смонтирована библиотека. Задаётся в docker-compose.yml."},
        {"key": "DATA_DIR", "label": "Каталог состояния",
         "value": str(settings.data_dir),
         "help": "Базы, ключ шифрования и кэш обложек. Задаётся в docker-compose.yml."},
        {"key": "MTR_SECRET_KEY", "label": "Ключ шифрования",
         "value": ("из файла " + str(settings.secret_key_file))
                  if settings.secret_key is None else "задан переменной окружения",
         "help": "Создаётся автоматически при первом старте и лежит в томе /data. "
                 "Смена ключа делает сохранённые пароли нечитаемыми."},
        {"key": "users", "label": "Учётные записи",
         "value": "ведутся на странице «Пользователи»",
         "help": "Пароли хранятся хешем в базе, в файлах конфигурации их нет."},
    ]


@router.post("/settings")
async def update_settings(
    request: Request,
    ctx: AppContext = Depends(get_ctx),
    session: SessionRecord = Depends(admin_csrf),
) -> JSONResponse:
    """Применяет изменения пачкой: {"values": {"key": value, ...}}.

    Неизвестный ключ — 400, а не молчаливый пропуск: опечатка в имени
    настройки должна быть видна сразу.
    """
    body = await _json_body(request)
    values = body.get("values")
    if not isinstance(values, dict) or not values:
        return _fail("Ожидался объект values с настройками")

    applied: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for key, raw in values.items():
        try:
            applied[key] = await ctx.settings_repo.set(
                str(key), raw, user_id=session.user.id
            )
        except ValueError as exc:
            errors[str(key)] = str(exc)

    if errors:
        return JSONResponse(
            {"ok": False, "detail": "Часть настроек не применена",
             "applied": applied, "errors": errors},
            status_code=400,
        )

    # Интервал скана и лимит транскодов читаются на ходу, поэтому
    # сообщаем наблюдателю о новом значении без перезапуска.
    await _refresh_runtime_settings(ctx)
    return _ok({"applied": applied})


@router.post("/settings/reset")
async def reset_setting(
    request: Request,
    ctx: AppContext = Depends(get_ctx),
    session: SessionRecord = Depends(admin_csrf),
) -> JSONResponse:
    """Сбрасывает настройку к значению по умолчанию."""
    body = await _json_body(request)
    key = str(body.get("key") or "")
    if not key:
        return _fail("Не указан ключ настройки")
    try:
        await ctx.settings_repo.reset(key)
    except ValueError as exc:
        return _fail(str(exc))
    await _refresh_runtime_settings(ctx)
    return _ok({"reset": key})


async def _refresh_runtime_settings(ctx: AppContext) -> None:
    """Подхватывает настройки, которые можно менять без перезапуска.

    Ключи с restart=True (уровень логов, cookie_secure, режим опроса) здесь
    намеренно не применяются: они читаются только при старте.
    """
    # Переносим переопределения в ctx.settings: формат отдачи и лимит
    # транскодов читаются из него в момент запроса.
    await apply_runtime_settings(ctx)

    overrides = await ctx.settings_repo.all_overrides()
    interval = int(overrides.get("scan_interval_sec", ctx.settings.scan_interval_sec) or 0)
    watch = bool(overrides.get("watch_enabled", ctx.settings.watch_enabled))
    if ctx.watcher is not None:
        ctx.watcher.set_interval(interval)
        # Наблюдатель создаётся всегда, но фоновые задачи до этого могли не
        # стартовать (при первом запуске оба параметра были выключены).
        if watch or interval > 0:
            await ctx.watcher.start()
        else:
            await ctx.watcher.stop()


# ----------------------------------------------------------------------
# Пользовательские предпочтения (доступны всем авторизованным)
# ----------------------------------------------------------------------
@router.get("/prefs")
async def get_prefs(
    ctx: AppContext = Depends(get_ctx),
    session: SessionRecord = Depends(current_session),
) -> JSONResponse:
    """Только то, что нужно интерфейсу при старте.

    Отдельно от /settings: страница настроек — админская, а вид библиотеки
    по умолчанию должен знать любой пользователь.
    """
    effective = await ctx.settings_repo.effective()
    return _ok(
        {
            "library_view": effective.get("library_view", "albums"),
            "default_transcode": effective.get("default_transcode", "raw"),
        }
    )

# ----------------------------------------------------------------------
# Импорт плейлистов из папки
# ----------------------------------------------------------------------
async def _build_plans(
    ctx: AppContext, *, dir_override: str | None = None
) -> tuple[list, list[str], dict]:
    """Общий разбор для превью и применения.

    Возвращает (планы, предупреждения, параметры). Ничего не пишет в БД.
    """
    from app.playlists import importer

    settings = await ctx.settings_repo.effective()
    raw_dir = (dir_override if dir_override is not None
               else str(settings.get("playlists_dir") or "")).strip()
    prefix_from = str(settings.get("playlist_prefix_from") or "").strip()
    prefix_to = str(settings.get("playlist_prefix_to") or "").strip()

    music_root = ctx.settings.music_dir
    # Пустая настройка = искать по всей библиотеке: плейлисты часто лежат
    # рядом с музыкой, а не в отдельном каталоге.
    search_root = Path(raw_dir) if raw_dir else music_root

    files, warnings = importer.find_playlist_files(search_root)

    plans = []
    for file_path in files:
        plan = importer.PlaylistPlan(
            path=str(file_path), name=importer.playlist_name(file_path)
        )
        entries, error = importer.read_playlist(file_path, music_root)
        if error:
            plan.error = error
            plans.append(plan)
            continue
        plan.total = len(entries)
        matched, unmatched = await importer.match_entries(
            entries,
            music_root=music_root,
            find_by_path=ctx.tracks.find_by_path,
            find_by_filename=ctx.tracks.find_by_filename,
            find_by_title=ctx.tracks.find_by_title,
            prefix_from=prefix_from,
            prefix_to=prefix_to,
        )
        plan.resolutions = matched
        plan.unmatched = unmatched
        plans.append(plan)

    plans, name_warnings = _dedupe_plans(plans)
    warnings.extend(name_warnings)

    params = {"dir": str(search_root), "prefix_from": prefix_from,
              "prefix_to": prefix_to}
    return plans, warnings, params


def _dedupe_plans(plans: list) -> tuple[list, list[str]]:
    """Убирает планы с одинаковым именем плейлиста.

    Имя плейлиста — это имя файла без расширения, и оно не уникально:
    `Rock/mix.m3u8` и `Pop/mix.m3u8` дают два плейлиста «mix». Раньше оба
    попадали в импорт и создавали два плейлиста с одинаковым названием —
    именно это выглядело как «импортируется дважды».

    Оставляем первый по порядку обхода, остальные перечисляем в
    предупреждении: пользователь должен знать, что часть файлов пропущена.
    """
    seen: dict[str, str] = {}
    kept: list = []
    warnings: list[str] = []

    for plan in plans:
        key = plan.name.strip().lower()
        if not key:
            kept.append(plan)
            continue
        if key in seen:
            warnings.append(
                f"имя «{plan.name}» встречается дважды: взят {seen[key]}, "
                f"пропущен {plan.path}"
            )
            continue
        seen[key] = plan.path
        kept.append(plan)

    return kept, warnings


@router.get("/playlists/import/check")
async def check_playlists_dir(
    ctx: AppContext = Depends(get_ctx),
    session: SessionRecord = Depends(current_session),
    dir: str | None = Query(None),
) -> JSONResponse:
    """Проверяет папку до импорта: существует ли и сколько в ней плейлистов.

    Нужен для мгновенной реакции на ввод пути, без полного разбора файлов.
    """
    from app.playlists import importer

    target = (dir or "").strip()
    if not target:
        settings = await ctx.settings_repo.effective()
        target = str(settings.get("playlists_dir") or "").strip()
    if not target:
        return _ok({"path": "", "exists": False, "files": 0,
                    "message": "Папка не указана — будет вся библиотека"})

    path = Path(target)
    if not path.exists():
        return _ok({"path": target, "exists": False, "files": 0,
                    "message": "Папка не найдена"})
    if not path.is_dir():
        return _ok({"path": target, "exists": False, "files": 0,
                    "message": "Это файл, а не папка"})
    try:
        files, warnings = importer.find_playlist_files(path)
    except importer.ImportError_ as exc:
        return _ok({"path": target, "exists": False, "files": 0,
                    "message": str(exc)})
    return _ok({"path": target, "exists": True, "files": len(files),
                "warnings": warnings,
                "message": f"Найдено плейлистов: {len(files)}"})


@router.get("/playlists/import/preview")
async def preview_playlist_import(
    request: Request,
    ctx: AppContext = Depends(get_ctx),
    session: SessionRecord = Depends(current_session),
    dir: str | None = Query(None),
) -> JSONResponse:
    """Разбор папки без записи: что найдено и что не сопоставилось."""
    from app.playlists.importer import ImportError_

    try:
        plans, warnings, params = await _build_plans(ctx, dir_override=dir)
    except ImportError_ as exc:
        return _fail(str(exc))
    except OSError as exc:
        return _fail(f"ошибка чтения папки: {exc}")

    # Сопоставляем с существующими плейлистами по имени.
    visible = await ctx.playlists.list_visible(session.user.id)
    by_name = {p.name.strip().lower(): p for p in visible}
    for plan in plans:
        found = by_name.get(plan.name.strip().lower())
        if found is not None:
            plan.existing_id = found.id
            plan.existing_count = found.song_count

    return _ok({
        "params": params,
        "warnings": warnings,
        "playlists": [plan.as_dict() for plan in plans],
        "total_files": len(plans),
        "total_matched": sum(p.matched_count for p in plans),
        "total_unmatched": sum(len(p.unmatched) for p in plans),
    })

@router.post("/playlists/import/apply")
async def apply_playlist_import(
    request: Request,
    ctx: AppContext = Depends(get_ctx),
    session: SessionRecord = Depends(csrf_protected),
) -> JSONResponse:
    """Создаёт или обновляет плейлисты по результату разбора.

    Тело:
      {
        "dir": "/music",                 # необязательно
        "items": [
          {"path": "/music/a.m3u8",
           "action": "create" | "replace" | "merge" | "skip",
           "is_public": false,
           "resolved": {"<raw_path>": <track_id>, ...}}   # ручные привязки
        ]
      }

    `resolved` закрывает дыры, которые не удалось сопоставить автоматически:
    пользователь выбрал кандидата в таблице, и при повторном импорте
    этот же путь снова попадёт в плейлист.
    """
    from app.playlists import importer

    body = await _json_body(request)
    items = body.get("items")
    if not isinstance(items, list) or not items:
        return _fail("Ожидался список items")

    try:
        plans, _warnings, _params = await _build_plans(
            ctx, dir_override=body.get("dir")
        )
    except importer.ImportError_ as exc:
        return _fail(str(exc))

    plan_by_path = {plan.path: plan for plan in plans}
    results: list[dict] = []
    visible = await ctx.playlists.list_visible(session.user.id)
    by_name = {p.name.strip().lower(): p for p in visible}

    for item in items:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "")
        action = str(item.get("action") or "create")
        plan = plan_by_path.get(path)
        if plan is None:
            results.append({"path": path, "status": "error",
                            "error": "файл не найден при повторном разборе"})
            continue
        if not plan.ok:
            # Файл не разобрался (пустой, HLS, битая кодировка). Создавать
            # плейлист с нулём треков нельзя: в списке появился бы пустой
            # «фантом» с тем же именем, что и рабочий файл рядом.
            results.append({"path": path, "name": plan.name, "status": "error",
                            "error": plan.error or "файл не разобран"})
            continue

        # Ручные привязки: {raw_path: track_id}. Заменяют None в resolutions
        # по индексу записи. Никакого повторного сопоставления не нужно:
        # порядок уже выровнен с файлом.
        resolved = item.get("resolved") or {}
        if resolved and plan.ok:
            entries, _err = importer.read_playlist(
                Path(path), ctx.settings.music_dir
            )
            fixed: list[int | None] = []
            for index, entry in enumerate(entries):
                chosen = resolved.get(entry.path)
                if chosen is not None:
                    try:
                        fixed.append(int(chosen))
                        continue
                    except (TypeError, ValueError):
                        pass
                fixed.append(
                    plan.resolutions[index]
                    if index < len(plan.resolutions) else None
                )
            plan.resolutions = fixed

        # None (несопоставленные) отбрасываем, дубли схлопываем, порядок храним.
        track_ids = list(dict.fromkeys(plan.matched))
        name = plan.name
        if not name:
            results.append({"path": path, "name": name, "status": "error",
                            "error": "пустое имя плейлиста"})
            continue

        existing = by_name.get(name.strip().lower())
        try:
            if action == "skip":
                results.append({"path": path, "name": name, "status": "skipped"})
                continue
            if action == "replace" and existing is not None:
                await ctx.playlists.delete(existing.id)
                created = await ctx.playlists.create(
                    session.user.id, name, is_public=bool(item.get("is_public", False)),
                    track_ids=track_ids,
                )
                results.append({"path": path, "name": name, "status": "replaced",
                                "playlist_id": created.id,
                                "tracks": len(track_ids)})
                by_name[name.strip().lower()] = created
            elif action == "merge" and existing is not None:
                before = existing.song_count
                await ctx.playlists.update(existing.id, append_track_ids=track_ids)
                after = (await ctx.playlists.get(existing.id)).song_count
                results.append({"path": path, "name": name, "status": "merged",
                                "playlist_id": existing.id,
                                "added": max(0, after - before), "tracks": after})
            else:
                # Защита от дубля: если плейлист с таким именем уже есть
                # (в базе или создан в этом же запросе), не создаём второй.
                # Пользователь выбрал «Создать новый», но два одинаковых
                # названия в списке — это не то, чего он ждёт.
                if existing is not None:
                    results.append({
                        "path": path, "name": name, "status": "error",
                        "error": f"плейлист «{name}» уже существует — "
                                 f"выберите «Дополнить» или «Заменить»",
                    })
                    continue
                created = await ctx.playlists.create(
                    session.user.id, name, is_public=bool(item.get("is_public", False)),
                    track_ids=track_ids,
                )
                results.append({"path": path, "name": name, "status": "created",
                                "playlist_id": created.id,
                                "tracks": len(track_ids)})
                # Обязательно: следующий файл с таким же именем должен
                # увидеть этот плейлист, а не создать второй. Раньше
                # by_name строился один раз до цикла.
                by_name[name.strip().lower()] = created
        except Exception as exc:  # noqa: BLE001 - показываем причину в UI
            logger.exception("импорт плейлиста %s не удался", path)
            error = str(exc)
            results.append({"path": path, "name": name, "status": "error",
                            "error": error})

    created = sum(1 for r in results if r["status"] in ("created", "replaced"))
    merged = sum(1 for r in results if r["status"] == "merged")
    failed = sum(1 for r in results if r["status"] == "error")
    return _ok({"results": results, "created": created, "merged": merged,
                "failed": failed, "skipped": sum(1 for r in results if r["status"] == "skipped")})
