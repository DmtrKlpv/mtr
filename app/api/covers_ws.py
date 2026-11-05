"""getCoverArt и WebSocket-прогресс сканирования.

`getCoverArt` — обязательный метод Subsonic: без него клиенты показывают
заглушки вместо обложек. Поддерживает два вида id:

  * `al-<album_id>` — обложка альбома;
  * `tr-<track_id>` — обложка трека (или альбома, если у трека своей нет);
  * голое число — трактуем как id трека (совместимость: часть клиентов
    подставляет `child.coverArt`, а мы отдаём туда `tr-…`).

`size` из протокола игнорируется: ресайз без Pillow невозможен, а тянуть
библиотеку ради превью обложек не хочется. Отдаём оригинал — браузер
масштабирует сам.

WebSocket `/ws/scan` отдаёт прогресс текущего и последнего скана. Нужен
для индикатора в UI: REST-опрос `getScanStatus` работает, но не даёт
промежуточных процентов.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import JSONResponse, Response

from app.bootstrap import AppContext
from app.subsonic.auth import AuthContext
from app.subsonic.deps import require_auth, require_auth_or_session
from app.subsonic.errors import SubsonicError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["covers"])

# Как часто WebSocket отправляет обновление, секунд.
WS_TICK_SEC = 1.0


def _parse_cover_id(raw: str) -> tuple[str, int] | None:
    """Разбирает id обложки. None — некорректный формат."""
    value = raw.strip()
    if not value:
        return None
    lowered = value.lower()
    for prefix, kind in (("al-", "album"), ("tr-", "track")):
        if lowered.startswith(prefix):
            tail = value[len(prefix):]
            if not tail.isdigit():
                return None
            return kind, int(tail)
    # Голое число: исторически это id трека.
    if value.isdigit():
        return "track", int(value)
    return None


async def _cover_for(ctx: AppContext, kind: str, entity_id: int):
    if kind == "album":
        return await ctx.covers.for_album(entity_id)
    return await ctx.covers.for_track(entity_id)


@router.get("/rest/getCoverArt.view", response_model=None)
@router.get("/rest/getCoverArt", response_model=None)
async def get_cover_art(
    request: Request,
    auth: AuthContext | None = Depends(require_auth_or_session),
    id: str | None = Query(None),
    size: str | None = Query(None),
) -> Response:
    """Отдаёт байты обложки. 404 — если картинки нигде нет."""
    if auth is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not id:
        raise HTTPException(status_code=400, detail="Required parameter is missing: id")

    parsed = _parse_cover_id(id)
    if parsed is None:
        # Бинарный метод: честный HTTP-статус важнее конверта subsonic-response.
        # Плеер решает по коду, пробовать ли снова, а не разбирает тело.
        raise HTTPException(
            status_code=400, detail=f"Invalid cover art id: {id!r}"
        )
    kind, entity_id = parsed

    ctx: AppContext = request.app.state.ctx
    cover = await _cover_for(ctx, kind, entity_id)
    if cover is None:
        raise HTTPException(status_code=404, detail=f"Cover art {id} not found")

    return Response(
        content=cover.data,
        media_type=cover.mime,
        headers={
            # Обложка неизменна для данного id: пусть кэширует браузер.
            "Cache-Control": "public, max-age=86400",
            "X-MTR-Cover-Source": cover.source,
        },
    )


@router.get("/api/v1/covers/stats", response_model=None)
async def cover_stats(
    request: Request, auth: AuthContext = Depends(require_auth)
) -> JSONResponse:
    """Сколько обложек в кэше — для админ-панели."""
    ctx: AppContext = request.app.state.ctx
    stats = await ctx.covers.stats()
    return JSONResponse(stats)


# ----------------------------------------------------------------------
# WebSocket: прогресс сканирования
# ----------------------------------------------------------------------
@router.websocket("/ws/scan")
async def scan_progress(websocket: WebSocket) -> None:
    """Транслирует состояние сканера.

    Авторизация: WebSocket не умеет передавать query-параметры так же
    удобно, как HTTP, но клиент может прислать их в URL — проверяем те же
    u/t/s. Без авторизации соединение закрывается кодом 1008.
    """
    ctx: AppContext = websocket.app.state.ctx
    throttle = websocket.app.state.auth_throttle

    params = dict(websocket.query_params)
    try:
        from app.subsonic.auth import authenticate

        await authenticate(
            ctx, params, ip=websocket.client.host if websocket.client else None,
            throttle=throttle,
        )
    except SubsonicError as exc:
        await websocket.close(code=1008, reason=exc.message[:120])
        return
    except Exception:  # noqa: BLE001 - закрываем без деталей
        await websocket.close(code=1008, reason="unauthorized")
        return

    await websocket.accept()
    logger.debug("WebSocket прогресса подключён")

    try:
        last_payload: str | None = None
        while True:
            payload = await _progress_payload(ctx)
            serialized = json.dumps(payload, ensure_ascii=False)
            # Отправляем только изменения: иначе 1 сообщение в секунду
            # впустую, пока скан не идёт.
            if serialized != last_payload:
                await websocket.send_text(serialized)
                last_payload = serialized
            await asyncio.sleep(WS_TICK_SEC)
    except WebSocketDisconnect:
        logger.debug("WebSocket прогресса отключён")
    except Exception:  # noqa: BLE001 - обрыв не должен шуметь в логах
        logger.debug("WebSocket прогресса завершён", exc_info=True)
    finally:
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass


async def _progress_payload(ctx: AppContext) -> dict:
    """Собирает состояние: наблюдатель + последний скан из БД."""
    # Наблюдатель живёт в worker-процессе: его состояние берём из статуса worker-а.
    from app.worker.ipc import read_status, status_path

    status = read_status(status_path(ctx.settings.data_dir))
    watcher_view = status.get("watcher") or {}
    payload: dict = {
        "watcher": watcher_view.get("state", "disabled"),
        "pending": int(watcher_view.get("pending", 0) or 0),
        "tracks": await ctx.tracks.count_tracks(),
    }

    last = ctx.watcher.last_result if ctx.watcher else None
    if last is not None:
        payload["last_scan"] = {
            "run_id": last.run_id,
            "mode": last.mode,
            "status": last.status,
            "seen": last.counters.seen,
            "added": last.counters.added,
            "updated": last.counters.updated,
            "removed": last.counters.removed,
            "failed": last.counters.failed,
        }
        return payload

    row = await ctx.music_db.fetch_one(
        "SELECT * FROM scan_runs ORDER BY id DESC LIMIT 1"
    )
    if row is not None:
        payload["last_scan"] = {
            "run_id": int(row["id"]),
            "mode": str(row["mode"]),
            "status": str(row["status"]),
            "seen": int(row["files_seen"] or 0),
            "added": int(row["files_added"] or 0),
            "updated": int(row["files_updated"] or 0),
            "removed": int(row["files_removed"] or 0),
            "failed": int(row["files_failed"] or 0),
            "started_at": str(row["started_at"]),
            "finished_at": row["finished_at"],
        }
    return payload
