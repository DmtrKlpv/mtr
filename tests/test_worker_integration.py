"""Интеграция веб ↔ worker (этап 17): плейлист отдаёт album_id, остановка не оставляет сирот."""

from __future__ import annotations

import asyncio
import os
import time

from app.worker import ipc
from app.worker.supervisor import WorkerSupervisor
from tests.conftest import make_mp3, tag_mp3
from tests.test_web import login_admin, web  # noqa: F401


async def test_playlist_tracks_expose_album_id(web):
    """Сборные обложки плейлиста строятся по album_id: поле должно быть в ответе."""
    client, app = web
    csrf = await login_admin(client)
    ctx = app.state.ctx
    music = ctx.settings.music_dir
    for i in range(2):
        p = make_mp3(music / f"al{i}" / "t.mp3", seconds=0.2)
        tag_mp3(p, title=f"T{i}", artist="A", album=f"Alb{i}")
    ipc.write_status(ipc.status_path(ctx.settings.data_dir), {"scan": {"status": "idle"}})
    from app.worker.runtime import WorkerRuntime
    await ipc.enqueue(ctx.music_db, "scan", {"mode": "full"})
    runtime = WorkerRuntime(ctx, poll_sec=0.01)
    await runtime.handle_commands()
    await runtime.wait_scan(timeout=60)

    created = await client.post("/api/web/playlists", json={"name": "Mix"},
                                headers={"X-CSRF-Token": csrf})
    pid = created.json()["data"]["id"]
    track_ids = [int(r["id"]) for r in await ctx.music_db.fetch_all("SELECT id FROM tracks")]
    r = await client.post(f"/api/web/playlists/{pid}/tracks", json={"track_ids": track_ids},
                          headers={"X-CSRF-Token": csrf})
    assert r.status_code in (200, 201), r.text
    detail = (await client.get(f"/api/web/playlists/{pid}")).json()["data"]
    tracks = detail["tracks"]
    assert tracks and all("album_id" in t for t in tracks)
    assert all(t["album_id"] is not None for t in tracks)


async def test_supervisor_starts_worker_and_stop_leaves_no_orphans(settings):
    """Супервизор поднимает живой worker (heartbeat), а stop() его завершает."""
    data = settings.data_dir
    supervisor = WorkerSupervisor(settings)
    await supervisor.start()
    pid = None
    try:
        deadline = time.monotonic() + 45
        status = {}
        while time.monotonic() < deadline:
            status = ipc.read_status(ipc.status_path(data))
            if ipc.worker_alive(status):
                break
            await asyncio.sleep(0.2)
        assert ipc.worker_alive(status), "worker не дал heartbeat за 45 с"
        pid = int(status["pid"])
        assert pid != os.getpid()
    finally:
        await supervisor.stop()
    await asyncio.sleep(0.5)
    if pid is not None:
        assert not _alive(pid), "worker остался после остановки супервизора"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
