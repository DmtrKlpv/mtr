"""WebP на armv7 и итог фазы обложек.

Предупреждение «WEBP support not installed» возникало не из кода приложения, а
из-за того, что колесо Pillow для armv7 (piwheels) слинковано с системными
libwebp.so.7 / libwebpmux.so.3 / libwebpdemux.so.2. Без этих пакетов в образе
PIL._webp не импортируется. Поэтому проверяем сам Dockerfile и поведение без декодера.
"""

from __future__ import annotations

import asyncio
import logging
import warnings
from io import BytesIO
from pathlib import Path

from PIL import Image

from app.covers import service
from app.covers.prefetch import CoverPass
from tests.conftest import make_mp3, tag_mp3
from tests.test_cover_thumbnails import _image

ROOT = Path(__file__).resolve().parent.parent


def test_dockerfile_installs_webp_runtime_for_armv7():
    """Без runtime-библиотек libwebp в образе WebP на armv7 не работает."""
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    for package in ("libwebp7", "libwebpmux3", "libwebpdemux2"):
        assert package in text, f"в Dockerfile нет {package}"


def test_webp_without_decoder_passes_original_without_warning(monkeypatch):
    """Декодера нет: обложка отдаётся как есть, Image.open не вызывается,
    предупреждения Pillow не появляются."""
    webp = BytesIO()
    Image.new("RGB", (64, 64), (5, 6, 7)).save(webp, format="WEBP")
    data = webp.getvalue()

    monkeypatch.setattr(service, "_probe_webp", lambda: False)

    def boom(*_a, **_kw):
        raise AssertionError("Image.open не должен вызываться без декодера")

    monkeypatch.setattr(Image, "open", boom)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert service.make_thumbnail(data) is None
    assert not [w for w in caught if "WEBP" in str(w.message)]


async def test_pass_logs_clear_summary_when_all_albums_done(ctx, music_dir: Path, caplog):
    """Завершение фазы пишется как «все альбомы обработаны», а не «кеш полон»."""
    for a in range(2):
        p = make_mp3(music_dir / f"al{a}" / "t.mp3", seconds=0.2)
        tag_mp3(p, title="T", artist=f"A{a}", album=f"Alb{a}",
                cover=_image(300) if a == 0 else None)
    await ctx.scanner.scan(mode="full")

    cp = CoverPass(ctx.covers, pause=0.0)
    with caplog.at_level(logging.INFO, logger="app.covers.prefetch"):
        cp.start("full")
        for _ in range(500):
            if not cp.running:
                break
            await asyncio.sleep(0.02)

    assert cp.state == "done"
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "все альбомы обработаны" in text
    assert "кеш полон" not in text
