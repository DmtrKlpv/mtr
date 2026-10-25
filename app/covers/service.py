"""Извлечение и кэширование обложек.

Источники, в порядке приоритета:

1. **Встроенная обложка трека** — ID3 `APIC`, FLAC `picture`, MP4 `covr`,
   Ogg/Opus `metadata_block_picture`.
2. **Файл в каталоге альбома** — `cover.jpg`, `folder.png`, `front.jpg` и т.п.
3. **Обложка другого трека того же альбома** — частая ситуация: теги
   проставлены криво, а картинка есть только у одного файла.

Почему кэш на диске, а не в БД: обложки — это мегабайты, а SQLite плохо
переносит крупные BLOB при частых чтениях. В `cover_cache` лежит только
метаинформация и путь к файлу в `COVERS_DIR`.

Ключ кэша — `al-<album_id>` / `tr-<track_id>`. При повторном скане файл
перезаписывается только если изменился mtime исходника, иначе лишний
ввод-вывод на каждый запрос плеера.
"""

from __future__ import annotations

import hashlib
import logging
import os
import uuid
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)

# Имена файлов-обложек в каталоге альбома, по убыванию приоритета.
COVER_FILENAMES: Final[tuple[str, ...]] = (
    "cover.jpg", "cover.jpeg", "cover.png", "cover.webp",
    "folder.jpg", "folder.jpeg", "folder.png",
    "front.jpg", "front.jpeg", "front.png",
    "album.jpg", "album.png", "albumartsmall.jpg",
)

# MIME по расширению. Отдаём только то, что браузеры показывают без плагинов.
MIME_BY_EXT: Final[dict[str, str]] = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png", ".webp": "image/webp",
    ".gif": "image/gif",
}

# Потолок размера: обложка на 40 МБ — это ошибка тегирования, а не картинка.
MAX_COVER_BYTES = 20 * 1024 * 1024


@dataclass(slots=True)
class CoverData:
    """Готовая обложка: байты и тип."""

    data: bytes
    mime: str
    source: str

    @property
    def size(self) -> int:
        return len(self.data)


def _kind_of(data: bytes) -> str:
    """Короткое имя формата по сигнатуре: для логов и проверки декодера."""
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    return "unknown"


_WEBP_PROBE: bool | None = None


def _probe_webp() -> bool:
    """Честная проверка WebP: декодирует крошечный файл, а не смотрит флаг.

    `features.check("webp")` иногда отвечает True, а декодер при этом не работает
    (сборка Pillow на armv7). Поэтому один раз на процесс пробуем декодировать
    настоящий WebP. Результат кешируется.
    """
    global _WEBP_PROBE
    if _WEBP_PROBE is not None:
        return _WEBP_PROBE
    try:
        from io import BytesIO

        from PIL import Image
    except ImportError:
        _WEBP_PROBE = False
        return False
    try:
        buffer = BytesIO()
        Image.new("RGB", (8, 8), (10, 20, 30)).save(buffer, format="WEBP")
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            with Image.open(BytesIO(buffer.getvalue())) as image:
                image.load()
        _WEBP_PROBE = True
    except Exception:  # noqa: BLE001 - любой сбой = декодера нет
        logger.warning(
            "WebP не декодируется этой сборкой Pillow: обложки WebP отдаются как есть"
        )
        _WEBP_PROBE = False
    return _WEBP_PROBE


def decoder_available(data: bytes) -> bool:
    """Есть ли в Pillow декодер для этих байт.

    Только WebP зависит от сборки Pillow (на части колёс armv7 libwebp не
    вкомпилирован, и Image.open падает с предупреждением «WEBP support not
    installed»). Остальные форматы считаем доступными.
    """
    if _kind_of(data) != "webp":
        return True
    return _probe_webp()


def webp_support() -> bool:
    """Для диагностики (selfcheck, doctor): декодирует ли Pillow WebP на самом деле."""
    return _probe_webp()


def _mime_of(data: bytes) -> str:
    """Определяет тип по сигнатуре, а не по расширению.

    Теги часто содержат PNG с расширением .jpg, и браузер тогда не покажет
    картинку. Сигнатура надёжнее.
    """
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    return "application/octet-stream"


# ----------------------------------------------------------------------
# Извлечение из тегов
# ----------------------------------------------------------------------
def _from_id3(path: Path) -> CoverData | None:
    """APIC-кадр из MP3 (ID3v2)."""
    from mutagen.id3 import ID3, ID3NoHeaderError

    try:
        tags = ID3(str(path))
    except ID3NoHeaderError:
        return None
    except Exception:  # noqa: BLE001 - битые теги не должны ломать скан
        return None

    frames = tags.getall("APIC")
    if not frames:
        return None
    # Предпочитаем «front cover» (тип 3), иначе первый попавшийся.
    chosen = next((frame for frame in frames if frame.type == 3), frames[0])
    if not chosen.data or len(chosen.data) > MAX_COVER_BYTES:
        return None
    return CoverData(chosen.data, _mime_of(chosen.data), "id3:APIC")


def _from_flac(path: Path) -> CoverData | None:
    """FLAC picture-блок."""
    from mutagen.flac import FLAC

    try:
        audio = FLAC(str(path))
    except Exception:  # noqa: BLE001
        return None
    if not audio.pictures:
        return None
    picture = next(
        (p for p in audio.pictures if p.type == 3), audio.pictures[0]
    )
    if not picture.data or len(picture.data) > MAX_COVER_BYTES:
        return None
    return CoverData(picture.data, _mime_of(picture.data), "flac:picture")


def _from_mp4(path: Path) -> CoverData | None:
    """MP4/M4A covr-атом."""
    from mutagen.mp4 import MP4, MP4Cover

    try:
        audio = MP4(str(path))
    except Exception:  # noqa: BLE001
        return None
    covers = audio.tags.get("covr") if audio.tags else None
    if not covers:
        return None
    cover = covers[0]
    data = bytes(cover)
    if not data or len(data) > MAX_COVER_BYTES:
        return None
    # format подсказывает тип, но сигнатура надёжнее.
    mime = _mime_of(data)
    if mime == "application/octet-stream":
        mime = (
            "image/png" if cover.imageformat == MP4Cover.FORMAT_PNG
            else "image/jpeg"
        )
    return CoverData(data, mime, "mp4:covr")


def _from_ogg(path: Path) -> CoverData | None:
    """Ogg/Opus: base64 в metadata_block_picture."""
    import base64

    from mutagen.flac import Picture
    from mutagen.oggopus import OggOpus
    from mutagen.oggvorbis import OggVorbis

    try:
        try:
            audio = OggOpus(str(path))
        except Exception:
            audio = OggVorbis(str(path))
        raw = audio.get("metadata_block_picture")
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    try:
        picture = Picture(base64.b64decode(raw[0]))
    except Exception:  # noqa: BLE001
        return None
    if not picture.data or len(picture.data) > MAX_COVER_BYTES:
        return None
    return CoverData(picture.data, _mime_of(picture.data), "ogg:picture")


# Максимальная сторона сохраняемой обложки, px.
#
# Выбрано по фактическим размерам в интерфейсе: карточка в сетке занимает
# ~169-185 CSS px, то есть до ~370 физических пикселей при DPR 2. Крупная
# обложка в карточке альбома ограничена max-width: 420px (.album-hero-cover),
# при DPR 2 это 840 px — там 600 чуть мягче, но это одна картинка на экран.
# Всё, что больше, — лишние мегабайты по сети и лишняя работа при отдаче.
MAX_COVER_SIDE = 600

# Качество JPEG для пересжатых обложек. 82 — точка, где на глаз не отличить
# от оригинала, а файл в разы меньше.
JPEG_QUALITY = 82


def make_thumbnail(
    data: bytes, *, max_side: int = MAX_COVER_SIDE
) -> tuple[bytes, str] | None:
    """Уменьшает картинку до max_side по большей стороне.

    Возвращает (байты, mime) или None, если Pillow не смог разобрать формат —
    тогда вызывающий код сохранит оригинал как есть.

    Переводим в RGB: у PNG-обложек бывает альфа-канал, а JPEG его не хранит.
    Белая подложка вместо чёрной — обложки почти всегда светлые по краям.
    """
    try:
        from io import BytesIO

        from PIL import Image, ImageOps
    except ImportError:
        logger.warning("Pillow не установлен — обложки сохраняются без уменьшения")
        return None

    if not decoder_available(data):
        # Например, WebP без libwebp в Pillow (часто на armv7). Оригинал
        # отдаём как есть: браузеры показывают WebP сами, только без уменьшения.
        logger.debug("декодер для %s недоступен — отдаём оригинал", _kind_of(data))
        return None

    try:
        with warnings.catch_warnings(), Image.open(BytesIO(data)) as image:
            warnings.simplefilter("ignore")
            image.load()
            # Учитываем EXIF-поворот: иначе четверть обложек с телефонов
            # сохранится лёжа.
            try:
                image = ImageOps.exif_transpose(image)
            except Exception:  # noqa: BLE001 - EXIF битый, поворот не критичен
                pass

            if max(image.size) <= max_side:
                return None  # уже маленькая: пересжимать вредно

            image = image.convert("RGB")
            image.thumbnail((max_side, max_side), Image.LANCZOS)

            buffer = BytesIO()
            image.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
            return buffer.getvalue(), "image/jpeg"
    except Exception:  # noqa: BLE001 - битая картинка не повод падать
        logger.debug("не удалось уменьшить обложку", exc_info=True)
        return None


def extract_embedded(path: Path) -> CoverData | None:
    """Пробует все встроенные источники по расширению файла."""
    suffix = path.suffix.lower()
    readers = {
        ".mp3": _from_id3,
        ".flac": _from_flac,
        ".m4a": _from_mp4,
        ".m4b": _from_mp4,
        ".mp4": _from_mp4,
        ".ogg": _from_ogg,
        ".oga": _from_ogg,
        ".opus": _from_ogg,
    }
    reader = readers.get(suffix)
    if reader is None:
        return None
    try:
        return reader(path)
    except Exception:  # noqa: BLE001 - обложка не повод падать
        logger.debug("не удалось прочитать обложку из %s", path, exc_info=True)
        return None


def extract_from_directory(track_path: Path) -> CoverData | None:
    """Ищет файл-обложку рядом с треком и в каталоге альбома."""
    for directory in (track_path.parent, track_path.parent.parent):
        if not directory.is_dir():
            continue
        for name in COVER_FILENAMES:
            candidate = directory / name
            try:
                if not candidate.is_file():
                    continue
                if candidate.stat().st_size > MAX_COVER_BYTES:
                    logger.warning("обложка слишком велика: %s", candidate)
                    continue
                data = candidate.read_bytes()
            except OSError:
                continue
            if not data:
                continue
            mime = MIME_BY_EXT.get(candidate.suffix.lower()) or _mime_of(data)
            return CoverData(data, mime, f"file:{candidate.name}")
    return None


def extract_cover(track_path: Path) -> CoverData | None:
    """Полный поиск: встроенная обложка, затем файл рядом."""
    return extract_embedded(track_path) or extract_from_directory(track_path)


def cache_filename(key: str, data: bytes) -> str:
    """Имя файла в кэше: ключ + хэш содержимого.

    Хэш в имени решает две задачи: смена обложки даёт новое имя (клиенты
    не показывают старое из своего кэша), а одинаковые картинки разных
    альбомов не дублируются на диске.
    """
    digest = hashlib.sha256(data).hexdigest()[:16]
    safe_key = "".join(
        char if char.isalnum() or char in "-_" else "_" for char in key
    )
    return f"{safe_key}-{digest}"


@dataclass(slots=True)
class CoverResolver:
    """Достаёт обложку: из кэша на диске или извлекает и сохраняет."""

    covers_dir: Path
    music_root: Path

    def path_for(self, filename: str) -> Path:
        return self.covers_dir / filename

    def read_cached(self, file_path: str) -> CoverData | None:
        path = Path(file_path)
        try:
            if not path.is_file():
                return None
            data = path.read_bytes()
        except OSError:
            return None
        if not data:
            return None
        return CoverData(data, _mime_of(data), "cache")

    def store(self, key: str, cover: CoverData) -> tuple[str, int]:
        """Пишет обложку на диск. Возвращает (путь, размер)."""
        filename = cache_filename(key, cover.data)
        target = self.covers_dir / filename
        if not target.is_file():
            self.covers_dir.mkdir(parents=True, exist_ok=True)
            # Атомарная запись: временный файл + rename, чтобы параллельный
            # запрос не прочитал половину картинки.
            # Имя временного файла уникально на каждую запись: фаза обложек и
            # запрос getCoverArt могут писать один ключ одновременно, и общее
            # «.tmp» давало FileNotFoundError при replace.
            tmp = target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp")
            try:
                tmp.write_bytes(cover.data)
                # Если параллельная запись уже положила файл — он идентичен
                # (имя содержит хэш содержимого), replace просто перезапишет.
                os.replace(tmp, target)
            except FileNotFoundError:
                # Временный файл исчез (очистка каталога). Не падаем: пробуем ещё раз.
                if not target.is_file():
                    raise
            finally:
                try:
                    tmp.unlink()
                except FileNotFoundError:
                    pass
        return str(target), cover.size

    def is_inside_music(self, path: Path) -> bool:
        """Защита от выдачи файлов вне библиотеки."""
        try:
            path.resolve().relative_to(self.music_root.resolve())
            return True
        except ValueError:
            return False
