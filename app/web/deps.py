"""Зависимости веб-интерфейса: сессия из cookie, CSRF, роли.

Отдельно от `app/subsonic/deps.py`, потому что механизмы разные:

* Subsonic — параметры в URL (`u`, `t`, `s`), ошибка = JSON-конверт.
* Веб — cookie + CSRF, ошибка = редирект на /login для страниц
  и HTTP 401/403 для JSON API.

Смешивать их нельзя: клиенты Subsonic не умеют cookie, а браузер не должен
передавать пароль в каждом запросе.
"""

from __future__ import annotations

import logging

from fastapi import Depends, HTTPException, Request, status

from app.bootstrap import AppContext
from app.web.sessions import CSRF_HEADER, SESSION_COOKIE, SessionRecord

logger = logging.getLogger(__name__)

# Методы, меняющие состояние: для них обязателен CSRF-токен.
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


async def optional_session(request: Request) -> SessionRecord | None:
    """Сессия из cookie или None. Не бросает исключений."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    return await request.app.state.ctx.sessions.resolve(token)


async def current_session(
    request: Request,
) -> SessionRecord:
    """Требует авторизованную сессию. Для JSON API — 401."""
    session = await optional_session(request)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    return session


async def current_page_session(request: Request) -> SessionRecord | None:
    """Для HTML-страниц: None означает «показать логин», а не ошибку."""
    return await optional_session(request)


async def require_admin(session: SessionRecord = Depends(current_session)) -> SessionRecord:
    """Управление сервером — только администратор."""
    if not session.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrative privileges required",
        )
    return session


async def csrf_protected(
    request: Request,
    session: SessionRecord = Depends(current_session),
) -> SessionRecord:
    """Проверяет CSRF-токен для небезопасных методов.

    Токен берётся из заголовка `X-CSRF-Token`: браузер не добавляет
    произвольные заголовки к межсайтовым запросам, поэтому это надёжнее,
    чем скрытое поле формы или значение из cookie.
    """
    if request.method in UNSAFE_METHODS:
        supplied = request.headers.get(CSRF_HEADER)
        if not session.csrf_token or not supplied or not _safe_eq(
            session.csrf_token, supplied
        ):
            logger.warning(
                "CSRF-токен отклонён: user=%s path=%s",
                session.user.username, request.url.path,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="CSRF token missing or invalid",
            )
    return session


def _safe_eq(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left, right)


async def admin_csrf(
    session: SessionRecord = Depends(csrf_protected),
) -> SessionRecord:
    """CSRF + роль администратора: для изменяющих админ-операций."""
    if not session.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrative privileges required",
        )
    return session
