"""Worker-процесс (этап 17): команды, статус, метка активности, настройки, отказ веба."""

from __future__ import annotations

import json
import time
from pathlib import Path

from app.worker import ipc
from app.worker.ipc import FileActivity, ActivityStamp, read_status, status_path, worker_alive
from app.worker.runtime import WorkerRuntime
from tests.conftest import make_mp3, tag_mp3
from tests.test_web import web  # noqa: F401 - фикстура для тестов веб-эндпоинтов
from tests.test_web import login_admin  # noqa: F401


async def test_scan_command_runs_in_worker_and_publishes_status(ctx, music_dir: Path):
    p = make_mp3(music_dir / "al" / "t.mp3", seconds=0.2)
    tag_mp3(p, title="T", artist="A", album="Alb")
    runtime = WorkerRuntime(ctx, poll_sec=0.01)
    await ipc.enqueue(ctx.music_db, "scan", {"mode": "full"})

    assert await runtime.handle_commands() == 1
    await runtime.wait_scan(timeout=30)
    await runtime.publish_status()

    status = read_status(runtime.status_file)
    assert status["scan"]["status"] == "completed"
    assert status["scan"]["added"] == 1
    assert "covers" in status and "heartbeat" in status
    assert worker_alive(status)
    # Команда помечена обработанной и повторно не запускается.
    assert await runtime.handle_commands() == 0


async def test_unknown_command_kind_is_rejected(ctx):
    try:
        await ipc.enqueue(ctx.music_db, "delete-everything")
    except ValueError:
        return
    raise AssertionError("неизвестная команда должна отклоняться")


async def test_scan_is_not_started_twice(ctx, music_dir: Path):
    p = make_mp3(music_dir / "al" / "t.mp3", seconds=0.2)
    tag_mp3(p, title="T", artist="A", album="Alb")
    runtime = WorkerRuntime(ctx, poll_sec=0.01)
    await ipc.enqueue(ctx.music_db, "scan", {"mode": "full"})
    await ipc.enqueue(ctx.music_db, "scan", {"mode": "full"})
    await runtime.handle_commands()
    await runtime.wait_scan(timeout=30)
    assert ctx.scan_state.status == "completed"
    # Вторая команда не должна была превратиться во второй проход.
    runs = await ctx.music_db.fetch_value("SELECT COUNT(*) FROM scan_runs")
    assert int(runs) <= 2


def test_status_write_is_atomic_and_readable(tmp_path: Path):
    path = tmp_path / "worker_status.json"
    ipc.write_status(path, {"scan": {"status": "idle"}})
    data = read_status(path)
    assert data["scan"]["status"] == "idle"
    assert "heartbeat" in data
    # Повреждённый файл не ломает чтение: пустой словарь, а не исключение.
    path.write_text("{not json", encoding="utf-8")
    assert read_status(path) == {}


def test_heartbeat_freshness():
    now = time.time()
    assert worker_alive({"heartbeat": now}, now=now)
    assert not worker_alive({"heartbeat": now - ipc.HEARTBEAT_TTL_SEC - 1}, now=now)
    assert not worker_alive({}, now=now)


def test_activity_stamp_and_file_activity(tmp_path: Path):
    stamp_file = tmp_path / "activity.stamp"
    reader = FileActivity(stamp_file, window_sec=2.0)
    assert not reader.busy()           # файла нет — никто не листает
    ActivityStamp(stamp_file).touch()
    assert reader.busy()               # только что трогали
    reader.set_window(0.0)
    assert not reader.busy()           # окно нулевое — не активен


async def test_settings_reload_applies_cover_pause(ctx):
    runtime = WorkerRuntime(ctx, poll_sec=0.01)
    await ctx.settings_repo.set("cover_max_wait_sec", 45, user_id=None)
    await ctx.settings_repo.set("cover_active_window_sec", 7, user_id=None)
    await runtime.apply_settings()
    assert ctx.cover_pass._max_wait == 45
    assert runtime._activity.window_sec == 7


async def test_web_refuses_scan_without_worker(web):
    client, app = web
    csrf = await login_admin(client)
    # Файла статуса нет: worker не запущен — веб не ставит команду и отвечает 503.
    status_file = status_path(app.state.ctx.settings.data_dir)
    if status_file.exists():
        status_file.unlink()
    r = await client.post("/api/web/scan?mode=full", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 503
    queued = await app.state.ctx.music_db.fetch_value("SELECT COUNT(*) FROM worker_commands")
    assert int(queued) == 0


async def test_web_queues_scan_when_worker_alive(web):
    client, app = web
    csrf = await login_admin(client)
    ctx = app.state.ctx
    ipc.write_status(status_path(ctx.settings.data_dir), {"scan": {"status": "idle"}})
    r = await client.post("/api/web/scan?mode=incremental", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 202, r.text
    row = await ctx.music_db.fetch_one("SELECT kind, payload FROM worker_commands ORDER BY id DESC LIMIT 1")
    assert row["kind"] == "scan"
    assert json.loads(row["payload"]) == {"mode": "incremental"}


async def test_cover_settings_are_editable_via_api(web):
    client, app = web
    csrf = await login_admin(client)
    r = await client.post(
        "/api/web/settings",
        json={"values": {"cover_active_window_sec": 5, "cover_max_wait_sec": 60}},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200, r.text
    effective = await app.state.ctx.settings_repo.effective()
    assert effective["cover_active_window_sec"] == 5
    assert effective["cover_max_wait_sec"] == 60
