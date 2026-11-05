"""Worker в простое: реже опрос, запись статуса только при изменении.

Раньше цикл работал каждые 0.5 с и на каждом проходе писал worker_status.json.
На NAS это ~2 записи и 2 запроса к БД в секунду даже без клиентов.
"""
from __future__ import annotations

import time

from app.worker import ipc, runtime as runtime_mod
from app.worker.ipc import HEARTBEAT_TTL_SEC, HEARTBEAT_WRITE_SEC, worker_alive
from app.worker.runtime import IDLE_POLL_SEC, POLL_SEC, WorkerRuntime


def _count_writes(monkeypatch):
    calls: list[dict] = []
    real = runtime_mod.write_status

    def spy(path, payload):
        calls.append(payload)
        return real(path, payload)

    monkeypatch.setattr(runtime_mod, "write_status", spy)
    return calls


async def test_idle_loop_uses_long_delay(ctx):
    runtime = WorkerRuntime(ctx)
    assert runtime._is_busy() is False
    assert runtime._next_delay(False) == IDLE_POLL_SEC > POLL_SEC


async def test_busy_loop_keeps_fast_delay_during_scan(ctx):
    runtime = WorkerRuntime(ctx)
    ctx.scan_state.status = "running"
    assert runtime._is_busy() is True
    assert runtime._next_delay(True) == POLL_SEC


async def test_status_not_written_when_unchanged(ctx, monkeypatch):
    calls = _count_writes(monkeypatch)
    runtime = WorkerRuntime(ctx)

    assert await runtime.publish_status() is True
    for _ in range(5):
        assert await runtime.publish_status() is False
    assert len(calls) == 1


async def test_status_written_on_change(ctx, monkeypatch):
    calls = _count_writes(monkeypatch)
    runtime = WorkerRuntime(ctx)
    await runtime.publish_status()

    ctx.scan_state.status = "running"
    assert await runtime.publish_status() is True
    assert len(calls) == 2


async def test_heartbeat_refreshed_on_timer_without_change(ctx, monkeypatch):
    calls = _count_writes(monkeypatch)
    runtime = WorkerRuntime(ctx)
    await runtime.publish_status()

    # Имитируем, что с последней записи прошло больше HEARTBEAT_WRITE_SEC.
    runtime._last_status_at = time.monotonic() - HEARTBEAT_WRITE_SEC - 1
    assert await runtime.publish_status() is True
    assert len(calls) == 2


async def test_idle_heartbeat_stays_alive_between_writes(ctx, tmp_path):
    """Между записями в простое heartbeat не должен считаться мёртвым."""
    runtime = WorkerRuntime(ctx, status_file=tmp_path / "worker_status.json")
    await runtime.publish_status()
    status = ipc.read_status(tmp_path / "worker_status.json")
    # Самая длинная пауза между записями в простое: idle-опрос + срок heartbeat.
    worst_gap = IDLE_POLL_SEC + HEARTBEAT_WRITE_SEC
    assert worker_alive(status, now=status["heartbeat"] + worst_gap)
    assert HEARTBEAT_TTL_SEC > worst_gap


async def test_commands_still_handled_when_idle(ctx):
    """Команды из очереди обрабатываются и в простое, только реже."""
    from app.worker import ipc as ipc_mod

    runtime = WorkerRuntime(ctx)
    await ipc_mod.enqueue(ctx.music_db, "reload", {})
    assert await runtime.handle_commands() == 1
