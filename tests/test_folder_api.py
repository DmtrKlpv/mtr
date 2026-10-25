"""HTTP-слой импорта из папки и ресинка (app/web/folder_api.py)."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from tests.conftest import make_mp3, tag_mp3
from tests.test_web import login_admin
from tests.test_web import web  # noqa: F401  # noqa: F401  (фикстура web)


def _track(path: Path, title: str, album: str, track: str) -> None:
    make_mp3(path, seconds=1.0)
    tag_mp3(path, title=title, artist="Artist1", album=album, track=track)


async def _wait(client, job: dict, csrf: str | None = None) -> dict:
    """Опрос задачи до завершения, как это делает интерфейс."""
    for _ in range(200):
        if job["state"] != "running":
            return job
        await asyncio.sleep(0.05)
        r = await client.get(f"/api/web/playlists/folder-jobs/{job['id']}")
        job = r.json()["data"]
    raise AssertionError(f"задача не завершилась: {job}")


async def test_folder_scan_job_creates_playlist(web):
    client, app = web
    csrf = await login_admin(client)
    music = app.state.ctx.settings.music_dir
    _track(music / "Artist1" / "Album" / "01.mp3", "One", "Album", "1")
    _track(music / "Artist1" / "Album" / "02.mp3", "Two", "Album", "2")
    await app.state.ctx.scanner.scan(mode="incremental")

    r = await client.post(
        "/api/web/playlists/folder-scan",
        json={"folder": str(music / "Artist1")},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 202, r.text
    job = await _wait(client, r.json()["data"])
    assert job["state"] == "done", job
    assert job["result"]["name"] == "Artist1"
    assert job["result"]["matched"] == 2
    assert job["percent"] == 100


async def test_folder_scan_rejects_path_outside_library(web):
    client, _ = web
    csrf = await login_admin(client)
    r = await client.post(
        "/api/web/playlists/folder-scan",
        json={"folder": "/etc"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 400
    assert r.json()["code"] == "folder_outside_root"


async def test_resync_preview_reports_stale_library(web):
    client, app = web
    csrf = await login_admin(client)
    music = app.state.ctx.settings.music_dir
    _track(music / "Artist1" / "A" / "01.mp3", "One", "A", "1")
    await app.state.ctx.scanner.scan(mode="incremental")
    r = await client.post(
        "/api/web/playlists/folder-scan",
        json={"folder": str(music / "Artist1")}, headers={"X-CSRF-Token": csrf},
    )
    pl_id = (await _wait(client, r.json()["data"]))["result"]["playlist_id"]

    # Папка изменилась после скана — ресинк должен отказать.
    later = os.stat(music / "Artist1").st_mtime + 300
    os.utime(music / "Artist1", (later, later))

    r = await client.post(
        f"/api/web/playlists/{pl_id}/resync/preview", headers={"X-CSRF-Token": csrf},
    )
    job = await _wait(client, r.json()["data"])
    assert job["state"] == "error"
    assert job["error_code"] == "library_stale"
    assert "Сначала запустите сканирование сервера" in job["error"]


async def test_resync_full_flow_relocate_then_confirm(web):
    client, app = web
    csrf = await login_admin(client)
    music = app.state.ctx.settings.music_dir
    _track(music / "Artist1" / "A" / "01.mp3", "One", "A", "1")
    _track(music / "Artist1" / "A" / "02.mp3", "Two", "A", "2")
    await app.state.ctx.scanner.scan(mode="incremental")
    r = await client.post(
        "/api/web/playlists/folder-scan",
        json={"folder": str(music / "Artist1")}, headers={"X-CSRF-Token": csrf},
    )
    pl_id = (await _wait(client, r.json()["data"]))["result"]["playlist_id"]

    # Файл переехал в другую папку, сервер проиндексировал.
    os.replace(music / "Artist1" / "A" / "01.mp3", music / "Artist1" / "01-moved.mp3")
    await app.state.ctx.scanner.scan(mode="incremental")

    r = await client.post(
        f"/api/web/playlists/{pl_id}/resync/preview", headers={"X-CSRF-Token": csrf},
    )
    job = await _wait(client, r.json()["data"])
    assert job["state"] == "done", job
    plan = job["result"]["plan"]
    assert len(plan["relocate"]) == 1 and plan["remove"] == []

    r = await client.post(
        f"/api/web/playlists/{pl_id}/resync/confirm",
        json={"token": job["result"]["token"], "remove_link_ids": []},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["relocated"] == 1

    # Токен одноразовый.
    again = await client.post(
        f"/api/web/playlists/{pl_id}/resync/confirm",
        json={"token": job["result"]["token"], "remove_link_ids": []},
        headers={"X-CSRF-Token": csrf},
    )
    assert again.status_code == 404


async def test_job_unknown_id_is_404(web):
    client, _ = web
    await login_admin(client)
    r = await client.get("/api/web/playlists/folder-jobs/nope")
    assert r.status_code == 404
