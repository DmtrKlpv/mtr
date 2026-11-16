"""Состояние сканирования: прогресс, этап, итоги.

Зачем отдельный модуль: `Scanner` сообщает прогресс через колбэк, но не
хранит его. Веб-интерфейсу нужно в любой момент ответить «что происходит
сейчас» — для прогресс-бара и для того, чтобы кнопка «Сканировать» не
запускалась дважды.

WebSocket `/ws/scan` отдаёт изменения в реальном времени, но UI не может
полагаться только на него: соединение рвётся при перезагрузке страницы.
Поэтому есть ещё REST-опрос `/api/web/scan/status`.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from app.utils.timeutil import utcnow_iso

logger = logging.getLogger(__name__)

# Сколько последних ошибок держать в памяти для панели прогресса.
MAX_RECENT_ERRORS = 20


@dataclass(slots=True)
class ScanState:
    """Текущее состояние сканера. Один экземпляр на процесс."""

    # idle | running | completed | failed | cancelled
    status: str = "idle"
    stage: str = ""              # walk_done | parsing | ...
    mode: str = ""               # full | incremental | watch
    run_id: int | None = None
    started_at: str | None = None
    finished_at: str | None = None

    seen: int = 0                # найдено файлов при обходе
    to_parse: int = 0            # сколько требуют разбора
    processed: int = 0           # сколько уже разобрано
    in_flight: int = 0           # сколько читается прямо сейчас
    # Файлы, читающиеся дольше порога: [{"path": ..., "sec": ...}].
    slow: list[dict] = field(default_factory=list)
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0
    failed: int = 0
    error: str | None = None
    # Последние ошибки по файлам: панель показывает их без похода в БД.
    # Каждая запись — {"path": ..., "reason": ...}.
    recent_errors: list[dict] = field(default_factory=list)

    # Защита от одновременного запуска: сравнение и установка статуса
    # идут под локом, иначе две вкладки стартуют два скана.
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def percent(self) -> float:
        """Процент разбора. При обходе — 0, пока не известен объём."""
        if self.status != "running":
            return 100.0 if self.status == "completed" else 0.0
        if self.stage == "walk_done" and not self.to_parse:
            return 100.0
        if not self.to_parse:
            return 0.0
        return min(100.0, round(self.processed / self.to_parse * 100, 1))

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "stage": self.stage,
            "mode": self.mode,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "seen": self.seen,
            "to_parse": self.to_parse,
            "processed": self.processed,
            "in_flight": self.in_flight,
            "slow": list(self.slow),
            "percent": self.percent,
            "added": self.added,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "removed": self.removed,
            "failed": self.failed,
            "error": self.error,
            "recent_errors": list(self.recent_errors),
        }

    # Переходы
    async def begin(self, mode: str) -> bool:
        """Пытается занять сканер. False — уже идёт другой проход."""
        async with self._lock:
            if self.status == "running":
                return False
            self.status = "running"
            self.stage = "walk"
            self.mode = mode
            self.started_at = utcnow_iso()
            self.finished_at = None
            self.run_id = None
            self.seen = self.to_parse = self.processed = 0
            self.in_flight = 0
            self.slow.clear()
            self.added = self.updated = self.unchanged = 0
            self.removed = self.failed = 0
            self.error = None
            self.recent_errors.clear()
            return True

    def update(self, event: dict) -> None:
        """Колбэк для Scanner._emit.

        Синхронный намеренно: Scanner вызывает его и сам решает, ждать ли
        корутину. Запись полей атомарна на уровне GIL, полноценный лок здесь
        не нужен и замедлил бы разбор.
        """
        stage = str(event.get("stage") or "")
        if stage:
            self.stage = stage
        if "run_id" in event and event["run_id"] is not None:
            self.run_id = int(event["run_id"])
        if "seen" in event:
            self.seen = int(event["seen"] or 0)
        if "to_parse" in event:
            self.to_parse = int(event["to_parse"] or 0)
        if "total" in event:
            self.to_parse = int(event["total"] or 0)
        if "processed" in event:
            self.processed = int(event["processed"] or 0)
        if "in_flight" in event:
            self.in_flight = int(event["in_flight"] or 0)
        if "slow" in event:
            items = event["slow"] or []
            self.slow = [
                {"path": str(item.get("path") or ""), "sec": float(item.get("sec") or 0)}
                for item in items
                if isinstance(item, dict)
            ]
        for key in ("added", "updated", "unchanged", "removed", "failed"):
            if key in event:
                setattr(self, key, int(event[key] or 0))
        last_error = event.get("last_error")
        if isinstance(last_error, dict) and last_error.get("path"):
            item = {
                "path": str(last_error.get("path") or ""),
                "reason": str(last_error.get("reason") or ""),
            }
            # Храним хвост, а не всё: полный список всё равно уходит в историю.
            if not self.recent_errors or self.recent_errors[-1] != item:
                self.recent_errors.append(item)
                del self.recent_errors[:-MAX_RECENT_ERRORS]

    async def finish(self, *, status: str, error: str | None = None) -> None:
        async with self._lock:
            self.status = status
            self.error = error
            self.finished_at = utcnow_iso()
            if status == "completed":
                self.processed = max(self.processed, self.to_parse)
                self.stage = "done"

    async def fail(self, error: str) -> None:
        await self.finish(status="failed", error=error)
