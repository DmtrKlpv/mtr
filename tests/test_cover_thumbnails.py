"""Уменьшение обложек до 600px и прогрев кеша.

Зачем: карточка в сетке занимает ~169-185 CSS px (до ~370 физических при
DPR 2), а из тега достаётся картинка 1500x1500 на несколько мегабайт. Отдавать
её целиком — главный расход CPU и сети на слабом ARM.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path


from app.covers.service import MAX_COVER_SIDE, make_thumbnail


def _image(size: int, fmt: str = "JPEG") -> bytes:
    """Генерирует сплошную картинку заданного размера."""
    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (size, size), (120, 160, 210)).save(buffer, format=fmt)
    return buffer.getvalue()


# ----------------------------------------------------------------------
# make_thumbnail
# ----------------------------------------------------------------------
def test_large_cover_is_shrunk_to_limit():
    original = _image(1500)
    result = make_thumbnail(original)

    assert result is not None
    data, mime = result
    assert mime == "image/jpeg"

    from PIL import Image

    with Image.open(BytesIO(data)) as image:
        assert max(image.size) == MAX_COVER_SIDE

    # Уменьшенная должна быть заметно легче: в сетке это важно.
    assert len(data) < len(original)


def test_small_cover_is_left_alone():
    """Картинку меньше лимита не пересжимаем — только потеряем качество."""
    original = _image(400)
    assert make_thumbnail(original) is None


def test_exact_limit_is_left_alone():
    original = _image(MAX_COVER_SIDE)
    assert make_thumbnail(original) is None


def test_transparent_png_becomes_jpeg():
    """У PNG бывает альфа-канал, а JPEG его не хранит — нужен конверт в RGB."""
    original = _image(1200, fmt="PNG")
    result = make_thumbnail(original)

    assert result is not None
    data, mime = result
    assert mime == "image/jpeg"

    from PIL import Image

    with Image.open(BytesIO(data)) as image:
        assert image.mode == "RGB"


def test_broken_image_returns_none():
    """Битые байты не должны ронять отдачу обложки."""
    assert make_thumbnail(b"not an image at all") is None


def test_non_square_is_limited_by_longer_side():
    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (2000, 800), (1, 2, 3)).save(buffer, format="JPEG")

    data, _ = make_thumbnail(buffer.getvalue())
    with Image.open(BytesIO(data)) as image:
        assert image.size == (MAX_COVER_SIDE, 240)


# ----------------------------------------------------------------------
# store: на диск кладётся уже превью
# ----------------------------------------------------------------------
async def test_store_writes_thumbnail_to_disk(ctx):
    from app.covers.service import CoverData

    big = _image(1400)
    await ctx.covers.store("al-999", CoverData(big, "image/jpeg", "embedded"))

    from PIL import Image

    row = await ctx.music_db.fetch_one(
        "SELECT file_path FROM cover_cache WHERE key = 'al-999'"
    )
    stored = Path(str(row["file_path"]))
    assert stored.is_file()

    with Image.open(stored) as image:
        assert max(image.size) == MAX_COVER_SIDE
    assert stored.stat().st_size < len(big)


# ----------------------------------------------------------------------
# Ленивое извлечение: обложка берётся только при запросе, фонового прохода нет
# ----------------------------------------------------------------------
async def test_scan_does_not_extract_covers(ctx, music_dir: Path, monkeypatch):
    """Скан не читает картинки: только флаг has_cover из заголовков тегов."""
    from tests.conftest import make_mp3, tag_mp3
    from app.repositories import covers as covers_module

    calls = 0

    def counting(path):
        nonlocal calls
        calls += 1
        return None

    monkeypatch.setattr(covers_module, "extract_cover", counting)
    monkeypatch.setattr(covers_module, "extract_from_directory", counting)

    for a in range(3):
        path = make_mp3(music_dir / f"al{a}" / "t.mp3", seconds=0.2)
        tag_mp3(path, title="T", artist=f"A{a}", album=f"Alb{a}")
    await ctx.scanner.scan(mode="full")

    assert calls == 0
    row = await ctx.music_db.fetch_one("SELECT COUNT(*) AS n FROM cover_cache")
    assert int(row["n"]) == 0


async def test_cover_is_extracted_on_first_request_only(ctx, music_dir: Path, monkeypatch):
    """Первый запрос читает файл, повторный отдаётся из кеша без чтения."""
    from tests.conftest import make_mp3, tag_mp3
    from app.repositories import covers as covers_module
    from app.covers.service import CoverData

    image = _image(1200)
    path = make_mp3(music_dir / "one" / "t.mp3", seconds=0.2)
    tag_mp3(path, title="T", artist="A", album="Alb", cover=image)
    await ctx.scanner.scan(mode="full")
    album_id = int(await ctx.music_db.fetch_value("SELECT album_id FROM tracks LIMIT 1"))

    calls = 0

    def fake(path):
        nonlocal calls
        calls += 1
        return CoverData(image, "image/jpeg", "embedded")

    monkeypatch.setattr(covers_module, "extract_cover", fake)
    monkeypatch.setattr(covers_module, "extract_from_directory", lambda p: None)

    first = await ctx.covers.for_album(album_id)
    after_first = calls
    second = await ctx.covers.for_album(album_id)

    assert first is not None and second is not None
    # Первый запрос может обращаться к каталогу и к тегам (несколько вызовов).
    # Важно другое: второй запрос к диску не ходит вовсе.
    assert calls == after_first, "второй запрос должен идти из кеша"


async def test_absent_marker_does_not_repeat_lookup(ctx, music_dir: Path, monkeypatch):
    """Маркер в БД переживает перезапуск: после него диск не читается."""
    from tests.conftest import make_mp3, tag_mp3
    from app.repositories import covers as covers_module

    path = make_mp3(music_dir / "one" / "t.mp3", seconds=0.2)
    tag_mp3(path, title="T", artist="A", album="Alb")
    await ctx.scanner.scan(mode="full")
    # Фоновая фаза обложек стартует после скана и пишет в ту же базу.
    # Дождаться её, иначе она может извлечь обложку во время проверки.
    import asyncio as _asyncio
    for _ in range(500):
        if not ctx.cover_pass.running:
            break
        await _asyncio.sleep(0.02)
    album_id = int(await ctx.music_db.fetch_value("SELECT album_id FROM tracks LIMIT 1"))
    await ctx.covers._mark_absent(f"al-{album_id}")

    calls = 0

    def counting(path):
        nonlocal calls
        calls += 1
        return None

    monkeypatch.setattr(covers_module, "extract_cover", counting)
    monkeypatch.setattr(covers_module, "extract_from_directory", counting)

    # Сбрасываем только память, БД остаётся — как после рестарта контейнера.
    ctx.covers.forget_missing()
    assert await ctx.covers.for_album(album_id) is None
    assert calls == 0, "маркер проигнорирован, обложка искалась заново"


async def test_purge_clears_markers_and_files(ctx, music_dir: Path):
    from app.covers.service import CoverData

    await ctx.covers.store("al-1", CoverData(_image(1200), "image/jpeg", "embedded"))
    await ctx.covers._mark_absent("al-2")

    removed = await ctx.covers.purge_all()

    assert removed == 1
    row = await ctx.music_db.fetch_one(
        "SELECT COUNT(*) AS n FROM cover_cache"
    )
    assert int(row["n"]) == 0


async def test_stats_ignores_absent_markers(ctx):
    row = await ctx.music_db.fetch_one(
        "SELECT COUNT(*) AS n FROM cover_cache WHERE file_path <> ''"
    )
    assert int(row["n"]) == 0

    await ctx.covers._mark_absent("al-7")
    stats = await ctx.covers.stats()

    # Маркер — не обложка: в статистике кеша ему делать нечего.
    assert stats["count"] == 0
