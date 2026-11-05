"""Поиск на главной и хлебные крошки (0.23.1)."""

from __future__ import annotations

from pathlib import Path

from app.repositories.library import LibraryRepository, _like
from app.repositories.tracks import TrackRepository
from tests.test_web import login_admin, web  # noqa: F401


async def test_search_finds_tracks_albums_artists(web):
    client, app = web
    await login_admin(client)
    data = (await client.get("/api/web/search?q=Queen")).json()["data"]
    # В фикстуре есть альбомы Queen: поиск обязан вернуть хоть что-то из альбомов.
    assert data["albums"], data
    names = " ".join(a["name"] for a in data["albums"]).lower()
    assert "queen" in names or any("queen" in (a.get("artist") or "").lower() for a in data["albums"])


async def test_search_empty_query_returns_empty(web):
    client, _ = web
    await login_admin(client)
    data = (await client.get("/api/web/search?q=%20%20")).json()["data"]
    assert data == {"tracks": [], "albums": [], "artists": []}


async def test_search_survives_special_characters(web):
    client, _ = web
    await login_admin(client)
    for query in ("%", "_", "\\", "'", "\"", "%_%"):
        r = await client.get(f"/api/web/search?q={query}")
        assert r.status_code == 200, (query, r.text)
        assert "tracks" in r.json()["data"]


def test_like_escapes_backslash_first():
    """Порядок замен важен: '\\' экранируется раньше '%' и '_'."""
    assert _like("100%") == "%100\\%%"
    assert _like("a_b") == "%a\\_b%"
    assert _like("a\\b") == "%a\\\\b%"
    assert _like("   ") is None


async def test_search_albums_and_artists_ignore_empty_albums(ctx, music_dir: Path):
    """Альбом без живых треков не попадает в поиск, исполнитель тоже."""
    from tests.conftest import make_mp3, tag_mp3

    lib: LibraryRepository = ctx.library
    music = ctx.settings.music_dir
    p = make_mp3(music / "a" / "t.mp3", seconds=0.2)
    tag_mp3(p, title="T", artist="Searchable", album="Findable")
    await ctx.scanner.scan(mode="full")

    albums = await lib.search_albums("Findable", limit=10)
    assert [a.name for a in albums] == ["Findable"]
    artists = await lib.search_artists("Searchable", limit=10)
    assert [a.name for a in artists] == ["Searchable"]

    await ctx.music_db.execute("UPDATE tracks SET missing = 1")
    assert await lib.search_albums("Findable", limit=10) == []
    assert await lib.search_artists("Searchable", limit=10) == []


async def test_tracks_search_does_not_break_on_backslash(ctx):
    tr: TrackRepository = ctx.tracks
    assert await tr.search("\\", limit=5) == []
    assert await tr.search("%", limit=5) == []
