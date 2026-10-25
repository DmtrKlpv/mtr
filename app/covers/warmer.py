"""Фоновый прогрев обложек: один альбом за раз, с уступкой и отменой.

Зачем. Синхронный прогрев внутри HTTP-запроса на слабом NAS идёт около
получаса: прокси обрывает запрос, прогресса не видно, отменить нельзя, а
второй запуск стартует параллельно первому. Здесь прогрев — отдельная задача
сервера, которая:

* обрабатывает по одному альбому и между альбомами делает паузу;
* ждёт, пока пользователь не перестанет запрашивать обложки и пока не
  освободятся слоты транскодирования (стриминг важнее прогрева);
* можно остановить в любой момент и запустить заново — уже готовые альбомы
  `pending_album_ids` пропускает, поэтому прогрев продолжается с места остановки.

Состояние держится в памяти процесса. После перезапуска контейнера прогрев
нужно запустить снова, но готовые превью никуда не деваются.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# Пауза между альбомами: отдаёт CPU и event loop другим запросам.
DEFAULT_PAUSE_SEC = 0.05
# Как часто проверять, не освободился ли сервер.
IDLE_POLL_SEC = 0.25


class _Covers(Protocol):
    foreground_busy: bool

    async def pending_album_ids(self, limit: int | None = None) -> list[int]: ...

    async def for_album(
        self, album_id: int, *, background: bool = False
    ) -> Any: ...



class _Limiter(Protocol):
    @property
    def active(self) -> int: ...


@dataclass
class WarmStatus:
    state: str = "idle"  # idle | running | waiting | done | cancelled | error
    total: int = 0
    done: int = 0
    made: int = 0
    absent: int = 0
    failed: int = 0
    current_album: int | None = None
    started_at: float | None = None
    finished_at: float | None = None
    last_error: str | None = None
    yielded: int = field(default=0)  # сколько раз уступали пользователю

    def as_dict(self) -> dict[str, Any]:
        elapsed = None
        if self.started_at is not None:
            end = self.finished_at or time.monotonic()
            elapsed = round(end - self.started_at, 1)
        remaining = max(0, self.total - self.done)
        per_album = (
            (elapsed / self.done) if elapsed and self.done else None
        )
        eta = round(remaining * per_album) if per_album else None
        return {
            "state": self.state,
            "total": self.total,
            "done": self.done,
            "made": self.made,
            "absent": self.absent,
            "failed": self.failed,
            "remaining": remaining,
            "current_album": self.current_album,
            "elapsed_sec": elapsed,
            "eta_sec": eta,
            "yielded": self.yielded,
            "last_error": self.last_error,
        }


class CoverWarmer:
    """Одна задача прогрева на процесс."""

    def __init__(
        self,
        covers: _Covers,
        limiter: _Limiter | None = None,
        *,
        pause_sec: float = DEFAULT_PAUSE_SEC,
        idle_poll_sec: float = IDLE_POLL_SEC,
    ) -> None:
        self._covers = covers
        self._limiter = limiter
        self._pause = max(0.0, pause_sec)
        self._idle_poll = max(0.01, idle_poll_sec)
        self._task: asyncio.Task | None = None
        self._cancel = asyncio.Event()
        self.status = WarmStatus()

    # ------------------------------------------------------------------
    # управление
    # ------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self, limit: int | None = None) -> bool:
        """Запускает прогрев. False — если уже идёт."""
        if self.running:
            return False
        self._cancel.clear()
        self.status = WarmStatus(state="running", started_at=time.monotonic())
        self._task = asyncio.create_task(self._run(limit), name="mtr-cover-warm")
        logger.info("прогрев обложек запущен (лимит: %s)", limit or "все")
        return True

    async def cancel(self) -> bool:
        """Останавливает прогрев и ждёт, пока задача завершится."""
        if not self.running:
            return False
        self._cancel.set()
        assert self._task is not None
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        return True

    async def shutdown(self) -> None:
        """Вызывается при остановке сервера."""
        await self.cancel()

    # ------------------------------------------------------------------
    # рабочая часть
    # ------------------------------------------------------------------
    def _busy(self) -> bool:
        """Нужно ли уступить: пользователь ждёт обложку или идёт транскод."""
        if self._covers.foreground_busy:
            return True
        return self._limiter is not None and self._limiter.active > 0

    async def _wait_until_idle(self) -> bool:
        """Ждёт освобождения сервера. False — если пришла отмена."""
        announced = False
        while self._busy():
            if self._cancel.is_set():
                return False
            if not announced:
                self.status.state = "waiting"
                self.status.yielded += 1
                announced = True
            await asyncio.sleep(self._idle_poll)
        if announced and not self._cancel.is_set():
            self.status.state = "running"
        return not self._cancel.is_set()

    async def _run(self, limit: int | None) -> None:
        try:
            ids = await self._covers.pending_album_ids(limit)
            self.status.total = len(ids)
            for album_id in ids:
                if self._cancel.is_set():
                    break
                if not await self._wait_until_idle():
                    break

                self.status.current_album = album_id
                try:
                    cover = await self._covers.for_album(album_id, background=True)
                except Exception as exc:  # noqa: BLE001 - один альбом не валит прогрев
                    logger.exception("прогрев: ошибка на альбоме %d", album_id)
                    self.status.failed += 1
                    self.status.last_error = f"альбом {album_id}: {exc}"
                else:
                    if cover is None:
                        self.status.absent += 1
                    else:
                        self.status.made += 1
                self.status.done += 1

                if self._pause:
                    await asyncio.sleep(self._pause)

            self.status.current_album = None
            self.status.finished_at = time.monotonic()
            if self._cancel.is_set():
                self.status.state = "cancelled"
                logger.info("прогрев обложек остановлен на %d из %d",
                            self.status.done, self.status.total)
            else:
                self.status.state = "done"
                logger.info(
                    "прогрев обложек завершён: готово %d, без картинки %d, ошибок %d",
                    self.status.made, self.status.absent, self.status.failed,
                )
        except asyncio.CancelledError:
            self.status.state = "cancelled"
            self.status.finished_at = time.monotonic()
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("прогрев обложек упал")
            self.status.state = "error"
            self.status.last_error = str(exc)
            self.status.finished_at = time.monotonic()
