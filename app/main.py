"""Точка входа ASGI-приложения.

Этап 1: приложение поднимается, инициализирует обе базы и отдаёт статус.
Полноценный Subsonic-слой, стриминг и админ-панель появятся на этапах 2-6.

Важно про жизненный цикл: AppContext создаётся один раз в lifespan и
закрывается при остановке. Если сделать это на уровне модуля, uvicorn
с --reload создавал бы базы при каждом импорте, а остановка не закрывала
бы соединения (WAL-файлы остались бы не свернутыми).
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app import __version__
from app.bootstrap import (
    AppContext,
    apply_runtime_settings,
)
from app.api.covers_ws import router as covers_router
from app.web.api import router as web_api_router
from app.web.folder_api import router as folder_api_router
from app.web.ui import router as web_ui_router
from app.api.stream import router as stream_router
from app.subsonic.auth import AuthContext, AuthThrottle
from app.subsonic.deps import require_auth_binary as _require_auth_http
from app.subsonic.errors import SubsonicError
from app.subsonic.response import error_response
from app.subsonic.router import router as subsonic_router
from app.config import Settings
from app.utils.activity import ActivityMiddleware
from app.diagnostics import log_startup_warnings
from app.security.http import (
    SecurityHeadersMiddleware,
    install_log_redaction,
)

logger = logging.getLogger("app")


def _configure_logging() -> None:
    """Единый формат логов для uvicorn и прикладного кода.

    Уровень здесь — только стартовый: настоящий берётся из базы и применяется
    чуть ниже, когда контекст уже создан.
    """
    level = os.environ.get("MTR_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # uvicorn ставит свои хендлеры — убираем дублирование строк.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers.clear()
        logging.getLogger(name).propagate = True
    # uvicorn пишет строку запроса целиком, включая p= и t=.
    # Фильтр вырезает значения до записи в лог.
    install_log_redaction()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Поднимает и корректно закрывает контекст приложения."""
    _configure_logging()

    settings = Settings.from_env()
    logger.info("MTR %s: music=%s data=%s", __version__,
                settings.music_dir, settings.data_dir)

    ctx = await AppContext.create(settings)
    app.state.ctx = ctx
    app.state.auth_throttle = AuthThrottle()
    app.state.library_repo = ctx.library
    # Фоновый прогрев обложек: одна задача на процесс, запускается через API.

    # Предупреждаем громко: если volume не проброшен, библиотека будет пустой.
    if not settings.music_dir.exists():
        logger.warning(
            "каталог музыки %s не существует — проверьте, что volume "
            "проброшен в контейнер (например -v /host/music:/music)",
            settings.music_dir,
        )

    # Значения, сохранённые в интерфейсе, лежат в таблице settings и главнее .env.
    # При старте переносим их в ctx.settings целиком, включая restart-ключи.
    # Основные переопределения уже применены в AppContext.create — до того,
    # как сканер и наблюдатель получили свои параметры. Здесь досинхронизируем
    # то, что могло измениться: например, лимит транскодеров.
    applied = await apply_runtime_settings(ctx, include_restart=True)
    if applied:
        logger.debug("досинхронизированы настройки: %s", ", ".join(applied))

    # Уровень логов меняется только при старте: uvicorn и приложение уже
    # настроены выше, поэтому поднимаем/опускаем корневой логгер здесь.
    root_level = getattr(logging, str(ctx.settings.log_level).upper(), logging.INFO)
    logging.getLogger().setLevel(root_level)

    # Наблюдатель создаётся всегда (чтобы интервал можно было включить из UI),
    # но фоновые задачи запускаем только когда он реально нужен.
    # Скан, наблюдатель и фаза обложек — в отдельном worker-процессе (см. app/worker).
    # Веб-процесс только ставит команды и читает статус.
    from app.worker.ipc import ActivityStamp, stamp_path
    from app.worker.supervisor import WorkerSupervisor

    app.state.activity = ActivityStamp(stamp_path(settings.data_dir))
    supervisor = WorkerSupervisor(settings)
    app.state.supervisor = supervisor
    await supervisor.start()
    # Проблема с ключом шифрования выглядит для клиента как код 40 —
    # показываем её в логе сразу, а не оставляем оператора гадать.
    await log_startup_warnings(ctx, secret=settings.secret_key)

    try:
        yield
    finally:
        await app.state.supervisor.stop()
        await ctx.close()
        logger.info("сервер остановлен")


# /docs и /openapi.json раскрывают структуру API. Для публичного инстанса
# их можно отключить: MTR_PUBLIC_DOCS=0
_docs_enabled = os.environ.get("MTR_PUBLIC_DOCS", "1").lower() not in ("0", "false", "no")

app = FastAPI(
    title="MTR Music Server",
    version=__version__,
    description="Собственный музыкальный сервер, совместимый с Subsonic API",
    lifespan=lifespan,
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
)
app.add_middleware(SecurityHeadersMiddleware)
# Отметка пользовательской активности для фазы обложек (см. utils/activity.py).
app.add_middleware(ActivityMiddleware)

app.include_router(stream_router)
app.include_router(subsonic_router)
app.include_router(covers_router)
app.include_router(web_api_router)
app.include_router(folder_api_router)
app.include_router(web_ui_router)


@app.exception_handler(SubsonicError)
async def _subsonic_error_handler(request: Request, exc: SubsonicError) -> JSONResponse:
    """Любая SubsonicError превращается в конверт subsonic-response.

    HTTP-статус 200 — требование протокола: клиент читает код из тела.
    Исключение — пути, где статус важен для плеера; там ошибка
    преобразуется в HTTPException раньше, в deps.require_auth_binary.
    """
    api_version = request.app.state.ctx.settings.subsonic_api_version
    return error_response(exc, api_version=api_version)


@app.get("/healthz", tags=["system"])
async def healthz(request: Request) -> JSONResponse:
    """Проверка живости: реально ли открыты обе базы."""
    ctx: AppContext = request.app.state.ctx
    try:
        users_version = await ctx.users_db.get_user_version()
        music_version = await ctx.music_db.get_user_version()
    except Exception as exc:  # noqa: BLE001 - healthcheck не должен падать
        return JSONResponse(
            status_code=503,
            content={"status": "error", "detail": str(exc)},
        )
    return JSONResponse(
        {
            "status": "ok",
            "version": __version__,
            "schema": {"users": users_version, "music": music_version},
        }
    )


@app.get("/api/v1/info", tags=["system"])
async def info(
    request: Request, auth: AuthContext = Depends(_require_auth_http)
) -> dict:
    """Сводка об инстансе — используется веб-интерфейсом."""
    ctx: AppContext = request.app.state.ctx
    return {
        "version": __version__,
        "subsonic_api_version": ctx.settings.subsonic_api_version,
        "music_dir": str(ctx.settings.music_dir),
        "music_dir_exists": ctx.settings.music_dir.exists(),
        "default_transcode": ctx.settings.default_transcode,
        "stage": 1,
    }


@app.get("/api/v1/stats", tags=["system"])
async def stats(
    request: Request, auth: AuthContext = Depends(_require_auth_http)
) -> dict:
    """Статистика библиотеки и число пользователей.

    Данные берутся из двух независимых баз двумя запросами и склеиваются
    в Python — JOIN между ними невозможен по определению.
    """
    ctx: AppContext = request.app.state.ctx
    library = await ctx.tracks.library_stats()
    library["users"] = await ctx.users.count_users()
    library["total_duration_sec"] = library.pop("total_ms") // 1000
    return library



async def index(
    request: Request, auth: AuthContext = Depends(_require_auth_http)
) -> HTMLResponse:
    """Минимальная заглушка вместо панели (полноценный UI — этап 6)."""
    ctx: AppContext = request.app.state.ctx
    library = await ctx.tracks.library_stats()
    users = await ctx.users.count_users()
    music_ok = ctx.settings.music_dir.exists()

    music_note = (
        f'<span class="ok">проброшен</span> — {ctx.settings.music_dir}'
        if music_ok
        else '<span class="bad">НЕ найден</span> — проверьте volume'
    )

    return HTMLResponse(
        f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<title>MTR Music Server</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ font: 15px/1.6 system-ui, sans-serif; background:#16181d; color:#e6e6e6;
         margin:0; padding:48px 24px; }}
  .card {{ max-width:720px; margin:0 auto; background:#1e2129; border:1px solid #2c313c;
           border-radius:12px; padding:28px 32px; }}
  h1 {{ margin:0 0 4px; font-size:22px; }}
  .sub {{ color:#8b93a1; margin-bottom:24px; font-size:13px; }}
  table {{ width:100%; border-collapse:collapse; }}
  td {{ padding:7px 0; border-bottom:1px solid #262b34; }}
  td:first-child {{ color:#8b93a1; width:45%; }}
  .ok {{ color:#5ac47e; }} .bad {{ color:#e0605e; }}
  code {{ background:#262b34; padding:2px 6px; border-radius:4px; font-size:13px; }}
  .note {{ margin-top:22px; color:#8b93a1; font-size:13px;
           border-top:1px solid #262b34; padding-top:16px; }}
</style></head><body><div class="card">
  <h1>MTR Music Server</h1>
  <div class="sub">версия {__version__} · этап 1 из 7 · API: <code>/healthz</code> <code>/api/v1/stats</code></div>
  <table>
    <tr><td>Треков в библиотеке</td><td>{library['tracks']:,}</td></tr>
    <tr><td>Отсутствующих файлов</td><td>{library['missing']:,}</td></tr>
    <tr><td>Исполнителей</td><td>{library['artists']:,}</td></tr>
    <tr><td>Альбомов</td><td>{library['albums']:,}</td></tr>
    <tr><td>Пользователей</td><td>{users}</td></tr>
    <tr><td>Каталог музыки</td><td>{music_note}</td></tr>
  </table>
  <div class="note">
    Индексация запускается из CLI:<br>
    <code>docker exec -it mtr-server python -m app.cli scan --mode full</code>
  </div>
</div></body></html>"""
    )
