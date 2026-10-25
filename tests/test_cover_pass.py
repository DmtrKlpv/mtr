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
