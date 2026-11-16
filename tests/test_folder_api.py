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
    assert [p["name"] for p in job["result"]["playlists"]] == ["Album"]
    assert job["result"]["playlists"][0]["matched"] == 2
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
    pl_id = (await _wait(client, r.json()["data"]))["result"]["playlists"][0]["playlist_id"]

    # Папка изменилась после скана — ресинк должен отказать.
    later = os.stat(music / "Artist1" / "A").st_mtime + 300
    os.utime(music / "Artist1" / "A", (later, later))

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
    pl_id = (await _wait(client, r.json()["data"]))["result"]["playlists"][0]["playlist_id"]

    # Файл перешёл в другую папку, сервер проиндексировал.
    (music / "Artist1" / "A" / "moved").mkdir()
    os.replace(music / "Artist1" / "A" / "01.mp3", music / "Artist1" / "A" / "moved" / "01-moved.mp3")

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


async def test_folder_roots_lists_first_level_folders_only(web):
    client, app = web
    await login_admin(client)
    music = app.state.ctx.settings.music_dir
    _track(music / "Zeta" / "A" / "01.mp3", "One", "A", "1")
    _track(music / "Alpha" / "01.mp3", "Two", "Alpha", "1")
    r = await client.get("/api/web/music-folders")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["root"] == str(music)
    names = [item["name"] for item in data["items"]]
    assert names[:2] == ["Alpha", "Zeta"]
    assert data["items"][0]["path"] == str(music / "Alpha")


async def test_synced_list_contains_every_playlist_built_from_folder(web):
    client, app = web
    csrf = await login_admin(client)
    music = app.state.ctx.settings.music_dir
    root = music / "Дискографии"
    _track(root / "Artist1" / "Album" / "01.mp3", "One", "Album", "1")
    _track(root / "Artist2" / "02.mp3", "Two", "Artist2", "1")
    await app.state.ctx.scanner.scan(mode="incremental")

    r = await client.post(
        "/api/web/playlists/folder-scan",
        json={"folder": str(root)}, headers={"X-CSRF-Token": csrf},
    )
    job = await _wait(client, r.json()["data"])
    assert job["state"] == "done", job
    assert [p["name"] for p in job["result"]["playlists"]] == ["Artist1", "Artist2"]

    r = await client.get("/api/web/folder-synced-playlists")
    items = r.json()["data"]["items"]
    assert sorted(i["origin_folder_path"] for i in items) == [str(root / "Artist1"), str(root / "Artist2")]

    again = await client.post(
        "/api/web/playlists/folder-scan",
        json={"folder": str(root)}, headers={"X-CSRF-Token": csrf},
    )
    job2 = await _wait(client, again.json()["data"])
    assert job2["result"]["playlists"] == []
    assert len(job2["result"]["already_synced"]) == 2


async def test_job_unknown_id_is_404(web):
    client, _ = web
    await login_admin(client)
    r = await client.get("/api/web/playlists/folder-jobs/nope")
    assert r.status_code == 404
