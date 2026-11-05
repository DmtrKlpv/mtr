"""Связь веб-процесса и worker-процесса.

Два процесса не делят ни соединение с БД, ни event loop. Связь — через три простых канала:

* команды — таблица `worker_commands` в mtr_music.db: веб пишет строку, worker читает
  необработанные и отмечает `handled_at`. Транзакционно, переживает перезапуск;
* статус — файл `DATA_DIR/worker_status.json`: worker записывает его атомарно (os.replace),
  веб читает. Heartbeat внутри файла: по нему веб понимает, жив ли worker;
* активность пользователя — файл `DATA_DIR/activity.stamp`: веб трогает его (не чаще
  раза в секунду), worker смотрит mtime. Блокировок БД на каждый запрос нет.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from app.utils.timeutil import utcnow_iso

# Worker считается живым, если его heartbeat младше этого срока.
# TTL увеличен с 5 до 15 с: в простое статус пишется не чаще раза в
# HEARTBEAT_WRITE_SEC, и heartbeat не должен казаться устаревшим между записями.
HEARTBEAT_TTL_SEC = 15.0
# Период записи heartbeat, когда состояние не меняется.
HEARTBEAT_WRITE_SEC = 5.0
# Как часто веб трогает файл активности (чтобы не писать на диск на каждый запрос).
TOUCH_EVERY_SEC = 1.0

COMMAND_KINDS = ("scan", "reload", "covers")
SCAN_MODES = ("full", "incremental")


def status_path(data_dir: Path) -> Path:
    return Path(data_dir) / "worker_status.json"


def stamp_path(data_dir: Path) -> Path:
    return Path(data_dir) / "activity.stamp"


# ----------------------------------------------------------------------
# Команды (веб → worker)
# ----------------------------------------------------------------------
async def enqueue(db, kind: str, payload: dict[str, Any] | None = None) -> int:
    """Ставит команду в очередь. Неизвестный вид — ValueError."""
    if kind not in COMMAND_KINDS:
        raise ValueError(f"неизвестная команда worker: {kind!r}")
    return await db.execute(
        "INSERT INTO worker_commands (kind, payload, created_at) VALUES (?, ?, ?)",
        (kind, json.dumps(payload or {}, ensure_ascii=False), utcnow_iso()),
    )


# ----------------------------------------------------------------------
# Статус (worker → веб)
# ----------------------------------------------------------------------
def write_status(path: Path, payload: dict[str, Any]) -> None:
    """Атомарная запись: читатель видит либо старый, либо новый файл целиком."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {**payload, "heartbeat": time.time()}
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".status-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(body, handle, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_status(path: Path) -> dict[str, Any]:
    """Последний статус worker-а или пустой словарь (файла нет / повреждён)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def worker_alive(status: dict[str, Any], now: float | None = None) -> bool:
    heartbeat = status.get("heartbeat")
    if not isinstance(heartbeat, (int, float)):
        return False
    current = time.time() if now is None else now
    return current - heartbeat < HEARTBEAT_TTL_SEC


# ----------------------------------------------------------------------
# Активность (веб трогает, worker читает)
# ----------------------------------------------------------------------
class ActivityStamp:
    """Веб-сторона: отмечает пользовательский запрос файлом. Совместима с ActivityTracker.touch()."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._last = float("-inf")

    def touch(self) -> None:
        now = time.monotonic()
        if now - self._last < TOUCH_EVERY_SEC:
            return
        self._last = now
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8"):
                pass
            os.utime(self.path, None)
        except OSError:
            # Нет записи на диск — не ломаем запрос пользователя.
            pass


class FileActivity:
    """Worker-сторона: «пользователь активен», если файл трогали менее window_sec назад.

    Интерфейс совпадает с ActivityTracker.busy(), поэтому CoverPass берёт его без изменений.
    """

    def __init__(self, path: Path, window_sec: float = 3.0) -> None:
        self.path = Path(path)
        self.window_sec = float(window_sec)

    def set_window(self, seconds: float) -> None:
        self.window_sec = max(0.0, float(seconds))

    def busy(self) -> bool:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return False
        return (time.time() - mtime) < self.window_sec
