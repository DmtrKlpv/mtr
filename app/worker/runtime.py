"""Цикл worker-процесса: команды из БД, статус в файл, сканирование и обложки.

Здесь живут скан, наблюдатель за папкой и фаза обложек. Веб-процесс их не запускает.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from app.bootstrap import AppContext
from app.utils.timeutil import utcnow_iso
from app.worker.ipc import (
    HEARTBEAT_WRITE_SEC,
    SCAN_MODES,
    FileActivity,
    stamp_path,
    status_path,
    write_status,
)

logger = logging.getLogger(__name__)

# Пауза между проверками очереди команд и публикацией статуса, когда идёт работа
# (скан, обложки, команды). Короткая, чтобы старт скана из интерфейса был быстрым.
POLL_SEC = 0.5
# Пауза в простое. Раньше было 0.5 с всегда: два запроса к БД и две записи
# worker_status.json в секунду на NAS, даже без клиентов и без работы.
IDLE_POLL_SEC = 3.0
# Сколько ждать завершения скана при остановке worker-а.
SHUTDOWN_GRACE_SEC = 30.0


class WorkerRuntime:
    def __init__(
        self,
        ctx: AppContext,
        *,
        status_file: Path | None = None,
        poll_sec: float = POLL_SEC,
        idle_poll_sec: float = IDLE_POLL_SEC,
    ) -> None:
        self.ctx = ctx
        self.status_file = Path(status_file) if status_file else status_path(
            ctx.settings.data_dir
        )
        self.poll_sec = poll_sec
        self.idle_poll_sec = idle_poll_sec
        # Последний записанный статус (без heartbeat) и время его записи.
        self._last_status_sig: str | None = None
        self._last_status_at: float = float("-inf")
        self._scan_task: asyncio.Task | None = None
        self._scan_tasks: set[asyncio.Task] = set()
        self._activity = FileActivity(stamp_path(ctx.settings.data_dir))

    # ------------------------------------------------------------------
    # Фоновая фаза обложек: активность и параметры из настроек
    # ------------------------------------------------------------------
    def attach_activity(self) -> None:
        """Подключает файл активности к фазе обложек вместо in-memory трекера."""
        self.ctx.cover_pass.set_activity(self._activity)

    async def apply_settings(self) -> None:
        """Читает настройки из БД и применяет к работающим объектам (без перезапуска)."""
        values = await self.ctx.settings_repo.effective()
        window = int(values.get("cover_active_window_sec", 3))
        max_wait = int(values.get("cover_max_wait_sec", 30))
        self._activity.set_window(window)
        self.ctx.cover_pass.set_max_wait(max_wait)
        # Размер захода сканера: 0 — все сразу.
        batch = int(values.get("scan_batch_tracks", 0) or 0)
        self.ctx.scanner.set_batch_size(batch)
        interval = int(values.get("scan_interval_sec", self.ctx.settings.scan_interval_sec) or 0)
        watch = bool(values.get("watch_enabled", self.ctx.settings.watch_enabled))
        watcher = self.ctx.watcher
        if watcher is not None:
            watcher.set_interval(interval)
            if watch or interval > 0:
                await watcher.start()
            else:
                await watcher.stop()
        logger.info(
            "worker: настройки применены (окно %s с, ожидание %s с, интервал скана %s с, "
            "заход скана %s треков)",
            window, max_wait, interval, batch or "все",
        )

    # ------------------------------------------------------------------
    # Команды
    # ------------------------------------------------------------------
    async def handle_commands(self) -> int:
        """Обрабатывает необработанные команды по порядку. Возвращает их число."""
        rows = await self.ctx.music_db.fetch_all(
            "SELECT id, kind, payload FROM worker_commands "
            "WHERE handled_at IS NULL ORDER BY id LIMIT 50"
        )
        for row in rows:
            command_id = int(row["id"])
            kind = str(row["kind"])
            try:
                payload = json.loads(row["payload"] or "{}")
            except ValueError:
                payload = {}
            try:
                await self._dispatch(kind, payload if isinstance(payload, dict) else {})
            except Exception:  # noqa: BLE001 - одна команда не должна ронять worker
                logger.exception("worker: команда %s (%s) не выполнена", command_id, kind)
            await self.ctx.music_db.execute(
                "UPDATE worker_commands SET handled_at = ? WHERE id = ?",
                (utcnow_iso(), command_id),
            )
        return len(rows)

    async def _dispatch(self, kind: str, payload: dict[str, Any]) -> None:
        if kind == "scan":
            mode = str(payload.get("mode") or "incremental")
            if mode not in SCAN_MODES:
                logger.warning("worker: режим скана %r не поддерживается", mode)
                return
            if self.scan_running():
                logger.info("worker: скан уже идёт, команда %s пропущена", mode)
                return
            self._scan_task = asyncio.create_task(self._run_scan(mode), name=f"scan-{mode}")
            self._scan_tasks.add(self._scan_task)
            self._scan_task.add_done_callback(self._scan_tasks.discard)
        elif kind == "reload":
            await self.apply_settings()
        elif kind == "covers":
            # Пересканирование обложек: полный проход по альбомам без записи в кеше.
            mode = str(payload.get("mode") or "incremental")
            if mode not in ("full", "incremental"):
                mode = "incremental"
            self.ctx.cover_pass.start(mode)
            logger.info("worker: фаза обложек запущена (%s)", mode)

    def scan_running(self) -> bool:
        return self._scan_task is not None and not self._scan_task.done()

    async def _run_scan(self, mode: str) -> None:
        state = self.ctx.scan_state
        if not await state.begin(mode):
            return
        try:
            await self.ctx.scanner.scan(mode=mode)
            await state.finish(status="completed")
        except Exception as exc:  # noqa: BLE001 - причину показываем в статусе
            logger.exception("worker: скан (%s) не удался", mode)
            await state.fail(str(exc))

    async def wait_scan(self, timeout: float = 60.0) -> None:
        """Ждёт завершения текущего скана (для тестов и остановки)."""
        if self._scan_task is not None:
            await asyncio.wait({self._scan_task}, timeout=timeout)

    # ------------------------------------------------------------------
    # Статус
    # ------------------------------------------------------------------
    def _is_busy(self) -> bool:
        """Идёт ли работа, для которой нужен частый опрос."""
        if self._scan_tasks or self.ctx.scan_state.status == "running":
            return True
        if self.ctx.cover_pass.state == "running":
            return True
        watcher = self.ctx.watcher
        return watcher is not None and watcher.pending > 0

    def _next_delay(self, busy: bool) -> float:
        return self.poll_sec if busy else self.idle_poll_sec

    async def publish_status(self) -> bool:
        """Пишет статус, если он изменился или пора обновить heartbeat.

        Возвращает True, если файл был записан. В простое запись идёт раз
        в HEARTBEAT_WRITE_SEC, а не на каждом проходе цикла.
        """
        watcher = self.ctx.watcher
        payload = {
            "pid": os.getpid(),
            "scan": self.ctx.scan_state.as_dict(),
            "covers": self.ctx.cover_pass.as_dict(),
            "watcher": (
                {"state": watcher.state, "pending": watcher.pending}
                if watcher is not None else None
            ),
        }
        signature = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        now = time.monotonic()
        heartbeat_due = now - self._last_status_at >= HEARTBEAT_WRITE_SEC
        if signature == self._last_status_sig and not heartbeat_due:
            return False
        # Файловая запись — блокирующая; на NAS она может занять заметное время.
        await asyncio.to_thread(write_status, self.status_file, payload)
        self._last_status_sig = signature
        self._last_status_at = now
        return True

    # ------------------------------------------------------------------
    # Цикл
    # ------------------------------------------------------------------
    async def run(self, stop: asyncio.Event, *, parent_pid: int | None = None) -> None:
        """Основной цикл. Выходит по stop или когда родитель (веб) исчез."""
        await self.apply_settings()
        while not stop.is_set():
            if parent_pid is not None and os.getppid() != parent_pid:
                logger.warning("worker: родительский процесс исчез — завершаюсь")
                break
            busy = False
            try:
                handled = await self.handle_commands()
                await self.publish_status()
                busy = handled > 0 or self._is_busy()
            except Exception:  # noqa: BLE001 - цикл переживает разовые сбои БД/диска
                logger.exception("worker: сбой цикла")
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._next_delay(busy))
            except asyncio.TimeoutError:
                pass
        await self.shutdown()

    async def shutdown(self) -> None:
        self.ctx.scanner.cancel()
        self.ctx.cover_pass.cancel_nowait()
        if self._scan_task is not None:
            await asyncio.wait({self._scan_task}, timeout=SHUTDOWN_GRACE_SEC)
        if self.ctx.watcher is not None:
            await self.ctx.watcher.stop()
        await self.ctx.close()
        logger.info("worker: остановлен")
