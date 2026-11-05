"""Эндпоинты Subsonic REST API (этап 3: авторизация, библиотека, стриминг).

Каждый метод доступен по двум путям — `/rest/<method>.view` и `/rest/<method>`:
клиенты вызывают оба варианта в зависимости от версии протокола, и
отсутствие одного из них ломает часть плееров.

Ошибки: JSON-методы отдают HTTP 200 с конвертом status=failed (требование
протокола), бинарные (stream/download) — честные 401/404.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import (
    JSONResponse,
    PlainTextResponse,
)

from app.api.stream import build_stream_response
from app.audio.ffmpeg import TranscodeBusy, TranscodeError
from app.audio.profiles import ProfileError
from app.audio.service import SourceMissing, TrackNotFound, parse_offset
from app.bootstrap import AppContext
from app.repositories.library import LibraryRepository
from app.subsonic import format as fmt
from app.subsonic import playlists_api as pl
from app.subsonic.auth import AuthContext
from app.subsonic.deps import (
    require_admin,
    require_auth,
    require_auth_or_session,
)
from app.subsonic.errors import ErrorCode, SubsonicError, missing
from app.subsonic.response import ok_response

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/rest", tags=["subsonic"])

# Сколько треков отдавать в search3/songs по умолчанию. Клиенты редко просят
# больше, а неограниченная выдача на большой библиотеке — это мегабайты JSON.
DEFAULT_SEARCH_SONGS = 50
MAX_SEARCH_COUNT = 500

# Папки в старом ID3-режиме (getIndexes → getMusicDirectory).
#
# id исполнителей здесь ТЕ ЖЕ, что в getArtists и getArtist: числа. Клиент
# ожидает один и тот же id исполнителя во всех методах.
#
# Конфликт: исполнитель 1 и альбом 1 — разные сущности с одним числом. Поэтому
# альбомы внутри папки исполнителя (и треки внутри альбома) получают префикс
# «al-<id>». Числовой id в getMusicDirectory означает исполнителя, если такой
# есть, иначе альбом — как и раньше для клиентов, знающих id альбома из getAlbum.
ALBUM_FOLDER_PREFIX = "al-"


# ----------------------------------------------------------------------
# Вспомогательное
# ----------------------------------------------------------------------
def _ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def _library(request: Request) -> LibraryRepository:
    return request.app.state.library_repo


def _api(auth: AuthContext) -> str:
    return auth.api_version


def _int_param(
    value: str | None, *, default: int, name: str, minimum: int = 0,
    maximum: int | None = None,
) -> int:
    """Разбор целочисленного query-параметра с понятной ошибкой.

    Потолок (``maximum``) по умолчанию НЕ применяется. Раньше здесь стояло
    10 000, и оно молча обрезало идентификаторы: любой трек с id > 10000
    превращался в 10000, из-за чего все файлы «сверх лимита» слипались в один
    и тот же трек. Ограничивать имеет смысл только размеры выборок (size,
    offset, *_count), и там ``maximum`` передаётся явно.
    """
    if value is None or not str(value).strip():
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise SubsonicError(
            ErrorCode.GENERIC, f"Invalid value for parameter {name}: {value!r}"
        ) from exc
    if parsed < minimum:
        return minimum
    if maximum is not None and parsed > maximum:
        return maximum
    return parsed


def _register(method: str, *paths: str) -> Callable:
    """Регистрирует метод по нескольким путям с одинаковым хендлером.

    FastAPI не позволяет повесить один декоратор на два пути, поэтому
    добавляем маршруты программно. Порядок регистрации важен: более
    специфичный путь (`/ping.view`) не конфликтует с `/ping`, так как
    это разные литералы, а не параметры.
    """
    def decorator(func: Callable) -> Callable:
        for path in paths:
            router.add_api_route(path, func, methods=[method], name=func.__name__)
        return func
    return decorator


# ----------------------------------------------------------------------
# Системные методы
# ----------------------------------------------------------------------
@_register("GET", "/ping.view", "/ping")
async def ping(request: Request, auth: AuthContext = Depends(require_auth)) -> JSONResponse:
    """Проверка соединения и учётных данных. Самый первый вызов любого клиента."""
    return ok_response({"version": _api(auth)}, api_version=_api(auth))


@_register("GET", "/getLicense.view", "/getLicense")
async def get_license(
    request: Request, auth: AuthContext = Depends(require_auth)
) -> JSONResponse:
    """Лицензия: у нас свободный сервер, valid=true без ограничений."""
    return ok_response(
        {"license": {"valid": True, "email": None, "licenseExpires": None}},
        api_version=_api(auth),
    )


@_register("GET", "/getMusicFolders.view", "/getMusicFolders")
async def get_music_folders(
    request: Request, auth: AuthContext = Depends(require_auth)
) -> JSONResponse:
    """Одна плоская папка: библиотека не разделена на коллекции."""
    return ok_response(
        {"musicFolders": {"musicFolder": [fmt.music_folder_json()]}},
        api_version=_api(auth),
    )


@_register("GET", "/getScanStatus.view", "/getScanStatus")
async def get_scan_status(
    request: Request, auth: AuthContext = Depends(require_auth)
) -> JSONResponse:
    """Состояние индексации. Клиенты опрашивают его после startScan."""
    ctx = _ctx(request)
    row = await ctx.music_db.fetch_one(
        "SELECT * FROM scan_runs ORDER BY id DESC LIMIT 1"
    )
    count = await ctx.tracks.count_tracks()
    payload: dict[str, Any] = {"scanStatus": {"scanning": False, "count": count}}
    if row is not None:
        payload["scanStatus"]["scanning"] = row["status"] == "running"
        payload["scanStatus"]["lastScan"] = row["started_at"]
    return ok_response(payload, api_version=_api(auth))


@_register("GET", "/startScan.view", "/startScan")
async def start_scan(
    request: Request,
    auth: AuthContext = Depends(require_admin),
    fullScan: str | None = Query(None),
) -> JSONResponse:
    """Принудительная индексация. Только admin (код 50 иначе).

    Скан выполняется синхронно в этом запросе: библиотека на десятки тысяч
    файлов проиндексируется минутами, и клиент по таймауту отвалится.
    Асинхронный запуск с прогрессом через WebSocket — этап 5. Здесь честно
    сообщаем, что индексация запущена, и отдаём статус.
    """
    ctx = _ctx(request)
    mode = "full" if (fullScan or "").lower() in ("true", "1", "yes") else "incremental"
    # Скан идёт в worker-процессе: ставим команду и отвечаем сразу.
    from app.worker.ipc import enqueue

    await enqueue(ctx.music_db, "scan", {"mode": mode})
    count = await ctx.tracks.count_tracks()
    return ok_response(
        {
            "scanStatus": {
                "scanning": False,
                "count": count,
                "lastScan": None,
            }
        },
        api_version=_api(auth),
    )


# ----------------------------------------------------------------------
# Пользователи
# ----------------------------------------------------------------------
@_register("GET", "/getUser.view", "/getUser")
async def get_user(
    request: Request,
    auth: AuthContext = Depends(require_auth),
    username: str | None = Query(None),
) -> JSONResponse:
    """Свой профиль. Чужой — только администратору."""
    ctx = _ctx(request)
    target = (username or "").strip() or auth.user.username

    if target.lower() != auth.user.username.lower():
        if auth.user.role != "admin":
            raise SubsonicError(
                ErrorCode.NOT_AUTHORIZED,
                "You are not authorized to view other users",
            )
        record = await ctx.users.get_by_username(target)
        if record is None:
            raise SubsonicError(ErrorCode.NOT_FOUND, f"User {target!r} not found")
        return ok_response(
            {"user": fmt.user_json(record)}, api_version=_api(auth)
        )

    return ok_response({"user": fmt.user_json(auth.user)}, api_version=_api(auth))


@_register("GET", "/getUsers.view", "/getUsers")
async def get_users(
    request: Request, auth: AuthContext = Depends(require_admin)
) -> JSONResponse:
    """Список всех учётных записей. Только admin."""
    ctx = _ctx(request)
    users = await ctx.users.list_users()
    payload = []
    for user in users:
        # UserPublic не содержит password_*, поэтому отдаём как есть.
        payload.append(
            {
                "username": user.username,
                "email": user.email,
                "scrobblingEnabled": False,
                "adminRole": user.role == "admin",
                "settingsRole": user.role == "admin",
                "downloadRole": True,
                "uploadRole": False,
                "playlistRole": True,
                "coverArtRole": False,
                "commentRole": False,
                "podcastRole": False,
                "shareRole": False,
                "videoConversionRole": False,
                "folder": [1],
            }
        )
    return ok_response({"users": {"user": payload}}, api_version=_api(auth))


# ----------------------------------------------------------------------
# Исполнители и альбомы
# ----------------------------------------------------------------------
@_register("GET", "/getArtists.view", "/getArtists")
async def get_artists(
    request: Request,
    auth: AuthContext = Depends(require_auth),
    musicFolderId: str | None = Query(None),
) -> JSONResponse:
    """Исполнители, сгруппированные по первой букве (ID3-режим)."""
    artists = await _library(request).list_artists()
    return ok_response(
        {"artists": {"ignoredArticles": "", "index": fmt.indexes_json(artists)}},
        api_version=_api(auth),
    )


@_register("GET", "/getIndexes.view", "/getIndexes")
async def get_indexes(
    request: Request,
    auth: AuthContext = Depends(require_auth),
    musicFolderId: str | None = Query(None),
    ifModifiedSince: str | None = Query(None),
) -> JSONResponse:
    """Устаревший обход библиотеки. Оставлен для старых клиентов."""
    artists = await _library(request).list_artists()
    index = fmt.legacy_index_json(artists)
    return ok_response(
        {
            "indexes": {
                "lastModified": _ctx(request).settings.subsonic_api_version,
                "ignoredArticles": "",
                "index": index,
            }
        },
        api_version=_api(auth),
    )


@_register("GET", "/getMusicDirectory.view", "/getMusicDirectory")
async def get_music_directory(
    request: Request,
    auth: AuthContext = Depends(require_auth),
    id: str | None = Query(None),
) -> JSONResponse:
    """Обход по «папкам»: исполнитель → альбомы → треки.

    Папка исполнителя: числовой id (тот же, что в getArtists).
    Папка альбома: «al-<id>» (внутри папки исполнителя) или числовой id.
    """
    if not id:
        raise missing("id")
    lib = _library(request)

    if id.startswith(ALBUM_FOLDER_PREFIX):
        album_id = _int_param(id[len(ALBUM_FOLDER_PREFIX):], default=0, name="id", minimum=1)
        return await _album_folder(request, auth, lib, album_id)

    entity_id = _int_param(id, default=0, name="id", minimum=1)
    if await lib.get_artist(entity_id) is not None:
        return await _artist_folder(request, auth, lib, entity_id)
    return await _album_folder(request, auth, lib, entity_id)


async def _album_folder(
    request: Request, auth: AuthContext, lib: LibraryRepository, album_id: int
) -> JSONResponse:
    """Папка альбома: его треки. parent у треков — «al-<id>», чтобы навигация не
    путала альбом с исполнителем того же номера."""
    album = await lib.get_album(album_id)
    if album is None:
        raise SubsonicError(ErrorCode.NOT_FOUND, f"Folder {album_id} not found")
    children = []
    for track in await lib.album_tracks(album_id):
        child = fmt.child_json(track)
        child["parent"] = f"{ALBUM_FOLDER_PREFIX}{album_id}"
        children.append(child)
    return ok_response(
        {
            "directory": {
                "id": f"{ALBUM_FOLDER_PREFIX}{album_id}",
                "name": album.name,
                "child": children,
            }
        },
        api_version=_api(auth),
    )


async def _artist_folder(
    request: Request, auth: AuthContext, lib: LibraryRepository, artist_id: int
) -> JSONResponse:
    """Папка исполнителя: его альбомы как вложенные папки («al-<id>»)."""
    artist = await lib.get_artist(artist_id)
    if artist is None:
        raise SubsonicError(ErrorCode.NOT_FOUND, f"Artist folder {artist_id} not found")
    children = [
        {
            "id": f"{ALBUM_FOLDER_PREFIX}{item.id}",
            "parent": str(artist_id),
            "isDir": True,
            "title": item.name,
            "album": item.name,
            "artist": artist.name,
            "year": item.year,
        }
        for item in await lib.artist_albums(artist_id)
    ]
    return ok_response(
        {
            "directory": {
                "id": str(artist_id),
                "name": artist.name,
                "child": children,
            }
        },
        api_version=_api(auth),
    )


@_register("GET", "/getArtistInfo2.view", "/getArtistInfo2")
@_register("GET", "/getArtistInfo.view", "/getArtistInfo")
async def get_artist_info(
    request: Request,
    auth: AuthContext = Depends(require_auth),
    id: str | None = Query(None),
) -> JSONResponse:
    """Информация об исполнителе: биография, похожие, картинки.

    Внешних источников у нас нет, поэтому отдаём пустую, но валидную структуру.
    Главное — не 404: клиенты вызывают этот метод при открытии исполнителя и на
    ошибке показывают пустую страницу вместо списка альбомов.
    """
    if not id:
        raise missing("id")
    return ok_response(
        {"artistInfo2": {"biography": "", "similarArtist": [], "imageUrl": []}},
        api_version=_api(auth),
    )


@_register("GET", "/getArtist.view", "/getArtist")
async def get_artist(
    request: Request,
    auth: AuthContext = Depends(require_auth),
    id: str | None = Query(None),
) -> JSONResponse:
    """Исполнитель и все его альбомы."""
    if not id:
        raise missing("id")
    artist_id = _int_param(id, default=0, name="id", minimum=1)
    lib = _library(request)
    artist = await lib.get_artist(artist_id)
    if artist is None:
        raise SubsonicError(ErrorCode.NOT_FOUND, f"Artist {id} not found")
    albums = await lib.artist_albums(artist_id)
    return ok_response(
        {"artist": fmt.artist_with_albums_json(artist, albums)},
        api_version=_api(auth),
    )


@_register("GET", "/getAlbum.view", "/getAlbum")
async def get_album(
    request: Request,
    auth: AuthContext = Depends(require_auth),
    id: str | None = Query(None),
) -> JSONResponse:
    """Альбом вместе со списком треков."""
    if not id:
        raise missing("id")
    album_id = _int_param(id, default=0, name="id", minimum=1)
    lib = _library(request)
    album = await lib.get_album(album_id)
    if album is None:
        raise SubsonicError(ErrorCode.NOT_FOUND, f"Album {id} not found")
    tracks = await lib.album_tracks(album_id)
    return ok_response(
        {"album": fmt.album_with_songs_json(album, tracks)},
        api_version=_api(auth),
    )


@_register("GET", "/getAlbumList2.view", "/getAlbumList2")
@_register("GET", "/getAlbumList.view", "/getAlbumList")
async def get_album_list(
    request: Request,
    auth: AuthContext = Depends(require_auth),
    type: str = Query("alphabeticalByArtist"),
    size: str | None = Query(None),
    offset: str | None = Query(None),
    genre: str | None = Query(None),
    fromYear: str | None = Query(None),
    toYear: str | None = Query(None),
) -> JSONResponse:
    """Списки альбомов: новые, частые, случайные, по году/жанру."""
    lib = _library(request)
    limit = _int_param(size, default=10, name="size", minimum=1, maximum=500)
    skip = _int_param(offset, default=0, name="offset", maximum=100_000)
    year_from = _int_param(fromYear, default=0, name="fromYear") or None
    year_to = _int_param(toYear, default=0, name="toYear") or None

    albums = await lib.list_albums(
        sort=type,
        limit=limit,
        offset=skip,
        genre=genre or None,
        from_year=year_from,
        to_year=year_to,
    )
    items = [fmt.album_json(album) for album in albums]
    # getAlbumList (без «2») по спецификации отдаёт другой ключ — albumList.
    key = "albumList2" if request.url.path.endswith("2.view") or request.url.path.endswith("2") else "albumList"
    return ok_response({key: {"album": items}}, api_version=_api(auth))


@_register("GET", "/getSong.view", "/getSong")
async def get_song(
    request: Request,
    auth: AuthContext = Depends(require_auth),
    id: str | None = Query(None),
) -> JSONResponse:
    """Один трек по id."""
    if not id:
        raise missing("id")
    track_id = _int_param(id, default=0, name="id", minimum=1)
    track = await _ctx(request).tracks.get_by_id(track_id)
    if track is None or track.missing:
        raise SubsonicError(ErrorCode.NOT_FOUND, f"Song {id} not found")
    return ok_response({"song": fmt.child_json(track)}, api_version=_api(auth))


@_register("GET", "/getGenres.view", "/getGenres")
async def get_genres(
    request: Request, auth: AuthContext = Depends(require_auth)
) -> JSONResponse:
    """Жанры с числом треков и альбомов."""
    genres = await _library(request).list_genres()
    payload = [
        {"value": name, "songCount": songs, "albumCount": albums}
        for name, songs, albums in genres
    ]
    return ok_response({"genres": {"genre": payload}}, api_version=_api(auth))


# ----------------------------------------------------------------------
# Поиск
# ----------------------------------------------------------------------
@_register("GET", "/search3.view", "/search3", "/search2.view", "/search2")
async def search3(
    request: Request,
    auth: AuthContext = Depends(require_auth),
    query: str | None = Query(None),
    artistCount: str | None = Query(None),
    artistOffset: str | None = Query(None),
    albumCount: str | None = Query(None),
    albumOffset: str | None = Query(None),
    songCount: str | None = Query(None),
    songOffset: str | None = Query(None),
) -> JSONResponse:
    """Полнотекстовый поиск по исполнителям, альбомам и трекам.

    Пустой query — не ошибка: клиенты шлют его при очистке поля поиска
    и ожидают пустой успешный ответ, а не код 10.
    """
    ctx = _ctx(request)
    lib = _library(request)
    text = (query or "").strip()

    if not text:
        empty = {"artist": [], "album": [], "song": []}
        return ok_response({"searchResult3": empty}, api_version=_api(auth))

    limit_artists = _int_param(artistCount, default=20, name="artistCount", maximum=MAX_SEARCH_COUNT)
    limit_albums = _int_param(albumCount, default=20, name="albumCount", maximum=MAX_SEARCH_COUNT)
    limit_songs = _int_param(songCount, default=DEFAULT_SEARCH_SONGS, name="songCount", maximum=MAX_SEARCH_COUNT)

    artists = await lib.search_artists(text, limit=limit_artists) if limit_artists else []
    albums = await lib.search_albums(text, limit=limit_albums) if limit_albums else []
    tracks = await ctx.tracks.search(text, limit=limit_songs) if limit_songs else []

    result = {
        "artist": [fmt.artist_json(artist) for artist in artists],
        "album": [fmt.album_json(album) for album in albums],
        "song": [fmt.child_json(track) for track in tracks],
    }
    return ok_response({"searchResult3": result}, api_version=_api(auth))


@_register("GET", "/search.view", "/search")
async def search_legacy(
    request: Request,
    auth: AuthContext = Depends(require_auth),
    query: str | None = Query(None),
    artistCount: str | None = Query(None),
    albumCount: str | None = Query(None),
    songCount: str | None = Query(None),
) -> JSONResponse:
    """Устаревший поиск: вместо ArtistID3/AlbumID3 — «папки».

    Старые клиенты ждут поля `name`/`id` без artistId, поэтому переупаковываем
    результат основного поиска, а не дублируем SQL.
    """
    ctx = _ctx(request)
    lib = _library(request)
    text = (query or "").strip()
    if not text:
        return ok_response(
            {"searchResult": {"match": []}}, api_version=_api(auth)
        )

    limit_artists = _int_param(
        artistCount, default=20, name="artistCount", maximum=MAX_SEARCH_COUNT
    )
    limit_albums = _int_param(
        albumCount, default=20, name="albumCount", maximum=MAX_SEARCH_COUNT
    )
    limit_songs = _int_param(
        songCount, default=DEFAULT_SEARCH_SONGS, name="songCount",
        maximum=MAX_SEARCH_COUNT,
    )

    artists = await lib.search_artists(text, limit=limit_artists) if limit_artists else []
    albums = await lib.search_albums(text, limit=limit_albums) if limit_albums else []
    tracks = await ctx.tracks.search(text, limit=limit_songs) if limit_songs else []

    matches: list[dict[str, Any]] = []
    for artist in artists:
        matches.append({"id": str(artist.id), "title": artist.name, "isDir": True})
    for album in albums:
        matches.append({"id": str(album.id), "title": album.name, "isDir": True})
    for track in tracks:
        matches.append(fmt.child_json(track))

    return ok_response({"searchResult": {"match": matches}}, api_version=_api(auth))


# ----------------------------------------------------------------------
# Бинарные методы: stream и download
# ----------------------------------------------------------------------
@_register("GET", "/stream.view", "/stream")
async def subsonic_stream(
    request: Request,
    auth: AuthContext | None = Depends(require_auth_or_session),
    id: str | None = Query(None),
    format: str | None = Query(None),
    timeOffset: str | None = Query(None),
    maxBitRate: str | None = Query(None),
    size: str | None = Query(None),
) -> Any:
    """Потоковое воспроизведение.

    Параметры Subsonic:
      * maxBitRate — потолок в кбит/с. Если он ниже целевого профиля,
        выбираем профиль не выше потолка (иначе клиент на мобильной сети
        захлебнётся);
      * format=raw — исходный файл; прочие значения — наши профили;
      * timeOffset — старт с указанной секунды.
    """
    if auth is None:
        raise HTTPException(
            status_code=401,
            detail="Not authenticated",
            headers={"WWW-Authenticate": 'Subsonic realm="mtr"'},
        )
    ctx = _ctx(request)
    if not id:
        raise SubsonicError(ErrorCode.MISSING_PARAMETER, "Required parameter is missing: id")

    track_id = _int_param(id, default=0, name="id", minimum=1)
    requested = _pick_format(format or ctx.settings.default_transcode, maxBitRate)
    offset = parse_offset(timeOffset)

    try:
        return await build_stream_response(ctx, track_id, requested, offset)
    except ProfileError as exc:
        raise SubsonicError(ErrorCode.GENERIC, str(exc)) from exc
    except TrackNotFound as exc:
        raise SubsonicError(ErrorCode.NOT_FOUND, str(exc)) from exc
    except SourceMissing as exc:
        raise SubsonicError(ErrorCode.NOT_FOUND, str(exc)) from exc
    except TranscodeBusy as exc:
        raise SubsonicError(ErrorCode.GENERIC, f"Server busy: {exc}") from exc
    except TranscodeError as exc:
        logger.error("сбой транскодирования трека %s: %s", id, exc)
        raise SubsonicError(ErrorCode.GENERIC, "Transcoding failed") from exc


@_register("GET", "/download.view", "/download")
async def subsonic_download(
    request: Request,
    auth: AuthContext | None = Depends(require_auth_or_session),
    id: str | None = Query(None),
) -> Any:
    """Скачивание оригинала. Всегда без транскодирования и без смещения."""
    if auth is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    ctx = _ctx(request)
    if not id:
        raise SubsonicError(ErrorCode.MISSING_PARAMETER, "Required parameter is missing: id")
    track_id = _int_param(id, default=0, name="id", minimum=1)
    try:
        return await build_stream_response(ctx, track_id, "raw", 0.0, as_download=True)
    except (TrackNotFound, SourceMissing) as exc:
        raise SubsonicError(ErrorCode.NOT_FOUND, str(exc)) from exc


def _pick_format(requested: str, max_bit_rate: str | None) -> str:
    """Учитывает maxBitRate клиента при выборе профиля.

    Клиент просит raw (или формат не задан), но ставит потолок 192 кбит/с —
    значит, исходный FLAC ему отдавать нельзя, надо перекодировать в MP3,
    не превышающий потолок.
    """
    ceiling_kbps = None
    if max_bit_rate:
        try:
            ceiling_kbps = int(max_bit_rate)
        except (TypeError, ValueError):
            ceiling_kbps = None

    normalized = (requested or "raw").strip().lower()
    if normalized not in ("raw", "") and normalized in (
        "mp3_128", "mp3_256", "mp3_320", "mp3_v0", "flac", "alac"
    ):
        return normalized

    if ceiling_kbps is None or ceiling_kbps <= 0:
        return "raw"

    # Выбираем наибольший профиль, не превышающий потолок.
    if ceiling_kbps >= 320:
        return "mp3_320"
    if ceiling_kbps >= 256:
        return "mp3_256"
    return "mp3_128"


# ----------------------------------------------------------------------
# Плейлисты, избранное, оценки, scrobble (этап 4)
# ----------------------------------------------------------------------
_register("GET", "/getPlaylists.view", "/getPlaylists")(pl.get_playlists)
_register("GET", "/getPlaylist.view", "/getPlaylist")(pl.get_playlist)
_register("GET", "/createPlaylist.view", "/createPlaylist")(pl.create_playlist)
_register("GET", "/updatePlaylist.view", "/updatePlaylist")(pl.update_playlist)
_register("GET", "/deletePlaylist.view", "/deletePlaylist")(pl.delete_playlist)
_register("GET", "/star.view", "/star")(pl.star)
_register("GET", "/unstar.view", "/unstar")(pl.unstar)
_register("GET", "/getStarred2.view", "/getStarred2")(pl.get_starred2)
_register("GET", "/getStarred.view", "/getStarred")(pl.get_starred2)
_register("GET", "/setRating.view", "/setRating")(pl.set_rating)
_register("GET", "/scrobble.view", "/scrobble")(pl.scrobble)


@_register("GET", "/exportPlaylist.view", "/exportPlaylist")
async def export_playlist_m3u(
    request: Request,
    auth: AuthContext = Depends(require_auth),
    id: str | None = Query(None),
) -> PlainTextResponse:
    """Экспорт плейлиста в .m3u8 (расширение OpenSubsonic)."""

    if not id:
        raise missing("id")
    ctx = _ctx(request)
    content, filename = await pl.export_playlist_m3u8(
        ctx, auth, _int_param(id, default=0, name="id", minimum=1)
    )
    return PlainTextResponse(
        content=content,
        media_type="audio/x-mpegurl",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )



# ----------------------------------------------------------------------
# Методы, которые клиенты вызывают, но данных для них у нас нет
#
# Раньше на них приходил 404 в формате FastAPI: {"detail": "Not Found"}.
# Разбор такого ответа в клиенте падает или показывает пустоту, хотя остальные
# разделы работают — со стороны это выглядит как «сломан один раздел».
#
# Отдаём валидный пустой ответ Subsonic: клиент видит «метод есть, данных нет».
# Перечисляем явно, а не ловим всё подряд: перехват /rest/* перекрыл бы
# getCoverArt, который регистрируется отдельным роутером ниже.
# ----------------------------------------------------------------------
async def _empty_ok(request: Request, auth: AuthContext = Depends(require_auth)) -> JSONResponse:
    return ok_response({}, api_version=_api(auth))


@_register("GET", "/getTopSongs.view", "/getTopSongs")
@_register("GET", "/getSimilarSongs.view", "/getSimilarSongs")
@_register("GET", "/getSimilarSongs2.view", "/getSimilarSongs2")
@_register("GET", "/getLyrics.view", "/getLyrics")
@_register("GET", "/getLyricsBySongId.view", "/getLyricsBySongId")
@_register("GET", "/getRandomSongs.view", "/getRandomSongs")
@_register("GET", "/getAlbumInfo.view", "/getAlbumInfo")
@_register("GET", "/getAlbumInfo2.view", "/getAlbumInfo2")
@_register("GET", "/getChatMessages.view", "/getChatMessages")
@_register("GET", "/getBookmarks.view", "/getBookmarks")
@_register("GET", "/getPlayQueue.view", "/getPlayQueue")
@_register("GET", "/getShares.view", "/getShares")
@_register("GET", "/getPodcasts.view", "/getPodcasts")
@_register("GET", "/getInternetRadioStations.view", "/getInternetRadioStations")
async def empty_method(request: Request, auth: AuthContext = Depends(require_auth)) -> JSONResponse:
    """Пустой, но валидный ответ для методов без данных в этой реализации."""
    return await _empty_ok(request, auth)
