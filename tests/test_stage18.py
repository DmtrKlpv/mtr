"""Этап 18: пакетный скан, поля для обложек треков и ссылки на исполнителя, настройки."""

from __future__ import annotations

from pathlib import Path

from app.scanner.library_scanner import Scanner
from app.repositories.tracks import TrackRepository
from app.db.database import Database
from app.db.migrations import _migrate_music
from tests.conftest import make_mp3, tag_mp3
from tests.test_web import login_admin, web  # noqa: F401


async def _db(path: Path) -> Database:
    db = Database(path, label="music")
    await db.connect()
    await _migrate_music(db)
    return db


def test_batch_size_defaults_to_all_at_once():
    """По умолчанию 0 — все файлы одним заходом (как раньше)."""
    scanner = Scanner(None, Path("/tmp"))  # конструктор не трогает БД
    assert scanner._batch_size == 0
    scanner.set_batch_size(100)
    assert scanner._batch_size == 100
    scanner.set_batch_size(-5)
    assert scanner._batch_size == 0


async def test_batched_scan_matches_single_pass(tmp_path: Path):
    """Скан пачками по 3 и одним заходом дают одинаковую библиотеку."""
    music = tmp_path / "music"
    for i in range(7):
        p = make_mp3(music / f"al{i % 3}" / f"{i}.mp3", seconds=0.2)
        tag_mp3(p, title=f"T{i}", artist=f"A{i % 2}", album=f"Alb{i % 3}")

    async def snapshot(batch: int, name: str) -> list[tuple]:
        db = await _db(tmp_path / f"{name}.db")
        try:
            scanner = Scanner(TrackRepository(db), music, parse_concurrency=2)
            scanner.set_batch_size(batch)
            result = await scanner.scan(mode="full")
            assert result.counters.failed == 0
            rows = await db.fetch_all(
                "SELECT path, title, artist, album FROM tracks ORDER BY path"
            )
            return [tuple(r) for r in rows]
        finally:
            await db.close()

    assert await snapshot(0, "all") == await snapshot(3, "batched")


async def test_album_payload_has_ids_for_covers_and_artist_link(web):
    """Альбом отдаёт artist_id и album_id у треков: без них не работают
    ссылка на исполнителя и обложки треков (al-<id>)."""
    from app.worker import ipc

    client, app = web
    csrf = await login_admin(client)
    ctx = app.state.ctx
    music = ctx.settings.music_dir
    p = make_mp3(music / "al" / "t.mp3", seconds=0.2)
    tag_mp3(p, title="T", artist="The Band", album="Alb")

    ipc.write_status(ipc.status_path(ctx.settings.data_dir), {"scan": {"status": "idle"}})
    from app.worker.runtime import WorkerRuntime
    await ipc.enqueue(ctx.music_db, "scan", {"mode": "full"})
    runtime = WorkerRuntime(ctx, poll_sec=0.01)
    await runtime.handle_commands()
    await runtime.wait_scan(timeout=60)

    # Альбом именно своего трека: в фикстуре уже могут быть другие альбомы.
    album_id = int(await ctx.music_db.fetch_value(
        "SELECT album_id FROM tracks WHERE path LIKE ?", (f"%{p.name}%",)
    ))
    album = (await client.get(f"/api/web/albums/{album_id}")).json()["data"]
    assert album["artist_id"] is not None
    assert album["artist"] == "The Band"
    assert album["tracks"]
    mine = [t for t in album["tracks"] if t["title"] == "T"]
    assert mine, "трек из теста должен быть в альбоме"
    for track in mine:
        assert track["album_id"] == album_id, "обложка трека должна браться из альбома"


async def test_scan_batch_setting_is_listed_and_editable(web):
    client, app = web
    csrf = await login_admin(client)
    items = (await client.get("/api/web/settings")).json()["data"]["items"]
    keys = {item["key"] for item in items}
    assert {"scan_batch_tracks", "cover_active_window_sec", "cover_max_wait_sec"} <= keys

    r = await client.post(
        "/api/web/settings",
        json={"values": {"scan_batch_tracks": 100}},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200, r.text
    effective = await app.state.ctx.settings_repo.effective()
    assert effective["scan_batch_tracks"] == 100


async def test_cover_warm_queues_worker_command(web):
    """Кнопка «Пересканировать обложки» ставит worker-у команду covers."""
    from app.worker import ipc

    client, app = web
    csrf = await login_admin(client)
    ctx = app.state.ctx
    ipc.write_status(ipc.status_path(ctx.settings.data_dir), {"scan": {"status": "idle"}})
    r = await client.post("/api/web/covers/warm", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 202, r.text
    row = await ctx.music_db.fetch_one(
        "SELECT kind FROM worker_commands ORDER BY id DESC LIMIT 1"
    )
    assert row["kind"] == "covers"
