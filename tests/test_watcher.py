"""Тесты этапа 5: точечный скан, фильтр событий, очередь, рекурсия.

Файловый наблюдатель здесь не проверяем через реальные события ОС —
это медленно и флакает. Вместо этого:
  * точечный скан проверяем напрямую (`Scanner.scan(paths=...)`);
  * фильтр и очередь — чистыми функциями;
  * сам цикл watchfiles — одним интеграционным тестом с жёстким таймаутом.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from app.scanner.watcher import (
    LibraryWatcher,
    PendingQueue,
    _is_relevant,
    is_within_root,
)
from tests.conftest import make_mp3, tag_mp3


# Фильтр событий
@pytest.mark.parametrize(
    "path,expected",
    [
        ("/music/A/B/01.mp3", True),
        ("/music/A/B/01.FLAC", True),
        ("/music/A/B/cover.jpg", False),
        ("/music/A/B/readme.txt", False),
        ("/music/A/B/list.m3u8", False),
        ("/music/A/.hidden.mp3", False),
        ("/music/.git/x.mp3", False),
        ("/music/__pycache__/x.mp3", False),
        ("/music/@eaDir/x.mp3", False),
        ("/music/A/B", True),          # каталог релевантен
        ("/music/A/Album", True),
    ],
)
def test_relevance_filter(path, expected):
    assert _is_relevant(path) is expected


def test_is_within_root_blocks_escape(tmp_path):
    root = tmp_path / "music"
    root.mkdir()
    assert is_within_root(str(root / "a.mp3"), root)
    assert not is_within_root("/etc/passwd", root)


# Очередь
def test_queue_deduplicates():
    q = PendingQueue()
    assert q.add(["a", "a", "b"]) == 2
    assert len(q) == 2


def test_queue_drain_empties_and_returns_sorted():
    q = PendingQueue()
    q.add(["z", "a", "m"])
    paths, overflow = q.drain()
    assert paths == ["a", "m", "z"]
    assert overflow is False
    assert len(q) == 0


def test_queue_overflow_flag(monkeypatch):
    import app.scanner.watcher as w

    monkeypatch.setattr(w, "MAX_QUEUE", 3)
    q = PendingQueue()
    q.add([str(i) for i in range(10)])
    assert len(q) == 3
    _, overflow = q.drain()
    assert overflow is True


# Точечный скан: рекурсия и изоляция
async def test_partial_scan_recurses_into_nested_subfolders(ctx, music_dir: Path):
    """Событие на каталог Artist обходит ВСЕ вложенные подпапки."""
    deep = music_dir / "Artist" / "Album" / "CD1"
    path = make_mp3(deep / "01.mp3")
    tag_mp3(path, title="Deep", artist="Artist", album="Album")
    make_mp3(music_dir / "Artist" / "Album" / "CD2" / "01.mp3")

    result = await ctx.scanner.scan(
        mode="incremental", paths=[str(music_dir / "Artist")]
    )
    assert result.counters.added == 2
    assert result.status == "completed"


async def test_partial_scan_does_not_touch_other_subtrees(ctx, music_dir: Path):
    """Точечный скан Artist не индексирует и не трогает Other."""
    make_mp3(music_dir / "Artist" / "a.mp3")
    make_mp3(music_dir / "Other" / "b.mp3")

    result = await ctx.scanner.scan(
        mode="incremental", paths=[str(music_dir / "Artist")]
    )
    assert result.counters.added == 1
    titles = {t.path for t in await ctx.tracks.list_tracks()}
    assert str(music_dir / "Other" / "b.mp3") not in titles


async def test_partial_scan_marks_missing_only_inside_scope(ctx, music_dir: Path):
    """Удалённый файл помечается, но трек в другом поддереве — нет."""
    keep = make_mp3(music_dir / "Keep" / "k.mp3")
    gone = make_mp3(music_dir / "Scope" / "g.mp3")
    await ctx.scanner.scan(mode="full")

    gone.unlink()
    result = await ctx.scanner.scan(
        mode="incremental", paths=[str(music_dir / "Scope")]
    )
    assert result.counters.removed == 1

    still = await ctx.tracks.get_by_path(str(keep))
    assert still is not None and still.missing is False


async def test_partial_scan_ignores_path_outside_root(ctx, music_dir: Path, tmp_path):
    """Путь вне библиотеки отбрасывается — сканер не выходит за корень."""
    outside = tmp_path / "outside"
    outside.mkdir()
    make_mp3(outside / "x.mp3")

    result = await ctx.scanner.scan(mode="incremental", paths=[str(outside)])
    assert result.counters.seen == 0


async def test_partial_scan_collapses_nested_paths(ctx, music_dir: Path):
    """Вложенные пути поглощаются родительским — каталог обходится один раз."""
    make_mp3(music_dir / "A" / "B" / "x.mp3")
    result = await ctx.scanner.scan(
        mode="incremental",
        paths=[str(music_dir / "A"), str(music_dir / "A" / "B")],
    )
    assert result.counters.added == 1


async def test_partial_scan_on_file_uses_parent_dir(ctx, music_dir: Path):
    """Событие на файл даёт сканирование его каталога с соседями."""
    make_mp3(music_dir / "Album" / "1.mp3")
    make_mp3(music_dir / "Album" / "2.mp3")
    result = await ctx.scanner.scan(
        mode="incremental", paths=[str(music_dir / "Album" / "1.mp3")]
    )
    assert result.counters.added == 2


async def test_partial_scan_on_removed_directory_marks_tracks(ctx, music_dir: Path):
    """Удалённый каталог: его треки помечаются отсутствующими."""
    import shutil

    make_mp3(music_dir / "Gone" / "a.mp3")
    make_mp3(music_dir / "Gone" / "b.mp3")
    await ctx.scanner.scan(mode="full")

    shutil.rmtree(music_dir / "Gone")
    result = await ctx.scanner.scan(
        mode="incremental", paths=[str(music_dir / "Gone")]
    )
    assert result.counters.removed == 2


async def test_full_scan_still_works_without_paths(ctx, music_dir: Path):
    """Регрессия: обычный полный проход не изменился."""
    make_mp3(music_dir / "x" / "a.mp3")
    result = await ctx.scanner.scan(mode="full")
    assert result.counters.added == 1


# Наблюдатель: жизненный цикл
async def test_watcher_start_stop_is_clean(ctx, music_dir: Path):
    watcher = LibraryWatcher(
        ctx.scanner, music_dir, force_polling=True,
        scan_interval_sec=0, poll_delay_ms=200,
    )
    await watcher.start()
    assert watcher.state in ("watching",) or watcher.state.startswith("error")
    await asyncio.sleep(0.2)
    await watcher.stop()
    assert watcher.state == "stopped"


async def test_watcher_detects_new_file_end_to_end(ctx, music_dir: Path):
    """Полный путь: событие ФС -> очередь -> точечный скан -> трек в БД.

    Жёсткий таймаут: если события не пришли — тест падает, а не висит.
    """
    watcher = LibraryWatcher(
        ctx.scanner, music_dir, force_polling=True,
        scan_interval_sec=0, poll_delay_ms=200,
    )
    await watcher.start()
    try:
        await asyncio.sleep(0.5)
        target = music_dir / "Live" / "new.mp3"
        make_mp3(target)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if await ctx.tracks.get_by_path(str(target)):
                break
            await asyncio.sleep(0.3)
        found = await ctx.tracks.get_by_path(str(target))
        assert found is not None, f"трек не появился за 15 с, состояние: {watcher.state}"
    finally:
        await watcher.stop()
