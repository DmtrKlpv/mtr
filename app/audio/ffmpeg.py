"""Управление процессом ffmpeg для потоковой отдачи.

Гарантии, ради которых модуль написан отдельно:
  * процесс ВСЕГДА завершается: при нормальном конце, ошибке, отмене корутины
    и обрыве соединения клиентом (aclose в finally);
  * stderr пишется во временный файл, а не в PIPE: иначе ffmpeg, заполнив
    буфер ошибок, зависает навсегда (классический deadlock);
  * чтение stdout идёт через to_thread, чтобы блокирующий read не держал event loop;
  * число одновременных процессов ограничено TranscodeLimiter.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import tempfile
from collections.abc import AsyncIterator, Callable

logger = logging.getLogger(__name__)

# 64 КБ: баланс между задержкой первого байта и числом системных вызовов.
CHUNK_SIZE = 64 * 1024
# Сколько ждать завершения после kill.
KILL_WAIT_SEC = 3.0


class TranscodeError(RuntimeError):
    """Не удалось запустить или выполнить ffmpeg."""


class TranscodeBusy(RuntimeError):
    """Достигнут лимит одновременных процессов (ответ 503)."""


class TranscodeLimiter:
    """Неблокирующий счётчик слотов: не ждём, а сразу отказываем.

    Ожидание слота в очереди означало бы, что клиент висит без ответа,
    а плеер решит, что сервер упал. Честный 503 с Retry-After лучше.
    """

    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise ValueError("лимит транскодов должен быть >= 1")
        self._limit = limit
        self._active = 0

    @property
    def active(self) -> int:
        return self._active

    @property
    def limit(self) -> int:
        return self._limit

    def resize(self, limit: int) -> None:
        """Меняет лимит на лету (настройка «Лимит транскодов»).

        Уже запущенные потоки не прерываются: новый лимит влияет только на
        следующие запросы. Если лимит подняли, свободные слоты появятся сразу.
        """
        if limit < 1:
            raise ValueError("лимит транскодов должен быть >= 1")
        self._limit = int(limit)

    def try_acquire(self) -> bool:
        if self._active >= self._limit:
            return False
        self._active += 1
        return True

    def release(self) -> None:
        # Защита от двойного освобождения: aclose может вызываться дважды.
        if self._active > 0:
            self._active -= 1


class TranscodeStream:
    """Один процесс ffmpeg и один потребитель.

    Использование:
        stream = TranscodeStream(argv, on_close=limiter.release)
        stream.start()
        async for chunk in stream.iterate():
            ...
    """

    def __init__(
        self,
        argv: list[str],
        *,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        self._argv = argv
        self._on_close = on_close
        self._proc: subprocess.Popen[bytes] | None = None
        self._stderr = None
        self._closed = False
        self.pid: int | None = None

    # Запуск
    def start(self) -> None:
        """Синхронный запуск. Ошибки превращаются в TranscodeError до ответа клиенту."""
        self._stderr = tempfile.TemporaryFile()
        try:
            self._proc = subprocess.Popen(
                self._argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=self._stderr,
                close_fds=True,
            )
        except FileNotFoundError as exc:
            self._cleanup_failed_start()
            raise TranscodeError("ffmpeg не найден в PATH") from exc
        except OSError as exc:
            self._cleanup_failed_start()
            raise TranscodeError(f"не удалось запустить ffmpeg: {exc}") from exc

        self.pid = self._proc.pid
        logger.debug("ffmpeg запущен pid=%d", self.pid)

    def _cleanup_failed_start(self) -> None:
        if self._stderr is not None:
            self._stderr.close()
            self._stderr = None
        self._fire_on_close()

    # Чтение
    async def iterate(self) -> AsyncIterator[bytes]:
        """Отдаёт байты из stdout. Гарантированно вызывает aclose()."""
        if self._proc is None or self._proc.stdout is None:
            raise TranscodeError("поток не запущен")
        stdout = self._proc.stdout
        try:
            while True:
                # read1 возвращает уже доступные байты, не дожидаясь полного чанка:
                # клиент начинает играть сразу, а не после накопления 64 КБ.
                chunk = await asyncio.to_thread(stdout.read1, CHUNK_SIZE)
                if not chunk:
                    break
                yield chunk
            # Нормальный конец: проверяем код выхода, чтобы не отдать
            # молча обрезанный трек как успешный.
            code = await asyncio.to_thread(self._proc.wait)
            if code != 0:
                logger.error("ffmpeg pid=%s завершился с кодом %s: %s",
                             self.pid, code, self._read_stderr())
        finally:
            await self.aclose()

    # Остановка
    async def aclose(self) -> None:
        """Идемпотентная остановка. Вызывается при любом исходе."""
        if self._closed:
            return
        self._closed = True
        proc = self._proc
        try:
            if proc is not None and proc.poll() is None:
                # Клиент ушёл раньше конца: процесс ещё пишет в закрытый пайп.
                proc.kill()
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(proc.wait), timeout=KILL_WAIT_SEC
                    )
                except asyncio.TimeoutError:
                    logger.error("ffmpeg pid=%s не завершился после kill", self.pid)
            if proc is not None and proc.stdout is not None:
                proc.stdout.close()
        finally:
            if self._stderr is not None:
                self._stderr.close()
                self._stderr = None
            self._fire_on_close()

    def _fire_on_close(self) -> None:
        if self._on_close is not None:
            callback, self._on_close = self._on_close, None
            try:
                callback()
            except Exception:  # noqa: BLE001 - освобождение слота не должно падать
                logger.exception("ошибка в on_close транскода")

    def _read_stderr(self) -> str:
        """Последние байты stderr для лога. Файл, а не PIPE — без deadlock."""
        if self._stderr is None:
            return ""
        try:
            self._stderr.seek(0)
            data = self._stderr.read(4096)
        except OSError:
            return ""
        return data.decode("utf-8", errors="replace").strip()
