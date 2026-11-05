"""Точка входа worker-процесса: `python -m app.worker`."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

from app.bootstrap import AppContext
from app.config import Settings
from app.worker.runtime import WorkerRuntime


def _configure_logging() -> None:
    level = os.environ.get("MTR_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


async def _amain(settings: Settings, parent_pid: int | None) -> None:
    ctx = await AppContext.create(settings)
    runtime = WorkerRuntime(ctx)
    runtime.attach_activity()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows: сигналов нет, останавливаемся по Ctrl+C
            pass
    logging.getLogger(__name__).info("worker запущен, pid=%s", os.getpid())
    await runtime.run(stop, parent_pid=parent_pid)


def main() -> int:
    _configure_logging()
    settings = Settings.from_env()
    # Родитель — веб-процесс. Если он исчез, worker завершится сам (нет сирот).
    parent = os.getppid()
    asyncio.run(_amain(settings, parent_pid=parent if parent > 1 else None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
