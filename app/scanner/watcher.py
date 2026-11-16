"""Наблюдение за каталогом библиотеки и пакетная индексация.

Схема:

    watchfiles.awatch(music_dir, force_polling=...)
        │  фильтр: только аудиофайлы и каталоги, без служебных
        ▼
    очередь изменённых путей (set — дедупликация)
        │  пакет по таймеру или по накоплению
        ▼
    Scanner.scan(mode="incremental", paths=<пакет>)

Почему так:

* `watchfiles` (Rust) даёт inotify на нативных томах и опрос на
  Windows-монтированиях (`force_polling`). Код не меняется, меняется флаг.
* События приходят из ВЛОЖЕННЫХ подпапок (проверено). Но debounce может
  схлопнуть часть файлов альбома: `watchfiles` отдаёт то, что увидел за окно.
  Поэтому при событии сканер обходит поддерево каталога целиком, а не только
  названные файлы.
* Страховочный полный инкрементальный скан по `MTR_SCAN_INTERVAL` закрывает
  всё, что watcher всё же пропустил.

Фильтр по содержимому намеренно простой: расширение из `SUPPORTED_EXTENSIONS`
и `SKIP_DIR_NAMES`. Отдельная зависимость `pathspec` не нужна.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from app.scanner.library_scanner import SKIP_DIR_NAMES, ScanResult, Scanner
from app.scanner.tag_parser import SUPPORTED_EXTENSIONS, is_supported_audio

logger = logging.getLogger(__name__)

# Окно склейки событий. Копирование альбома даёт десятки событий за секунды.
DEBOUNCE_MS = 2000
# Сколько ждать, прежде чем сбросить накопленную очередь одним сканом.
FLUSH_DELAY_SEC = 2.0
# Потолок очереди: при массовой копии лучше полный скан, чем тысячи путей.
MAX_QUEUE = 2000


def _is_relevant(path: str) -> bool:
    """Аудиофайл или каталог вне служебных — только такие события нужны.

    Каталоги релевантны: в них мог быть скопирован целый альбом, и тогда
    сканер обойдёт поддерево рекурсивно.

    Файлы с неаудио-расширением (обложки, .txt, .m3u8) отбрасываются ДО
    любых проверок существования: событие может относиться к уже удалённому
    файлу, и `not p.exists()` не должен делать его «каталогом».
    """
    p = Path(path)
    for part in p.parts[:-1]:
        if part in SKIP_DIR_NAMES or part.startswith("."):
            return False
    if p.name.startswith("."):
        return False

    suffix = p.suffix.lower()
    if suffix:
        # Расширение есть: это файл. Аудио — берём, прочее — нет.
        return suffix in SUPPORTED_EXTENSIONS and is_supported_audio(path)

    # Расширения нет. Существующий каталог — берём; несуществующий тоже
    # (каталог мог быть удалён, и его треки надо пометить отсутствующими).
    try:
        if p.is_dir():
            return True
    except OSError:
        pass
    return True


@dataclass
class PendingQueue:
    """Очередь путей с дедупликацией. Потокобезопасна через asyncio.

    Путь хранится как строка. Перед сканом вложенные пути поглощаются
    родительскими — см. Scanner._scope_roots, поэтому здесь дубликаты
    по иерархии не схлопываем, это делает сканер.
    """

    _items: set[str] = field(default_factory=set)
    overflow: bool = False

    def add(self, paths: Iterable[str]) -> int:
        added = 0
        for raw in paths:
            if len(self._items) >= MAX_QUEUE:
                self.overflow = True
                break
            if raw not in self._items:
                self._items.add(raw)
                added += 1
        return added

    def drain(self) -> tuple[list[str], bool]:
        """Забирает очередь целиком. Возвращает (пути, нужен_полный_скан)."""
        items = sorted(self._items)
        overflow = self.overflow
        self._items.clear()
        self.overflow = False
        return items, overflow

    def __len__(self) -> int:
        return len(self._items)


class LibraryWatcher:
    """Фоновая задача: следит за каталогом и запускает точечные сканы."""

    def __init__(
        self,
        scanner: Scanner,
        music_root: str | Path,
        *,
        force_polling: bool,
        scan_interval_sec: int,
        poll_delay_ms: int = 1000,
    ) -> None:
        self._scanner = scanner
        self._root = Path(music_root)
        self._force_polling = force_polling
        self._scan_interval = max(0, int(scan_interval_sec))
        self._poll_delay_ms = max(100, int(poll_delay_ms))
        self._queue = PendingQueue()
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._last_result: ScanResult | None = None
        self._state = "idle"

    # Публичное состояние (для Subsonic getScanStatus и отладки)
    @property
    def state(self) -> str:
        return self._state

    @property
    def pending(self) -> int:
        return len(self._queue)

    @property
    def last_result(self) -> ScanResult | None:
        return self._last_result

    def set_interval(self, seconds: int) -> None:
        """Меняет интервал страховочного скана без перезапуска.

        Настройка приходит из веб-интерфейса. Если интервал включили с нуля,
        задача таймера могла не стартовать — запускаем её на ходу.
        """
        new_value = max(0, int(seconds))
        if new_value == self._scan_interval:
            return
        self._scan_interval = new_value
        logger.info("интервал скана изменён на %s с", new_value)
        if not self._tasks:
            return
        has_timer = any(task.get_name() == "mtr-interval" for task in self._tasks)
        if new_value > 0 and not has_timer:
            self._tasks.append(
                asyncio.create_task(self._interval_loop(), name="mtr-interval")
            )
        elif new_value == 0 and has_timer:
            for task in list(self._tasks):
                if task.get_name() == "mtr-interval":
                    task.cancel()
                    self._tasks.remove(task)

    # Жизненный цикл
    async def start(self) -> None:
        if self._tasks:
            return
        self._stop.clear()
        self._state = "watching"
        self._tasks.append(asyncio.create_task(self._watch_loop(), name="mtr-watch"))
        self._tasks.append(asyncio.create_task(self._flush_loop(), name="mtr-flush"))
        if self._scan_interval > 0:
            self._tasks.append(
                asyncio.create_task(self._interval_loop(), name="mtr-interval")
            )
        logger.info(
            "наблюдение за %s запущено (polling=%s, интервал=%ss)",
            self._root, self._force_polling, self._scan_interval,
        )

    async def stop(self) -> None:
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()
        self._state = "stopped"
        logger.info("наблюдение остановлено")

    # Циклы
    async def _watch_loop(self) -> None:
        """Читает события watchfiles, фильтрует и кладёт в очередь.

        Импорт внутри функции: если watchfiles недоступен, приложение
        стартует, а наблюдение честно сообщает об ошибке в состоянии.
        """
        try:
            import watchfiles
        except ImportError:
            logger.error("watchfiles не установлен — наблюдение отключено")
            self._state = "error: watchfiles not installed"
            return

        try:
            async for changes in watchfiles.awatch(
                self._root,
                debounce=DEBOUNCE_MS,
                recursive=True,
                force_polling=self._force_polling,
                poll_delay_ms=self._poll_delay_ms,
                stop_event=self._stop,
                ignore_permission_denied=True,
            ):
                relevant = [path for _, path in changes if _is_relevant(path)]
                if relevant:
                    added = self._queue.add(relevant)
                    if added:
                        logger.debug("в очередь добавлено: %d", added)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - наблюдатель не должен ронять сервер
            logger.exception("ошибка наблюдателя: %s", exc)
            self._state = f"error: {exc}"

    async def _flush_loop(self) -> None:
        """Раз в FLUSH_DELAY_SEC забирает очередь и запускает точечный скан."""
        while not self._stop.is_set():
            try:
                await asyncio.sleep(FLUSH_DELAY_SEC)
            except asyncio.CancelledError:
                raise
            if len(self._queue) == 0 and not self._queue.overflow:
                continue
            await self._flush_once()

    async def _interval_loop(self) -> None:
        """Страховочный полный инкрементальный скан по расписанию."""
        while not self._stop.is_set():
            try:
                await asyncio.sleep(self._scan_interval)
            except asyncio.CancelledError:
                raise
            logger.info("плановый инкрементальный скан (MTR_SCAN_INTERVAL)")
            self._last_result = await self._scanner.scan(mode="incremental")

    async def _flush_once(self) -> None:
        paths, overflow = self._queue.drain()
        if overflow:
            # Очередь переполнилась: точечный режим уже не покрывает изменения.
            logger.warning("очередь переполнена — запускаю полный инкрементальный скан")
            self._state = "scanning (overflow)"
            self._last_result = await self._scanner.scan(mode="incremental")
        elif paths:
            self._state = f"scanning {len(paths)} path(s)"
            self._last_result = await self._scanner.scan(
                mode="incremental", paths=paths
            )
        self._state = "watching"


def is_within_root(path: str, root: Path) -> bool:
    """Защита: событие не должно вывести сканер за пределы библиотеки."""
    try:
        Path(os.path.realpath(path)).relative_to(os.path.realpath(root))
        return True
    except ValueError:
        return False
