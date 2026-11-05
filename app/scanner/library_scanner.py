"""Ядро индексирования медиатеки.

Этап 1 покрывает синхронный обход библиотеки (полный и инкрементальный).
Фоновый watchdog и WebSocket-прогресс — этап 5; здешний Scanner построен
так, чтобы их можно было добавить без переписывания: он отдаёт события
через колбэк on_progress, а не печатает в лог сам.

Инкрементальная логика:
    * собираем снимок файлов на диске (path -> size, mtime);
    * сравниваем с картой из БД по ПУТИ (см. TrackRepository.known_paths);
    * новые и изменённые файлы разбираем mutagen-ом;
    * отсутствующие в снимке помечаем missing=1, но не удаляем.

Парсер вызывается в thread pool: mutagen — блокирующий ввод-вывод, и
вешать на нём event loop нельзя.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from app.repositories.tracks import (
    ScanCounters,
    TrackRepository,
)
from app.scanner.tag_parser import is_supported_audio, parse_track

logger = logging.getLogger(__name__)

# Каталоги, которые никогда не индексируем.
SKIP_DIR_NAMES: Final[frozenset[str]] = frozenset(
    {
        ".git", ".svn", "__pycache__", "node_modules", "@eaDir",
        "#recycle", "#snapshot", ".Trash", ".Trashes", "lost+found",
        "System Volume Information", "$RECYCLE.BIN",
    }
)

# Сколько файлов разбирать параллельно. mutagen — CPU+I/O, но диски
# (особенно сетевые) не любят десятки параллельных чтений.
PARSE_CONCURRENCY = 4
# Размер батча для периодического прогресса.
PROGRESS_EVERY = 25
# Файлов в одной транзакции записи. Меньше — чаще commit (медленнее, но короче блокировка).
SCAN_BATCH = 100
# Сколько ошибок отдавать в ответе на запуск скана (в историю пишется больше).
MAX_API_ERRORS = 20

ProgressCallback = Callable[[dict[str, object]], Awaitable[None] | None]


@dataclass(slots=True)
class ScanResult:
    """Итог прохода — отдаётся в API и пишется в scan_runs."""

    run_id: int
    mode: str
    counters: ScanCounters
    status: str = "completed"
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "mode": self.mode,
            "status": self.status,
            "seen": self.counters.seen,
            "added": self.counters.added,
            "updated": self.counters.updated,
            "unchanged": self.counters.unchanged,
            "removed": self.counters.removed,
            "failed": self.counters.failed,
            "errors": [e.as_dict() for e in self.counters.errors[:MAX_API_ERRORS]],
        }


class Scanner:
    """Обход библиотеки и синхронизация с mtr_music.db."""

    def __init__(
        self,
        repo: TrackRepository,
        music_root: str | Path,
        *,
        on_progress: ProgressCallback | None = None,
        parse_concurrency: int = PARSE_CONCURRENCY,
        on_scan_start: Callable[[str], None] | None = None,
        on_scan_done: Callable[[str], None] | None = None,
    ) -> None:
        self._repo = repo
        self._root = Path(music_root)
        self._on_progress = on_progress
        # Синхронные хуки: вызываются в начале скана (mode) и после успешного
        # завершения. Ошибки в хуке не должны ломать скан.
        self._on_scan_start = on_scan_start
        self._on_scan_done = on_scan_done
        self._lock = asyncio.Lock()
        self._cancelled = False
        # Меняется из настроек: на слабом CPU меньше параллельных чтений даёт
        # более ровную нагрузку, чем дефолтные 4.
        self._parse_concurrency = max(1, int(parse_concurrency))
        # 0 — обрабатывать все файлы одним заходом (прежнее поведение).
        self._batch_size = 0

    # ------------------------------------------------------------------
    # Публичное API
    # ------------------------------------------------------------------
    def cancel(self) -> None:
        """Просит текущий проход остановиться после текущего файла."""
        self._cancelled = True

    def set_batch_size(self, value: int) -> None:
        """Сколько треков брать за один заход (0 — все сразу).

        Заходы идут строго последовательно: следующий начинается после записи
        предыдущего. На большой библиотеке маленькие заходы дают ровный прогресс
        и короткие блокировки БД.
        """
        self._batch_size = max(0, int(value))

    def set_parse_concurrency(self, value: int) -> None:
        """Меняет число одновременных чтений тегов (со страницы настроек)."""
        self._parse_concurrency = max(1, int(value))

    async def scan(
        self,
        *,
        mode: str = "incremental",
        paths: list[str] | None = None,
    ) -> ScanResult:
        """Запускает проход. Параллельные вызовы сериализуются локом.

        `paths` — точечный скан: обходятся только указанные каталоги
        и файлы, ВСЕ вложенные подпапки обходятся рекурсивно. Без `paths`
        обходится весь корень.
        """
        if mode not in ("full", "incremental", "watch"):
            raise ValueError(f"недопустимый режим: {mode!r}")

        if self._on_scan_start is not None:
            self._safe_hook(self._on_scan_start, mode)
        async with self._lock:
            self._cancelled = False
            result = await self._scan_locked(mode, paths=paths)
        if self._on_scan_done is not None:
            self._safe_hook(self._on_scan_done, mode)
        return result

    @staticmethod
    def _safe_hook(hook: Callable[[str], None], mode: str) -> None:
        try:
            hook(mode)
        except Exception:  # noqa: BLE001 - хук не должен валить скан
            logger.exception("хук скана упал")

    # ------------------------------------------------------------------
    # Внутренняя реализация
    # ------------------------------------------------------------------
    async def _scan_locked(
        self, mode: str, *, paths: list[str] | None = None
    ) -> ScanResult:
        counters = ScanCounters()
        scope = "partial" if paths else "full-root"
        run_id = await self._repo.start_scan_run(mode)
        logger.info(
            "скан #%d (%s, %s) начат, корень: %s",
            run_id, mode, scope, self._root,
        )

        try:
            if not self._root.exists():
                raise FileNotFoundError(
                    f"каталог музыки не найден: {self._root}. "
                    f"Проверьте, что volume проброшен в контейнер."
                )

            if paths:
                scoped_roots = self._scope_roots(paths)
                disk_snapshot: dict[str, tuple[int, float]] = {}
                for scoped in scoped_roots:
                    part = await asyncio.to_thread(self._walk, scoped)
                    disk_snapshot.update(part)
            else:
                scoped_roots = [self._root]
                disk_snapshot = await asyncio.to_thread(self._walk, self._root)
            counters.seen = len(disk_snapshot)

            known = await self._repo.known_paths()
            if paths:
                # В частичном проходе «отсутствующими» считаем только
                # треки внутри обойдённых поддеревьев, остальные не трогаем.
                known = {
                    path: meta for path, meta in known.items()
                    if self._under_any(path, scoped_roots)
                }
            to_parse = self._select_changed(disk_snapshot, known, mode)

            await self._emit(
                {
                    "stage": "walk_done",
                    "run_id": run_id,
                    "seen": counters.seen,
                    "to_parse": len(to_parse),
                    "mode": mode,
                }
            )

            await self._parse_batch(to_parse, counters, run_id)

            # Пометка отсутствующих — только в полном проходе. Инкрементальный
            # проходит по подмножеству и не вправе объявлять остальное удалённым.
            # В частичном проходе known уже сужен до обойдённых поддеревьев,
            # поэтому помечаем внутри них: удалённый файл там не виден,
            # а чужие треки вне поддерева не затрагиваются.
            if (mode == "full" or paths) and not self._cancelled:
                gone = [path for path in known if path not in disk_snapshot]
                counters.removed = await self._repo.mark_missing(gone)
                if counters.removed:
                    logger.info("помечено отсутствующими: %d", counters.removed)

            status = "cancelled" if self._cancelled else "completed"
            await self._repo.finish_scan_run(
                run_id, status=status, counters=counters
            )
            logger.info(
                "скан #%d %s: seen=%d added=%d updated=%d unchanged=%d "
                "removed=%d failed=%d",
                run_id, status, counters.seen, counters.added, counters.updated,
                counters.unchanged, counters.removed, counters.failed,
            )
            return ScanResult(
                run_id=run_id, mode=mode, counters=counters, status=status
            )

        except Exception as exc:
            logger.exception("скан #%d провалился", run_id)
            await self._repo.finish_scan_run(
                run_id, status="failed", counters=counters, error=str(exc)
            )
            return ScanResult(
                run_id=run_id,
                mode=mode,
                counters=counters,
                status="failed",
                error=str(exc),
            )

    def _scope_roots(self, paths: list[str]) -> list[Path]:
        """Приводит пути событий к каталогам внутри корня библиотеки.

        Путь вне `music_root` отбрасывается: watchfiles не должен выводить
        сканер за пределы библиотеки, даже если путь пришёл кривым.
        Файл заменяется родительским каталогом — рекурсия всё равно
        захватит его и соседей.
        """
        root = self._root.resolve()
        result: list[Path] = []
        seen: set[Path] = set()
        for raw in paths:
            candidate = Path(raw).resolve()
            try:
                candidate.relative_to(root)
            except ValueError:
                logger.warning("путь вне библиотеки отброшен: %s", raw)
                continue
            if candidate.is_file():
                candidate = candidate.parent
            # Несуществующий каталог (удалён) остаётся в списке: его треки
            # попадут в «отсутствующие» по снимку ниже, а не по ошибке обхода.
            if candidate not in seen:
                seen.add(candidate)
                result.append(candidate)
        # Вложенные каталоги поглощаются родительскими — не обходим дважды.
        result.sort(key=lambda p: len(p.parts))
        pruned: list[Path] = []
        for candidate in result:
            if not any(self._is_within(candidate, parent) for parent in pruned):
                pruned.append(candidate)
        return pruned

    @staticmethod
    def _is_within(child: Path, parent: Path) -> bool:
        try:
            child.relative_to(parent)
            return True
        except ValueError:
            return False

    def _under_any(self, path: str, roots: list[Path]) -> bool:
        target = Path(path)
        return any(self._is_within(target, root) for root in roots)

    @staticmethod
    def _walk(root: Path) -> dict[str, tuple[int, float]]:
        """Синхронный обход: path -> (size, mtime).

        Выполняется в отдельном потоке. Симлинки не разворачиваем, чтобы
        не уйти в цикл; ошибки доступа к отдельным файлам игнорируем.
        """
        snapshot: dict[str, tuple[int, float]] = {}
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            # Мутируем dirnames на месте — os.walk учитывает это при спуске.
            dirnames[:] = [
                d for d in dirnames
                if d not in SKIP_DIR_NAMES and not d.startswith(".")
            ]
            for name in filenames:
                full = os.path.join(dirpath, name)
                if not is_supported_audio(full):
                    continue
                try:
                    stat = os.stat(full)
                except OSError:
                    continue
                if not os.path.isfile(full) or stat.st_size == 0:
                    continue
                snapshot[full] = (int(stat.st_size), float(stat.st_mtime))
        return snapshot

    @staticmethod
    def _select_changed(
        disk: dict[str, tuple[int, float]],
        known: dict[str, tuple[int, int, float, bool]],
        mode: str,
    ) -> list[str]:
        """Отбирает пути, которые надо разобрать.

        full        — все файлы (принудительная переиндексация тегов);
        incremental — новые, изменённые по (size, mtime) и те, что были
                      помечены missing: вернувшийся файл может совпасть
                      по метаданным с прежним и иначе не расчистится.
        """
        if mode == "full":
            return sorted(disk)

        pending: list[str] = []
        for path, (size, mtime) in disk.items():
            previous = known.get(path)
            if previous is None:
                pending.append(path)
                continue
            _, prev_size, prev_mtime, prev_missing = previous
            if prev_missing or prev_size != size or abs(prev_mtime - mtime) >= 1e-6:
                pending.append(path)
        return sorted(pending)

    async def _parse_batch(
        self, paths: list[str], counters: ScanCounters, run_id: int
    ) -> None:
        """Разбирает файлы пачками; запись каждой пачки — одной транзакцией.

        Разбор идёт параллельно (семафор), запись — по одному файлу внутри общей
        транзакции пачки. Каждый файл записывается под своим SAVEPOINT (его даёт
        upsert_from_metadata при вложенной транзакции): ошибка одного файла откатывает
        только его, остальные пачки не теряются. Раньше на каждый трек был свой commit.
        """
        semaphore = asyncio.Semaphore(self._parse_concurrency)
        total = len(paths)
        processed = 0
        # Размер захода: из настроек (0 — все сразу, одним заходом).
        step = self._batch_size if self._batch_size > 0 else max(total, 1)

        for start in range(0, total, step):
            if self._cancelled:
                break
            chunk = paths[start:start + step]

            async def parse_one(path: str):
                async with semaphore:
                    return path, await self._parse_file(path, counters)

            parsed = await asyncio.gather(*(parse_one(p) for p in chunk))

            async with self._repo.batch():
                for path, meta in parsed:
                    if meta is None:
                        processed += 1
                        continue
                    await self._store_file(path, meta, counters)
                    processed += 1
                    if processed % PROGRESS_EVERY == 0 or processed == total:
                        await self._emit_progress(run_id, processed, total, counters)

            if processed == total or processed % PROGRESS_EVERY == 0:
                await self._emit_progress(run_id, processed, total, counters)

    async def _emit_progress(
        self, run_id: int, processed: int, total: int, counters: ScanCounters
    ) -> None:
        await self._emit(
            {
                "stage": "parsing",
                "run_id": run_id,
                "processed": processed,
                "total": total,
                "added": counters.added,
                "updated": counters.updated,
                "failed": counters.failed,
                # Последняя ошибка — чтобы панель показала причину,
                # не дожидаясь конца прохода.
                "last_error": (
                    counters.errors[-1].as_dict() if counters.errors else None
                ),
            }
        )

    async def _parse_file(self, path: str, counters: ScanCounters):
        """Чтение и разбор тегов одного файла (без записи в БД)."""
        try:
            meta = await asyncio.to_thread(parse_track, path)
        except Exception as exc:  # noqa: BLE001 - парсер не должен бросать, но
            counters.add_error(path, f"{type(exc).__name__}: {exc}")
            logger.warning("не удалось разобрать %s: %s", path, exc)
            return None
        if not meta.ok:
            counters.add_error(path, meta.error or "не удалось прочитать теги")
            logger.warning("пропущен файл %s: %s", path, meta.error)
            return None
        return meta

    async def _store_file(self, path: str, meta, counters: ScanCounters):
        """Запись одного файла в текущей транзакции пачки. Ошибка — только этот файл."""
        try:
            outcome = await self._repo.upsert_from_metadata(meta)
        except Exception as exc:  # noqa: BLE001
            counters.add_error(path, f"запись в БД: {exc}")
            logger.exception("ошибка записи трека %s", path)
            return None
        if outcome.action == "added":
            counters.added += 1
        elif outcome.action == "updated":
            counters.updated += 1
        else:
            counters.unchanged += 1
        return outcome

    async def _emit(self, event: dict[str, object]) -> None:
        """Отправляет событие прогресса, если подписчик задан."""
        if self._on_progress is None:
            return
        try:
            result = self._on_progress(event)
            if asyncio.iscoroutine(result):
                await result
        except Exception:  # noqa: BLE001 - сбой UI не должен валить скан
            logger.exception("ошибка обработчика прогресса")
