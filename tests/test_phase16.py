"""Этап 16: отдельное чтение, батч-запись сканера, запрос исполнителей, пауза фазы обложек."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.covers.prefetch import CoverPass
from app.db.database import Database
from app.db.migrations import _migrate_music
from app.repositories.library import LibraryRepository
from app.utils.activity import ActivityTracker, is_user_path
from tests.conftest import make_mp3, tag_mp3


# --- A: чтение не стоит за записью ---------------------------------------
async def test_reader_sees_committed_rows_during_open_write(tmp_path: Path):
    db = Database(tmp_path / "m.db", label="music")
    await db.connect()
    try:
        await _migrate_music(db)
        await db.execute("INSERT INTO artists (name, created_at, updated_at) VALUES ('a', 'x', 'x')")
        async with db.transaction():
            await db.execute("INSERT INTO artists (name, created_at, updated_at) VALUES ('b', 'x', 'x')")
            # Другая задача (не владелец транзакции) читает через reader:
            # видит только закоммиченное.
            async def foreign_read() -> int:
                return int(await db.fetch_value("SELECT COUNT(*) FROM artists", default=0))
            seen = await asyncio.create_task(foreign_read())
            assert seen == 1
            # Владелец видит свою незакоммиченную строку.
            assert int(await db.fetch_value("SELECT COUNT(*) FROM artists", default=0)) == 2
        assert int(await db.fetch_value("SELECT COUNT(*) FROM artists", default=0)) == 2
    finally:
        await db.close()


async def test_reader_is_read_only(tmp_path: Path):
    db = Database(tmp_path / "m.db", label="music")
    await db.connect()
    try:
        await _migrate_music(db)
        import aiosqlite
        assert db._reader_conn is not None
        with pytest.raises(aiosqlite.Error):
            await db._reader_conn.execute("INSERT INTO artists (name, created_at, updated_at) VALUES ('x','x','x')")
    finally:
        await db.close()


# --- B: батч-запись сканера --------------------------------------------
async def test_scan_batches_keep_results_and_isolate_bad_file(ctx, music_dir: Path):
    for i in range(7):
        p = make_mp3(music_dir / "Art" / f"Alb{i % 2}" / f"{i:02d}.mp3", seconds=0.2)
        tag_mp3(p, title=f"T{i}", artist="Art", album=f"Alb{i % 2}", track=str(i + 1))
    broken = music_dir / "Art" / "Alb0" / "99.mp3"
    broken.write_bytes(b"not an audio file at all")
    result = await ctx.scanner.scan(mode="full")
    assert result.counters.added == 7
    assert result.counters.failed == 1
    assert await ctx.music_db.fetch_value("SELECT COUNT(*) FROM tracks", default=0) == 7


async def test_batch_size_does_not_change_results(ctx, music_dir: Path, monkeypatch):
    from app.scanner import library_scanner
    for i in range(5):
        p = make_mp3(music_dir / f"A{i}" / "t.mp3", seconds=0.2)
