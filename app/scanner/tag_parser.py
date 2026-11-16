"""Извлечение метаданных из аудиофайлов через mutagen.

Главный принцип: парсер НИКОГДА не роняет сканирование. Любой файл либо
даёт осмысленный TrackMetadata, либо возвращает объект с ok=False и
человекочитаемой причиной — сканер посчитает его в files_failed и пойдёт
дальше.

Обрабатываемые форматы: mp3, flac, ogg (vorbis/opus/speex), m4a/mp4/aac,
wav, aiff, wma/asf, ape, wv, mpc, dsdiff/dsf (по возможности).

Типы полей приводятся к тому, что ждёт SQLite:
    * списки mutagen (Vorbis comments, MP4-атомы) -> строки через первый
      элемент или склейку;
    * "3/12" -> int 3;
    * float-секунды -> int миллисекунды;
    * bitrate в бит/с -> int кбит/с.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Расширения, которые сканер вообще рассматривает как аудио.
SUPPORTED_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".mp3", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".m4b", ".mp4",
        ".aac", ".wav", ".aiff", ".aif", ".wma", ".ape", ".wv", ".mpc",
        ".dsf", ".dff", ".alac", ".oga",
    }
)

# Ограничения длины строк — защита от патологических тегов в огромных файлах.
MAX_STRING_LEN = 512
MAX_TITLE_LEN = 512

# Управляющие символы, которые ломают JSON и HTML.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# Ведущий номер трэка: "03", "3/12", "3 of 12".
_TRACK_NUM = re.compile(r"^\s*(\d{1,4})")
# Год в произвольной строке: "1998-05-04", "1998".
_YEAR = re.compile(r"(\d{4})")


@dataclass(slots=True)
class TrackMetadata:
    """Результат разбора одного файла, уже приведённый к типам БД."""

    path: str
    filename: str
    title: str = ""
    artist: str | None = None
    album_artist: str | None = None
    album: str | None = None
    track_number: int | None = None
    disc_number: int | None = None
    year: int | None = None
    genre: str | None = None
    duration_ms: int = 0
    bitrate: int | None = None
    sample_rate: int | None = None
    channels: int | None = None
    format: str | None = None
    size_bytes: int = 0
    mtime: float = 0.0
    has_cover: bool = False
    ok: bool = True
    error: str | None = None
    # Служебное: заполняется, если удалось прочитать обложку.
    cover_mime: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


# Приведение типов
def _clean_text(value: Any) -> str | None:
    """Нормализует произвольное значение тега в безопасную строку."""
    if value is None:
        return None

    # mutagen часто отдаёт списки: Vorbis comments, MP4-атомы, ASF.
    if isinstance(value, (list, tuple)):
        parts = [_clean_text(item) for item in value]
        joined = [part for part in parts if part]
        if not joined:
            return None
        # Дубли внутри одного тега (частый артефакт рипперов) убираем.
        seen: list[str] = []
        for part in joined:
            if part not in seen:
                seen.append(part)
        value = ", ".join(seen)

    if isinstance(value, bytes):
        # ID3 иногда отдаёт байты; пробуем utf-8, затем latin-1.
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            value = value.decode("latin-1", errors="replace")

    text = str(value)
    text = _CONTROL_CHARS.sub("", text)
    text = text.replace("\u0000", "")
    # Неразрывные пробелы и типографские кавычки — в ASCII-эквиваленты.
    text = text.replace("\u00a0", " ").strip()

    if not text:
        return None
    if len(text) > MAX_STRING_LEN:
        text = text[:MAX_STRING_LEN].rstrip()
    return text or None


def _to_int(value: Any, *, maximum: int | None = None) -> int | None:
    """Достаёт первое целое из значения: "3/12" -> 3, "1998-05" -> 1998."""
    text = _clean_text(value)
    if not text:
        return None
    match = _TRACK_NUM.match(text)
    if not match:
        return None
    try:
        number = int(match.group(1))
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    if maximum is not None and number > maximum:
        return None
    return number


def _to_year(value: Any) -> int | None:
    """Год из TDRC/TYER/©day/date — допускает полную дату."""
    text = _clean_text(value)
    if not text:
        return None
    match = _YEAR.search(text)
    if not match:
        return None
    year = int(match.group(1))
    # Отсекаем мусор: год вне разумного диапазона грампластинок и будущего.
    if 1000 <= year <= 2200:
        return year
    return None


def _clean_genre(value: Any) -> str | None:
    """Убирает ID3-нумерацию вида "(17)Rock" и "(17)".Чистит лишние скобки."""
    text = _clean_text(value)
    if not text:
        return None
    text = re.sub(r"^\(\d+\)\s*", "", text).strip()
    return text or None


def _human_size(path: Path) -> int:
    """Размер файла; 0, если файл исчез между листингом и разбором."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _mtime(path: Path) -> float:
    """mtime файла; 0.0 при ошибке доступа."""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


# Извлечение обложки (только факт наличия — байты пишутся на этапе 5)
def _id3_has_cover(tags: Any) -> str | None:
    """Ищет APIC-кадр в ID3 и возвращает mime, если он есть."""
    try:
        for key in tags.keys():
            if str(key).startswith("APIC"):
                frame = tags[key]
                mime = getattr(frame, "mime", None)
                return _clean_text(mime) or "image/jpeg"
    except Exception:  # noqa: BLE001 - повреждённые ID3 не должны ломать разбор
        return None
    return None


def _vorbis_has_cover(fileobj: Any) -> str | None:
    """FLAC-картинки и base64 metadata_block_picture в Vorbis comments."""
    pictures = getattr(fileobj, "pictures", None)
    if pictures:
        mime = getattr(pictures[0], "mime", None)
        return _clean_text(mime) or "image/jpeg"
    try:
        block = fileobj.get("metadata_block_picture")
    except Exception:  # noqa: BLE001
        return None
    if block:
        # Формат не разбираем: наличие блока = наличие картинки.
        return "image/jpeg"
    return None


def _mp4_has_cover(tags: Any) -> str | None:
    """Атом covr в MP4."""
    try:
        covers = tags.get("covr")
    except Exception:  # noqa: BLE001
        return None
    if not covers:
        return None
    first = covers[0]
    image_format = getattr(first, "imageformat", None)
    # mutagen отдаёт MP4Cover.FORMAT_PNG / FORMAT_JPEG
    if image_format is not None:
        try:
            from mutagen.mp4 import MP4Cover

            if image_format == MP4Cover.FORMAT_PNG:
                return "image/png"
        except Exception:  # noqa: BLE001
            pass
    return "image/jpeg"


def _asf_has_cover(tags: Any) -> str | None:
    """WM/Picture в ASF/WMA."""
    try:
        pictures = tags.get("WM/Picture")
    except Exception:  # noqa: BLE001
        return None
    if not pictures:
        return None
    return "image/jpeg"


# Разбор по форматам
def _extract_id3(audio: Any, meta: TrackMetadata) -> None:
    """MP3, WAV с ID3, AIFF — все используют ID3-кадры."""
    tags = getattr(audio, "tags", None)
    if tags is None:
        return

    # TXXX-кадры («пользовательские») иногда содержат нормальные значения,
    # когда риппер поленился. Достаём их как fallback.
    def first_frame(*keys: str) -> Any:
        for key in keys:
            try:
                frame = tags.get(key)
            except Exception:  # noqa: BLE001
                continue
            if frame is not None:
                return frame
        return None

    meta.title = _clean_text(first_frame("TIT2", "TT2")) or meta.title
    meta.artist = _clean_text(first_frame("TPE1", "TP1"))
    meta.album_artist = _clean_text(first_frame("TPE2", "TP2"))
    meta.album = _clean_text(first_frame("TALB", "TAL"))
    meta.track_number = _to_int(first_frame("TRCK", "TRK"))
    meta.disc_number = _to_int(first_frame("TPOS", "TPA"))
    meta.year = _to_year(first_frame("TDRC", "TYER", "TDRL", "TDOR", "TYE"))
    meta.genre = _clean_genre(first_frame("TCON", "TCO"))

    meta.cover_mime = _id3_has_cover(tags)


def _extract_vorbis(fileobj: Any, meta: TrackMetadata) -> None:
    """FLAC и Ogg (vorbis/opus): ключи в нижнем регистре."""

    def get(*keys: str) -> Any:
        for key in keys:
            try:
                value = fileobj.get(key)
            except Exception:  # noqa: BLE001
                continue
            if value not in (None, [], ""):
                return value
        return None

    meta.title = _clean_text(get("title")) or meta.title
    meta.artist = _clean_text(get("artist"))
    meta.album_artist = _clean_text(get("albumartist", "album_artist", "ensemble"))
    meta.album = _clean_text(get("album"))
    meta.track_number = _to_int(get("tracknumber", "track", "tracktotal"))
    meta.disc_number = _to_int(get("discnumber", "disc", "disctotal"))
    meta.year = _to_year(get("date", "year", "originaldate"))
    meta.genre = _clean_genre(get("genre"))

    meta.cover_mime = _vorbis_has_cover(fileobj)


def _extract_mp4(fileobj: Any, meta: TrackMetadata) -> None:
    """MP4/M4A/AAC: атомы вида ©nam, aART, trkn."""
    tags = getattr(fileobj, "tags", None)
    if tags is None:
        return

    def get(*keys: str) -> Any:
        for key in keys:
            try:
                value = tags.get(key)
            except Exception:  # noqa: BLE001
                continue
            if value not in (None, [], ""):
                return value
        return None

    meta.title = _clean_text(get("\xa9nam")) or meta.title
    meta.artist = _clean_text(get("\xa9ART"))
    meta.album_artist = _clean_text(get("aART"))
    meta.album = _clean_text(get("\xa9alb"))

    # trkn/disk в MP4 — список кортежей [(номер, всего)].
    raw_track = get("trkn")
    if isinstance(raw_track, list) and raw_track:
        pair = raw_track[0]
        if isinstance(pair, tuple) and pair:
            meta.track_number = int(pair[0]) or None
        else:
            meta.track_number = _to_int(pair)
    raw_disc = get("disk")
    if isinstance(raw_disc, list) and raw_disc:
        pair = raw_disc[0]
        if isinstance(pair, tuple) and pair:
            meta.disc_number = int(pair[0]) or None
        else:
            meta.disc_number = _to_int(pair)

    meta.year = _to_year(get("\xa9day", "\xa9yr"))
    meta.genre = _clean_genre(get("\xa9gen", "gnre"))

    meta.cover_mime = _mp4_has_cover(tags)


def _extract_asf(fileobj: Any, meta: TrackMetadata) -> None:
    """WMA/ASF: ключи в стиле WM/AlbumTitle."""
    tags = getattr(fileobj, "tags", None)
    if tags is None:
        return

    def get(*keys: str) -> Any:
        for key in keys:
            try:
                value = tags.get(key)
            except Exception:  # noqa: BLE001
                continue
            if value not in (None, [], ""):
                return value
        return None

    meta.title = _clean_text(get("Title")) or meta.title
    meta.artist = _clean_text(get("Author", "WM/Artist"))
    meta.album_artist = _clean_text(get("WM/AlbumArtist"))
    meta.album = _clean_text(get("WM/AlbumTitle"))
    meta.track_number = _to_int(get("WM/TrackNumber", "WM/Track"))
    meta.disc_number = _to_int(get("WM/PartOfSet"))
    meta.year = _to_year(get("WM/Year", "WM/OriginalReleaseYear"))
    meta.genre = _clean_genre(get("WM/Genre"))

    meta.cover_mime = _asf_has_cover(tags)


def _extract_apev2(fileobj: Any, meta: TrackMetadata) -> None:
    """APE/WavPack/Musepack: APEv2-теги + иногда ID3 сверху."""

    def get(*keys: str) -> Any:
        for source_name in ("tags",):
            source = getattr(fileobj, source_name, None)
            if source is None:
                continue
            for key in keys:
                try:
                    value = source.get(key)
                except Exception:  # noqa: BLE001
                    continue
                if value not in (None, [], ""):
                    return value
        return None

    meta.title = _clean_text(get("Title", "title")) or meta.title
    meta.artist = _clean_text(get("Artist", "artist"))
    meta.album_artist = _clean_text(get("Album Artist", "album artist"))
    meta.album = _clean_text(get("Album", "album"))
    meta.track_number = _to_int(get("Track", "track"))
    meta.disc_number = _to_int(get("Disc", "disc"))
    meta.year = _to_year(get("Year", "year", "Date"))
    meta.genre = _clean_genre(get("Genre", "genre"))

    cover = get("Cover Art (Front)", "cover art (front)")
    if cover:
        meta.cover_mime = "image/jpeg"


# Карта расширение -> обработчик тегов.
# Порядок важен: сначала пробуем специфичный для контейнера обработчик.
_EXTRACTORS: dict[str, Callable[[Any, TrackMetadata], None]] = {
    ".mp3": _extract_id3,
    ".wav": _extract_id3,
    ".aiff": _extract_id3,
    ".aif": _extract_id3,
    ".flac": _extract_vorbis,
    ".ogg": _extract_vorbis,
    ".oga": _extract_vorbis,
    ".opus": _extract_vorbis,
    ".m4a": _extract_mp4,
    ".m4b": _extract_mp4,
    ".mp4": _extract_mp4,
    ".aac": _extract_mp4,
    ".alac": _extract_mp4,
    ".wma": _extract_asf,
    ".ape": _extract_apev2,
    ".wv": _extract_apev2,
    ".mpc": _extract_apev2,
}


def _apply_audio_info(audio: Any, meta: TrackMetadata) -> None:
    """Длительность, битрейт, частота, каналы из info-объекта mutagen."""
    info = getattr(audio, "info", None)
    if info is None:
        return

    # mutagen отдаёт секунды float, включая None у некоторых контейнеров.
    length = getattr(info, "length", None)
    if isinstance(length, (int, float)) and length > 0:
        meta.duration_ms = int(round(float(length) * 1000))

    bitrate = getattr(info, "bitrate", None)
    if isinstance(bitrate, (int, float)) and bitrate > 0:
        meta.bitrate = int(round(float(bitrate) / 1000))

    sample_rate = getattr(info, "sample_rate", None)
    if isinstance(sample_rate, (int, float)) and sample_rate > 0:
        meta.sample_rate = int(sample_rate)

    channels = getattr(info, "channels", None)
    if isinstance(channels, (int, float)) and channels > 0:
        meta.channels = int(channels)


def _guess_format(audio: Any, path: Path) -> str:
    """Человекочитаемый формат: из mime mutagen или из расширения."""
    mime = getattr(audio, "mime", None)
    if isinstance(mime, list) and mime:
        mime = mime[0]
    if isinstance(mime, str) and mime:
        return mime
    return path.suffix.lower().lstrip(".") or "unknown"


# Публичные алиасы нормализаторов: ими пользуются репозитории,
# чтобы не импортировать приватные имена.
clean_text = _clean_text
to_int = _to_int
to_year = _to_year
clean_genre = _clean_genre


# Публичный API
def is_supported_audio(path: str | Path) -> bool:
    """Является ли файл аудио, которое мы умеем разбирать."""
    suffix = Path(path).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        return False
    # Скрытые файлы и служебный мусор macOS/Windows не индексируем.
    name = Path(path).name
    if name.startswith("."):
        return False
    if name.lower() in {"thumbs.db", "desktop.ini"}:
        return False
    return True


def parse_track(path: str | Path, *, music_root: str | Path | None = None) -> TrackMetadata:
    """Разбирает один аудиофайл.

    Никогда не выбрасывает исключение: любая проблема возвращается в
    TrackMetadata(error=...). Если тегов нет вовсе, title выводится из
    имени файла — трек всё равно попадает в библиотеку.
    """
    file_path = Path(path)
    filename = file_path.name
    stem = file_path.stem

    meta = TrackMetadata(
        path=str(file_path),
        filename=filename,
        title=stem,
        size_bytes=_human_size(file_path),
        mtime=_mtime(file_path),
        format=file_path.suffix.lower().lstrip(".") or None,
    )

    if not file_path.exists():
        meta.ok = False
        meta.error = "файл не найден"
        return meta
    if not file_path.is_file():
        meta.ok = False
        meta.error = "не является обычным файлом"
        return meta
    if meta.size_bytes == 0:
        meta.ok = False
        meta.error = "файл нулевого размера"
        return meta

    try:
        import mutagen

        audio = mutagen.File(str(file_path))
    except Exception as exc:  # noqa: BLE001 - mutagen бросает десятки типов
        # Частая причина: файл не аудио, усечён или это неподдерживаемый кодек.
        logger.warning("mutagen не смог открыть %s: %s", file_path, exc)
        meta.ok = False
        meta.error = f"не читается mutagen: {type(exc).__name__}: {exc}"
        return meta

    if audio is None:
        meta.ok = False
        meta.error = "неопознанный аудиоформат"
        return meta

    try:
        _apply_audio_info(audio, meta)
    except Exception as exc:  # noqa: BLE001
        logger.debug("ошибка чтения info у %s: %s", file_path, exc)

    meta.format = _guess_format(audio, file_path)

    extractor = _EXTRACTORS.get(file_path.suffix.lower())
    if extractor is not None:
        try:
            extractor(audio, meta)
        except Exception as exc:  # noqa: BLE001
            # Теги частично повреждены — это не повод терять трек целиком.
            logger.warning("ошибка разбора тегов %s: %s", file_path, exc)
            meta.extra["tag_error"] = f"{type(exc).__name__}: {exc}"
    else:
        # Неизвестное расширение — пробуем универсальный путь FLAC/Vorbis.
        try:
            _extract_vorbis(audio, meta)
        except Exception:  # noqa: BLE001
            pass

    # Финальная нормализация обязательных полей.
    if not meta.title or not meta.title.strip():
        meta.title = stem or filename or "Без названия"
    if len(meta.title) > MAX_TITLE_LEN:
        meta.title = meta.title[:MAX_TITLE_LEN].rstrip()
    meta.has_cover = bool(meta.cover_mime)

    # Длительность 0 допустима (битый info), но неотрицательна.
    if meta.duration_ms < 0:
        meta.duration_ms = 0

    # Если album_artist не задан, а artist есть — альбом принадлежит артисту.
    # Это упрощает группировку и не создаёт пустых альбомных исполнителей.
    if not meta.album_artist and meta.artist:
        meta.album_artist = meta.artist

    return meta
