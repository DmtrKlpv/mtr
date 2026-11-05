"""Запуск и наблюдение за worker-процессом из веб-процесса.

Веб поднимает `python -m app.worker` дочерним процессом, следит за ним и перезапускает
с нарастающей паузой. Если живой worker уже есть (heartbeat свежий), второй не запускается.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from pathlib import Path

from app.config import Settings
from app.worker.ipc import read_status, status_path, worker_alive

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
RESTART_DELAYS = (1.0, 2.0, 5.0, 10.0, 30.0)
# Если worker проработал дольше этого срока, счётчик падений сбрасывается.
STABLE_AFTER_SEC = 60.0
STOP_GRACE_SEC = 15.0
ALIVE_RECHECK_SEC = 2.0


class WorkerSupervisor:
    def __init__(self, settings: Settings, *, python: str | None = None) -> None:
        self.settings = settings
        self.python = python or sys.executable
        self.status_file = status_path(settings.data_dir)
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._stopping = False

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="mtr-worker-supervisor")

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    def _child_env(self) -> dict[str, str]:
        """Окружение worker-процесса: пути и ключи берём из НАСТРОЕК веба, а не из среды.

        Настройки могут быть переданы в код напрямую (тесты, нестандартный запуск), и тогда
        переменных окружения с этими путями нет. Без явной передачи worker откроет пути по
        умолчанию (/data) и упадёт. Поэтому все пути передаём явно.
        """
        s = self.settings
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        env["MUSIC_DIR"] = str(s.music_dir)
        env["DATA_DIR"] = str(s.data_dir)
        env["USERS_DB"] = str(s.users_db)
        env["MUSIC_DB"] = str(s.music_db)
        env["COVERS_DIR"] = str(s.covers_dir)
        env["MTR_SECRET_KEY_FILE"] = str(s.secret_key_file)
        if s.secret_key:
            env["MTR_SECRET_KEY"] = s.secret_key
        return env

    async def _spawn(self) -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            self.python, "-m", "app.worker",
            cwd=str(PROJECT_ROOT), env=self._child_env(),
        )

    async def _loop(self) -> None:
        failures = 0
        while not self._stopping:
            if not worker_alive(read_status(self.status_file)):
                started = time.monotonic()
                logger.info("запускаю worker-процесс")
                try:
                    self._proc = await self._spawn()
                except OSError:
                    logger.exception("не удалось запустить worker-процесс")
                    await self._sleep_backoff(failures)
                    failures += 1
                    continue
                code = await self._proc.wait()
                self._proc = None
                if self._stopping:
                    break
                if time.monotonic() - started > STABLE_AFTER_SEC:
                    failures = 0
                logger.warning("worker-процесс завершился с кодом %s — перезапуск", code)
                await self._sleep_backoff(failures)
                failures += 1
            else:
                # Живой worker уже работает (например, оставшийся от прошлого запуска веба).
                await asyncio.sleep(ALIVE_RECHECK_SEC)

    async def _sleep_backoff(self, failures: int) -> None:
        delay = RESTART_DELAYS[min(failures, len(RESTART_DELAYS) - 1)]
        try:
            await asyncio.wait_for(self._wait_stop(), timeout=delay)
        except asyncio.TimeoutError:
            pass

    async def _wait_stop(self) -> None:
        while not self._stopping:
            await asyncio.sleep(0.2)

    async def stop(self) -> None:
        self._stopping = True
        proc = self._proc
        if proc is not None and proc.returncode is None:
            try:
                proc.send_signal(signal.SIGTERM)
                await asyncio.wait_for(proc.wait(), timeout=STOP_GRACE_SEC)
            except (ProcessLookupError, asyncio.TimeoutError):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
