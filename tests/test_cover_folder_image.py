"""Обложка из файла в папке альбома важнее картинки из тегов.

Настройка `cover_prefer_folder_image`: если рядом с треками есть картинка
(cover, folder, front, orig, pic — в любом регистре), берём её, а картинку из
тегов файла — только когда файла нет.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.covers.service import (
    COVER_BASENAMES,
    extract_cover,
    extract_from_directory,
    find_folder_images,
    is_cover_filename,
)
from app.worker import ipc
from app.worker.runtime import WorkerRuntime
from tests.conftest import make_mp3, tag_mp3
from tests.test_cover_thumbnails import _image
from tests.test_web import login_admin, web  # noqa: F401 - фикстура

TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)
TINY_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + b"\xff\xd9"


async def _scan(ctx) -> None:
    """Скан без фоновой фазы обложек: тест готовит кеш сам."""
    await ctx.scanner.scan(mode="full")
    ctx.cover_pass.cancel_nowait()
    await ctx.cover_pass.stop()


async def _album_id(ctx, name: str) -> int:
    return int(await ctx.music_db.fetch_value(
        "SELECT id FROM albums WHERE name = ?", (name,)
    ))


# Имена файлов
@pytest.mark.parametrize("name", [
    "cover.jpg", "Cover.JPG", "COVER.JPEG", "folder.png", "Folder.PNG",
    "front.jpeg", "Front.JPG", "orig.jpg", "Orig.PNG", "pic.webp",
    "cover.jfif", "COVER.JFIF",
])
def test_cover_names_any_case(name: str):
    assert is_cover_filename(name)


@pytest.mark.parametrize("name", [
    "01.mp3", "cover.txt", "folder", "cover.jpg.tmp", "back.jpg",
    "artwork.jpg", "coverflac.png", "mycover.jpg",
])
def test_non_cover_names_are_ignored(name: str):
    assert not is_cover_filename(name)


def test_all_requested_basenames_present():
    for base in ("folder", "cover", "front", "orig", "pic"):
        assert base in COVER_BASENAMES


# Выбор файла в каталоге
def test_priority_cover_before_folder(music_dir: Path):
    album = music_dir / "A" / "Al"
    track = make_mp3(album / "01.mp3")
    (album / "folder.png").write_bytes(TINY_PNG)
    (album / "cover.jpg").write_bytes(TINY_JPEG)

    cover = extract_from_directory(track)
    assert cover is not None and cover.source == "file:cover.jpg"


def test_pic_is_lower_priority_than_front(music_dir: Path):
    album = music_dir / "B" / "Al"
    track = make_mp3(album / "01.mp3")
    (album / "pic.png").write_bytes(TINY_PNG)
    (album / "front.jpg").write_bytes(TINY_JPEG)

    cover = extract_from_directory(track)
    assert cover is not None and cover.source == "file:front.jpg"


def test_uppercase_name_is_found(music_dir: Path):
    """На Linux `Cover.JPG` не совпадал с `cover.jpg` и обложка терялась."""
    album = music_dir / "C" / "Al"
    track = make_mp3(album / "01.mp3")
    (album / "Cover.JPG").write_bytes(TINY_PNG)

    cover = extract_from_directory(track)
    assert cover is not None and cover.source == "file:Cover.JPG"


def test_jfif_is_supported(music_dir: Path):
    album = music_dir / "D" / "Al"
    track = make_mp3(album / "01.mp3")
    (album / "folder.jfif").write_bytes(TINY_JPEG)

    cover = extract_from_directory(track)
    assert cover is not None and cover.mime == "image/jpeg"


def test_larger_file_wins_among_same_name_variants(music_dir: Path):
    album = music_dir / "E" / "Al"
    make_mp3(album / "01.mp3")
    (album / "cover.png").write_bytes(TINY_PNG)
    (album / "cover.jpeg").write_bytes(TINY_JPEG + b"\x00" * 200)

    assert find_folder_images(album)[0].name == "cover.jpeg"


def test_choice_is_stable_between_calls(music_dir: Path):
    album = music_dir / "F" / "Al"
    make_mp3(album / "01.mp3")
    for name in ("cover.jpg", "cover.png", "cover.webp"):
        (album / name).write_bytes(TINY_JPEG)

    first = [p.name for p in find_folder_images(album)]
    second = [p.name for p in find_folder_images(album)]
    assert first == second


# Порядок источников для файла
def _track_with_embedded(music_dir: Path) -> Path:
    path = make_mp3(music_dir / "G" / "Al" / "01.mp3", seconds=0.2)
    tag_mp3(path, title="T", artist="A", album="Alb", cover=_image(600))
    return path


def test_folder_image_wins_over_embedded(music_dir: Path):
    path = _track_with_embedded(music_dir)
    (path.parent / "cover.jpg").write_bytes(TINY_JPEG)

    cover = extract_cover(path, prefer_folder_image=True)
    assert cover is not None and cover.source == "file:cover.jpg"


def test_embedded_wins_when_setting_is_off(music_dir: Path):
    path = _track_with_embedded(music_dir)
    (path.parent / "cover.jpg").write_bytes(TINY_JPEG)

    cover = extract_cover(path, prefer_folder_image=False)
    assert cover is not None and not cover.source.startswith("file:")


def test_embedded_used_when_no_folder_image(music_dir: Path):
    path = _track_with_embedded(music_dir)
    cover = extract_cover(path, prefer_folder_image=True)
    assert cover is not None and not cover.source.startswith("file:")


# Через репозиторий: альбом
async def test_album_prefers_folder_image(ctx, music_dir: Path):
    path = make_mp3(music_dir / "H" / "Al" / "01.mp3", seconds=0.2)
    tag_mp3(path, title="T", artist="A", album="AlbH", cover=_image(600))
    (path.parent / "FRONT.PNG").write_bytes(TINY_PNG)

    ctx.covers.prefer_folder_image = True
    await _scan(ctx)
    cover = await ctx.covers.for_album(await _album_id(ctx, "AlbH"))
    assert cover is not None and cover.source == "file:FRONT.PNG"


async def test_album_uses_embedded_when_setting_off(ctx, music_dir: Path):
    path = make_mp3(music_dir / "I" / "Al" / "01.mp3", seconds=0.2)
    tag_mp3(path, title="T", artist="A", album="AlbI", cover=_image(600))
    (path.parent / "cover.jpg").write_bytes(TINY_JPEG)

    ctx.covers.prefer_folder_image = False
    await _scan(ctx)
    cover = await ctx.covers.for_album(await _album_id(ctx, "AlbI"))
    assert cover is not None and not cover.source.startswith("file:")


async def test_multi_folder_album_uses_first_folder(ctx, music_dir: Path):
    """Альбом в двух папках: обложка берётся из папки первого трека."""
    first = make_mp3(music_dir / "J" / "Al" / "01.mp3", seconds=0.2)
    tag_mp3(first, title="T1", artist="A", album="AlbJ", track="1")
    second = make_mp3(music_dir / "J" / "Al" / "cd2" / "01.mp3", seconds=0.2)
    tag_mp3(second, title="T2", artist="A", album="AlbJ", track="2")
    (first.parent / "cover.jpg").write_bytes(TINY_JPEG)
    (second.parent / "cover.png").write_bytes(TINY_PNG)

    ctx.covers.prefer_folder_image = True
    await _scan(ctx)
    cover = await ctx.covers.for_album(await _album_id(ctx, "AlbJ"))
    assert cover is not None and cover.source == "file:cover.jpg"


def test_album_folder_image_wins_over_parent(music_dir: Path):
    """Обложка альбома перебивает картинку в каталоге исполнителя."""
    artist = music_dir / "M"
    album = artist / "Al"
    track = make_mp3(album / "01.mp3")
    (artist / "cover.png").write_bytes(TINY_PNG)
    (album / "folder.jpg").write_bytes(TINY_JPEG)

    cover = extract_from_directory(track)
    assert cover is not None and cover.source == "file:folder.jpg"


def test_nested_artist_covers_album_uses_own_folder(music_dir: Path):
    """Раскладка `Исполнитель/Каверы/2002/Альбом`: обложка в самом альбоме."""
    album = music_dir / "N" / "Каверы" / "2002" / "Al"
    track = make_mp3(album / "01.mp3")
    (album / "folder.jpg").write_bytes(TINY_JPEG)
    # Картинка у «Каверов» не должна перебивать обложку альбома.
    (album.parent / "cover.png").write_bytes(TINY_PNG)

    cover = extract_from_directory(track)
    assert cover is not None and cover.source == "file:folder.jpg"


# Пересборка
async def test_rebuild_resets_only_albums_with_folder_image(ctx, music_dir: Path):
    with_file = make_mp3(music_dir / "K" / "Al1" / "01.mp3", seconds=0.2)
    tag_mp3(with_file, title="T", artist="A1", album="AlbK1", cover=_image(600))
    (with_file.parent / "cover.jpg").write_bytes(TINY_JPEG)

    without = make_mp3(music_dir / "K" / "Al2" / "01.mp3", seconds=0.2)
    tag_mp3(without, title="T", artist="A2", album="AlbK2", cover=_image(600))

    await _scan(ctx)
    k1 = await _album_id(ctx, "AlbK1")
    k2 = await _album_id(ctx, "AlbK2")
    # Обе обложки посчитаны из тегов: есть что сбрасывать.
    assert await ctx.covers.for_album(k1) is not None
    assert await ctx.covers.for_album(k2) is not None

    assert await ctx.covers.albums_with_folder_image() == [k1]
    result = await ctx.covers.rebuild_from_folders()
    assert result["albums"] == 1

    async def has_row(album_id: int) -> bool:
        return bool(await ctx.music_db.fetch_value(
            "SELECT 1 FROM cover_cache WHERE key = ?", (f"al-{album_id}",), default=0,
        ))

    assert not await has_row(k1), "альбом с картинкой в папке должен быть сброшен"
    assert await has_row(k2), "альбом без картинки в папке трогать не нужно"


async def test_rebuild_does_not_block_worker_loop(ctx, music_dir: Path):
    """Пересборка — задача фазы: команда обрабатывается сразу, цикл не блокируется."""
    track = make_mp3(music_dir / "P" / "Alb" / "01.mp3", seconds=0.2)
    tag_mp3(track, title="T", artist="A", album="AlbP", cover=_image(600))
    (track.parent / "cover.jpg").write_bytes(TINY_JPEG)

    await _scan(ctx)
    runtime = WorkerRuntime(ctx, poll_sec=0.01)
    await ipc.enqueue(ctx.music_db, "covers", {"mode": "rebuild"})

    # Команда обработана сразу: сама пересборка — задача фазы, а не цикла.
    assert await runtime.handle_commands() == 1
    assert ctx.cover_pass.running, "пересборка должна идти в фазе обложек"

    await ctx.cover_pass.stop()

    album_id = await _album_id(ctx, "AlbP")
    cover = await ctx.covers.for_album(album_id)
    assert cover is not None and cover.source == "file:cover.jpg"


async def test_rebuild_requested_during_pass_keeps_mode(ctx, music_dir: Path):
    """Во время работы фазы запрошенный rebuild не превращается в incremental."""
    pass_ = ctx.cover_pass
    assert pass_.start("full") is True
    # Второй запрос приходит, пока фаза идёт: запоминается именно его режим.
    assert pass_.start("rebuild") is False
    assert pass_._restart is True
    assert pass_._restart_mode == "rebuild"
    pass_.cancel_nowait()
    await pass_.stop()


async def test_deferred_rebuild_runs_after_current_pass(ctx, music_dir: Path):
    """Пересборка, запрошенная во время фазы, реально выполняется после неё.

    Проверяются не флаги, а порядок: сначала текущий проход, затем пересборка.
    """
    import asyncio as _asyncio

    from app.covers.prefetch import CoverPass

    pass_ = CoverPass(ctx.covers, pause=0)
    calls: list[str] = []
    album_ids = list(range(1, 51))

    async def fake_pending(*, mode: str = "incremental", limit=None):
        calls.append(mode)
        return album_ids

    async def fake_one(album_id: int) -> None:
        await _asyncio.sleep(0)

    async def fake_rebuild() -> dict[str, int]:
        calls.append("rebuild-work")
        return {"albums": 0, "tracks": 0}

    ctx.covers.pending_album_ids = fake_pending
    pass_._one = fake_one
    ctx.covers.rebuild_from_folders = fake_rebuild

    assert pass_.start("incremental") is True
    # Просим пересборку, пока фаза идёт: она должна встать в очередь на перезапуск.
    assert pass_.start("rebuild") is False

    loop = _asyncio.get_running_loop()
    first = pass_._task
    await _asyncio.wait({first}, timeout=10)
    # После перезапуска задача фазы уже другая: ждём, пока отработает она.
    deadline = loop.time() + 10
    while "rebuild-work" not in calls and loop.time() < deadline:
        await _asyncio.sleep(0.01)
    if "rebuild-work" not in calls:
        pass_.cancel_nowait()
        await pass_.stop()
        raise AssertionError(f"пересборка не запустилась за 10 с: {calls}")
    await _asyncio.wait({pass_._task}, timeout=10)

    # Первый проход инкрементальный, пересборка — после него.
    assert calls[0] == "incremental", calls
    assert "rebuild-work" in calls, f"пересборка не выполнилась: {calls}"
    assert calls.index("rebuild-work") > 0


async def test_worker_rebuild_command_switches_to_folder_image(ctx, music_dir: Path):
    """Кнопка «Пересобрать из папок» реально меняет обложку на файл из папки."""
    path = make_mp3(music_dir / "L" / "Al" / "01.mp3", seconds=0.2)
    tag_mp3(path, title="T", artist="A", album="AlbL", cover=_image(600))
    (path.parent / "cover.jpg").write_bytes(TINY_JPEG)

    await _scan(ctx)
    album_id = await _album_id(ctx, "AlbL")
    ctx.covers.prefer_folder_image = False
    before = await ctx.covers.for_album(album_id)
    assert before is not None and not before.source.startswith("file:")

    runtime = WorkerRuntime(ctx, poll_sec=0.01)
    # Настройка из БД: по умолчанию включено, worker применяет её сам.
    await runtime.apply_settings()
    assert ctx.covers.prefer_folder_image is True

    await ipc.enqueue(ctx.music_db, "covers", {"mode": "rebuild"})
    assert await runtime.handle_commands() == 1
    await ctx.cover_pass.stop()

    after = await ctx.covers.for_album(album_id)
    assert after is not None and after.source == "file:cover.jpg"


# HTTP: кнопка и статус
async def test_setting_is_visible_and_switchable_via_api(web):  # noqa: F811
    """Настройка есть на странице настроек и переключается из интерфейса."""
    client, app = web
    csrf = await login_admin(client)
    ctx = app.state.ctx

    items = (await client.get("/api/web/settings")).json()["data"]["items"]
    item = next((i for i in items if i["key"] == "cover_prefer_folder_image"), None)
    assert item is not None, "настройки нет в списке — её не видно в интерфейсе"
    assert item["kind"] == "bool"
    assert item["value"] is True, "по умолчанию обложка берётся из папки"

    r = await client.post(
        "/api/web/settings",
        json={"values": {"cover_prefer_folder_image": False}},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200, r.text
    assert ctx.covers.prefer_folder_image is False, "переключатель не дошёл до репозитория"

    # Выключенная настройка возвращает порядок «сначала теги».
    album_dir = Path(ctx.settings.music_dir) / "S" / "Al"
    track = make_mp3(album_dir / "01.mp3", seconds=0.2)
    tag_mp3(track, title="T", artist="A", album="AlbS", cover=_image(600))
    (album_dir / "cover.jpg").write_bytes(TINY_JPEG)
    await _scan(ctx)
    cover = await ctx.covers.for_album(await _album_id(ctx, "AlbS"))
    assert cover is not None and not cover.source.startswith("file:")


async def test_rebuild_endpoint_queues_worker_command(web):  # noqa: F811
    client, app = web
    csrf = await login_admin(client)
    ctx = app.state.ctx
    ipc.write_status(ipc.status_path(ctx.settings.data_dir), {"scan": {"status": "idle"}})

    r = await client.post(
        "/api/web/covers/rebuild-from-folders", headers={"X-CSRF-Token": csrf}
    )
    assert r.status_code == 202, r.text
    row = await ctx.music_db.fetch_one(
        "SELECT kind, payload FROM worker_commands ORDER BY id DESC LIMIT 1"
    )
    assert row["kind"] == "covers"
    assert json.loads(row["payload"]) == {"mode": "rebuild"}


async def test_rebuild_endpoint_requires_csrf(web):  # noqa: F811
    client, app = web
    await login_admin(client)
    r = await client.post("/api/web/covers/rebuild-from-folders")
    assert r.status_code == 403, r.text


async def test_status_reports_queued_covers(web):  # noqa: F811
    client, app = web
    await login_admin(client)
    ctx = app.state.ctx
    ipc.write_status(ipc.status_path(ctx.settings.data_dir), {"scan": {"status": "idle"}})
    await ipc.enqueue(ctx.music_db, "covers", {"mode": "rebuild"})

    data = (await client.get("/api/web/scan/status")).json()["data"]
    assert data["queued_covers"] is True
    assert data["queued"] is False
