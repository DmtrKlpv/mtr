"""Тесты прогресса сканирования: счётчик идёт во время разбора, а не после записи."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from app.db.database import Database
from app.db.migrations import _migrate_music
from app.repositories.tracks import TrackRepository
from app.scanner import library_scanner as scanner_mod
from app.scanner.library_scanner import Scanner
from tests.conftest import make_mp3, tag_mp3


async def _db(path: Path) -> Database:
    db = Database(path, label="music")
    await db.connect()
    await _migrate_music(db)
    return db


def _library(music: Path, count: int) -> None:
    for i in range(count):
        path = make_mp3(music / f"al{i % 4}" / f"{i}.mp3", seconds=0.15)
        tag_mp3(path, title=f"T{i}", artist=f"A{i % 3}", album=f"Alb{i % 4}")


async def test_progress_goes_before_all_files_are_parsed(tmp_path: Path):
    """К первому событию разобрана часть файлов, а не все: иначе панель стоит на нуле."""
    music = tmp_path / "music"
    files = 60
    _library(music, files)

    parses = 0
    progress: list[tuple[int, int]] = []

    db = await _db(tmp_path / "m.db")
    try:
        scanner = Scanner(TrackRepository(db), music, parse_concurrency=1)
        parse_file = scanner._parse_file

        async def counting_parse(path: str, counters):
            nonlocal parses
            try:
                return await parse_file(path, counters)
            finally:
                parses += 1

        def on_progress(event: dict) -> None:
            if event.get("stage") == "parsing":
                progress.append((int(event["processed"]), parses))

        scanner._parse_file = counting_parse
        scanner._on_progress = on_progress
        # Один заход на всю библиотеку: именно так выглядел баг на 37 812 файлах.
        scanner.set_batch_size(files)
        result = await scanner.scan(mode="full")
        assert result.counters.failed == 0
    finally:
        await db.close()

    assert progress, "событий разбора нет"
    processed = [p for p, _ in progress]
    # Счётчик монотонен и доходит до конца.
    assert processed == sorted(processed), progress
    assert processed[-1] == files, progress
    # Ключевое: первое событие приходит, когда разобрана лишь часть файлов.
    first_processed, parsed_at_first = progress[0]
    assert first_processed < files, progress
    assert parsed_at_first < files, f"к первому событию разобрано всё: {progress}"


async def test_default_setting_stores_in_several_batches(tmp_path: Path, monkeypatch):
    """Настройка «0» режет запись на заходы по SCAN_BATCH, а не одной транзакцией."""
    music = tmp_path / "music"
    files = 30
    _library(music, files)
    monkeypatch.setattr(scanner_mod, "SCAN_BATCH", 7)

    batches = 0
    db = await _db(tmp_path / "m2.db")
    try:
        repo = TrackRepository(db)
        batch = repo.batch

        def counting_batch():
            nonlocal batches
            batches += 1
            return batch()

        repo.batch = counting_batch
        scanner = Scanner(repo, music, parse_concurrency=2)
        assert scanner._batch_size == 0  # настройка «авто»
        await scanner.scan(mode="full")
    finally:
        await db.close()

    # 30 файлов по 7 в заходе — 5 транзакций, у старого кода была бы одна.
    assert batches == 5, batches


async def test_final_progress_event_is_single(tmp_path: Path):
    """Итоговое значение приходит один раз: панель не дёргается в конце."""
    music = tmp_path / "music"
    _library(music, 25)

    progress: list[int] = []
    db = await _db(tmp_path / "m3.db")
    try:
        scanner = Scanner(TrackRepository(db), music, parse_concurrency=2)
        scanner._on_progress = lambda e: (
            progress.append(int(e["processed"]))
            if e.get("stage") == "parsing" else None
        )
        await scanner.scan(mode="full")
    finally:
        await db.close()

    assert progress[-1] == 25, progress
    assert progress.count(25) == 1, progress


async def test_progress_moves_per_file_when_parsing_is_slow(tmp_path: Path):
    """Медленный разбор: каждый файл сдвигает счётчик, а не ждёт накопления 25 штук."""
    music = tmp_path / "music"
    files = 4
    _library(music, files)

    progress: list[int] = []
    db = await _db(tmp_path / "m3.db")
    try:
        scanner = Scanner(TrackRepository(db), music, parse_concurrency=1)
        parse_file = scanner._parse_file

        async def slow_parse(path: str, counters):
            await asyncio.sleep(0.3)
            return await parse_file(path, counters)

        def on_progress(event: dict) -> None:
            if event.get("stage") == "parsing":
                progress.append(int(event["processed"]))

        scanner._parse_file = slow_parse
        scanner._on_progress = on_progress
        result = await scanner.scan(mode="full")
        assert result.counters.failed == 0
    finally:
        await db.close()

    # Сторож может повторить уже отправленное значение: сравниваем шаги без повторов.
    steps = [value for i, value in enumerate(progress) if i == 0 or value != progress[i - 1]]
    assert steps == [1, 2, 3, 4], progress


async def test_watchdog_reports_while_first_file_is_slow(tmp_path: Path):
    """Сторож отдаёт события, пока ни один файл ещё не вернулся.

    Раньше прогресс отправлялся только после возвращения файла: один
    медленный файл на сетевом диске держал панель на «0 из N».
    """
    music = tmp_path / "music"
    _library(music, 3)

    events: list[dict] = []
    db = await _db(tmp_path / "m4.db")
    try:
        scanner = Scanner(TrackRepository(db), music, parse_concurrency=1)
        parse_file = scanner._parse_file

        async def slow_parse(path: str, counters):
            await asyncio.sleep(2.0)
            return await parse_file(path, counters)

        def on_progress(event: dict) -> None:
            if event.get("stage") == "parsing":
                events.append(dict(event))

        scanner._parse_file = slow_parse
        scanner._on_progress = on_progress
        scanner_mod.WATCHDOG_INTERVAL_SEC = 0.05

        task = asyncio.create_task(scanner.scan(mode="full"))
        await asyncio.sleep(0.4)
        # Файл ещё читается, но события уже идут и видно, что работа кипит.
        assert len(events) >= 2, events
        assert all(event["processed"] == 0 for event in events)
        assert any(event.get("in_flight") for event in events), events
        assert all(event["total"] == 3 for event in events)
        await task
    finally:
        scanner_mod.WATCHDOG_INTERVAL_SEC = 1.0
        await db.close()


async def test_parse_timeout_skips_file_and_scan_continues(tmp_path: Path):
    """Файл, который не читается за отведённое время, не держит скан."""
    music = tmp_path / "music"
    _library(music, 2)

    real_parse = scanner_mod.parse_track
    stuck_name = "0.mp3"

    def hanging_parse(path, **kwargs):
        # Сетевой диск: файл открывается, но не отдаётся.
        if Path(path).name == stuck_name:
            time.sleep(1.0)
        return real_parse(path, **kwargs)

    db = await _db(tmp_path / "m5.db")
    try:
        scanner = Scanner(TrackRepository(db), music, parse_concurrency=1)
        scanner_mod.parse_track = hanging_parse
        scanner._parse_timeout_sec = 0.2
        result = await scanner.scan(mode="full")
        assert result.status == "completed"
        assert result.counters.failed == 1, result.counters.failed
        assert any("таймаут" in err.reason for err in result.counters.errors), [
            err.reason for err in result.counters.errors
        ]
        # Второй файл всё равно попал в библиотеку.
        assert result.counters.added == 1, result.counters
    finally:
        scanner_mod.parse_track = real_parse
        await db.close()


async def test_slow_files_listed_while_reading(tmp_path: Path):
    """Долгий файл виден по имени: понятно, на чём именно стоит разбор."""
    music = tmp_path / "music"
    _library(music, 2)

    slow_seen: list[list[dict]] = []
    db = await _db(tmp_path / "m6.db")
    try:
        scanner = Scanner(TrackRepository(db), music, parse_concurrency=1)
        parse_file = scanner._parse_file

        async def slow_parse(path: str, counters):
            await asyncio.sleep(0.6)
            return await parse_file(path, counters)

        def on_progress(event: dict) -> None:
            if event.get("stage") == "parsing" and event.get("slow"):
                slow_seen.append(list(event["slow"]))

        scanner._parse_file = slow_parse
        scanner._on_progress = on_progress
        scanner_mod.SLOW_FILE_SEC = 0.1
        scanner_mod.WATCHDOG_INTERVAL_SEC = 0.05
        await scanner.scan(mode="full")
    finally:
        scanner_mod.SLOW_FILE_SEC = 5.0
        scanner_mod.WATCHDOG_INTERVAL_SEC = 1.0
        await db.close()

    assert slow_seen, "долгие файлы ни разу не попали в событие"
    item = slow_seen[0][0]
    assert item["path"].endswith(".mp3")
    assert item["sec"] >= 0.1
