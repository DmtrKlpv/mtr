"""Фаза обложек после скана: извлечение по альбомам с прогрессом и отменой.

Зачем. После скана у многих альбомов нет записи в `cover_cache`. Чтобы сетка
открывалась быстро, обложки можно извлечь заранее, но так, чтобы это не
вешало систему (NAS на слабом CPU, диск медленный).

Как устроено:
  * альбомы обрабатываются строго по одному, сеть/диск не перегружаются;
  * у каждого альбома есть таймаут: зависший файл не держит фазу бесконечно;
  * пауза между альбомами отдаёт event loop обычным запросам;
  * фаза отменяется при старте нового скана и при остановке сервера;
  * результаты пишутся в `cover_cache` (файл + запись или маркер «нет картинки»),
    поэтому повторная фаза пропускает уже обработанные альбомы.

Фаза не стартует сама при запуске сервера: только после успешного скана.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app.repositories.covers import CoverRepository

logger = logging.getLogger(__name__)

# Сколько ждём один альбом. Больше — считаем ошибкой и идём дальше.
ALBUM_TIMEOUT_SEC = 20.0
# Пауза между альбомами: отдаём event loop и диск обычным запросам.
PAUSE_SEC = 0.05
# Шаг проверки активности пользователя и предельное ожидание альбома.
QUIET_STEP_SEC = 0.5


class CoverPass:
    """Одна фоновая задача на процесс. Состояние — в памяти (для опроса UI)."""

    def __init__(
        self,
        covers: CoverRepository,
        *,
        album_timeout: float = ALBUM_TIMEOUT_SEC,
        pause: float = PAUSE_SEC,
        activity=None,
        max_wait: float | None = None,
    ) -> None:
        self._covers = covers
        # Источник «пользователь сейчас активен». По умолчанию — общий трекер запросов.
        from app.utils.activity import ACTIVITY, MAX_WAIT_SEC

        self._activity = activity or ACTIVITY
        self._max_wait = MAX_WAIT_SEC if max_wait is None else max_wait
        self.paused_for_user = False
        self._timeout = max(1.0, album_timeout)
        self._pause = max(0.0, pause)
        self._task: asyncio.Task | None = None
        self._cancel = False
        self._restart = False
        self.state = "idle"        # idle | running | done | cancelled | error
        self.total = 0
        self.done = 0
        self.made = 0
        self.absent = 0
        self.failed = 0
        self.current_album: int | None = None
        self.last_error: str | None = None
        self._started: float | None = None
        self._finished: float | None = None
        self._album_started: float | None = None

    # ------------------------------------------------------------------
    def set_activity(self, activity) -> None:
        """Источник «пользователь активен»: файл в worker-процессе, трекер в тестах."""
        self._activity = activity

    def set_max_wait(self, seconds: float) -> None:
        """Сколько фаза ждёт одного альбома из-за активности. Меняется без перезапуска."""
        self._max_wait = max(1.0, float(seconds))

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self, mode: str = "incremental") -> bool:
        """Запускает фазу. Вызывается из хука после скана.

        Полный скан снимает маркеры «картинки нет»: теги могли измениться.
        Если фаза уже идёт — просим перезапуск после её выхода.
        """
        if self.running:
            self._restart = True
            return False
        self._cancel = False
        self._restart = False
        self._task = asyncio.get_running_loop().create_task(
            self._run(mode), name="mtr-cover-pass",
        )
        return True

    def cancel_nowait(self) -> None:
        """Просит остановиться после текущего альбома. Не ждёт."""
        self._cancel = True
        self._restart = False

    async def stop(self) -> None:
        """Остановка при выключении сервера."""
        self.cancel_nowait()
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------------------
    async def _run(self, mode: str) -> None:
        self.state = "running"
        self.done = self.made = self.absent = self.failed = 0
        self.current_album = None
        self.last_error = None
        self._started = time.monotonic()
        self._finished = None
        try:
            if mode == "full":
                cleared = await self._covers.clear_absent_markers()
                logger.info("полный скан: снято маркеров отсутствия обложек: %d", cleared)
            pending = await self._covers.pending_album_ids(mode=mode)
            self.total = len(pending)
            for album_id in pending:
                if self._cancel:
                    self.state = "cancelled"
                    break
                await self._wait_for_quiet()
                if self._cancel:
                    self.state = "cancelled"
                    break
                await self._one(album_id)
                await asyncio.sleep(self._pause)
            else:
                self.state = "done"
        except asyncio.CancelledError:
            self.state = "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001 - фаза не должна падать молча
            logger.exception("фаза обложек упала")
            self.state = "error"
            self.last_error = str(exc)
        finally:
            self.current_album = None
            self._finished = time.monotonic()
            self._log_summary()
            if self._restart and not self._cancel:
                self._restart = False
                self.start("incremental")

    async def _wait_for_quiet(self) -> None:
        """Ждёт, пока пользователь не перестанет листать (не дольше MAX_WAIT_SEC).

        Фон уступает запросам из браузера, но не голодает: через лимит идёт дальше.
        """
        waited = 0.0
        while self._activity.busy() and waited < self._max_wait and not self._cancel:
            self.paused_for_user = True
            await asyncio.sleep(QUIET_STEP_SEC)
            waited += QUIET_STEP_SEC
        self.paused_for_user = False

    async def _one(self, album_id: int) -> None:
        self.current_album = album_id
        self._album_started = time.monotonic()
        try:
            cover = await asyncio.wait_for(
                self._covers.for_album(album_id), timeout=self._timeout,
            )
        except asyncio.TimeoutError:
            self.failed += 1
            self.last_error = f"альбом {album_id}: таймаут {self._timeout:.0f} с"
            logger.warning("обложка альбома %d: таймаут", album_id)
        except Exception as exc:  # noqa: BLE001 - один альбом не валит фазу
            self.failed += 1
            self.last_error = f"альбом {album_id}: {exc}"
            logger.warning("обложка альбома %d: %s", album_id, exc)
        else:
            if cover is None:
                self.absent += 1
            else:
                self.made += 1
        finally:
            self.done += 1

    def _log_summary(self) -> None:
        """Одна строка по итогам фазы: что именно значит завершение.

        «done» означает «все альбомы без записи в кеше обработаны» — лимита
        размера кеша нет, поэтому слова про «полный кеш» здесь не используются.
        """
        elapsed = (self._finished - self._started) if self._started else 0.0
        if self.state == "done":
            logger.info(
                "обложки: все альбомы обработаны — с картинкой %d, без картинки %d, "
                "ошибок %d, за %.0f с",
                self.made, self.absent, self.failed, elapsed,
            )
        elif self.state == "cancelled":
            logger.info(
                "обложки: остановлено на %d из %d — с картинкой %d, без картинки %d, ошибок %d",
                self.done, self.total, self.made, self.absent, self.failed,
            )
        elif self.state == "error":
            logger.error("обложки: фаза прервана ошибкой: %s", self.last_error)

    # ------------------------------------------------------------------
    def as_dict(self) -> dict[str, Any]:
        end = self._finished or time.monotonic()
        elapsed = round(end - self._started, 1) if self._started else None
        percent = round(self.done / self.total * 100) if self.total else (
            100 if self.state == "done" else 0
        )
        eta = None
        if elapsed and self.done and self.state == "running":
            eta = round((self.total - self.done) * elapsed / self.done)
        return {
            "state": self.state,
            "total": self.total,
            "done": self.done,
            "made": self.made,
            "absent": self.absent,
            "failed": self.failed,
            "percent": percent,
            "current_album": self.current_album,
            "paused_for_user": self.paused_for_user,
            # Сколько уже занимает текущий альбом: видно, что фаза не встала.
            "current_album_sec": round(time.monotonic() - self._album_started, 1)
            if self.current_album is not None and self._album_started else None,
            "elapsed_sec": elapsed,
            "eta_sec": eta,
            "last_error": self.last_error,
        }
