"""FastAPI-зависимости для Subsonic-эндпоинтов.

Две разные зависимости для двух разных нужд:

* `require_auth` — для JSON-методов. Ошибка оформляется конвертом
  subsonic-response с HTTP 200: так требует протокол, клиент читает код
  из тела.

* `require_auth_binary` — для stream/download/getCoverArt. Здесь HTTP-статус
  важнее конверта: плеер по 401 понимает, что надо переспросить пароль,
  а по 200 с JSON-ошибкой — нет. Поэтому отдаём честные 401/403.

IP берётся из request.client, а не из X-Forwarded-For: заголовку можно
подделать и обойти троттлинг. За обратным прокси IP будет общим — тогда
лимит считается по логину, что всё равно полезно.
"""

from __future__ import annotations

from fastapi import HTTPException, Request

from app.bootstrap import AppContext
from app.subsonic.auth import AuthContext, AuthThrottle, authenticate
from app.subsonic.errors import ErrorCode, SubsonicError

# Коды, которые для бинарного эндпоинта означают «нужна авторизация».
# MISSING_PARAMETER сюда входит: запрос без u/t/p — это запрос без
# учётных данных, а не синтаксическая ошибка.
_UNAUTHORIZED_CODES = frozenset({
    ErrorCode.WRONG_CREDENTIALS,
    ErrorCode.TOKEN_AUTH_UNSUPPORTED,
    ErrorCode.NOT_AUTHORIZED,
    ErrorCode.MISSING_PARAMETER,
})


def _client_ip(request: Request) -> str | None:
    if request.client is None:
        return None
    return request.client.host


async def _authenticate(request: Request) -> AuthContext:
    ctx: AppContext = request.app.state.ctx
    throttle: AuthThrottle = request.app.state.auth_throttle
    return await authenticate(
        ctx,
        request.query_params,
        ip=_client_ip(request),
        throttle=throttle,
    )


async def require_auth(request: Request) -> AuthContext:
    """Авторизация для JSON-методов. Ошибку обработает глобальный хендлер."""
    return await _authenticate(request)


async def require_auth_binary(request: Request) -> AuthContext:
    """Авторизация для бинарных методов: ошибка превращается в HTTP 401/403."""
    try:
        return await _authenticate(request)
    except SubsonicError as exc:
        # Для бинарных методов важнее HTTP-семантика: клиент без полных
        # учётных данных получает 401 и понимает, что надо авторизоваться.
        # 400 остаётся только для действительно некорректных запросов.
        if exc.code in _UNAUTHORIZED_CODES:
            raise HTTPException(
                status_code=401,
                detail=exc.message,
                headers={"WWW-Authenticate": 'Subsonic realm="mtr"'},
            ) from exc
        raise HTTPException(status_code=400, detail=exc.message) from exc


async def require_admin(request: Request) -> AuthContext:
    """Авторизация + проверка роли admin (код 50 при отказе)."""
    auth = await _authenticate(request)
    if auth.user.role != "admin":
        raise SubsonicError(
            ErrorCode.NOT_AUTHORIZED,
            "Administrative privileges required for this operation",
        )
    return auth

async def require_auth_or_session(request: Request) -> AuthContext | None:
    """Авторизация для бинарных методов: Subsonic-параметры ИЛИ сессия.

    Зачем: плеер веб-интерфейса обращается к `/stream?id=…` и
    `/rest/getCoverArt?id=…` из браузера, где пароля и токена нет —
    есть только cookie сессии. Subsonic-клиенты, наоборот, cookie не умеют.

    Порядок: если в запросе есть Subsonic-учётные данные (`u` и `t`/`p`) —
    проверяем их; иначе пробуем сессию. Возвращает None, если не сработало
    ни то, ни другое: вызывающая сторона решает, отдать 401 или 404.
    """
    params = request.query_params
    has_subsonic = bool(params.get("u")) and bool(params.get("t") or params.get("p"))

    if has_subsonic:
        return await _authenticate(request)

    from app.web.sessions import SESSION_COOKIE

    ctx: AppContext = request.app.state.ctx
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    session = await ctx.sessions.resolve(token)
    if session is None:
        return None

    # Оборачиваем сессию в AuthContext: остальной код ожидает его.
    return AuthContext(
        user=session.user,
        client="web",
        api_version=ctx.settings.subsonic_api_version,
        params=params,
    )
