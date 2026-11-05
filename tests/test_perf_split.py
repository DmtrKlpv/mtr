"""Проверки разделения нагрузки (0.21.0): чтение отдельно от записи, батч сканера,
список исполнителей, пауза фазы обложек при активности пользователя."""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.db.database import Database
from app.db.migrations import _migrate_music
from app.repositories.library import LibraryRepository
from app.repositories.tracks import TrackRepository
from app.scanner.library_scanner import Scanner
from app.utils.activity import ActivityTracker, ActivityMiddleware, is_user_path
from app.covers.prefetch import CoverPass
from tests.conftest import make_mp3, tag_mp3

NOW = "2026-01-01T00:00:00Z"


async def _music_db(path: Path) -> Database:
    db = Database(path, label="music")
    await db.connect()
    await _migrate_music(db)
    return db


# ----------------------------------------------------------------------
# A. Чтение отдельно от записи
# ----------------------------------------------------------------------
async def test_reader_sees_only_committed_rows(tmp_path: Path):
    """Чтение из другой задачи видит закоммиченное, а не незакоммиченную запись."""
    db = await _music_db(tmp_path / "m.db")
    try:
        await db.execute(
            "INSERT INTO artists (name, created_at, updated_at) VALUES (?,?,?)",
            ("Committed", NOW, NOW),
        )
        started = asyncio.Event()
        release = asyncio.Event()

        async def writer():
            async with db.transaction():
                await db.execute(
                    "INSERT INTO artists (name, created_at, updated_at) VALUES (?,?,?)",
                    ("Uncommitted", NOW, NOW),
                )
                started.set()
                await release.wait()

        writer_task = asyncio.create_task(writer())
        await started.wait()

        # Чтение из постороннего контекста: не владелец транзакции.
        seen = await db.fetch_all("SELECT name FROM artists ORDER BY name")
        assert [row["name"] for row in seen] == ["Committed"]

        release.set()
        await writer_task
        after = await db.fetch_all("SELECT name FROM artists ORDER BY name")
        assert [row["name"] for row in after] == ["Committed", "Uncommitted"]
    finally:
        await db.close()


async def test_owner_reads_its_own_uncommitted_rows(tmp_path: Path):
    """Владелец транзакции видит свои незакоммиченные строки (читает через writer)."""
    db = await _music_db(tmp_path / "m.db")
    try:
        async with db.transaction():
            await db.execute(
                "INSERT INTO artists (name, created_at, updated_at) VALUES (?,?,?)",
                ("Mine", NOW, NOW),
            )
            rows = await db.fetch_all("SELECT name FROM artists")
            assert [row["name"] for row in rows] == ["Mine"]
    finally:
        await db.close()


async def test_reader_is_read_only(tmp_path: Path):
    """Читающее соединение не даёт писать: query_only."""
    db = await _music_db(tmp_path / "m.db")
    try:
        assert db._reader_conn is not None
        import sqlite3
        raised = False
        try:
            await db._reader_conn.execute(
                "INSERT INTO artists (name, created_at, updated_at) VALUES ('x',?,?)",
                (NOW, NOW),
            )
        except sqlite3.Error:
            raised = True
        assert raised
    finally:
        await db.close()


# ----------------------------------------------------------------------
# B. Батч записи в сканере: ошибка одного файла не теряет остальные
# ----------------------------------------------------------------------
async def test_scan_batch_keeps_good_files_when_one_fails(tmp_path: Path, monkeypatch):
    music = tmp_path / "music"
    for i in range(6):
        p = make_mp3(music / f"al{i}" / "t.mp3", seconds=0.2)
        tag_mp3(p, title=f"T{i}", artist=f"A{i}", album=f"Alb{i}")
    db = await _music_db(tmp_path / "m.db")
    try:
        repo = TrackRepository(db)
        original = repo.upsert_from_metadata
        bad = str(music / "al3" / "t.mp3")

        async def flaky(meta):
            if meta.path == bad:
                raise RuntimeError("boom")
            return await original(meta)

        monkeypatch.setattr(repo, "upsert_from_metadata", flaky)
        scanner = Scanner(repo, music, parse_concurrency=2)
        result = await scanner.scan(mode="full")
        assert result.counters.failed == 1
        assert result.counters.added == 5
        count = await db.fetch_value("SELECT COUNT(*) FROM tracks")
        assert int(count) == 5
    finally:
        await db.close()


async def test_batch_result_equals_single_file_result(tmp_path: Path):
    """Пачка по 1 и по 100 дают одинаковое содержимое таблицы tracks."""
    from app.scanner import library_scanner as scanner_mod

    music = tmp_path / "music"
    for i in range(7):
        p = make_mp3(music / f"al{i % 3}" / f"{i}.mp3", seconds=0.2)
        tag_mp3(p, title=f"T{i}", artist=f"A{i % 2}", album=f"Alb{i % 3}", track=str(i + 1))

    async def snapshot(batch: int) -> list[tuple]:
        scanner_mod.SCAN_BATCH = batch
        db = await _music_db(tmp_path / f"m{batch}.db")
        try:
            await Scanner(TrackRepository(db), music).scan(mode="full")
            rows = await db.fetch_all(
                "SELECT path, title, artist, album, track_number FROM tracks ORDER BY path"
            )
            return [tuple(r) for r in rows]
        finally:
            await db.close()

    original = scanner_mod.SCAN_BATCH
    try:
        one = await snapshot(1)
        many = await snapshot(100)
    finally:
        scanner_mod.SCAN_BATCH = original
    assert one == many
    assert len(one) == 7


# ----------------------------------------------------------------------
# C. Список исполнителей: тот же результат, что раньше
# ----------------------------------------------------------------------
async def test_artist_list_and_count_ignore_missing_tracks(tmp_path: Path):
    db = await _music_db(tmp_path / "m.db")
    try:
        await db.execute("INSERT INTO artists (id, name, created_at, updated_at) VALUES (1,'Live',?,?)", (NOW, NOW))
        await db.execute("INSERT INTO artists (id, name, created_at, updated_at) VALUES (2,'Ghost',?,?)", (NOW, NOW))
        await db.execute("INSERT INTO albums (id, name, artist_id, created_at, updated_at) VALUES (1,'A1',1,?,?)", (NOW, NOW))
        await db.execute("INSERT INTO albums (id, name, artist_id, created_at, updated_at) VALUES (2,'A2',1,?,?)", (NOW, NOW))
        await db.execute("INSERT INTO albums (id, name, artist_id, created_at, updated_at) VALUES (3,'G1',2,?,?)", (NOW, NOW))
        await db.execute(
            "INSERT INTO tracks (path, filename, title, album_id, missing, added_at, updated_at) VALUES ('/a','a','a',1,0,?,?)",
            (NOW, NOW))
        await db.execute(
            "INSERT INTO tracks (path, filename, title, album_id, missing, added_at, updated_at) VALUES ('/b','b','b',2,0,?,?)",
            (NOW, NOW))
        await db.execute(
            "INSERT INTO tracks (path, filename, title, album_id, missing, added_at, updated_at) VALUES ('/g','g','g',3,1,?,?)",
            (NOW, NOW))
        lib = LibraryRepository(db)
        rows = await lib.list_artists()
        assert [(r.name, r.album_count) for r in rows] == [("Live", 2)]
        assert await lib.count_artists() == 1
    finally:
        await db.close()


# ----------------------------------------------------------------------
# D. Пауза фазы обложек при активности пользователя
# ----------------------------------------------------------------------
def test_user_paths_and_exclusions():
    assert is_user_path("/api/web/albums")
    assert is_user_path("/api/web/artists")
    assert not is_user_path("/api/web/scan/status")
    assert not is_user_path("/api/web/covers/ready")
    assert not is_user_path("/rest/getCoverArt")
    assert not is_user_path("/static/app.js")
    assert not is_user_path("/rest/stream")


def test_tracker_busy_then_idle():
    t = ActivityTracker(window_sec=0.2)
    assert not t.busy()
    t.touch()
    assert t.busy()
    import time as _t
    _t.sleep(0.25)
    assert not t.busy()


async def test_cover_pass_waits_for_quiet_and_is_capped(tmp_path: Path):
    """Пока активность свежая, фаза ждёт; через max_wait идёт дальше."""
    tracker = ActivityTracker(window_sec=60.0)
    tracker.touch()

    class Covers:
        async def pending_album_ids(self, *, mode="incremental", limit=None):
            return [1]

        async def clear_absent_markers(self):
            return 0

        async def for_album(self, album_id):
            return None

    cp = CoverPass(Covers(), pause=0.0, activity=tracker, max_wait=1.0)
    cp.start("incremental")
    loop = asyncio.get_running_loop()
    end = loop.time() + 5
    seen_paused = False
    while cp.running and loop.time() < end:
        if cp.paused_for_user:
            seen_paused = True
        await asyncio.sleep(0.05)
    assert seen_paused
    assert cp.state == "done"


async def test_middleware_marks_only_user_requests():
    tracker = ActivityTracker(window_sec=60.0)
    seen = []

    async def app(scope, receive, send):
        seen.append(scope["path"])

    mw = ActivityMiddleware(app, tracker)
    await mw({"type": "http", "path": "/api/web/scan/status"}, None, None)
    assert not tracker.busy()
    await mw({"type": "http", "path": "/api/web/albums"}, None, None)
    assert tracker.busy()
    assert seen == ["/api/web/scan/status", "/api/web/albums"]
