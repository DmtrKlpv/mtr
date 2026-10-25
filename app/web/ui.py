"""HTML-маршруты веб-интерфейса + вход/выход.

Страницы:
  GET  /login   — форма входа
  POST /api/web/login — проверка пароля, выдача cookie
  GET  /app     — оболочка приложения (требует сессию)
  GET  /        — редирект на /app или /login

Проверка пароля здесь идёт по PBKDF2-хэшу и **не зависит** от
`MTR_SECRET_KEY` — та же логика, что в Subsonic-слое после исправления
v0.3.2. Если обратимая копия не читается, она перешифровывается при входе.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)

from app.bootstrap import AppContext
from app.repositories.users import UserError
from app.subsonic.auth import AuthThrottle, throttle_key
from app.web.deps import current_page_session, get_ctx
from app.web.sessions import CSRF_COOKIE, SESSION_COOKIE
from app.web.templates import render_app, render_login, render_setup

logger = logging.getLogger(__name__)
router = APIRouter(tags=["web-ui"])


def _cookie_secure(ctx: AppContext) -> bool:
    return ctx.settings.cookie_secure


def _serve(path, mime: str):
    """Общий ответ для статики: кэш на 5 минут, никакой инлайн-подстановки."""
    from fastapi.responses import FileResponse

    if not path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(
        path,
        media_type=mime,
        headers={"Cache-Control": "public, max-age=300"},
    )


@router.get("/login", response_class=HTMLResponse, include_in_schema=False,
            response_model=None)
async def login_page(
    request: Request, ctx: AppContext = Depends(get_ctx)
) -> Response:
    # Уже вошли — сразу в приложение.
    session = await current_page_session(request)
    if session is not None:
        return RedirectResponse("/app", status_code=status.HTTP_303_SEE_OTHER)
    # Учёток нет — предлагаем создать администратора, а не показываем форму
    # входа, за которой некому открыть дверь.
    if await ctx.users.count_users() == 0:
        return RedirectResponse("/setup", status_code=status.HTTP_303_SEE_OTHER)
    return HTMLResponse(render_login())


@router.get("/setup", response_class=HTMLResponse, include_in_schema=False,
            response_model=None)
async def setup_page(
    request: Request, ctx: AppContext = Depends(get_ctx)
) -> Response:
    """Первый запуск: создание администратора.

    Доступна только пока в базе нет ни одной учётной записи. Как только
    администратор создан, страница закрывается — иначе это была бы дыра:
    любой желающий мог бы добавить себе админский аккаунт.
    """
    if await ctx.users.count_users() > 0:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    return HTMLResponse(render_setup())


@router.post("/api/web/login", include_in_schema=False, response_model=None)
async def login_submit(
    request: Request, ctx: AppContext = Depends(get_ctx)
) -> JSONResponse:
    """Проверяет пароль и ставит сессионную cookie."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")

    if not username or not password:
        return JSONResponse(
            {"detail": "Введите имя и пароль"}, status_code=400
        )

    ip = request.client.host if request.client else None
    throttle: AuthThrottle = request.app.state.auth_throttle
    key = throttle_key(username, ip)
    if throttle.is_blocked(key):
        logger.warning("вход заблокирован после серии неудач: %s (%s)", username, ip)
        return JSONResponse(
            {"detail": "Слишком много попыток. Попробуйте позже."},
            status_code=429,
        )

    record = await ctx.users.get_by_username(username)
    # Ответ одинаковый для «нет такого» и «неверный пароль»: не даём
    # перечислять логины.
    if record is None or not record.is_active:
        throttle.record_failure(key)
        return JSONResponse(
            {"detail": "Неверное имя или пароль"}, status_code=401
        )

    if not ctx.cipher.verify_password(password, record.password_hash):
        throttle.record_failure(key)
        return JSONResponse(
            {"detail": "Неверное имя или пароль"}, status_code=401
        )

    throttle.reset(key)

    # Самовосстановление обратимой копии (как в Subsonic-слое).
    if await ctx.users.get_plain_password(record.id) is None:
        try:
            await ctx.users.reencrypt_password(record.id, password)
            logger.warning(
                "пароль %r перешифрован текущим MTR_SECRET_KEY", username
            )
        except Exception:  # noqa: BLE001
            logger.exception("не удалось перешифровать пароль %r", username)

    token, csrf = await ctx.sessions.create(
        record.id,
        user_agent=request.headers.get("user-agent"),
        ip=ip,
    )
    await ctx.users.touch_login(record.id)

    response = JSONResponse({"ok": True, "redirect": "/app"})
    secure = _cookie_secure(ctx)
    response.set_cookie(
        SESSION_COOKIE, token,
        httponly=True, samesite="lax", secure=secure,
        max_age=30 * 24 * 3600, path="/",
    )
    # CSRF-токен НЕ httponly: JS читает его и отправляет в заголовке.
    response.set_cookie(
        CSRF_COOKIE, csrf,
        httponly=False, samesite="lax", secure=secure,
        max_age=30 * 24 * 3600, path="/",
    )
    return response


@router.post("/api/web/setup", include_in_schema=False, response_model=None)
async def setup_submit(
    request: Request, ctx: AppContext = Depends(get_ctx)
) -> Response:
    """Создаёт первого администратора и сразу выдаёт сессию.

    Работает только на пустой базе. Пароль проверяется теми же правилами, что
    и везде (не короче 8 символов), и попадает в базу хешем — в файлах его нет.
    """
    if await ctx.users.count_users() > 0:
        # Кто-то успел раньше: дальше только обычный вход.
        return JSONResponse(
            {"detail": "Администратор уже создан"}, status_code=409
        )

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")

    if not username or not password:
        return JSONResponse(
            {"detail": "Введите имя и пароль"}, status_code=400
        )

    try:
        record = await ctx.users.create_user(
            username, password, role="admin", display_name="Administrator"
        )
    except UserError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    except Exception:  # noqa: BLE001
        logger.exception("не удалось создать первого администратора")
        return JSONResponse(
            {"detail": "Не удалось создать администратора"}, status_code=500
        )

    logger.warning("создан первый администратор %r через страницу первого запуска", username)

    token, csrf = await ctx.sessions.create(record.id)
    secure = bool(ctx.settings.cookie_secure)
    response = JSONResponse({"ok": True, "user": username})
    response.set_cookie(
        SESSION_COOKIE, token,
        httponly=True, samesite="lax", secure=secure,
        max_age=30 * 24 * 3600, path="/",
    )
    response.set_cookie(
        CSRF_COOKIE, csrf,
        httponly=False, samesite="lax", secure=secure,
        max_age=30 * 24 * 3600, path="/",
    )
    return response


@router.get("/app", response_class=HTMLResponse, include_in_schema=False,
            response_model=None)
async def app_page(
    request: Request, ctx: AppContext = Depends(get_ctx)
) -> Response:
    session = await current_page_session(request)
    if session is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    return HTMLResponse(
        render_app(
            username=session.user.username,
            role=session.user.role,
            csrf=session.csrf_token,
        )
    )


@router.get("/", include_in_schema=False, response_model=None)
async def root(request: Request) -> RedirectResponse:
    """Корень ведёт в интерфейс; /healthz остаётся для HEALTHCHECK."""
    session = await current_page_session(request)
    target = "/app" if session is not None else "/login"
    return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)


# ----------------------------------------------------------------------
# Статика
# ----------------------------------------------------------------------
@router.get("/static/{filename}", include_in_schema=False, response_model=None)
async def static_file(filename: str):
    """Отдаёт файлы интерфейса из app/web/static.

    Имя проверяется по белому списку: путь составляется из параметра,
    и `..` не должен вывести за пределы каталога.
    """
    from pathlib import Path


    # Иконки — отдельный каталог и отдельный маршрут: разрешённый список
    # имён не смешивается с кодом и стилями.
    allowed = {
        "app.js": "application/javascript",
        "login.js": "application/javascript",
        "setup.js": "application/javascript",
        "cover_queue.js": "application/javascript",
        "scan_import.js": "application/javascript",
        "folder_sync.js": "application/javascript",
        "dialogs.js": "application/javascript",
        "style.css": "text/css",
        "theme.css": "text/css",
        "icon.svg": "image/svg+xml",
        "placeholder.svg": "image/svg+xml",
        "favicon.ico": "image/x-icon",
        "favicon-32.png": "image/png",
        "apple-touch-icon.png": "image/png",
        "icon-192.png": "image/png",
        "icon-512.png": "image/png",
        "manifest.webmanifest": "application/manifest+json",
    }
    mime = allowed.get(filename)
    if mime is None:
        raise HTTPException(status_code=404, detail="Not found")

    path = Path(__file__).parent / "static" / filename

    return _serve(path, mime)


@router.get("/static/icons/{filename}", include_in_schema=False, response_model=None)
async def icon_file(filename: str):
    """Отдаёт SVG-иконку из app/web/static/icons.

    Имя проверяется регулярным выражением: только буквы, цифры и дефис,
    с обязательным .svg. Точки и слэши отсекаются, поэтому `..` не пройдёт.
    """
    import re
    from pathlib import Path

    from fastapi.responses import FileResponse

    if not re.fullmatch(r"[a-z0-9-]{1,40}\.svg", filename):
        raise HTTPException(status_code=404, detail="Not found")

    path = Path(__file__).parent / "static" / "icons" / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(
        path,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/favicon.ico", include_in_schema=False, response_model=None)
async def root_favicon():
    """Браузеры запрашивают /favicon.ico сами, без ссылки в HTML.

    Отдаём тот же файл, что и /static/favicon.ico, чтобы не держать
    две копии и не расходиться.
    """
    from pathlib import Path

    from fastapi.responses import FileResponse

    path = Path(__file__).parent / "static" / "favicon.ico"
    return FileResponse(
        path,
        media_type="image/x-icon",
        headers={"Cache-Control": "public, max-age=86400"},
    )
