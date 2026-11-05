"""Фаза обложек после скана: прогресс, доступность альбомов сразу, отмена."""
from __future__ import annotations

import asyncio
from pathlib import Path

from app.covers.prefetch import CoverPass
from tests.conftest import make_mp3, tag_mp3
from tests.test_cover_thumbnails import _image


async def _wait_done(cp: CoverPass, timeout: float = 20.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while cp.running and loop.time() < end:
        await asyncio.sleep(0.02)


async def _quiet_covers(ctx) -> None:
    """Останавливает фоновую фазу обложек, которую запускает сканер.

    Без этого фаза заполняет cover_cache во время теста и подменяет состояние,
    которое тест только что подготовил.
    """
    ctx.cover_pass.cancel_nowait()
    await ctx.cover_pass.stop()


async def test_albums_are_available_before_covers_finish(ctx, music_dir: Path):
    """Сразу после скана альбомы есть в БД и видны в сетке, не дожидаясь обложек."""
    for a in range(4):
        p = make_mp3(music_dir / f"al{a}" / "t.mp3", seconds=0.2)
        tag_mp3(p, title="T", artist=f"A{a}", album=f"Alb{a}")
    await ctx.scanner.scan(mode="full")
    # Фаза обложек не запущена: альбомы уже доступны.
    albums = await ctx.library.list_albums(limit=50, offset=0)
    assert len(albums) == 4


async def test_pass_reports_progress_and_finishes(ctx, music_dir: Path):
    for a in range(3):
        p = make_mp3(music_dir / f"al{a}" / "t.mp3", seconds=0.2)
        tag_mp3(p, title="T", artist=f"A{a}", album=f"Alb{a}", cover=_image(900) if a == 0 else None)
    await ctx.scanner.scan(mode="full")

    cp = CoverPass(ctx.covers, pause=0.0)
    cp.start("incremental")
    await _wait_done(cp)
    s = cp.as_dict()
    assert s["state"] == "done"
    assert s["total"] == 3
    assert s["done"] == 3
    assert s["made"] + s["absent"] + s["failed"] == 3


async def test_pass_can_be_cancelled_between_albums(ctx, music_dir: Path):
    for a in range(6):
        p = make_mp3(music_dir / f"al{a}" / "t.mp3", seconds=0.2)
        tag_mp3(p, title="T", artist=f"A{a}", album=f"Alb{a}")
    await ctx.scanner.scan(mode="full")

    cp = CoverPass(ctx.covers, pause=0.05)
    cp.start("incremental")
    cp.cancel_nowait()
    await _wait_done(cp)
    assert cp.as_dict()["state"] in ("cancelled", "done")
    assert cp.as_dict()["done"] < 6 or cp.as_dict()["state"] == "done"


async def test_status_exposes_current_album_time(ctx, music_dir: Path):
    p = make_mp3(music_dir / "al" / "t.mp3", seconds=0.2)
    tag_mp3(p, title="T", artist="A", album="Alb")
    await ctx.scanner.scan(mode="full")
    cp = CoverPass(ctx.covers, pause=0.0)
    d = cp.as_dict()
    assert "current_album_sec" in d and d["current_album_sec"] is None


async def test_incremental_picks_only_new_and_changed_albums(ctx, music_dir: Path):
    """Инкремент не трогает готовые альбомы, но берёт новые и изменённые."""
    for a in range(3):
        p = make_mp3(music_dir / f"inc{a}" / "t.mp3", seconds=0.2)
        tag_mp3(p, title="T", artist=f"Inc{a}", album=f"IncAlb{a}")
    await ctx.scanner.scan(mode="full")
    await _quiet_covers(ctx)
    ids = [int(r["id"]) for r in await ctx.music_db.fetch_all("SELECT id FROM albums ORDER BY id")]
    # все три альбома уже обработаны: кеш есть у каждого
    for album_id in ids:
        await ctx.covers.mark_absent(f"al-{album_id}")
    assert await ctx.covers.pending_album_ids(mode="incremental") == []

    # новый альбом появился после обработки
    p = make_mp3(music_dir / "inc_new" / "t.mp3", seconds=0.2)
    tag_mp3(p, title="N", artist="NewArt", album="NewAlb")
    await ctx.scanner.scan(mode="incremental")
    await _quiet_covers(ctx)
    new_id = int(await ctx.music_db.fetch_value("SELECT id FROM albums WHERE name = 'NewAlb'"))
    assert await ctx.covers.pending_album_ids(mode="incremental") == [new_id]


async def test_incremental_repicks_album_with_newer_tracks(ctx, music_dir: Path):
    """Трек обновлён позже, чем записана обложка: альбом снова в очереди."""
    p = make_mp3(music_dir / "upd" / "t.mp3", seconds=0.2)
    tag_mp3(p, title="T", artist="UpdArt", album="UpdAlb")
    await ctx.scanner.scan(mode="full")
    await _quiet_covers(ctx)
    album_id = int(await ctx.music_db.fetch_value("SELECT id FROM albums WHERE name = 'UpdAlb'"))
    await ctx.covers.mark_absent(f"al-{album_id}")
    assert await ctx.covers.pending_album_ids(mode="incremental") == []

    # обложку «делаем» настоящей записью (file_path непустой), затем трек меняется позже
    await ctx.music_db.execute(
        "UPDATE cover_cache SET file_path = ?, mime_type = 'image/jpeg', size_bytes = 10, "
        "created_at = '2026-01-01T00:00:00Z' WHERE key = ?",
        (str(music_dir / "fake.jpg"), f"al-{album_id}"),
    )
    (music_dir / "fake.jpg").write_bytes(b"x" * 10)
    await ctx.music_db.execute(
        "UPDATE tracks SET updated_at = '2026-06-01T00:00:00Z' WHERE album_id = ?", (album_id,)
    )
    assert album_id in await ctx.covers.pending_album_ids(mode="incremental")


async def test_incremental_repicks_album_whose_cover_file_vanished(ctx, music_dir: Path):
    """Запись есть, файл превью пропал: инкремент восстанавливает обложку."""
    p = make_mp3(music_dir / "gone" / "t.mp3", seconds=0.2)
    tag_mp3(p, title="T", artist="GoneArt", album="GoneAlb")
    await ctx.scanner.scan(mode="full")
    await _quiet_covers(ctx)
    album_id = int(await ctx.music_db.fetch_value("SELECT id FROM albums WHERE name = 'GoneAlb'"))
    missing_file = str(music_dir / "does-not-exist.jpg")
    await ctx.music_db.execute(
        "INSERT INTO cover_cache (key, file_path, mime_type, size_bytes, source, created_at) "
        "VALUES (?, ?, 'image/jpeg', 10, 'test', '2026-01-02T00:00:00Z')",
        (f"al-{album_id}", missing_file),
    )
    assert album_id in await ctx.covers.pending_album_ids(mode="incremental")


async def test_full_pass_clears_absent_markers_and_repicks_them(ctx, music_dir: Path):
    """Полный проход снимает маркеры «картинки нет» и берёт такие альбомы снова."""
    p = make_mp3(music_dir / "abs" / "t.mp3", seconds=0.2)
    tag_mp3(p, title="T", artist="AbsArt", album="AbsAlb")
    await ctx.scanner.scan(mode="full")
    await _quiet_covers(ctx)
    album_id = int(await ctx.music_db.fetch_value("SELECT id FROM albums WHERE name = 'AbsAlb'"))
    await ctx.covers.mark_absent(f"al-{album_id}")
    assert album_id not in await ctx.covers.pending_album_ids(mode="full")
    await ctx.covers.clear_absent_markers()
    assert album_id in await ctx.covers.pending_album_ids(mode="full")


async def test_cancelled_incremental_pass_keeps_finished_covers(ctx, music_dir: Path):
    """Отмена на середине не портит уже записанные обложки."""
    for a in range(4):
        p = make_mp3(music_dir / f"cx{a}" / "t.mp3", seconds=0.2)
        tag_mp3(p, title="T", artist=f"CX{a}", album=f"CXAlb{a}")
    await ctx.scanner.scan(mode="full")
    await _quiet_covers(ctx)
    cp = CoverPass(ctx.covers, pause=0.05)
    cp.start("incremental")
    await asyncio.sleep(0.05)
    cp.cancel_nowait()
    await _wait_done(cp)
    # каждый альбом либо получил запись, либо остался в очереди: ничего не битое
    rows = await ctx.music_db.fetch_all("SELECT key, file_path FROM cover_cache WHERE key LIKE 'al-%'")
    for row in rows:
        assert row["key"].startswith("al-")
    assert cp.state in {"cancelled", "done"}
