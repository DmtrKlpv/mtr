"""Тесты этапа 6: веб-интерфейс и полное управление сервером.

Главное, что проверяем — безопасность и разграничение прав:

* без сессии нет доступа ни к страницам, ни к API;
* CSRF обязателен для всех изменяющих методов;
* обычный пользователь не видит админ-разделы и не может управлять сервером;
* выход и блокировка немедленно лишают доступа;
* path traversal в статике невозможен.
"""

from __future__ import annotations

import asyncio

from collections.abc import AsyncIterator

import httpx
import pytest

from app.web.sessions import SESSION_COOKIE, _hash_token

PASSWORD = "adminpass12345"
USER_PASSWORD = "userpass12345"


@pytest.fixture
async def web(settings, monkeypatch, music_dir) -> AsyncIterator[tuple]:
    monkeypatch.setenv("MUSIC_DIR", str(settings.music_dir))
    monkeypatch.setenv("DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("MTR_SECRET_KEY", settings.secret_key or "test")

    from app.main import app
    from tests.conftest import make_mp3, tag_mp3

    plan = [
        ("Queen", "Opera", "Bohemian"),
        ("Queen", "Opera", "Love"),
        ("Solo", "X", "Alone"),
    ]
    for index, (artist, album, title) in enumerate(plan):
        path = make_mp3(music_dir / f"{index}.mp3")
        tag_mp3(path, title=title, artist=artist, album=album)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as client:
        async with app.router.lifespan_context(app):
            await app.state.ctx.users.create_user("admin", PASSWORD, role="admin")
            await app.state.ctx.users.create_user("bob", USER_PASSWORD, role="user")
            await app.state.ctx.scanner.scan(mode="full")
            yield client, app


async def login(client: httpx.AsyncClient, username: str, password: str):
    response = await client.post(
        "/api/web/login", json={"username": username, "password": password}
    )
    return response, response.cookies.get("mtr_csrf")


async def login_admin(client: httpx.AsyncClient) -> str:
    response, csrf = await login(client, "admin", PASSWORD)
    assert response.status_code == 200, response.text
    return csrf


# ----------------------------------------------------------------------
# Доступ без сессии
# ----------------------------------------------------------------------
async def test_root_redirects_to_login(web):
    client, _ = web
    response = await client.get("/")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


async def test_app_page_requires_session(web):
    client, _ = web
    response = await client.get("/app")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


@pytest.mark.parametrize(
    "path",
    [
        "/api/web/session", "/api/web/stats", "/api/web/albums",
        "/api/web/playlists", "/api/web/users", "/api/web/server",
    ],
)
async def test_api_requires_session(web, path):
    client, _ = web
    response = await client.get(path)
    assert response.status_code == 401


async def test_login_page_available_anonymously(web):
    client, _ = web
    response = await client.get("/login")
    assert response.status_code == 200
    assert "login-form" in response.text


# ----------------------------------------------------------------------
# Вход
# ----------------------------------------------------------------------
async def test_login_success_sets_cookies(web):
    client, _ = web
    response, csrf = await login(client, "admin", PASSWORD)
    assert response.status_code == 200
    assert SESSION_COOKIE in response.cookies
    assert csrf

    cookie_header = response.headers.get("set-cookie", "")
    assert "HttpOnly" in cookie_header
    assert "samesite=lax" in cookie_header.lower()


async def test_login_wrong_password_401(web):
    client, _ = web
    response, _ = await login(client, "admin", "wrong-password")
    assert response.status_code == 401


async def test_login_unknown_user_same_response(web):
    """Нельзя различить «нет пользователя» и «неверный пароль»."""
    client, _ = web
    r1, _ = await login(client, "ghost", PASSWORD)
    r2, _ = await login(client, "admin", "wrong-password")
    assert r1.status_code == r2.status_code == 401
    assert r1.json()["detail"] == r2.json()["detail"]


async def test_login_missing_fields_400(web):
    client, _ = web
    response = await client.post("/api/web/login", json={"username": "admin"})
    assert response.status_code == 400


async def test_inactive_user_cannot_login(web):
    client, app = web
    record = await app.state.ctx.users.get_by_username("bob")
    await app.state.ctx.users.set_active(record.id, False)
    response, _ = await login(client, "bob", USER_PASSWORD)
    assert response.status_code == 401


async def test_login_page_redirects_when_authenticated(web):
    client, _ = web
    await login_admin(client)
    response = await client.get("/login")
    assert response.status_code == 303
    assert response.headers["location"] == "/app"


# ----------------------------------------------------------------------
# Сессия хранится хэшем
# ----------------------------------------------------------------------
async def test_session_token_is_hashed_in_db(web):
    """В базе не должно быть открытого токена: утечка БД не даёт войти."""
    client, app = web
    response, _ = await login(client, "admin", PASSWORD)
    raw_token = response.cookies.get(SESSION_COOKIE)

    rows = await app.state.ctx.users_db.fetch_all("SELECT token_hash FROM sessions")
    stored = [row["token_hash"] for row in rows]
    assert stored, "сессия не записана"
    assert raw_token not in stored, "токен хранится в открытом виде"
    assert _hash_token(raw_token) in stored


# ----------------------------------------------------------------------
# CSRF
# ----------------------------------------------------------------------
async def test_post_without_csrf_forbidden(web):
    client, _ = web
    await login_admin(client)
    response = await client.post(
        "/api/web/users", json={"username": "x", "password": "y"}
    )
    assert response.status_code == 403


async def test_post_with_wrong_csrf_forbidden(web):
    client, _ = web
    await login_admin(client)
    response = await client.post(
        "/api/web/users",
        json={"username": "x", "password": "y"},
        headers={"X-CSRF-Token": "definitely-wrong"},
    )
    assert response.status_code == 403


async def test_post_with_csrf_allowed(web):
    client, _ = web
    csrf = await login_admin(client)
    response = await client.post(
        "/api/web/users",
        json={"username": "carol", "password": "carolpass12345"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 201


async def test_get_does_not_require_csrf(web):
    client, _ = web
    await login_admin(client)
    assert (await client.get("/api/web/users")).status_code == 200


# ----------------------------------------------------------------------
# Роли
# ----------------------------------------------------------------------
async def test_regular_user_cannot_list_users(web):
    client, _ = web
    await login(client, "bob", USER_PASSWORD)
    assert (await client.get("/api/web/users")).status_code == 403


async def test_regular_user_cannot_see_server(web):
    client, _ = web
    await login(client, "bob", USER_PASSWORD)
    assert (await client.get("/api/web/server")).status_code == 403


async def test_regular_user_cannot_scan(web):
    client, _ = web
    csrf = (await login(client, "bob", USER_PASSWORD))[1]
    response = await client.post("/api/web/scan", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 403


async def test_regular_user_cannot_create_users(web):
    client, _ = web
    csrf = (await login(client, "bob", USER_PASSWORD))[1]
    response = await client.post(
        "/api/web/users",
        json={"username": "evil", "password": "evilpass12345", "role": "admin"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 403


async def test_regular_user_can_read_library(web):
    client, _ = web
    await login(client, "bob", USER_PASSWORD)
    assert (await client.get("/api/web/stats")).status_code == 200
    assert (await client.get("/api/web/albums")).status_code == 200


# ----------------------------------------------------------------------
# Управление пользователями
# ----------------------------------------------------------------------
async def test_create_and_list_users(web):
    client, _ = web
    csrf = await login_admin(client)
    await client.post(
        "/api/web/users",
        json={"username": "dave", "password": "davepass12345"},
        headers={"X-CSRF-Token": csrf},
    )
    data = (await client.get("/api/web/users")).json()["data"]["items"]
    assert {user["username"] for user in data} == {"admin", "bob", "dave"}


async def test_duplicate_username_rejected(web):
    client, _ = web
    csrf = await login_admin(client)
    response = await client.post(
        "/api/web/users",
        json={"username": "bob", "password": "anotherpass123"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 400


async def test_reset_password_revokes_sessions(web):
    """Смена пароля админом выкидывает пользователя со всех устройств."""
    client, app = web
    bob = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    await login(bob, "bob", USER_PASSWORD)
    assert (await bob.get("/api/web/session")).status_code == 200

    csrf = await login_admin(client)
    record = await app.state.ctx.users.get_by_username("bob")
    response = await client.post(
        f"/api/web/users/{record.id}/password",
        json={"password": "brandnewpass123"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    assert response.json()["data"]["sessions_revoked"] >= 1
    assert (await bob.get("/api/web/session")).status_code == 401
    await bob.aclose()


async def test_block_user_revokes_sessions(web):
    client, app = web
    bob = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    await login(bob, "bob", USER_PASSWORD)

    csrf = await login_admin(client)
    record = await app.state.ctx.users.get_by_username("bob")
    await client.post(
        f"/api/web/users/{record.id}/active",
        json={"is_active": False},
        headers={"X-CSRF-Token": csrf},
    )
    assert (await bob.get("/api/web/session")).status_code == 401
    await bob.aclose()


async def test_cannot_delete_self(web):
    client, app = web
    csrf = await login_admin(client)
    me = await app.state.ctx.users.get_by_username("admin")
    response = await client.delete(
        f"/api/web/users/{me.id}", headers={"X-CSRF-Token": csrf}
    )
    assert response.status_code == 400
    assert "собственную" in response.json()["detail"]


async def test_cannot_demote_last_admin(web):
    client, app = web
    csrf = await login_admin(client)
    me = await app.state.ctx.users.get_by_username("admin")
    response = await client.post(
        f"/api/web/users/{me.id}/role",
        json={"role": "user"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 400


# ----------------------------------------------------------------------
# Библиотека и сервер
# ----------------------------------------------------------------------
async def test_albums_and_search(web):
    client, _ = web
    await login_admin(client)
    albums = (await client.get("/api/web/albums")).json()["data"]["items"]
    assert any(album["name"] == "Opera" and album["song_count"] == 2 for album in albums)

    found = (await client.get("/api/web/search?q=Bohemian")).json()["data"]
    assert [track["title"] for track in found["tracks"]] == ["Bohemian"]


async def test_server_info_for_admin(web):
    client, _ = web
    await login_admin(client)
    data = (await client.get("/api/web/server")).json()["data"]
    assert data["version"]
    assert data["schema"]["users"] >= 2
    assert data["music_dir_exists"] is True


async def test_scan_endpoint(web):
    """Скан ставится в очередь worker-процесса; здесь worker имитируется вручную."""
    from app.worker import ipc
    from app.worker.runtime import WorkerRuntime

    client, app = web
    csrf = await login_admin(client)
    ctx = app.state.ctx
    # Worker «жив»: свежий heartbeat. Без него веб отвечает 503 (см. test_worker).
    ipc.write_status(ipc.status_path(ctx.settings.data_dir), {"scan": {"status": "idle"}})
    response = await client.post(
        "/api/web/scan?mode=full", headers={"X-CSRF-Token": csrf}
    )
    assert response.status_code == 202

    runtime = WorkerRuntime(ctx, poll_sec=0.01)
    assert await runtime.handle_commands() == 1
    await runtime.wait_scan(timeout=60)
    await runtime.publish_status()

    status = (await client.get("/api/web/scan/status")).json()["data"]
    assert status["current"]["status"] == "completed"
    assert status["current"]["seen"] == 3
    assert status["worker"]["alive"] is True


# ----------------------------------------------------------------------
# Аккаунт
# ----------------------------------------------------------------------
async def test_change_own_password(web):
    client, app = web
    csrf = (await login(client, "bob", USER_PASSWORD))[1]
    response = await client.post(
        "/api/web/account/password",
        json={"current": USER_PASSWORD, "next": "newbobpass12345"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200

    fresh = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    assert (await login(fresh, "bob", "newbobpass12345"))[0].status_code == 200
    assert (await login(fresh, "bob", USER_PASSWORD))[0].status_code == 401
    await fresh.aclose()


async def test_change_password_wrong_current(web):
    client, _ = web
    csrf = (await login(client, "bob", USER_PASSWORD))[1]
    response = await client.post(
        "/api/web/account/password",
        json={"current": "wrong", "next": "whatever12345"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 400


async def test_list_own_sessions(web):
    client, _ = web
    await login_admin(client)
    data = (await client.get("/api/web/account/sessions")).json()["data"]
    assert len(data["items"]) >= 1
    assert data["current_id"]


async def test_cannot_revoke_foreign_session(web):
    client, app = web
    bob = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    await login(bob, "bob", USER_PASSWORD)
    bob_sessions = (await bob.get("/api/web/account/sessions")).json()["data"]["items"]
    bob_session_id = bob_sessions[0]["id"]

    csrf = await login_admin(client)
    response = await client.delete(
        f"/api/web/account/sessions/{bob_session_id}",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 404
    assert (await bob.get("/api/web/session")).status_code == 200
    await bob.aclose()


# ----------------------------------------------------------------------
# Выход
# ----------------------------------------------------------------------
async def test_logout_invalidates_session(web):
    client, app = web
    csrf = await login_admin(client)
    assert await app.state.ctx.users_db.fetch_all("SELECT id FROM sessions")

    response = await client.post("/api/web/logout", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200
    assert await app.state.ctx.users_db.fetch_all("SELECT id FROM sessions") == []


# ----------------------------------------------------------------------
# Статика
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "name,mime",
    [("app.js", "javascript"), ("login.js", "javascript"), ("style.css", "css")],
)
async def test_static_files_served(web, name, mime):
    client, _ = web
    response = await client.get(f"/static/{name}")
    assert response.status_code == 200
    assert mime in response.headers["content-type"]


@pytest.mark.parametrize(
    "name", ["../main.py", "unknown.txt", "config.py", "database.py"]
)
async def test_static_rejects_unknown_and_traversal(web, name):
    client, _ = web
    response = await client.get(f"/static/{name}")
    assert response.status_code in (404, 400, 307)


# ----------------------------------------------------------------------
# Троттлинг входа
# ----------------------------------------------------------------------
async def test_login_throttled_after_failures(web):
    client, _ = web
    for _ in range(11):
        await client.post(
            "/api/web/login", json={"username": "admin", "password": "wrong"}
        )
    response, _ = await login(client, "admin", PASSWORD)
    assert response.status_code == 429


# ----------------------------------------------------------------------
# Плеер: бинарные методы принимают сессию ИЛИ Subsonic-токен
# ----------------------------------------------------------------------
async def test_stream_works_with_session_cookie(web):
    """Без этого музыка в веб-интерфейсе не играла бы: браузер не имеет токена."""
    client, _ = web
    await login_admin(client)
    response = await client.get("/stream?id=1")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/")


async def test_stream_still_requires_auth_anonymously(web):
    client, _ = web
    assert (await client.get("/stream?id=1")).status_code == 401


async def test_rest_stream_works_with_session_cookie(web):
    client, _ = web
    await login_admin(client)
    response = await client.get("/rest/stream.view?id=1")
    assert response.status_code == 200


async def test_cover_art_works_with_session_cookie(web):
    """Обложки в сетке альбомов грузятся по cookie, не по токену."""
    client, app = web
    await login_admin(client)
    # Кладём обложку рядом с треком и сбрасываем кэш альбомов.
    ctx = app.state.ctx
    music_dir = ctx.settings.music_dir
    # Фоновая фаза обложек после скана могла уже записать «картинки нет».
    # Дождаться её и снять маркеры, как при появлении обложки в тегах.
    for _ in range(500):
        if not ctx.cover_pass.running:
            break
        await asyncio.sleep(0.02)
    (music_dir / "cover.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"x" * 64)
    await ctx.covers.clear_absent_markers()

    albums = (await client.get("/api/web/albums")).json()["data"]["items"]
    response = await client.get(f"/rest/getCoverArt?id=al-{albums[0]['id']}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"


async def test_subsonic_token_still_works_without_cookie(web):
    """Subsonic-клиенты cookie не умеют — токен должен работать как раньше."""
    import hashlib

    client, _ = web
    salt = "abc123def456"
    token = hashlib.md5((PASSWORD + salt).encode()).hexdigest()
    query = f"u=admin&t={token}&s={salt}&v=1.16.1&c=test"

    fresh = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=web[1]), base_url="http://test"
    )
    assert (await fresh.get(f"/rest/stream.view?{query}&id=1")).status_code == 200
    await fresh.aclose()
