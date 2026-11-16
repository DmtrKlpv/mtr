"""Subsonic-методы плейлистов и личных отметок (избранное/оценки).

Взято из спецификации 1.16.1: `getPlaylists`, `getPlaylist`, `createPlaylist`,
`updatePlaylist`, `deletePlaylist`, `star`, `unstar`, `getStarred2`,
`setRating`, `scrobble`.

Особенности реализации:

* `updatePlaylist` по протоколу передаёт состав **позиционными**
  параметрами (`songIdToAdd`, `songIndexToRemove`), которые могут
  повторяться. Поэтому они принимаются как `list[str]`.
* `createPlaylist` с существующим `playlistId` — это обновление, так
  исторически сложилось в протоколе.
* Звёздочки и оценки хранятся в `mtr_music.db`, `user_id` — мягкая ссылка
  на пользователя без FK между базами.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Depends, Query, Request
from fastapi.responses import JSONResponse

from app.bootstrap import AppContext
from app.playlists.m3u import export_m3u8, safe_export_filename
from app.repositories.playlists import PlaylistError, PlaylistRow, PlaylistRepository
from app.subsonic import format as fmt
from app.subsonic.auth import AuthContext
from app.subsonic.deps import require_auth
from app.subsonic.errors import ErrorCode, SubsonicError, missing
from app.subsonic.response import ok_response
from app.utils.timeutil import utcnow_iso

logger = logging.getLogger(__name__)


def _ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def _int(value: str | None, *, default: int = 0, name: str = "id") -> int:
    if value is None or not str(value).strip():
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise SubsonicError(
            ErrorCode.GENERIC, f"Invalid value for parameter {name}: {value!r}"
        ) from exc


def _playlist_json(playlist: PlaylistRow, *, include_owner: bool = True) -> dict[str, Any]:
    """Плейлист в формате Subsonic Playlist."""
    payload: dict[str, Any] = {
        "id": str(playlist.id),
        "name": playlist.name,
        "comment": playlist.comment,
        "owner": None,  # заполняется выше по стеку, где известен username
        "public": playlist.is_public,
        "songCount": playlist.song_count,
        "duration": playlist.duration_sec,
        "created": playlist.created_at,
        "changed": playlist.updated_at,
        "coverArt": f"pl-{playlist.id}",
    }
    if not include_owner:
        payload.pop("owner", None)
    return {key: value for key, value in payload.items() if value is not None}


async def _owner_name(ctx: AppContext, owner_id: int) -> str:
    """Имя владельца. Живёт в другой БД — отдельный запрос, без JOIN."""
    try:
        record = await ctx.users.get_by_id(owner_id)
    except Exception:  # noqa: BLE001 - имя не критично для ответа
        return "unknown"
    return record.username if record else "unknown"


async def _visible_playlist(
    ctx: AppContext, auth: AuthContext, playlist_id: int
) -> PlaylistRow:
    """Загружает плейлист и проверяет право на чтение (код 70/50)."""
    playlist = await ctx.playlists.get(playlist_id)
    if playlist is None:
        raise SubsonicError(ErrorCode.NOT_FOUND, f"Playlist {playlist_id} not found")
    if not PlaylistRepository.can_view(playlist, auth.user.id):
        # Не подтверждаем существование чужого приватного плейлиста.
        raise SubsonicError(ErrorCode.NOT_FOUND, f"Playlist {playlist_id} not found")
    return playlist


async def _editable_playlist(
    ctx: AppContext, auth: AuthContext, playlist_id: int
) -> PlaylistRow:
    playlist = await ctx.playlists.get(playlist_id)
    if playlist is None:
        raise SubsonicError(ErrorCode.NOT_FOUND, f"Playlist {playlist_id} not found")
    if not PlaylistRepository.can_edit(
        playlist, auth.user.id, is_admin=auth.is_admin
    ):
        raise SubsonicError(
            ErrorCode.NOT_AUTHORIZED, "You are not authorized to edit this playlist"
        )
    return playlist


# Плейлисты
async def get_playlists(
    request: Request,
    auth: AuthContext = Depends(require_auth),
    username: str | None = Query(None),
) -> JSONResponse:
    """Список плейлистов: свои и публичные.

    `username` позволяет администратору посмотреть плейлисты другого
    пользователя — но только публичные, личные остаются личными.
    """
    ctx = _ctx(request)
    repo = ctx.playlists

    target_user = auth.user
    if username and username.lower() != auth.user.username.lower():
        if not auth.is_admin:
            raise SubsonicError(
                ErrorCode.NOT_AUTHORIZED, "You are not authorized to browse other users"
            )
        record = await ctx.users.get_by_username(username)
        if record is None:
            raise SubsonicError(ErrorCode.NOT_FOUND, f"User {username!r} not found")
        target_user = record

    playlists = await repo.list_visible(target_user.id)
    # Имена владельцев: один запрос на уникальные id вместо N запросов.
    names: dict[int, str] = {auth.user.id: auth.user.username}
    for playlist in playlists:
        if playlist.owner_id not in names:
            names[playlist.owner_id] = await _owner_name(ctx, playlist.owner_id)

    items = []
    for playlist in playlists:
        payload = _playlist_json(playlist)
        payload["owner"] = names.get(playlist.owner_id, "unknown")
        items.append(payload)

    return ok_response(
        {"playlists": {"playlist": items}}, api_version=auth.api_version
    )


async def get_playlist(
    request: Request,
    auth: AuthContext = Depends(require_auth),
    id: str | None = Query(None),
) -> JSONResponse:
    """Плейлист с треками. Для смарт-плейлиста состав вычисляется на лету."""
    if not id:
        raise missing("id")
    ctx = _ctx(request)
    playlist = await _visible_playlist(ctx, auth, _int(id, name="id"))
    tracks = await ctx.playlists.tracks(playlist)

    payload = _playlist_json(playlist)
    payload["owner"] = await _owner_name(ctx, playlist.owner_id)
    payload["songCount"] = len(tracks)
    payload["duration"] = sum(track.duration_sec for track in tracks)
    payload["entry"] = [fmt.child_json(track) for track in tracks]

    return ok_response({"playlist": payload}, api_version=auth.api_version)


async def create_playlist(
    request: Request,
    auth: AuthContext = Depends(require_auth),
    playlistId: str | None = Query(None),
    name: str | None = Query(None),
    songId: list[str] = Query(default_factory=list),
) -> JSONResponse:
    """Создаёт плейлист. С существующим `playlistId` — обновляет состав."""
    ctx = _ctx(request)
    track_ids = [_int(value, name="songId") for value in songId]

    try:
        if playlistId:
            playlist = await _editable_playlist(ctx, auth, _int(playlistId, name="playlistId"))
            updated = await ctx.playlists.update(
                playlist.id,
                name=name if name is not None else None,
                track_ids=track_ids if songId else None,
            )
            return ok_response(
                {
                    "playlist": {
                        **_playlist_json(updated),
                        "owner": auth.user.username,
                    }
                },
                api_version=auth.api_version,
            )

        if not name:
            raise missing("name")
        created = await ctx.playlists.create(
            auth.user.id, name, track_ids=track_ids
        )
    except PlaylistError as exc:
        raise SubsonicError(ErrorCode.GENERIC, str(exc)) from exc

    return ok_response(
        {
            "playlist": {
                **_playlist_json(created),
                "owner": auth.user.username,
            }
        },
        api_version=auth.api_version,
    )


async def update_playlist(
    request: Request,
    auth: AuthContext = Depends(require_auth),
    playlistId: str | None = Query(None),
    name: str | None = Query(None),
    comment: str | None = Query(None),
    public: str | None = Query(None),
    songIdToAdd: list[str] = Query(default_factory=list),
    songIndexToRemove: list[str] = Query(default_factory=list),
) -> JSONResponse:
    """Обновляет состав и свойства.

    По протоколу состав меняется инкрементально: `songIdToAdd` добавляет,
    `songIndexToRemove` удаляет по ПОЗИЦИИ (а не по id трека). Индексы
    применяются от большего к меньшему, иначе нумерация съезжает и
    удаляется не то.
    """
    if not playlistId:
        raise missing("playlistId")
    ctx = _ctx(request)
    playlist = await _editable_playlist(ctx, auth, _int(playlistId, name="playlistId"))

    is_public = None
    if public is not None:
        is_public = public.strip().lower() in ("true", "1", "yes")

    try:
        if name is not None or comment is not None or is_public is not None:
            playlist = await ctx.playlists.update(
                playlist.id, name=name, comment=comment, is_public=is_public
            )

        # Удаление по позициям: сначала большие, чтобы младшие не смещались.
        if songIndexToRemove:
            indexes: list[int] = []
            for value in songIndexToRemove:
                try:
                    index = int(value)
                except (TypeError, ValueError):
                    continue
                if index >= 0:
                    indexes.append(index)
            for index in sorted(set(indexes), reverse=True):
                await _remove_at_position(ctx, playlist.id, index)
            await ctx.playlists._renumber(playlist.id)  # noqa: SLF001

        if songIdToAdd:
            added = [_int(value, name="songIdToAdd") for value in songIdToAdd]
            await ctx.playlists.update(playlist.id, append_track_ids=added)
    except PlaylistError as exc:
        raise SubsonicError(ErrorCode.GENERIC, str(exc)) from exc

    return ok_response({}, api_version=auth.api_version)


async def _remove_at_position(ctx: AppContext, playlist_id: int, index: int) -> None:
    """Удаляет запись по позиции. Индекс 0 — первый трек плейлиста."""
    row = await ctx.music_db.fetch_one(
        "SELECT id FROM playlist_tracks WHERE playlist_id = ? "
        "ORDER BY position, id LIMIT 1 OFFSET ?",
        (playlist_id, index),
    )
    if row is None:
        return
    await ctx.music_db.execute(
        "DELETE FROM playlist_tracks WHERE id = ?", (int(row["id"]),)
    )


async def delete_playlist(
    request: Request,
    auth: AuthContext = Depends(require_auth),
    id: str | None = Query(None),
) -> JSONResponse:
    """Удаляет плейлист. Права — как на редактирование."""
    if not id:
        raise missing("id")
    ctx = _ctx(request)
    playlist = await _editable_playlist(ctx, auth, _int(id, name="id"))
    await ctx.playlists.delete(playlist.id)
    return ok_response({}, api_version=auth.api_version)


# Избранное и оценки
async def star(
    request: Request,
    auth: AuthContext = Depends(require_auth),
    id: str | None = Query(None),
    albumId: str | None = Query(None),
    artistId: str | None = Query(None),
) -> JSONResponse:
    """Ставит отметку «избранное» на трек (альбомы/артисты — этап 4.1)."""
    ctx = _ctx(request)
    if not id:
        if albumId or artistId:
            # Честный отказ лучше молчаливого «ок», которое ничего не сделало.
            raise SubsonicError(
                ErrorCode.GENERIC,
                "Starring albums and artists is not supported yet, use id",
            )
        raise missing("id")

    track_id = _int(id, name="id")
    track = await ctx.tracks.get_by_id(track_id)
    if track is None or track.missing:
        raise SubsonicError(ErrorCode.NOT_FOUND, f"Song {id} not found")

    await ctx.music_db.execute(
        "INSERT OR IGNORE INTO track_stars (user_id, track_id, starred_at) "
        "VALUES (?, ?, ?)",
        (auth.user.id, track_id, utcnow_iso()),
    )
    return ok_response({}, api_version=auth.api_version)


async def unstar(
    request: Request,
    auth: AuthContext = Depends(require_auth),
    id: str | None = Query(None),
    albumId: str | None = Query(None),
    artistId: str | None = Query(None),
) -> JSONResponse:
    ctx = _ctx(request)
    if not id:
        raise missing("id")
    await ctx.music_db.execute(
        "DELETE FROM track_stars WHERE user_id = ? AND track_id = ?",
        (auth.user.id, _int(id, name="id")),
    )
    return ok_response({}, api_version=auth.api_version)


async def get_starred2(
    request: Request,
    auth: AuthContext = Depends(require_auth),
    musicFolderId: str | None = Query(None),
) -> JSONResponse:
    """Избранные треки текущего пользователя."""
    ctx = _ctx(request)
    rows = await ctx.music_db.fetch_all(
        "SELECT t.* FROM track_stars s JOIN tracks t ON t.id = s.track_id "
        "WHERE s.user_id = ? AND t.missing = 0 "
        "ORDER BY s.starred_at DESC",
        (auth.user.id,),
    )
    from app.repositories.tracks import _row_to_track

    songs = [fmt.child_json(_row_to_track(row)) for row in rows]
    # Альбомы и исполнители в избранном пока не поддерживаются — пустые списки,
    # а не отсутствие ключей: клиенты ожидают все три.
    return ok_response(
        {"starred2": {"artist": [], "album": [], "song": songs}},
        api_version=auth.api_version,
    )


async def set_rating(
    request: Request,
    auth: AuthContext = Depends(require_auth),
    id: str | None = Query(None),
    rating: str | None = Query(None),
) -> JSONResponse:
    """Оценка 1..5. 0 — снять оценку (так требует протокол)."""
    if not id:
        raise missing("id")
    ctx = _ctx(request)
    track_id = _int(id, name="id")
    value = _int(rating, default=0, name="rating")
    if not 0 <= value <= 5:
        raise SubsonicError(
            ErrorCode.GENERIC, "rating must be between 0 and 5"
        )

    track = await ctx.tracks.get_by_id(track_id)
    if track is None or track.missing:
        raise SubsonicError(ErrorCode.NOT_FOUND, f"Song {id} not found")

    if value == 0:
        await ctx.music_db.execute(
            "DELETE FROM track_ratings WHERE user_id = ? AND track_id = ?",
            (auth.user.id, track_id),
        )
    else:
        await ctx.music_db.execute(
            "INSERT INTO track_ratings (user_id, track_id, rating, rated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT (user_id, track_id) DO UPDATE SET "
            "rating = excluded.rating, rated_at = excluded.rated_at",
            (auth.user.id, track_id, value, utcnow_iso()),
        )
    return ok_response({}, api_version=auth.api_version)


async def scrobble(
    request: Request,
    auth: AuthContext = Depends(require_auth),
    id: str | None = Query(None),
    submission: str | None = Query(None),
    time: str | None = Query(None),
) -> JSONResponse:
    """Отметка о прослушивании.

    `submission=false` — это «начал играть», его НЕ считаем: иначе
    статистика раздувается при каждом переключении трека. Учитываем только
    `submission=true` (прослушал).
    """
    if not id:
        raise missing("id")
    ctx = _ctx(request)
    track_id = _int(id, name="id")

    is_submission = (submission or "true").strip().lower() in ("true", "1", "yes")
    if not is_submission:
        return ok_response({}, api_version=auth.api_version)

    track = await ctx.tracks.get_by_id(track_id)
    if track is None or track.missing:
        raise SubsonicError(ErrorCode.NOT_FOUND, f"Song {id} not found")

    played_at = utcnow_iso()
    async with ctx.music_db.transaction():
        await ctx.music_db.execute(
            "INSERT INTO playback_log (track_id, user_id, played_at, client, played_ms) "
            "VALUES (?, ?, ?, ?, ?)",
            (track_id, auth.user.id, played_at, auth.client, track.duration_ms),
        )
        # Агрегат держим отдельно: смарт-фильтр play_count > N не должен
        # превращаться в COUNT(*) по всему журналу.
        await ctx.music_db.execute(
            "INSERT INTO play_stats (track_id, play_count, last_played_at, updated_at) "
            "VALUES (?, 1, ?, ?) "
            "ON CONFLICT (track_id) DO UPDATE SET "
            "play_count = play_count + 1, "
            "last_played_at = excluded.last_played_at, "
            "updated_at = excluded.updated_at",
            (track_id, played_at, played_at),
        )
    return ok_response({}, api_version=auth.api_version)


# Импорт/экспорт M3U8
async def export_playlist_m3u8(
    ctx: AppContext, auth: AuthContext, playlist_id: int
) -> tuple[str, str]:
    """Возвращает (содержимое, безопасное имя файла) для отдачи клиенту."""
    playlist = await _visible_playlist(ctx, auth, playlist_id)
    tracks = await ctx.playlists.tracks(playlist)
    return export_m3u8(playlist, tracks), safe_export_filename(playlist.name)
