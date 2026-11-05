"""Сопоставление плейлистов, когда теги разъехались.

Сценарий: в плейлисте исполнитель перечислением, в библиотеке он один, а
соавторы вынесены в название — `(feat. …)`, `(via …)`. Точное сравнение такие
пары не находит; варианты должен предлагать приблизительный поиск.
"""
from __future__ import annotations

import pytest

from app.playlists import importer
from app.playlists.matching import (
    MATCH_BY_FILENAME,
    MATCH_BY_TITLE_ARTIST,
    MATCH_BY_TITLE_BASE,
    TrackIndex,
    artist_variants,
    base_title,
    normalize_filename,
)
from tests.conftest import make_mp3, tag_mp3


# ----------------------------------------------------------------------
# Нормализация
# ----------------------------------------------------------------------
def test_artist_variants_split_enumeration():
    assert artist_variants("Fallaout boy, Idx, Korasakov") == [
        "fallaout boy", "idx", "korasakov", "fallaout boy idx korasakov",
    ]


def test_artist_variants_cover_all_separators():
    for value in (
        "Main Star feat. Helper",
        "Main Star ft. Helper",
        "Main Star with Helper",
        "Main Star via Helper",
        "Main Star vs Helper",
        "Main Star & Helper",
        "Main Star / Helper",
        "Main Star x Helper",
    ):
        variants = artist_variants(value)
        assert "main star" in variants, value
        assert "helper" in variants, value


def test_artist_variants_keep_whole_name():
    """`Simon & Garfunkel` не должно распадаться безвозвратно."""
    assert "simon & garfunkel" in artist_variants("Simon & Garfunkel")


def test_artist_variants_from_brackets():
    assert artist_variants("Queen (feat. David Bowie)") == ["queen", "david bowie"]


def test_base_title_drops_collaborators():
    assert base_title("My favorite game (feat. Idx, Korasakov)") == "my favorite game"
    assert base_title("My favorite game (via Idx, Korasakov)") == "my favorite game"
    assert base_title("My favorite game feat. Idx") == "my favorite game"


def test_base_title_keeps_real_part_of_name():
    """`(Official Video)` — часть названия, а не соавторы."""
    assert base_title("Song (Official Video)") == "song official video"


def test_normalize_filename_variants():
    assert normalize_filename("02 - Fallaout boy - My favorite game.mp3") == [
        "fallaout boy my favorite game", "my favorite game", "fallaout boy",
    ]
    # Без «Артист - » хвост соавторов срезан, остаётся один вариант.
    assert normalize_filename("My favorite game (feat. Idx).mp3") == ["my favorite game"]


# ----------------------------------------------------------------------
# Индекс и варианты
# ----------------------------------------------------------------------
@pytest.fixture()
async def indexed(ctx):
    """Библиотека: артист один, соавторы в названии."""
    p1 = make_mp3(ctx.settings.music_dir / "fb" / "01 - My favorite game.mp3", seconds=0.2)
    tag_mp3(p1, title="My favorite game (feat. Idx, Korasakov)",
            artist="Fallaout boy", album="A")
    p2 = make_mp3(ctx.settings.music_dir / "zz" / "02 - Another.mp3", seconds=0.2)
    tag_mp3(p2, title="Another song (via Korasakov)", artist="Korasakov", album="B")
    await ctx.scanner.scan(mode="full")
    ctx.cover_pass.cancel_nowait()
    await ctx.cover_pass.stop()
    index = TrackIndex.build(await ctx.tracks.all_live_for_index())
    return index, ctx


async def test_suggestion_prefers_title_and_artist(indexed):
    index, _ = indexed
    found = index.suggest(
        title="My favorite game", artist="Fallaout boy, Idx, Korasakov",
    )
    assert found, "вариант должен найтись"
    track, label = found[0]
    assert label == MATCH_BY_TITLE_ARTIST
    assert track["artist"] == "Fallaout boy"


async def test_suggestion_finds_by_guest_artist(indexed):
    """Гость из перечисления тоже даёт вариант."""
    index, _ = indexed
    found = index.suggest(title="My favorite game", artist="Idx")
    assert found
    assert found[0][1] == MATCH_BY_TITLE_BASE


async def test_suggestion_by_filename(indexed):
    index, _ = indexed
    found = index.suggest(
        title=None, artist=None, filename="01 - My favorite game.mp3",
    )
    assert found
    assert found[0][1] == MATCH_BY_FILENAME


async def test_index_ignores_missing_tracks(indexed):
    index, ctx = indexed
    await ctx.music_db.execute("UPDATE tracks SET missing = 1")
    fresh = TrackIndex.build(await ctx.tracks.all_live_for_index())
    assert fresh.size == 0


# ----------------------------------------------------------------------
# Импорт .m3u: приблизительные варианты не подставляются молча
# ----------------------------------------------------------------------
async def test_import_offers_variants_instead_of_guessing(indexed, tmp_path):
    index, ctx = indexed
    playlist = tmp_path / "mix.m3u8"
    playlist.write_text(
        "#EXTM3U\n"
        "#EXTINF:10,Fallaout boy, Idx, Korasakov - My favorite game\n"
        "/old/place/renamed_track.mp3\n",
        encoding="utf-8",
    )
    entries, error = importer.read_playlist(playlist, ctx.settings.music_dir)
    assert error is None and len(entries) == 1

    matched, unmatched = await importer.match_entries(
        entries,
        music_root=ctx.settings.music_dir,
        find_by_path=ctx.tracks.find_by_path,
        find_by_filename=ctx.tracks.find_by_filename,
        find_by_title=ctx.tracks.find_by_title,
        index=index,
    )
    # Не подставлено автоматически: решение за пользователем.
    assert matched == [None]
    assert len(unmatched) == 1
    item = unmatched[0]
    assert item.candidates, "должны быть варианты для ручного выбора"
    assert item.candidates[0].artist == "Fallaout boy"
    assert item.candidates[0].note  # причина видна в интерфейсе


async def test_import_still_matches_exact_path_automatically(indexed, tmp_path):
    """Точное совпадение по пути остаётся автоматическим."""
    index, ctx = indexed
    real = str(ctx.settings.music_dir / "fb" / "01 - My favorite game.mp3")
    playlist = tmp_path / "exact.m3u8"
    playlist.write_text(f"#EXTM3U\n{real}\n", encoding="utf-8")
    entries, error = importer.read_playlist(playlist, ctx.settings.music_dir)
    assert error is None

    matched, unmatched = await importer.match_entries(
        entries,
        music_root=ctx.settings.music_dir,
        find_by_path=ctx.tracks.find_by_path,
        find_by_filename=ctx.tracks.find_by_filename,
        find_by_title=ctx.tracks.find_by_title,
        index=index,
    )
    assert matched[0] is not None
    assert unmatched == []


# ----------------------------------------------------------------------
# Ресинк папок: похожие варианты предлагаются, но не применяются
# ----------------------------------------------------------------------
async def test_folder_resync_offers_similar_without_moving(ctx, tmp_path):
    """Пропавший трек с разъехавшимися тегами: предлагаем, но не переставляем."""
    from app.playlists import folder_sync
    from app.repositories.playlists import PlaylistRepository

    folder = ctx.settings.music_dir / "resync"
    folder.mkdir(parents=True)
    old = make_mp3(folder / "a.mp3", seconds=0.2)
    tag_mp3(old, title="My favorite game", artist="Fallaout boy, Idx, Korasakov",
            album="A")
    await ctx.scanner.scan(mode="full")
    ctx.cover_pass.cancel_nowait()
    await ctx.cover_pass.stop()

    playlists = PlaylistRepository(ctx.music_db)
    user = await ctx.users.create_user("alice", "secretpass123", role="admin")
    # Плейлист «из папки»: создаём напрямую, origin_folder_path есть только в БД.
    playlist_id = await ctx.music_db.execute(
        "INSERT INTO playlists (name, owner_id, is_public, origin_folder_path, "
        "created_at, updated_at) VALUES (?, ?, 0, ?, ?, ?)",
        ("Resync", user.id, str(folder), "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
    )
    track_id = int(await ctx.music_db.fetch_value(
        "SELECT id FROM tracks WHERE path = ?", (str(old),)))
    await ctx.music_db.execute(
        "INSERT INTO playlist_tracks (playlist_id, track_id, position, added_at) "
        "VALUES (?, ?, 0, '2026-01-01T00:00:00Z')",
        (playlist_id, track_id),
    )

    # Теги переписали: артист один, соавторы ушли в название. Файл переименован.
    old.unlink()
    new = make_mp3(folder / "renamed.mp3", seconds=0.2)
    tag_mp3(new, title="My favorite game (feat. Idx, Korasakov)",
            artist="Fallaout boy", album="A")
    await ctx.scanner.scan(mode="incremental")
    ctx.cover_pass.cancel_nowait()
    await ctx.cover_pass.stop()

    plan = await folder_sync.build_resync_plan(
        db=ctx.music_db, playlists=playlists,
        music_root=ctx.settings.music_dir, playlist_id=playlist_id,
        user_id=user.id, is_admin=True,
    )
    # Ничего не переставлено автоматически.
    assert plan.relocate == []
    assert len(plan.ambiguous) == 1
    item = plan.ambiguous[0]
    assert item["candidates"], "должны быть варианты"
    assert item["candidates"][0]["title"].startswith("My favorite game")
    assert item["candidates"][0]["note"]
