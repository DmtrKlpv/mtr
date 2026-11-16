"""Тесты безопасности HTTP-слоя.

Закрывают находки аудита:
  * пароль и токен не попадают в лог;
  * служебные эндпоинты закрыты авторизацией;
  * security-заголовки присутствуют и не ломают стриминг.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import httpx
import pytest

from app.security.http import (
    RedactSecretsFilter,
    install_log_redaction,
    redact_url,
)
from tests.conftest import make_mp3, tag_mp3

PASSWORD = "changeme123"
SALT = "abc123def456"


def auth_q(password: str = PASSWORD) -> str:
    token = hashlib.md5((password + SALT).encode()).hexdigest()
    return f"u=admin&t={token}&s={SALT}&v=1.16.1&c=test&f=json"


# Маскирование секретов
@pytest.mark.parametrize(
    "url,expected",
    [
        ("/rest/ping.view?u=admin&p=secret&v=1.16.1",
         "/rest/ping.view?u=admin&p=***&v=1.16.1"),
        ("/rest/ping.view?u=admin&t=abc&s=salt&v=1.16.1",
         "/rest/ping.view?u=admin&t=***&s=***&v=1.16.1"),
        ("/stream?id=5&jwt=tok", "/stream?id=5&jwt=***"),
        ("/healthz", "/healthz"),
        ("", ""),
    ],
)
def test_redact_url(url, expected):
    assert redact_url(url) == expected


def test_redact_keeps_non_secret_params():
    """Параметры, не являющиеся секретами, должны остаться читаемыми."""
    result = redact_url("/rest/search3.view?query=Queen&p=x&songCount=10")
    assert "query=Queen" in result
    assert "songCount=10" in result
    assert "p=***" in result


def test_redact_does_not_touch_param_name_substrings():
    """Параметр 'songs' не должен маскироваться из-за префикса 's'."""
    assert redact_url("/x?songs=5&size=10") == "/x?songs=5&size=10"


def test_filter_masks_uvicorn_access_args():
    """uvicorn.access пишет через %-форматирование: маскируем аргументы."""
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname="", lineno=0,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1", "GET", "/rest/ping.view?u=admin&p=changeme123", "1.1", 200),
        exc_info=None,
    )
    RedactSecretsFilter().filter(record)
    rendered = record.getMessage()
    assert "changeme123" not in rendered
    assert "p=***" in rendered


def test_filter_masks_message_without_args():
    record = logging.LogRecord(
        name="app", level=logging.INFO, pathname="", lineno=0,
        msg="запрос /rest/ping.view?u=admin&t=deadbeef&s=salt",
        args=(), exc_info=None,
    )
    RedactSecretsFilter().filter(record)
    assert "deadbeef" not in record.getMessage()


def test_install_is_idempotent():
    install_log_redaction()
    install_log_redaction()
    filters = [f for f in logging.getLogger("uvicorn.access").filters
               if isinstance(f, RedactSecretsFilter)]
    assert len(filters) == 1


# HTTP: закрытые служебные эндпоинты
@pytest.fixture
async def api(settings, monkeypatch, music_dir):
    monkeypatch.setenv("MUSIC_DIR", str(settings.music_dir))
    monkeypatch.setenv("DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("MTR_SECRET_KEY", settings.secret_key or "test")

    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            await app.state.ctx.users.create_user("admin", PASSWORD, role="admin")
            yield client, app


@pytest.mark.parametrize("path", ["/api/v1/info", "/api/v1/stats"])
async def test_service_endpoints_require_auth(api, path):
    """Без учётных данных служебные эндпоинты не отдают ничего."""
    client, _ = api
    r = await client.get(path)
    assert r.status_code == 401


@pytest.mark.parametrize("path", ["/api/v1/info", "/api/v1/stats"])
async def test_service_endpoints_work_with_auth(api, path):
    client, _ = api
    r = await client.get(f"{path}?{auth_q()}")
    assert r.status_code == 200


async def test_stats_does_not_leak_without_auth(api):
    """Главное: статистика библиотеки больше не видна анонимно."""
    client, _ = api
    r = await client.get("/api/v1/stats")
    assert r.status_code == 401
    assert "tracks" not in r.text


async def test_healthz_stays_open(api):
    """HEALTHCHECK в Dockerfile не имеет учётных данных — должен работать."""
    client, _ = api
    r = await client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# Security-заголовки
async def test_security_headers_present(api):
    client, _ = api
    r = await client.get("/healthz")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer"


async def test_csp_only_on_html(api):
    """CSP на аудио бессмыслен, на HTML — обязателен."""
    client, app = api

    r = await client.get("/login")
    assert r.status_code == 200
    assert "content-security-policy" in r.headers

    # Аудио: CSP отсутствует.
    path = make_mp3(Path(app.state.ctx.settings.music_dir) / "csp.mp3")
    tag_mp3(path, title="CSP")
    await app.state.ctx.scanner.scan(mode="full")
    track = (await app.state.ctx.tracks.list_tracks())[0]
    r = await client.get(f"/stream?id={track.id}&{auth_q()}")
    assert r.status_code == 200
    assert "content-security-policy" not in r.headers


async def test_streaming_not_buffered_by_middleware(api):
    """Middleware не должен ломать потоковую отдачу перекодированного аудио."""
    client, app = api
    from tests.conftest import make_flac

    make_flac(Path(app.state.ctx.settings.music_dir) / "s.flac", seconds=1.0)
    await app.state.ctx.scanner.scan(mode="full")
    track = (await app.state.ctx.tracks.list_tracks())[0]

    r = await client.get(f"/stream?id={track.id}&format=mp3_320&{auth_q()}")
    assert r.status_code == 200
    assert len(r.content) > 1000
    assert r.headers["x-mtr-strategy"] == "encode"


# /docs: схема обязана строиться, CDN разрешён только там
async def test_openapi_schema_builds(api):
    """Регрессия: аннотация без импорта ломала всю схему (500)."""
    client, _ = api
    r = await client.get("/openapi.json")
    assert r.status_code == 200
    assert r.json()["paths"], "схема пустая"


async def test_docs_page_opens(api):
    client, _ = api
    for path in ("/docs", "/redoc"):
        r = await client.get(path)
        assert r.status_code == 200, f"{path} не открывается"
        assert "text/html" in r.headers["content-type"]


async def test_docs_csp_allows_cdn(api):
    """Swagger UI грузит JS/CSS с cdn.jsdelivr.net — иначе страница пустая."""
    client, _ = api
    csp = (await client.get("/docs")).headers["content-security-policy"]
    assert "cdn.jsdelivr.net" in csp


async def test_main_page_csp_does_not_allow_cdn(api):
    """Ослабление действует только на /docs, не на остальные страницы."""
    client, _ = api
    response = await client.get("/login")
    csp = response.headers["content-security-policy"]
    assert "cdn.jsdelivr.net" not in csp


async def test_every_route_serialises_to_openapi():
    """Каждый маршрут должен попадать в схему.

    Прямая регрессия на PydanticUserError: аннотация возвращаемого типа,
    которую нельзя разрешить, ломает /openapi.json целиком.
    """
    from fastapi import FastAPI
    from fastapi.openapi.utils import get_openapi

    from app.main import app

    broken = []
    for route in app.routes:
        if not (hasattr(route, "path") and hasattr(route, "endpoint")):
            continue
        probe = FastAPI()
        probe.router.routes.append(route)
        try:
            get_openapi(title="probe", version="1", routes=probe.routes)
        except Exception as exc:  # noqa: BLE001
            broken.append((route.path, type(exc).__name__))
    assert broken == [], f"маршруты ломают OpenAPI: {broken}"
