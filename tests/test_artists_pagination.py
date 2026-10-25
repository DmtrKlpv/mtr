"""Исполнители отдаются страницами, без запроса на каждого.

Раньше /artists делал отдельный запрос first_album_id() на каждого
исполнителя: на 5254 записях это были тысячи обращений в SQLite подряд.
"""

from __future__ import annotations

import time

from app.repositories.library import LibraryRepository


async def _seed(db, artists: int = 200) -> None:
    now = "2026-01-01"
    await db.execute_many(
        "INSERT INTO artists (name,created_at,updated_at) VALUES (?,?,?)",
        [(f"Artist {i:04d}", now, now) for i in range(artists)],
    )
    albums, tracks = [], []
    for i in range(artists):
        for a in range(2):
            album_id = i * 10 + a + 1
            albums.append((album_id, f"Album {a}", i + 1, now, now))
            tracks.append((
                f"/m/{i}/{a}.mp3", "x.mp3", "T", f"Artist {i}", i + 1,
                f"Album {a}", album_id, 1000, 1, 0.0, now, now,
            ))
    await db.execute_many(
        "INSERT INTO albums (id,name,artist_id,created_at,updated_at) "
        "VALUES (?,?,?,?,?)", albums)
    await db.execute_many(
        "INSERT INTO tracks (path,filename,title,artist,artist_id,album,"
        "album_id,duration_ms,size_bytes,mtime,added_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", tracks)


async def test_artists_page_is_limited(ctx):
    await _seed(ctx.music_db, artists=200)

    page = await ctx.library.list_artists(limit=25, offset=0)

    assert len(page) == 25
    assert await ctx.library.count_artists() == 200


async def test_artists_pagination_has_no_overlap(ctx):
    await _seed(ctx.music_db, artists=50)

    first = await ctx.library.list_artists(limit=20, offset=0)
    second = await ctx.library.list_artists(limit=20, offset=20)

    ids_first = {a.id for a in first}
    ids_second = {a.id for a in second}
    assert not ids_first & ids_second


async def test_artist_album_map_is_single_query(ctx):
    """Одна карта на всю страницу вместо запроса на каждого исполнителя."""
    await _seed(ctx.music_db, artists=60)

    page = await ctx.library.list_artists(limit=60, offset=0)
    ids = [a.id for a in page]

    started = time.monotonic()
    mapping = await ctx.library.artist_album_map(ids)
    elapsed = time.monotonic() - started

    assert len(mapping) == len(ids)
    assert all(isinstance(v, int) for v in mapping.values())
    # Один запрос на 60 исполнителей должен укладываться в десятки миллисекунд.
    assert elapsed < 0.5, f"карта альбомов заняла {elapsed:.2f}с"


async def test_artist_album_map_empty_input(ctx):
    assert await ctx.library.artist_album_map([]) == {}


async def test_artists_without_tracks_are_excluded(ctx):
    """Исполнитель без треков не попадает в список и в счётчик."""
    await _seed(ctx.music_db, artists=5)
    now = "2026-01-01"
    await ctx.music_db.execute(
        "INSERT INTO artists (name,created_at,updated_at) VALUES (?,?,?)",
        ("Без альбомов", now, now),
    )

    total = await ctx.library.count_artists()
    names = {a.name for a in await ctx.library.list_artists(limit=100, offset=0)}

    assert total == 5
    assert "Без альбомов" not in names
