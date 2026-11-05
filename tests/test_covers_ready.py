"""Готовность обложек пачкой: /api/web/covers/ready не извлекает обложки."""

from __future__ import annotations

from pathlib import Path

from app.repositories import covers as covers_module
from tests.conftest import make_mp3, tag_mp3
from tests.test_web import login_admin, web  # noqa: F401


async def test_ready_keys_reads_only_cache_table(ctx, music_dir: Path, monkeypatch):
    """Ключи без записи в cover_cache не возвращаются и не извлекаются."""
    p = make_mp3(music_dir / "al" / "t.mp3", seconds=0.2)
    tag_mp3(p, title="T", artist="A", album="Alb")
    await ctx.scanner.scan(mode="full")
    albums = await ctx.library.list_albums(limit=5, offset=0)
    album_id = albums[0].id

    calls = 0

    def counting(*_a, **_kw):
        nonlocal calls
        calls += 1
        return None

    monkeypatch.setattr(covers_module, "extract_cover", counting)
    monkeypatch.setattr(covers_module, "extract_from_directory", counting)

    result = await ctx.covers.ready_keys([f"al-{album_id}", "al-999999"])
    assert result == {}          # ничего не закешировано — ничего и не говорим
    assert calls == 0            # и ничего не извлекаем


async def test_ready_reports_ready_and_absent(ctx, music_dir: Path):
    p = make_mp3(music_dir / "al" / "t.mp3", seconds=0.2)
    tag_mp3(p, title="T", artist="A", album="Alb")
    await ctx.scanner.scan(mode="full")
    album_id = (await ctx.library.list_albums(limit=5, offset=0))[0].id
    await ctx.music_db.execute(
        "INSERT INTO cover_cache (key, file_path, mime_type, size_bytes, source, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (f"al-{album_id}", "x.jpg", "image/jpeg", 10, "file", "2026-01-01T00:00:00Z"),
    )
    await ctx.music_db.execute(
        "INSERT INTO cover_cache (key, file_path, mime_type, size_bytes, source, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("tr-1", "", "", 0, "none", "2026-01-01T00:00:00Z"),
    )
    result = await ctx.covers.ready_keys([f"al-{album_id}", "tr-1"])
    assert result == {f"al-{album_id}": "ready", "tr-1": "absent"}


async def test_endpoint_filters_invalid_keys(web):
    client, app = web
    await login_admin(client)
    r = await client.get("/api/web/covers/ready?keys=al-1,evil,../etc,tr-x,tr-2")
    assert r.status_code == 200
    data = r.json()["data"]["ready"]
    assert set(data.keys()) <= {"al-1", "tr-2"}


async def test_endpoint_requires_session(web):
    client, _ = web
    r = await client.get("/api/web/covers/ready?keys=al-1")
    assert r.status_code in (401, 403)
