"""Импорт и экспорт плейлистов в формате M3U8.

M3U8 — это UTF-8 M3U. Спецификация вольная, поэтому парсер терпимый:

  * `#EXTM3U` — заголовок, может отсутствовать;
  * `#EXTINF:<секунды>,<артист> - <название>` — метаданные следующего трека;
  * `#EXT-X-...` — HLS-плейлисты, не наш формат: файл целиком отвергается;
  * строки-пути — абсолютные или относительные (от папки самого .m3u8).

Сопоставление с библиотекой идёт по ПУТИ (основной способ), затем по
паре «артист + название» как запасной: плейлисты часто переносят между
машинами, и абсолютные пути там уже не совпадают.

Экспорт пишет `#EXTINF` с длительностью: без неё некоторые плееры
показывают нулевые треки.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from app.repositories.playlists import PlaylistRow
from app.repositories.tracks import TrackRow

logger = logging.getLogger(__name__)

# Отсекаем гигантские файлы: плейлист на миллион строк — это атака, не музыка.
MAX_LINES = 50_000
MAX_FILE_BYTES = 8 * 1024 * 1024
_WINDOWS_DRIVE = re.compile(r"[A-Za-z]:/")


class M3UError(Exception):
    """Файл не является корректным M3U/M3U8."""


@dataclass(slots=True)
class M3UEntry:
    """Одна запись плейлиста: путь и необязательные метаданные."""

    path: str
    title: str | None = None
    artist: str | None = None
    duration_sec: int | None = None
    # Заполняется при сопоставлении с библиотекой.
    track_id: int | None = None


@dataclass(slots=True)
class M3UParseResult:
    entries: list[M3UEntry] = field(default_factory=list)
    name: str = ""
    hls: bool = False


def parse_m3u(text: str, *, source_dir: Path | None = None) -> M3UParseResult:
    """Разбирает содержимое .m3u8.

    Относительные пути разрешаются от `source_dir` — так плейлисты,
    перенесённые вместе с музыкой, продолжают работать.
    """
    if len(text) > MAX_FILE_BYTES:
        raise M3UError("файл слишком большой")

    lines = text.splitlines()
    if len(lines) > MAX_LINES:
        raise M3UError(f"слишком много строк (> {MAX_LINES})")

    result = M3UParseResult()
    pending_title: str | None = None
    pending_artist: str | None = None
    pending_duration: int | None = None

    for raw in lines:
        line = raw.strip()
        if not line:
            continue

        if line.startswith("#EXTM3U"):
            # Заголовок; в строке может быть атрибутов на несколько КБ.
            continue

        if line.startswith("#EXT-X-"):
            # Признак HLS. Настоящий HLS-плейлист состоит только из тегов
            # и ссылок на сегменты .ts — импортировать его как музыку нельзя.
            result.hls = True
            continue

        if line.startswith("#EXTINF:"):
            payload = line[len("#EXTINF:"):]
            seconds_part, _, title_part = payload.partition(",")
            try:
                pending_duration = int(float(seconds_part.strip()))
            except (TypeError, ValueError):
                pending_duration = None
            title_part = title_part.strip()
            if title_part:
                # Формат «Артист - Название» — самый распространённый.
                artist, sep, title = title_part.partition(" - ")
                if sep:
                    pending_artist = artist.strip() or None
                    pending_title = title.strip() or None
                else:
                    pending_title = title_part
            continue

        if line.startswith("#"):
            # Прочие комментарии игнорируем: тегов в M3U много и они вольные.
            continue

        entry = M3UEntry(
            path=_resolve_path(line, source_dir),
            title=pending_title,
            artist=pending_artist,
            duration_sec=pending_duration,
        )
        result.entries.append(entry)
        pending_title = pending_artist = pending_duration = None

    if not result.entries:
        raise M3UError("в файле нет ни одного трека")

    if result.hls:
        # Все строки оказались тегами HLS: принимать нечего.
        raise M3UError(
            "это HLS-плейлист (сегменты потокового видео), а не список треков"
        )

    return result


def _resolve_path(raw_path: str, source_dir: Path | None) -> str:
    """Приводит путь к абсолютному в Linux-нотации.

    Обратные слэши Windows нормализуются: плейлисты часто приходят
    с машины на Windows, а библиотека живёт в Linux-контейнере.
    """
    normalized = raw_path.replace("\\", "/")
    if "://" in normalized:
        # Сетевые URL не поддерживаем: файла на диске нет.
        return normalized
    if _WINDOWS_DRIVE.match(normalized):
        # Буква диска — абсолютный путь Windows; Path() на Linux так его не считает.
        return normalized
    path = Path(normalized)
    if path.is_absolute():
        return str(PurePosixPath(normalized))
    if source_dir is not None:
        return str(PurePosixPath(str(source_dir), normalized))
    return normalized


def export_m3u8(playlist: PlaylistRow, tracks: list[TrackRow]) -> str:
    """Собирает содержимое .m3u8. Пути — абсолютные, как в библиотеке."""
    lines: list[str] = ["#EXTM3U"]
    for track in tracks:
        duration = track.duration_sec
        artist = track.artist or ""
        title = track.title
        label = f"{artist} - {title}" if artist else title
        # #EXTINF требует целые секунды; 0 допустим, но плееры его не любят,
        # поэтому неизвестную длительность пишем как -1 (стандарт для «н/д»).
        seconds = duration if duration > 0 else -1
        lines.append(f"#EXTINF:{seconds},{label}")
        lines.append(track.path)
    return "\n".join(lines) + "\n"


def safe_export_filename(name: str) -> str:
    """Имя файла без символов, ломающих ФС, и без обхода каталогов.

    Защита от `../../etc/passwd` в названии плейлиста: клиент задаёт имя
    сам, и оно попадает в имя файла при экспорте.
    """
    cleaned = "".join(
        char for char in name
        if char not in ('/', '\\', "\0", ":", "*", "?", '"', "<", ">", "|")
    ).strip().strip(".")
    cleaned = cleaned.replace(os.sep, "_")
    if not cleaned:
        cleaned = "playlist"
    return f"{cleaned[:120]}.m3u8"


async def match_entries(
    entries: list[M3UEntry],
    *,
    by_path,
    by_title_artist,
) -> tuple[list[int], int]:
    """Сопоставляет записи M3U с треками библиотеки.

    Возвращает (список track_id в порядке файла, число несопоставленных).

    Два прохода: сначала по нормализованному пути (точное совпадение),
    затем по паре «артист + название» для тех, что не нашлись. Второй
    проход нужен при переносе плейлиста с другой машины.
    """
    resolved: list[int] = []
    unmatched = 0

    for entry in entries:
        if entry.path and "://" not in entry.path:
            track_id = await by_path(entry.path)
            if track_id is not None:
                resolved.append(track_id)
                continue
        if entry.title:
            track_id = await by_title_artist(entry.artist, entry.title)
            if track_id is not None:
                resolved.append(track_id)
                continue
        unmatched += 1

    return resolved, unmatched
