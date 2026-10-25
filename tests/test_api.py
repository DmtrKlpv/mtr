"""Тесты HTTP-слоя (этап 1): healthz, info, stats, заглушка UI.

Используется httpx ASGITransport — приложение поднимается в том же
процессе, но проходит полный lifespan, включая создание AppContext.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

import hashlib

from app import __version__
from app.db.migrations import MUSIC_SCHEMA_VERSION, SCHEMA_VERSION
from app.config import Settings

PASSWORD = "apipass12345"
SALT = "abc123def456"


def auth_q() -> str:
    """Параметры авторизации: служебные эндпоинты закрыты с этапа 3.3."""
    token = hashlib.md5((PASSWORD + SALT).encode()).hexdigest()
    return f"u=admin&t={token}&s={SALT}&v=1.16.1&c=test&f=json"


@pytest.fixture
async def client(settings: Settings, monkeypatch) -> AsyncIterator[httpx.AsyncClient]:
    """Клиент с подставленными через env путями к временным БД.

    Settings.from_env() читается внутри lifespan, поэтому окружение
    подменяется до старта приложения — иначе оно полезло бы в /data.
    """
    monkeypatch.setenv("MUSIC_DIR", str(settings.music_dir))
    monkeypatch.setenv("DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("USERS_DB", str(settings.users_db))
    monkeypatch.setenv("MUSIC_DB", str(settings.music_db))
    monkeypatch.setenv("COVERS_DIR", str(settings.covers_dir))
    monkeypatch.setenv("MTR_SECRET_KEY", settings.secret_key or "test")

    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        # ASGITransport не выполняет lifespan сам — запускаем вручную.
        async with app.router.lifespan_context(app):
            await app.state.ctx.users.create_user("admin", PASSWORD, role="admin")
            yield ac


async def test_healthz_ok(client: httpx.AsyncClient):
    response = await client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__
    assert body["schema"] == {
        "users": SCHEMA_VERSION, "music": MUSIC_SCHEMA_VERSION
    }


async def test_info_reports_paths(client: httpx.AsyncClient, settings: Settings):
    response = await client.get(f"/api/v1/info?{auth_q()}")
    assert response.status_code == 200
    body = response.json()
    assert body["music_dir"] == str(settings.music_dir)
    assert body["music_dir_exists"] is True
    assert body["subsonic_api_version"] == "1.16.1"


async def test_stats_empty_library(client: httpx.AsyncClient):
    body = (await client.get(f"/api/v1/stats?{auth_q()}")).json()
    assert body["tracks"] == 0
    # Фикстура создаёт админа для доступа к эндпоинту.
    assert body["users"] == 1
    assert "total_duration_sec" in body
    assert "total_ms" not in body  # конвертировано в секунды


async def test_stats_reflect_indexed_library(
    client: httpx.AsyncClient, music_dir: Path
):
    """Индексация через CLI-путь видна HTTP-слою (это разные подключения)."""
    from tests.conftest import make_mp3, tag_mp3

    path = make_mp3(music_dir / "track.mp3", seconds=1.0)
    tag_mp3(path, title="T", artist="A", album="Al")

    from app.main import app

    ctx = app.state.ctx
    await ctx.scanner.scan(mode="full")

    body = (await client.get(f"/api/v1/stats?{auth_q()}")).json()
    assert body["tracks"] == 1
    assert body["artists"] == 1
    assert body["total_duration_sec"] >= 1


async def test_index_redirects_to_web_ui(client: httpx.AsyncClient):
    """С этапа 6 корень ведёт в веб-интерфейс, а не отдаёт заглушку."""
    response = await client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] in ("/login", "/app")


async def test_setup_creates_first_admin(settings: Settings, music_dir: Path, monkeypatch):
    """Первый администратор создаётся страницей /setup, а не переменными env."""
    monkeypatch.setenv("MUSIC_DIR", str(settings.music_dir))
    monkeypatch.setenv("DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("MTR_SECRET_KEY", settings.secret_key or "test")

    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        async with app.router.lifespan_context(app):
            # На пустой базе /login уводит на /setup.
            page = await ac.get("/login", follow_redirects=False)
            assert page.status_code == 303
            assert page.headers["location"] == "/setup"

            response = await ac.post(
                "/api/web/setup",
                json={"username": "bootadmin", "password": "bootpassword1"},
            )
            assert response.status_code == 200

            record = await app.state.ctx.users.get_by_username("bootadmin")
            assert record is not None
            assert record.role == "admin"
            # Пароль должен быть расшифровываемым — иначе Subsonic упадёт.
            assert await app.state.ctx.users.get_plain_password(record.id) == "bootpassword1"

            # После создания страница закрывается.
            again = await ac.get("/setup", follow_redirects=False)
            assert again.status_code == 303
            assert again.headers["location"] == "/login"


async def test_setup_rejects_second_admin(settings: Settings, music_dir: Path, monkeypatch):
    """Повторный вызов не создаёт второго администратора."""
    monkeypatch.setenv("MUSIC_DIR", str(settings.music_dir))
    monkeypatch.setenv("DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("MTR_SECRET_KEY", settings.secret_key or "test")

    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        async with app.router.lifespan_context(app):
            first = await ac.post(
                "/api/web/setup",
                json={"username": "admin", "password": "password123"},
            )
            assert first.status_code == 200

            second = await ac.post(
                "/api/web/setup",
                json={"username": "intruder", "password": "password123"},
            )
            assert second.status_code == 409
            assert await app.state.ctx.users.get_by_username("intruder") is None


async def test_setup_rejects_short_password(settings: Settings, music_dir: Path, monkeypatch):
    monkeypatch.setenv("MUSIC_DIR", str(settings.music_dir))
    monkeypatch.setenv("DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("MTR_SECRET_KEY", settings.secret_key or "test")

    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        async with app.router.lifespan_context(app):
            response = await ac.post(
                "/api/web/setup",
                json={"username": "admin", "password": "1234"},
            )
            assert response.status_code == 400
            assert await app.state.ctx.users.count_users() == 0
