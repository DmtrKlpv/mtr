"""Этап 20: импорт m3u с прогрессом, крошка «Альбомы», чистка сервера."""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.playlists.import_jobs import ImportItem, ImportJob
from tests.conftest import make_mp3, tag_mp3
from tests.test_web import login_admin, web  # noqa: F401


def test_import_job_progress_is_monotonic():
    """Процент не превышает 99 до завершения и становится 100 в конце."""
    job = ImportJob(id="x", total=4)
    assert job.percent == 0
    job.done = 2
    assert job.percent == 50
    job.done = 4
    assert job.percent == 99      # ещё не done: 100 показывать рано
    job.state = "done"
    assert job.percent == 100


def test_import_job_empty_total_does_not_divide_by_zero():
    job = ImportJob(id="x")
    assert job.percent == 0


def test_import_item_states():
    item = ImportItem(path="/music/a.m3u", name="a")
    assert item.state == "pending"          # жёлтый вопрос в интерфейсе
    item.state = "done"
    item.total, item.matched = 3, 2
    data = item.as_dict()
    assert data["matched"] == 2 and data["total"] == 3


async def test_import_start_reports_per_playlist_progress(web, tmp_path: Path):
    """Импорт запускается в фоне и отдаёт прогресс по каждому плейлисту."""
    client, app = web
    csrf = await login_admin(client)
    ctx = app.state.ctx
    music = ctx.settings.music_dir

    # Два плейлиста в отдельной папке внутри библиотеки.
    song1 = make_mp3(music / "a" / "1.mp3", seconds=0.2)
    tag_mp3(song1, title="One", artist="A", album="Alb1")
    song2 = make_mp3(music / "b" / "2.mp3", seconds=0.2)
    tag_mp3(song2, title="Two", artist="B", album="Alb2")
    await ctx.scanner.scan(mode="full")

    pl_dir = music / "lists"
    pl_dir.mkdir(parents=True, exist_ok=True)
    (pl_dir / "one.m3u").write_text(f"#EXTM3U\n{song1}\n", encoding="utf-8")
    (pl_dir / "two.m3u").write_text(f"#EXTM3U\n{song2}\n", encoding="utf-8")

    started = await client.post(
        "/api/web/playlists/import/start",
        json={"dir": str(pl_dir)},
        headers={"X-CSRF-Token": csrf},
    )
    assert started.status_code == 202, started.text
    job_id = started.json()["data"]["id"]

    state = {}
    for _ in range(200):
        state = (await client.get(f"/api/web/playlists/import/jobs/{job_id}")).json()["data"]
        if state["state"] in ("done", "error"):
            break
        await asyncio.sleep(0.05)

    assert state["state"] == "done", state
    assert state["total"] == 2
    assert state["percent"] == 100
    # По каждому плейлисту есть отдельное состояние — это и рисует интерфейс.
    items = state["items"]
    assert len(items) == 2
    assert all(item["state"] == "done" for item in items)
    assert {item["name"] for item in items} == {"one", "two"}
    assert state["result"]["total_files"] == 2


async def test_import_progress_unknown_job_is_404(web):
    client, _ = web
    await login_admin(client)
    r = await client.get("/api/web/playlists/import/jobs/nope")
    assert r.status_code == 404


async def test_import_apply_uses_plans_from_job(web, tmp_path: Path):
    """Применение не разбирает папку заново: берёт планы из задачи."""
    client, app = web
    csrf = await login_admin(client)
    ctx = app.state.ctx
    music = ctx.settings.music_dir
    song = make_mp3(music / "a" / "1.mp3", seconds=0.2)
    tag_mp3(song, title="One", artist="A", album="Alb1")
    await ctx.scanner.scan(mode="full")

    pl_dir = music / "lists"
    pl_dir.mkdir(parents=True, exist_ok=True)
    (pl_dir / "mix.m3u").write_text(f"#EXTM3U\n{song}\n", encoding="utf-8")

    started = await client.post(
        "/api/web/playlists/import/start",
        json={"dir": str(pl_dir)},
        headers={"X-CSRF-Token": csrf},
    )
    job_id = started.json()["data"]["id"]
    for _ in range(200):
        state = (await client.get(f"/api/web/playlists/import/jobs/{job_id}")).json()["data"]
        if state["state"] in ("done", "error"):
            break
        await asyncio.sleep(0.05)
    assert state["state"] == "done"

    path = state["items"][0]["path"]
    applied = await client.post(
        "/api/web/playlists/import/apply",
        json={"job_id": job_id, "items": [{"path": path, "action": "create"}]},
        headers={"X-CSRF-Token": csrf},
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["data"]["created"] == 1
