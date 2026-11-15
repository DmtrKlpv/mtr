"""Рубрикатор: буква имени и фильтр по ней в трёх списках."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.utils.alphabet import BUCKETS, letter_of
from tests.conftest import make_mp3, tag_mp3
from tests.test_web import login_admin, web  # noqa: F401 - фикстура


@pytest.mark.parametrize("name,want", [
    ("The Prodigy", "T"),
    ("ария", "А"),
    # Ё и Е — одна рубрика, как в словарях.
    ("Ёлка", "Е"),
    ("ёлка", "Е"),
    ("ёж", "Е"),
    # Не Ё, а отдельная буква.
    ("Йорк", "Й"),
    ("01 Album", "0-9"),
    ("25/17", "0-9"),
    # Спецсимволы в начале пропускаются.
    ("$100", "0-9"),
    ("[Unknown]", "U"),
    ('"Кавычки"', "К"),
    ("«Ёлка»", "Е"),
    ("-Тире", "Т"),
    ("!!!Void", "V"),
    # Диакритика сворачивается к базовой букве.
    ("Édith", "E"),
    ("æther", "A"),
    ("øya", "O"),
    # Не латиница и не кириллица — отдельная рубрика.
    ("日本語", "Другие"),
    ("Ω", "Другие"),
    ("", "Другие"),
    (None, "Другие"),
    ("!!!", "Другие"),
])
def test_letter_of(name, want):
    assert letter_of(name) == want


def test_buckets_start_and_end():
    assert BUCKETS[0] == "0-9"
    assert BUCKETS[1] == "A"
    assert BUCKETS[-1] == "Другие"
    # Ё отдельной кнопки не имеет.
    assert "Ё" not in BUCKETS
    assert "Й" in BUCKETS and "Ъ" in BUCKETS


def test_buckets_have_no_duplicates():
    assert len(set(BUCKETS)) == len(BUCKETS)


# ----------------------------------------------------------------------
# Фильтр в API
# ----------------------------------------------------------------------
def _library(music_dir: Path) -> None:
    """Три альбома у трёх исполнителей: латиница, кириллица с Ё, цифра."""
    for album, artist, folder in [
        ("Abbey Road", "The Beatles", "A"),
        ("Ёлка и всё", "Ёлка", "B"),
        ("100 лет", "25/17", "C"),
    ]:
        path = make_mp3(music_dir / folder / "01.mp3", seconds=0.2)
        tag_mp3(path, title="T", artist=artist, album=album)


async def _scan(ctx) -> None:
    await ctx.scanner.scan(mode="full")
    ctx.cover_pass.cancel_nowait()
    await ctx.cover_pass.stop()


async def test_albums_filtered_by_letter(ctx, music_dir: Path):
    _library(music_dir)
    await _scan(ctx)

    async def names(letter: str) -> list[str]:
        rows = await ctx.library.list_albums(letter=letter)
        return [row.name for row in rows]

    assert await names("A") == ["Abbey Road"]
    # Ёлка попадает в рубрику Е.
    assert await names("Е") == ["Ёлка и всё"]
    assert await names("0-9") == ["100 лет"]
    assert await names("Я") == []
    assert await names("Другие") == []


async def test_album_count_respects_letter(ctx, music_dir: Path):
    _library(music_dir)
    await _scan(ctx)
    assert await ctx.library.count_albums() == 3
    assert await ctx.library.count_albums(letter="A") == 1
    assert await ctx.library.count_albums(letter="Другие") == 0


async def test_artists_filtered_by_letter(ctx, music_dir: Path):
    _library(music_dir)
    await _scan(ctx)

    async def names(letter: str) -> list[str]:
        rows = await ctx.library.list_artists(letter=letter)
        return [row.name for row in rows]

    assert await names("T") == ["The Beatles"]
    assert await names("Е") == ["Ёлка"]
    assert await names("0-9") == ["25/17"]
    assert await ctx.library.count_artists() == 3
    assert await ctx.library.count_artists(letter="Е") == 1


async def test_unknown_letter_is_ignored(ctx, music_dir: Path):
    """Мусор в параметре не ломает список и ничего не фильтрует."""
    _library(music_dir)
    await _scan(ctx)
    rows = await ctx.library.list_albums(letter="'; DROP TABLE albums; --")
    assert len(rows) == 3


async def test_albums_endpoint_accepts_letter(web):  # noqa: F811
    client, app = web
    await login_admin(client)
    ctx = app.state.ctx
    _library(Path(ctx.settings.music_dir))
    await _scan(ctx)

    data = (await client.get("/api/web/albums?letter=A")).json()["data"]
    assert [item["name"] for item in data["items"]] == ["Abbey Road"]
    assert data["total"] == 1
    assert data["letter"] == "A"

    artists = (await client.get("/api/web/artists?letter=A")).json()["data"]
    assert artists["items"] == []


async def test_playlists_filtered_by_letter(web):  # noqa: F811
    client, app = web
    await login_admin(client)
    ctx = app.state.ctx
    admin = await ctx.users.get_by_username("admin")
    for name in ("Ёлки", "Abba", "Джаз"):
        await ctx.playlists.create(admin.id, name)

    async def names(letter: str) -> list[str]:
        rows = await ctx.playlists.list_visible(admin.id, letter=letter)
        return [row.name for row in rows]

    assert await names("A") == ["Abba"]
    assert await names("Е") == ["Ёлки"]
    assert await names("Д") == ["Джаз"]
    assert sorted(await names("")) == sorted(["Ёлки", "Abba", "Джаз"])

    data = (await client.get("/api/web/playlists?letter=A")).json()["data"]
    assert [item["name"] for item in data["items"]] == ["Abba"]
