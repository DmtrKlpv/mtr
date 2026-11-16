"""Тесты репозитория треков и сканера.

Главная проверка этапа: полная переиндексация НЕ сдвигает track_id,
поэтому плейлисты остаются целыми.
"""

from __future__ import annotations

from pathlib import Path


from tests.conftest import make_flac, make_mp3, tag_flac, tag_mp3


# Upsert и справочники
async def test_upsert_insert_then_unchanged(ctx, music_dir: Path):
    path = make_mp3(music_dir / "a.mp3")
    tag_mp3(path, title="A", artist="Artist")

    first = await ctx.scanner.scan(mode="incremental")
    assert first.counters.added == 1
    assert first.counters.failed == 0

    track = (await ctx.tracks.list_tracks())[0]
    assert track.title == "A"
    assert track.artist == "Artist"

    # Второй проход без изменений ничего не добавляет и не обновляет.
    second = await ctx.scanner.scan(mode="incremental")
    assert second.counters.added == 0
    assert second.counters.updated == 0
    assert second.counters.unchanged == 0  # файл не попал в очередь разбора


async def test_artist_and_album_deduplication(ctx, music_dir: Path):
    """Два трека одного исполнителя -> один artist и один album."""
    for index in range(2):
        path = make_mp3(music_dir / f"t{index}.mp3")
        tag_mp3(path, title=f"T{index}", artist="Same Artist", album="Same Album")

    await ctx.scanner.scan(mode="incremental")

    artist_count = await ctx.music_db.fetch_value("SELECT COUNT(*) FROM artists")
    album_count = await ctx.music_db.fetch_value("SELECT COUNT(*) FROM albums")
    assert artist_count == 1
    assert album_count == 1


async def test_case_insensitive_artist_merge(ctx, music_dir: Path):
    """'The Beatles' и 'the beatles' — один исполнитель."""
    p1 = make_mp3(music_dir / "a1.mp3")
    tag_mp3(p1, title="A", artist="The Beatles")
    p2 = make_mp3(music_dir / "a2.mp3")
    tag_mp3(p2, title="B", artist="the beatles")

    await ctx.scanner.scan(mode="incremental")
    count = await ctx.music_db.fetch_value("SELECT COUNT(*) FROM artists")
    assert count == 1


async def test_play_stats_row_created(ctx, music_dir: Path):
    """Свежий трек сразу имеет строку статистики для смарт-плейлистов."""
    path = make_mp3(music_dir / "s.mp3")
    tag_mp3(path, title="S")
    await ctx.scanner.scan(mode="incremental")

    track = (await ctx.tracks.list_tracks())[0]
    row = await ctx.music_db.fetch_one(
        "SELECT play_count FROM play_stats WHERE track_id = ?", (track.id,)
    )
    assert row is not None
    assert row["play_count"] == 0


# ГЛАВНЫЙ ИНВАРИАНТ: id стабилен между ресканами
async def test_full_rescan_preserves_track_ids_and_playlists(ctx, music_dir: Path):
    path = make_mp3(music_dir / "keep.mp3")
    tag_mp3(path, title="Keep Me", artist="A")

    await ctx.scanner.scan(mode="incremental")
    track = (await ctx.tracks.list_tracks())[0]
    original_id = track.id

    # Пользователь кладёт трек в плейлист.
    playlist_id = await ctx.music_db.execute(
        "INSERT INTO playlists (owner_id, name, is_public, is_smart, "
        "created_at, updated_at) VALUES (1, 'My List', 0, 0, 'now', 'now')"
    )
    await ctx.music_db.execute(
        "INSERT INTO playlist_tracks (playlist_id, track_id, position, added_at) "
        "VALUES (?, ?, 0, 'now')",
        (playlist_id, original_id),
    )

    # Принудительная полная переиндексация.
    await ctx.scanner.scan(mode="full")

    after = (await ctx.tracks.list_tracks())[0]
    assert after.id == original_id, "track_id изменился — плейлисты бы сломались"

    # Плейлист по-прежнему указывает на живой трек.
    row = await ctx.music_db.fetch_one(
        "SELECT t.title FROM playlist_tracks pt JOIN tracks t ON t.id = pt.track_id "
        "WHERE pt.playlist_id = ?",
        (playlist_id,),
    )
    assert row is not None
    assert row["title"] == "Keep Me"


async def test_modified_file_updates_in_place(ctx, music_dir: Path):
    """Изменение тегов обновляет ту же строку, а не создаёт новую."""
    path = make_mp3(music_dir / "m.mp3")
    tag_mp3(path, title="Before", artist="A")
    await ctx.scanner.scan(mode="incremental")

    track_before = (await ctx.tracks.list_tracks())[0]

    # Перезаписываем тег и «старим» mtime, чтобы diff его заметил.
    tag_mp3(path, title="After", artist="A")
    import os

    stat = path.stat()
    os.utime(path, (stat.st_atime, stat.st_mtime + 10))

    result = await ctx.scanner.scan(mode="incremental")
    assert result.counters.updated == 1
    assert result.counters.added == 0

    tracks = await ctx.tracks.list_tracks()
    assert len(tracks) == 1
    assert tracks[0].id == track_before.id
    assert tracks[0].title == "After"


# Пометка missing вместо удаления
async def test_deleted_file_marked_missing_not_removed(ctx, music_dir: Path):
    path = make_mp3(music_dir / "gone.mp3")
    tag_mp3(path, title="Gone")
    await ctx.scanner.scan(mode="incremental")

    track = (await ctx.tracks.list_tracks())[0]
    playlist_id = await ctx.music_db.execute(
        "INSERT INTO playlists (owner_id, name, is_public, is_smart, "
        "created_at, updated_at) VALUES (1, 'P', 0, 0, 'now', 'now')"
    )
    await ctx.music_db.execute(
        "INSERT INTO playlist_tracks (playlist_id, track_id, position, added_at) "
        "VALUES (?, ?, 0, 'now')",
        (playlist_id, track.id),
    )

    path.unlink()
    result = await ctx.scanner.scan(mode="full")

    assert result.counters.removed == 1
    # Строка существует, но помечена и не видна в обычной выдаче.
    assert await ctx.tracks.get_by_id(track.id) is not None
    assert (await ctx.tracks.get_by_id(track.id)).missing is True
    assert await ctx.tracks.list_tracks() == []
    # Позиция в плейлисте сохранилась.
    assert await ctx.music_db.fetch_value(
        "SELECT COUNT(*) FROM playlist_tracks WHERE track_id = ?", (track.id,)
    ) == 1


async def test_returning_file_clears_missing(ctx, music_dir: Path):
    path = make_mp3(music_dir / "back.mp3")
    tag_mp3(path, title="Back")
    await ctx.scanner.scan(mode="incremental")
    track = (await ctx.tracks.list_tracks())[0]

    path.unlink()
    await ctx.scanner.scan(mode="full")
    assert (await ctx.tracks.get_by_id(track.id)).missing is True

    # Возвращаем файл с тем же содержимым и mtime, что был в БД.
    make_mp3(music_dir / "back.mp3")
    tag_mp3(music_dir / "back.mp3", title="Back")
    # Подгоняем mtime к сохранённому, чтобы diff не сработал как update.
    import os

    os.utime(music_dir / "back.mp3", (track.mtime, track.mtime))

    await ctx.scanner.scan(mode="incremental")
    refreshed = await ctx.tracks.get_by_id(track.id)
    assert refreshed.missing is False


# Обработка ошибок в пачке
async def test_broken_file_does_not_stop_scan(ctx, music_dir: Path):
    good = make_mp3(music_dir / "good.mp3")
    tag_mp3(good, title="Good")
    (music_dir / "broken.mp3").write_bytes(b"not audio at all")

    result = await ctx.scanner.scan(mode="incremental")

    assert result.counters.added == 1
    assert result.counters.failed == 1
    assert result.counters.errors
    assert result.status == "completed"
    assert len(await ctx.tracks.list_tracks()) == 1


async def test_scan_records_run_in_db(ctx, music_dir: Path):
    make_mp3(music_dir / "x.mp3")
    result = await ctx.scanner.scan(mode="full")

    row = await ctx.music_db.fetch_one(
        "SELECT * FROM scan_runs WHERE id = ?", (result.run_id,)
    )
    assert row["status"] == "completed"
    assert row["files_seen"] == 1
    assert row["finished_at"] is not None


async def test_scan_without_music_dir_fails_gracefully(ctx, tmp_path: Path):
    from app.scanner.library_scanner import Scanner

    scanner = Scanner(ctx.tracks, tmp_path / "does-not-exist")
    result = await scanner.scan(mode="incremental")
    assert result.status == "failed"
    assert "не найден" in (result.error or "")


async def test_ignored_extensions_and_hidden_files(ctx, music_dir: Path):
    (music_dir / "cover.jpg").write_bytes(b"jpeg")
    (music_dir / ".hidden.mp3").write_bytes(b"x")
    (music_dir / "notes.txt").write_text("hi")
    (music_dir / "__pycache__").mkdir()
    (music_dir / "__pycache__" / "deep.mp3").write_bytes(b"x")

    result = await ctx.scanner.scan(mode="incremental")
    assert result.counters.seen == 0


async def test_nested_directories_walked(ctx, music_dir: Path):
    deep = music_dir / "a" / "b" / "c"
    path = make_mp3(deep / "deep.mp3")
    tag_mp3(path, title="Deep")

    result = await ctx.scanner.scan(mode="incremental")
    assert result.counters.added == 1
    assert (await ctx.tracks.list_tracks())[0].title == "Deep"


# Поиск и статистика
async def test_search(ctx, music_dir: Path):
    p1 = make_mp3(music_dir / "1.mp3")
    tag_mp3(p1, title="Moonlight Sonata", artist="Beethoven", album="Piano")
    p2 = make_flac(music_dir / "2.flac")
    tag_flac(p2, title="Sunrise", artist="Debussy", album="Piano")
    await ctx.scanner.scan(mode="incremental")

    assert len(await ctx.tracks.search("moon")) == 1
    assert len(await ctx.tracks.search("Piano")) == 2
    assert len(await ctx.tracks.search("Beethoven")) == 1
    assert await ctx.tracks.search("zzz") == []
    assert await ctx.tracks.search("") == []


async def test_library_stats(ctx, music_dir: Path):
    for index in range(3):
        path = make_mp3(music_dir / f"s{index}.mp3", seconds=1.0)
        tag_mp3(path, title=f"S{index}", artist="A", album="Al")
    await ctx.scanner.scan(mode="incremental")

    stats = await ctx.tracks.library_stats()
    assert stats["tracks"] == 3
    assert stats["artists"] == 1
    assert stats["albums"] == 1
    assert stats["total_ms"] > 0


async def test_purge_missing_removes_rows(ctx, music_dir: Path):
    path = make_mp3(music_dir / "p.mp3")
    tag_mp3(path, title="P")
    await ctx.scanner.scan(mode="incremental")
    path.unlink()
    await ctx.scanner.scan(mode="full")

    removed = await ctx.tracks.purge_missing()
    assert removed == 1
    assert await ctx.tracks.count_tracks(include_missing=True) == 0


# Транзакции
async def test_nested_transaction_rollback(ctx):
    """Вложенная транзакция не открывает второй BEGIN и откатывается."""
    await ctx.music_db.execute(
        "INSERT INTO artists (name, created_at, updated_at) VALUES ('Outer', 'now', 'now')"
    )
    try:
        async with ctx.music_db.transaction():
            await ctx.music_db.execute(
                "INSERT INTO artists (name, created_at, updated_at) "
                "VALUES ('Inner', 'now', 'now')"
            )
            async with ctx.music_db.transaction():
                await ctx.music_db.execute(
                    "INSERT INTO artists (name, created_at, updated_at) "
                    "VALUES ('Nested', 'now', 'now')"
                )
            raise RuntimeError("откат")
    except RuntimeError:
        pass

    names = {
        row["name"]
        for row in await ctx.music_db.fetch_all("SELECT name FROM artists")
    }
    # Первый INSERT был вне транзакции и обязан сохраниться.
    assert "Outer" in names
    assert "Inner" not in names
    assert "Nested" not in names
