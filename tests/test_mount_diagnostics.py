"""Диагностика неправильного монтирования.

Сценарий, ради которого это написано: вместо папки с музыкой смонтировался
анонимный том (пустая запись `-v ":/music"`). Docker ошибки не выдаёт, внутри
оказывается пусто, а имя источника выглядит как хеш. Симптом — база полна
треков, каталог пуст.
"""

from __future__ import annotations

from pathlib import Path

from app.diagnostics import run_doctor


async def _seed_tracks(ctx, count: int = 60) -> None:
    now = "2026-01-01"
    await ctx.music_db.execute(
        "INSERT INTO artists (name,created_at,updated_at) VALUES (?,?,?)",
        ("Artist", now, now),
    )
    await ctx.music_db.execute(
        "INSERT INTO albums (name,artist_id,created_at,updated_at) VALUES (?,?,?,?)",
        ("Album", 1, now, now),
    )
    await ctx.music_db.execute_many(
        "INSERT INTO tracks (path,filename,title,artist,artist_id,album,"
        "album_id,duration_ms,size_bytes,mtime,added_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (f"/music/{i}.mp3", f"{i}.mp3", "T", "Artist", 1, "Album", 1,
             1000, 1, 0.0, now, now)
            for i in range(count)
        ],
    )


async def test_empty_music_dir_with_full_db_is_reported(ctx, music_dir: Path):
    """Пустой /music при непустой базе — признак чужого тома."""
    await _seed_tracks(ctx, count=60)

    report = await run_doctor(ctx)

    assert report.music_mount_suspicious is True
    assert any("/music" in problem or "пуст" in problem for problem in report.problems)


async def test_empty_dir_on_empty_db_is_not_a_problem(ctx, music_dir: Path):
    """Пустая библиотека на пустой базе — это просто первый запуск."""
    report = await run_doctor(ctx)

    assert report.music_mount_suspicious is False
    assert not any("пуст" in problem for problem in report.problems)


async def test_populated_music_dir_is_clean(ctx, music_dir: Path):
    """Нормальный случай: файлы на месте, треков в базе нет — проблем нет."""
    from tests.conftest import make_mp3, tag_mp3

    for i in range(5):
        path = make_mp3(music_dir / f"t{i}.mp3", seconds=0.2)
        tag_mp3(path, title=f"T{i}", artist="Artist", album="Album")
    await ctx.scanner.scan(mode="full")

    report = await run_doctor(ctx)

    assert report.music_mount_suspicious is False
    assert not any("пуст" in problem for problem in report.problems)
