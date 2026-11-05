"""Активность пользователя: чтобы фоновая фаза обложек не мешала просмотру.

Фаза обложек между альбомами спрашивает `ACTIVITY.busy()`. Если пользователь недавно
делал запросы к интерфейсу, фаза ждёт (короткими шагами, не дольше `MAX_WAIT_SEC`),
чтобы диск и соединение с БД доставались запросам из браузера.

Что считается активностью: обычные запросы к веб-API и страницам. Не считаются:
опросы (статус скана, готовность обложек), сами обложки (`getCoverArt`) и стриминг
(`/stream`). Иначе фон никогда не продвинется: браузер опрашивает постоянно.
"""

from __future__ import annotations

import time

# Сколько секунд после последнего запроса считаем пользователя активным.
ACTIVE_WINDOW_SEC = 3.0
# Сколько фаза может ждать одного альбома из-за активности. Дальше идёт всё равно.
MAX_WAIT_SEC = 30.0

# Пути, которые не считаются активностью (опросы и тяжёлые фоновые загрузки).
EXCLUDED_PREFIXES: tuple[str, ...] = (
    "/api/web/scan/status",
    "/api/web/covers/ready",
    "/api/web/covers/warm",
    "/api/web/playlists/folder-jobs",
    "/rest/getCoverArt",
    "/stream",
    "/rest/stream",
    "/static/",
    "/healthz",
)


def is_user_path(path: str) -> bool:
    """True для пути, который считается действием пользователя."""
    if not path.startswith(("/api/web/", "/rest/", "/")):
        return False
    return not any(path.startswith(prefix) for prefix in EXCLUDED_PREFIXES)


class ActivityTracker:
    """Время последнего пользовательского запроса (монотонные часы, в памяти процесса)."""

    def __init__(self, window_sec: float = ACTIVE_WINDOW_SEC) -> None:
        self.window_sec = window_sec
        self._last = float("-inf")

    def touch(self) -> None:
        self._last = time.monotonic()

    def busy(self) -> bool:
        return (time.monotonic() - self._last) < self.window_sec

    def reset(self) -> None:
        self._last = float("-inf")


ACTIVITY = ActivityTracker()


class ActivityMiddleware:
    """Чистый ASGI-middleware: отмечает пользовательские HTTP-запросы.

    Не BaseHTTPMiddleware: тот ломает потоковые ответы (стриминг, range-запросы).
    """

    def __init__(self, app, tracker=None) -> None:
        self.app = app
        self.tracker = tracker

    def _resolve(self, scope):
        if self.tracker is not None:
            return self.tracker
        # В веб-процессе lifespan кладёт файловую метку в app.state.activity.
        app = scope.get("app")
        stamp = getattr(getattr(app, "state", None), "activity", None)
        return stamp or ACTIVITY

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and is_user_path(scope.get("path", "")):
            self._resolve(scope).touch()
        await self.app(scope, receive, send)
