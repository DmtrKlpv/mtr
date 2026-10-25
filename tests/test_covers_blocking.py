"""Обложки не должны блокировать event loop и не должны перечитывать файлы.

Именно из-за этого интерфейс зависал на «Загрузка / Обновление базы»:
разбор картинки шёл прямо в async-обработчике, и пока читался файл,
сервер не отвечал ни на один запрос.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from app.repositories import covers as covers_module


async def test_album_without_cover_does_not_block_loop(ctx, music_dir, monkeypatch):
    """Извлечение в потоке: пока идёт чтение, loop продолжает работать."""
    from tests.conftest import make_mp3, tag_mp3

    for a in range(6):
        for t in range(2):
            path = make_mp3(music_dir / f"al{a:02d}" / f"t{t}.mp3", seconds=0.2)
            tag_mp3(path, title=f"T{t}", artist=f"A{a}", album=f"Alb{a}")
    await ctx.scanner.scan(mode="full")

    # Извлечение искусственно медленное — как чтение по сети.
    delay = 0.05

    def slow_cover(path):
        time.sleep(delay)
        return None

    monkeypatch.setattr(covers_module, "extract_cover", slow_cover)
    monkeypatch.setattr(covers_module, "extract_from_directory", slow_cover)

    ticks = 0
    stop = asyncio.Event()

    async def ticker():
        nonlocal ticks
        while not stop.is_set():
            await asyncio.sleep(0.01)
            ticks += 1

    task = asyncio.create_task(ticker())
    try:
        await asyncio.gather(*(ctx.covers.for_album(i) for i in range(1, 7)))
    finally:
        stop.set()
        task.cancel()

    # Если бы разбор шёл в event loop, тиков было бы 0-1.
    assert ticks > 3, "event loop стоял во время извлечения обложек"


async def test_extraction_concurrency_is_limited(ctx, music_dir, monkeypatch):
    """Одновременно читается не больше COVER_EXTRACT_CONCURRENCY файлов."""
    from tests.conftest import make_mp3, tag_mp3

    for a in range(8):
        path = make_mp3(music_dir / f"al{a:02d}" / "t.mp3", seconds=0.2)
        tag_mp3(path, title="T", artist=f"A{a}", album=f"Alb{a}")
    await ctx.scanner.scan(mode="full")

    peak = 0
    current = 0
    lock = asyncio.Lock()

    def slow_cover(path):
        nonlocal peak, current
        # Счётчик потоков: инкремент/декремент не атомарны, но для оценки пика
        # достаточно приблизительной картины.
        current += 1
        peak = max(peak, current)
        time.sleep(0.03)
        current -= 1
        return None

    monkeypatch.setattr(covers_module, "extract_cover", slow_cover)
    monkeypatch.setattr(covers_module, "extract_from_directory", slow_cover)

    await asyncio.gather(*(ctx.covers.for_album(i) for i in range(1, 9)))

    assert peak <= covers_module.COVER_EXTRACT_CONCURRENCY + 1, (
        f"пик параллельных чтений {peak}, лимит "
        f"{covers_module.COVER_EXTRACT_CONCURRENCY}"
    )


async def test_missing_cover_is_remembered(ctx, music_dir, monkeypatch):
    """Альбом без картинки не перечитывается повторно."""
    from tests.conftest import make_mp3, tag_mp3

    path = make_mp3(music_dir / "one" / "t.mp3", seconds=0.2)
    tag_mp3(path, title="T", artist="A", album="Alb")
    await ctx.scanner.scan(mode="full")

    calls = 0

    def counting_cover(path):
        nonlocal calls
        calls += 1
        return None

    monkeypatch.setattr(covers_module, "extract_cover", counting_cover)
    monkeypatch.setattr(covers_module, "extract_from_directory", counting_cover)

    first = await ctx.covers.for_album(1)
    after_first = calls
    second = await ctx.covers.for_album(1)

    assert first is None and second is None
    assert after_first > 0
    # Второй заход не должен трогать диск вовсе.
    assert calls == after_first, "обложка искалась заново, хотя её точно нет"


async def test_forget_missing_allows_new_lookup(ctx, music_dir, monkeypatch):
    """После очистки кеша обложки ищутся заново."""
    from tests.conftest import make_mp3, tag_mp3

    path = make_mp3(music_dir / "one" / "t.mp3", seconds=0.2)
    tag_mp3(path, title="T", artist="A", album="Alb")
    await ctx.scanner.scan(mode="full")

    def no_cover(path):
        return None

    monkeypatch.setattr(covers_module, "extract_cover", no_cover)
    monkeypatch.setattr(covers_module, "extract_from_directory", no_cover)
    await ctx.covers.for_album(1)

    calls = 0

    def counting(path):
        nonlocal calls
        calls += 1
        return None

    monkeypatch.setattr(covers_module, "extract_cover", counting)
    monkeypatch.setattr(covers_module, "extract_from_directory", counting)

    ctx.covers.forget_missing()
    await ctx.covers.for_album(1)

    assert calls > 0, "после сброса кеша поиск не повторился"
