"""Тесты этапа 2: выбор стратегии, ffmpeg-процессы, HTTP /stream.

Чистые тесты resolve_plan не требуют ffmpeg. Интеграционные используют
реальный бинарник из imageio-ffmpeg — проверяем фактический выход, а не argv.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import httpx
import pytest

from app.audio.ffmpeg import TranscodeLimiter, TranscodeStream
from app.audio.profiles import (
    ProfileError,
    build_ffmpeg_argv,
    resolve_plan,
)
from app.audio.service import parse_format, parse_offset
from tests.conftest import make_flac, make_mp3, tag_mp3


# Чистая логика выбора стратегии (без ffmpeg)
@pytest.mark.parametrize(
    "src,requested,offset,expected_kind",
    [
        ("/music/a.mp3", "raw", 0, "file"),
        ("/music/a.flac", "raw", 0, "file"),
        ("/music/a.mp3", "raw", 30, "copy"),
        ("/music/a.flac", "raw", 30, "copy"),
        ("/music/a.m4a", "raw", 5, "copy"),
        ("/music/a.wav", "raw", 5, "encode"),   # WAV нельзя скопировать
        ("/music/a.flac", "mp3_320", 0, "encode"),
        ("/music/a.flac", "flac", 0, "encode"),
        ("/music/a.flac", "alac", 0, "encode"),
    ],
)
def test_strategy_selection(src, requested, offset, expected_kind):
    plan = resolve_plan(
        source_path=src, source_bitrate_kbps=900, requested=requested, offset_sec=offset
    )
    assert plan.kind == expected_kind


def test_raw_with_offset_on_wav_falls_back_to_mp3_320_and_reports_it():
    plan = resolve_plan(
        source_path="/music/a.wav", source_bitrate_kbps=None, requested="raw", offset_sec=10
    )
    assert plan.substituted is True
    assert plan.format_name == "mp3_320"
    assert "wav" in (plan.substitution_reason or "")


def test_no_upscale_for_low_bitrate_mp3():
    """128 кбит/с MP3 при запросе mp3_320 не перекодируется — отдаётся как есть."""
    plan = resolve_plan(
        source_path="/music/a.mp3", source_bitrate_kbps=128, requested="mp3_320", offset_sec=0
    )
    assert plan.kind == "file"
    assert plan.substituted is True
    assert "не выше" in (plan.substitution_reason or "")


def test_downscale_mp3_is_real_encode():
    """320 кбит/с MP3 при запросе mp3_128 — честное перекодирование вниз."""
    plan = resolve_plan(
        source_path="/music/a.mp3", source_bitrate_kbps=320, requested="mp3_128", offset_sec=0
    )
    assert plan.kind == "encode"
    assert plan.substituted is False


def test_unknown_format_rejected():
    with pytest.raises(ProfileError):
        resolve_plan(source_path="/a.mp3", source_bitrate_kbps=None, requested="ogg_999", offset_sec=0)


def test_negative_offset_rejected():
    with pytest.raises(ProfileError):
        resolve_plan(source_path="/a.mp3", source_bitrate_kbps=None, requested="raw", offset_sec=-1)


@pytest.mark.parametrize("value,expected", [
    (None, 0.0), ("", 0.0), ("12.5", 12.5), ("0", 0.0),
    ("999999", 86400.0),  # зажимается до суток
])
def test_parse_offset_valid(value, expected):
    assert parse_offset(value) == expected


@pytest.mark.parametrize("value", ["abc", "-3", "nan", "inf", "-inf"])
def test_parse_offset_invalid(value):
    with pytest.raises(ProfileError):
        parse_offset(value)


def test_parse_format_default_and_normalisation():
    assert parse_format(None, "raw") == "raw"
    assert parse_format("  MP3_320 ", "raw") == "mp3_320"
    with pytest.raises(ProfileError):
        parse_format("opus", "raw")


def test_argv_has_seek_before_input_and_no_shell():
    plan = resolve_plan(
        source_path="/music/a.flac", source_bitrate_kbps=None, requested="mp3_320", offset_sec=12.25
    )
    argv = build_ffmpeg_argv(plan, source_path="/music/a.flac", offset_sec=12.25)
    assert argv[0] == "ffmpeg"                       # без абсолютного пути
    assert argv.index("-ss") < argv.index("-i")      # seek ДО входа
    assert "-vn" in argv                             # обложка не попадает в поток
    assert "-map_metadata" in argv
    assert all(isinstance(a, str) for a in argv)     # никаких shell-объектов


def test_limiter_refuses_over_capacity_and_recovers():
    lim = TranscodeLimiter(2)
    assert lim.try_acquire() and lim.try_acquire()
    assert lim.try_acquire() is False
    lim.release()
    assert lim.try_acquire() is True


def test_limiter_release_is_safe_to_repeat():
    lim = TranscodeLimiter(1)
    lim.release()
    lim.release()
    assert lim.active == 0


# Интеграция с реальным ffmpeg
async def _collect(plan_kind: str, path: Path, fmt: str, offset: float) -> bytes:
    plan = resolve_plan(
        source_path=str(path), source_bitrate_kbps=None, requested=fmt, offset_sec=offset
    )
    assert plan.kind in ("copy", "encode")
    argv = build_ffmpeg_argv(plan, source_path=str(path), offset_sec=offset)
    stream = TranscodeStream(argv)
    stream.start()
    buf = bytearray()
    async for chunk in stream.iterate():
        buf.extend(chunk)
    return bytes(buf)


async def test_encode_mp3_320_produces_mp3(music_dir: Path):
    src = make_flac(music_dir / "a.flac", seconds=2.0)
    data = await _collect("encode", src, "mp3_320", 0)
    assert len(data) > 1000
    # MP3 начинается с ID3-тега или с кадровой синхронизации 0xFFEx
    assert data[:3] == b"ID3" or (data[0] == 0xFF and (data[1] & 0xE0) == 0xE0)


async def test_encode_flac_produces_flac_magic(music_dir: Path):
    src = make_mp3(music_dir / "b.mp3", seconds=1.0)
    data = await _collect("encode", src, "flac", 0)
    assert data[:4] == b"fLaC"


async def test_encode_alac_produces_mp4_container(music_dir: Path):
    src = make_mp3(music_dir / "c.mp3", seconds=1.0)
    data = await _collect("encode", src, "alac", 0)
    assert data[4:8] == b"ftyp"


async def test_seek_shortens_output(music_dir: Path):
    """timeOffset реально отрезает начало: 4-секундный файл, offset=3 -> заметно короче."""
    src = make_mp3(music_dir / "long.mp3", seconds=4.0, bitrate="128k")
    full = await _collect("copy", src, "mp3_128", 0)
    tail = await _collect("copy", src, "mp3_128", 3.0)
    assert len(tail) < len(full) * 0.6
    assert len(tail) > 0


async def test_copy_mode_uses_stream_copy_not_reencode(music_dir: Path):
    """copy с offset не должен менять качество: размер соответствует исходнику."""
    src = make_mp3(music_dir / "copy.mp3", seconds=3.0, bitrate="128k")
    tail = await _collect("copy", src, "raw", 1.0)
    # 2 секунды при 128 кбит/с ≈ 32 КБ; допускаем заметный разброс заголовков.
    assert 20_000 < len(tail) < 60_000


async def test_failed_ffmpeg_releases_slot_and_logs(music_dir: Path, tmp_path: Path):
    """Битый вход: ffmpeg падает, слот освобождается, поток не виснет."""
    bad = music_dir / "broken.mp3"
    bad.write_bytes(b"not audio")
    limiter = TranscodeLimiter(1)
    assert limiter.try_acquire()

    plan = resolve_plan(
        source_path=str(bad), source_bitrate_kbps=None, requested="mp3_320", offset_sec=0
    )
    argv = build_ffmpeg_argv(plan, source_path=str(bad), offset_sec=0)
    stream = TranscodeStream(argv, on_close=limiter.release)
    stream.start()
    data = b"".join([c async for c in stream.iterate()])

    assert data == b""  # ffmpeg ничего не выдал
    assert limiter.active == 0  # слот освобождён


async def test_client_disconnect_kills_ffmpeg(music_dir: Path):
    """Ключевое требование этапа: обрыв клиента убивает процесс ffmpeg."""
    src = make_flac(music_dir / "long.flac", seconds=60.0)
    limiter = TranscodeLimiter(1)
    assert limiter.try_acquire()

    plan = resolve_plan(
        source_path=str(src), source_bitrate_kbps=None, requested="mp3_320", offset_sec=0
    )
    argv = build_ffmpeg_argv(plan, source_path=str(src), offset_sec=0)
    stream = TranscodeStream(argv, on_close=limiter.release)
    stream.start()
    pid = stream.pid
    assert pid is not None

    # Читаем немного и «рвём соединение»: закрываем генератор раньше конца.
    gen = stream.iterate()
    first = await gen.__anext__()
    assert first
    await gen.aclose()

    # Процесс должен исчезнуть. Даём короткое окно на reap.
    deadline = time.monotonic() + 5
    alive = True
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            alive = False
            break
        # Процесс-зомби ещё «существует» для kill(0) до wait(): ждём.
        await asyncio.sleep(0.05)
    assert alive is False, f"ffmpeg pid={pid} пережил обрыв клиента"
    assert limiter.active == 0


async def test_cancellation_kills_ffmpeg(music_dir: Path):
    """Отмена задачи (например, таймаут uvicorn) тоже убивает процесс."""
    src = make_flac(music_dir / "cancel.flac", seconds=60.0)
    limiter = TranscodeLimiter(1)
    limiter.try_acquire()

    plan = resolve_plan(
        source_path=str(src), source_bitrate_kbps=None, requested="mp3_320", offset_sec=0
    )
    argv = build_ffmpeg_argv(plan, source_path=str(src), offset_sec=0)
    stream = TranscodeStream(argv, on_close=limiter.release)
    stream.start()
    pid = stream.pid

    async def consume():
        async for _ in stream.iterate():
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await asyncio.sleep(0.3)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert limiter.active == 0


# HTTP-уровень
# Учётные данные для тестов этапа 2: /stream с этапа 3 требует авторизации.
STREAM_USER = "streamer"
STREAM_PASSWORD = "streampass123"
STREAM_SALT = "testsalt1234"


def stream_auth() -> dict[str, str]:
    """Параметры Subsonic-авторизации с корректным md5-токеном."""
    import hashlib

    token = hashlib.md5((STREAM_PASSWORD + STREAM_SALT).encode()).hexdigest()
    return {
        "u": STREAM_USER, "t": token, "s": STREAM_SALT,
        "v": "1.16.1", "c": "test", "f": "json",
    }


@pytest.fixture
async def http(settings, monkeypatch):
    monkeypatch.setenv("MUSIC_DIR", str(settings.music_dir))
    monkeypatch.setenv("DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("MTR_SECRET_KEY", settings.secret_key or "test")
    monkeypatch.setenv("MTR_MAX_TRANSCODES", "2")

    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        async with app.router.lifespan_context(app):
            await app.state.ctx.users.create_user(STREAM_USER, STREAM_PASSWORD)
            yield ac, app


async def _index_one(app, music_dir: Path, name: str, maker) -> int:
    path = maker(music_dir / name)
    if path.suffix == ".mp3":
        tag_mp3(path, title="T", artist="A", album="B")
    await app.state.ctx.scanner.scan(mode="full")
    tracks = await app.state.ctx.tracks.list_tracks(limit=500)
    return next(t.id for t in tracks if t.path == str(path))


async def test_http_raw_returns_file_with_range_support(http, music_dir: Path):
    client, app = http
    track_id = await _index_one(app, music_dir, "raw.mp3", make_mp3)

    r = await client.get("/stream", params={**stream_auth(), "id": track_id})
    assert r.status_code == 200
    assert r.headers["X-MTR-Strategy"] == "file"
    assert r.headers["X-MTR-Format"] == "raw"
    assert r.headers.get("accept-ranges") == "bytes"


async def test_http_encode_returns_mp3_stream(http, music_dir: Path):
    client, app = http
    track_id = await _index_one(app, music_dir, "enc.flac", make_flac)

    r = await client.get("/stream", params={**stream_auth(), "id": track_id, "format": "mp3_320"})
    assert r.status_code == 200
    assert r.headers["X-MTR-Strategy"] == "encode"
    assert r.headers["content-type"] == "audio/mpeg"
    assert len(r.content) > 1000


async def test_http_seek_returns_copy(http, music_dir: Path):
    client, app = http
    track_id = await _index_one(app, music_dir, "seek.mp3", make_mp3)

    r = await client.get("/stream", params={**stream_auth(), "id": track_id, "timeOffset": "0.5"})
    assert r.status_code == 200
    assert r.headers["X-MTR-Strategy"] == "copy"


async def test_http_substitution_is_reported(http, music_dir: Path):
    client, app = http
    track_id = await _index_one(app, music_dir, "wav-like.mp3", make_mp3)

    # MP3 из ffmpeg 128k при запросе 320k — подмена на raw с пометкой.
    r = await client.get("/stream", params={**stream_auth(), "id": track_id, "format": "mp3_320"})
    assert r.status_code == 200
    assert r.headers.get("X-MTR-Substituted") == "1"
    # Причина передаётся percent-encoded (ASCII-заголовки), клиент декодирует.
    from urllib.parse import unquote

    reason = unquote(r.headers.get("X-MTR-Substitution-Reason", ""))
    assert "не выше" in reason


async def test_http_bad_format_400(http, music_dir: Path):
    client, app = http
    track_id = await _index_one(app, music_dir, "x.mp3", make_mp3)
    r = await client.get("/stream", params={**stream_auth(), "id": track_id, "format": "ogg"})
    assert r.status_code == 400


async def test_http_bad_offset_400(http, music_dir: Path):
    client, app = http
    track_id = await _index_one(app, music_dir, "y.mp3", make_mp3)
    r = await client.get("/stream", params={**stream_auth(), "id": track_id, "timeOffset": "abc"})
    assert r.status_code == 400


async def test_http_unknown_track_404(http):
    client, _ = http
    r = await client.get("/stream", params={**stream_auth(), "id": 999999})
    assert r.status_code == 404


async def test_http_missing_source_404(http, music_dir: Path):
    client, app = http
    track_id = await _index_one(app, music_dir, "gone.mp3", make_mp3)
    (music_dir / "gone.mp3").unlink()
    r = await client.get("/stream", params={**stream_auth(), "id": track_id})
    assert r.status_code == 404


async def test_http_busy_returns_503_with_retry_after(http, music_dir: Path):
    client, app = http
    track_id = await _index_one(app, music_dir, "busy.flac", make_flac)
    limiter = app.state.ctx.stream_service.limiter
    # Занимаем все слоты (лимит = 2 из MTR_MAX_TRANSCODES)
    assert limiter.try_acquire() and limiter.try_acquire()

    r = await client.get("/stream", params={**stream_auth(), "id": track_id, "format": "mp3_320"})
    assert r.status_code == 503
    assert r.headers.get("Retry-After") == "2"
