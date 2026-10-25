"""Фавикон и иконки PWA: файлы на месте, отдаются с правильным типом,
а страницы их объявляют."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

STATIC = Path(__file__).resolve().parent.parent / "app" / "web" / "static"

ICON_FILES = {
    "favicon.ico": "image/x-icon",
    "favicon-32.png": "image/png",
    "apple-touch-icon.png": "image/png",
    "icon-192.png": "image/png",
    "icon-512.png": "image/png",
    "icon.svg": "image/svg+xml",
    "manifest.webmanifest": "application/manifest+json",
}


@pytest.mark.parametrize("name", sorted(ICON_FILES))
def test_icon_file_exists_and_not_empty(name):
    path = STATIC / name
    assert path.is_file(), f"нет файла {name}"
    assert path.stat().st_size > 100


def test_png_icons_have_expected_sizes():
    from PIL import Image

    expected = {"favicon-32.png": (32, 32), "icon-192.png": (192, 192),
                "icon-512.png": (512, 512), "apple-touch-icon.png": (180, 180)}
    for name, size in expected.items():
        assert Image.open(STATIC / name).size == size, name


def test_manifest_is_valid_json_and_points_to_existing_icons():
    import json

    manifest = json.loads((STATIC / "manifest.webmanifest").read_text(encoding="utf-8"))
    for icon in manifest["icons"]:
        local = STATIC / icon["src"].replace("/static/", "")
        assert local.is_file(), icon["src"]


@pytest.fixture
async def client(settings, monkeypatch, music_dir):
    monkeypatch.setenv("MUSIC_DIR", str(settings.music_dir))
    monkeypatch.setenv("DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("MTR_SECRET_KEY", settings.secret_key or "test")
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        async with app.router.lifespan_context(app):
            yield c


@pytest.mark.parametrize("name,mime", sorted(ICON_FILES.items()))
async def test_icons_served_with_correct_type(client, name, mime):
    response = await client.get(f"/static/{name}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(mime)


async def test_root_favicon_served(client):
    """Браузер запрашивает /favicon.ico сам, без ссылки в HTML."""
    response = await client.get("/favicon.ico")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/x-icon")


async def test_login_page_declares_icons(client):
    # На пустой базе /login уводит на /setup — проверяем ту страницу, которая
    # реально отдаётся. Иконки общие: обе страницы собираются из HEAD_COMMON.
    html = (await client.get("/setup")).text
    assert 'rel="icon"' in html
    assert "apple-touch-icon" in html
    assert "manifest.webmanifest" in html


async def test_icons_not_blocked_by_csp(client):
    """Иконки с 'self' не должны попадать под запрет CSP."""
    response = await client.get("/setup")
    csp = response.headers["content-security-policy"]
    assert "img-src 'self'" in csp
