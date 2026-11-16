"""Тесты этапа 5 (вторая часть): обложки и WebSocket-прогресс."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from app.covers.service import (
    MAX_COVER_BYTES,
    _mime_of,
    cache_filename,
    extract_cover,
    extract_embedded,
    extract_from_directory,
)
from app.api.covers_ws import _parse_cover_id
from tests.conftest import make_flac, make_mp3, tag_mp3

PASSWORD = "changeme123"
SALT = "abc123def456"

# Минимальный валидный PNG 1x1 — сигнатура и IHDR-заголовок корректны.
TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)
TINY_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + b"\xff\xd9"


def auth_q(username: str = "admin") -> str:
    token = hashlib.md5((PASSWORD + SALT).encode()).hexdigest()
    return f"u={username}&t={token}&s={SALT}&v=1.16.1&c=test&f=json"


def body(response: httpx.Response) -> dict:
    return response.json()["subsonic-response"]


# Определение типа и имя кэша (без БД)
@pytest.mark.parametrize(
    "data,expected",
    [
        (TINY_PNG, "image/png"),
        (TINY_JPEG, "image/jpeg"),
        (b"GIF89a" + b"x" * 10, "image/gif"),
        (b"RIFF" + b"\x00" * 4 + b"WEBP" + b"x", "image/webp"),
        (b"junk", "application/octet-stream"),
    ],
)
def test_mime_detection_by_signature(data, expected):
    assert _mime_of(data) == expected


def test_cache_filename_is_stable_and_content_addressed():
    name1 = cache_filename("al-5", TINY_PNG)
    name2 = cache_filename("al-5", TINY_PNG)
    name3 = cache_filename("al-5", TINY_JPEG)
    assert name1 == name2, "одинаковые данные должны давать одно имя"
    assert name1 != name3, "разные данные — разные имена"
    assert name1.startswith("al-5-")


def test_cache_filename_sanitises_key():
    """Ключ не должен содержать символов пути."""
    name = cache_filename("../../etc/passwd", TINY_PNG)
    assert "/" not in name and ".." not in name


# Разбор id обложки
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("al-12", ("album", 12)),
        ("AL-12", ("album", 12)),
        ("tr-7", ("track", 7)),
        ("42", ("track", 42)),
        ("al-abc", None),
        ("xx-1", None),
        ("", None),
    ],
)
def test_parse_cover_id(raw, expected):
    assert _parse_cover_id(raw) == expected


# Извлечение обложек с диска
def test_extract_from_directory_by_priority(music_dir: Path):
    album = music_dir / "Artist" / "Album"
    track = make_mp3(album / "01.mp3")
    # cover.jpg важнее folder.png по приоритету.
    (album / "folder.png").write_bytes(TINY_PNG)
    (album / "cover.jpg").write_bytes(TINY_JPEG)

    cover = extract_from_directory(track)
    assert cover is not None
    assert cover.mime == "image/jpeg"
    assert cover.source == "file:cover.jpg"


def test_extract_from_parent_directory(music_dir: Path):
    """Обложка в корне исполнителя, трек — в подпапке альбома."""
    artist = music_dir / "Artist"
    track = make_mp3(artist / "Album" / "01.mp3")
    (artist / "cover.png").write_bytes(TINY_PNG)

    cover = extract_from_directory(track)
    assert cover is not None and cover.mime == "image/png"


def test_extract_embedded_from_flac(music_dir: Path):
    from mutagen.flac import Picture

    path = make_flac(music_dir / "a.flac", seconds=1.0)
    from mutagen.flac import FLAC

    audio = FLAC(str(path))
    picture = Picture()
    picture.type = 3
    picture.mime = "image/png"
    picture.data = TINY_PNG
    audio.add_picture(picture)
    audio.save()

    cover = extract_embedded(path)
    assert cover is not None
    assert cover.mime == "image/png"
    assert cover.source == "flac:picture"


def test_embedded_preferred_over_file(music_dir: Path):
    """Встроенная обложка важнее файла рядом."""
    from mutagen.flac import FLAC, Picture

    path = make_flac(music_dir / "b.flac", seconds=1.0)
    audio = FLAC(str(path))
    picture = Picture()
    picture.type = 3
    picture.data = TINY_PNG
    audio.add_picture(picture)
    audio.save()
    (music_dir / "cover.jpg").write_bytes(TINY_JPEG)

    cover = extract_cover(path)
    assert cover.source == "flac:picture"


def test_no_cover_returns_none(music_dir: Path):
    path = make_mp3(music_dir / "bare.mp3")
    assert extract_cover(path) is None


def test_oversized_cover_file_skipped(music_dir: Path):
    album = music_dir / "Big"
    track = make_mp3(album / "01.mp3")
    # Файл больше лимита — не читаем (защита от «обложки» на гигабайт).
    (album / "cover.jpg").write_bytes(b"x" * (MAX_COVER_BYTES + 1))
    assert extract_from_directory(track) is None


# HTTP
@pytest.fixture
async def lib(settings, monkeypatch, music_dir) -> AsyncIterator[tuple]:
    monkeypatch.setenv("MUSIC_DIR", str(settings.music_dir))
    monkeypatch.setenv("DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("MTR_SECRET_KEY", settings.secret_key or "test")

    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            await app.state.ctx.users.create_user("admin", PASSWORD, role="admin")

            album = music_dir / "Queen" / "Opera"
            tagged = make_mp3(album / "01.mp3")
            tag_mp3(tagged, title="Bohemian", artist="Queen", album="Opera")
            (album / "cover.jpg").write_bytes(TINY_JPEG)
            # Второй трек без обложки в своих тегах, но с файлом в каталоге.
            other = make_mp3(album / "02.mp3")
            tag_mp3(other, title="Love", artist="Queen", album="Opera")

            bare = make_mp3(music_dir / "NoCover" / "x.mp3")
            tag_mp3(bare, title="NoArt", artist="Nobody", album="Nothing")

            await app.state.ctx.scanner.scan(mode="full")
            yield client, app


async def test_get_cover_art_by_album(lib):
    client, app = lib
    albums = await app.state.ctx.library.list_albums(limit=50)
    opera = next(a for a in albums if a.name == "Opera")

    r = await client.get(f"/rest/getCoverArt.view?id=al-{opera.id}&{auth_q()}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert len(r.content) > 10


async def test_get_cover_art_by_track_falls_back_to_album(lib):
    """У трека своей картинки нет — отдаётся обложка альбома."""
    client, app = lib
    tracks = await app.state.ctx.tracks.list_tracks(limit=50)
    love = next(t for t in tracks if t.title == "Love")

    r = await client.get(f"/rest/getCoverArt.view?id=tr-{love.id}&{auth_q()}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"


async def test_cover_is_cached_after_first_request(lib):
    client, app = lib
    albums = await app.state.ctx.library.list_albums(limit=50)
    opera = next(a for a in albums if a.name == "Opera")

    before = await app.state.ctx.covers.stats()
    await client.get(f"/rest/getCoverArt.view?id=al-{opera.id}&{auth_q()}")
    after = await app.state.ctx.covers.stats()
    assert after["count"] > before["count"]


async def test_cover_cache_hit_serves_from_disk(lib):
    client, app = lib
    albums = await app.state.ctx.library.list_albums(limit=50)
    opera = next(a for a in albums if a.name == "Opera")
    await client.get(f"/rest/getCoverArt.view?id=al-{opera.id}&{auth_q()}")

    r = await client.get(f"/rest/getCoverArt.view?id=al-{opera.id}&{auth_q()}")
    assert r.headers["x-mtr-cover-source"] == "cache"


async def test_missing_cover_gives_404(lib):
    client, app = lib
    tracks = await app.state.ctx.tracks.list_tracks(limit=50)
    bare = next(t for t in tracks if t.title == "NoArt")
    r = await client.get(f"/rest/getCoverArt.view?id=tr-{bare.id}&{auth_q()}")
    assert r.status_code == 404


async def test_bad_cover_id_gives_400(lib):
    """Бинарный метод отдаёт HTTP-статус, а не конверт с кодом в теле."""
    client, _ = lib
    r = await client.get(f"/rest/getCoverArt.view?id=xx-abc&{auth_q()}")
    assert r.status_code == 400


async def test_cover_requires_auth(lib):
    client, app = lib
    albums = await app.state.ctx.library.list_albums(limit=50)
    opera = next(a for a in albums if a.name == "Opera")
    r = await client.get(f"/rest/getCoverArt.view?id=al-{opera.id}")
    assert r.status_code == 401


async def test_cover_stats_endpoint(lib):
    client, app = lib
    albums = await app.state.ctx.library.list_albums(limit=50)
    opera = next(a for a in albums if a.name == "Opera")
    await client.get(f"/rest/getCoverArt.view?id=al-{opera.id}&{auth_q()}")

    r = await client.get(f"/api/v1/covers/stats?{auth_q()}")
    assert r.status_code == 200
    assert r.json()["count"] >= 1


# WebSocket
async def test_ws_rejects_without_auth(lib):
    from starlette.testclient import TestClient

    _, app = lib
    with TestClient(app) as client:
        with pytest.raises(Exception):
            with client.websocket_connect("/ws/scan") as ws:
                ws.receive_text()


async def test_ws_sends_progress(lib):
    from starlette.testclient import TestClient

    _, app = lib
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/scan?{auth_q()}") as ws:
            payload = json.loads(ws.receive_text())
    assert "watcher" in payload
    assert "tracks" in payload
    assert payload["tracks"] >= 1
