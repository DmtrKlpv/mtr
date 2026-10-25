"""Уменьшение обложек до 600px и прогрев кеша.

Зачем: карточка в сетке занимает ~169-185 CSS px (до ~370 физических при
DPR 2), а из тега достаётся картинка 1500x1500 на несколько мегабайт. Отдавать
её целиком — главный расход CPU и сети на слабом ARM.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest

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
# Прогрев
# ----------------------------------------------------------------------
async def test_warm_prepares_missing_albums(ctx, music_dir: Path):
    from tests.conftest import make_mp3, tag_mp3

    for a in range(3):
        for t in range(2):
            path = make_mp3(music_dir / f"al{a}" / f"t{t}.mp3", seconds=0.2)
            tag_mp3(path, title=f"T{t}", artist=f"A{a}", album=f"Alb{a}")
    await ctx.scanner.scan(mode="full")

    pending_before = await ctx.covers.pending_album_ids()
    assert len(pending_before) == 3

    result = await ctx.covers.warm()

    assert result["total"] == 3
    # Картинок в файлах нет — все три должны получить маркер «нет обложки».
    assert result["absent"] == 3
    assert await ctx.covers.pending_album_ids() == []


async def test_warm_is_idempotent(ctx, music_dir: Path):
    """Повторный прогрев не должен ничего делать."""
    from tests.conftest import make_mp3, tag_mp3

    path = make_mp3(music_dir / "one" / "t.mp3", seconds=0.2)
    tag_mp3(path, title="T", artist="A", album="Alb")
    await ctx.scanner.scan(mode="full")

    first = await ctx.covers.warm()
    second = await ctx.covers.warm()

    assert first["total"] == 1
    assert second["total"] == 0


async def test_absent_marker_does_not_repeat_lookup(ctx, music_dir: Path, monkeypatch):
    """Маркер в БД переживает перезапуск: после него диск не читается."""
    from tests.conftest import make_mp3, tag_mp3
    from app.repositories import covers as covers_module

    path = make_mp3(music_dir / "one" / "t.mp3", seconds=0.2)
    tag_mp3(path, title="T", artist="A", album="Alb")
    await ctx.scanner.scan(mode="full")
    await ctx.covers.warm()

    calls = 0

    def counting(path):
        nonlocal calls
        calls += 1
        return None

    monkeypatch.setattr(covers_module, "extract_cover", counting)
    monkeypatch.setattr(covers_module, "extract_from_directory", counting)

    # Сбрасываем только память, БД остаётся — как после рестарта контейнера.
    ctx.covers.forget_missing()
    assert await ctx.covers.for_album(1) is None
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
