"""Импорт плейлистов из папки: поиск M3U/M3U8, разбор, сопоставление с библиотекой.

Задача: у пользователя есть папка с .m3u8 (часто размазана по библиотеке или
лежит отдельным каталогом). Нужно за один проход найти все файлы во всех
подпапках, разобрать каждый и сопоставить записи с треками в базе.

Сопоставление идёт по трём стратегиям, от точной к мягкой:

1. **По пути** — основной способ. Путь из m3u8 нормализуется (обратные слэши,
   лишние сегменты) и ищется в `tracks.path`.
2. **По префиксу** — если плейлист создан на другой машине (`M:\\Music\\...`
   или `/home/user/Music/...`). Пользователь задаёт пару «было → стало»,
   и префикс подменяется до поиска.
3. **По имени файла + артисту/названию** — как запасной вариант, когда путь
   восстановить нельзя. Возвращаем НЕСКОЛЬКО кандидатов: выбирает человек,
   автоматически угадывать опаснее, чем показать варианты.

Модуль ничего не пишет в БД: он только читает и возвращает план импорта.
Запись делает `PlaylistImporter` в API-слое, чтобы разбор можно было
показать пользователю до применения.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from app.playlists.matching import MATCH_LABELS, TrackIndex
from app.playlists.m3u import M3UEntry, M3UError, parse_m3u

logger = logging.getLogger(__name__)

# Расширения плейлистов. .m3u без «8» — тоже встречается (старые Winamp).
PLAYLIST_SUFFIXES = frozenset({".m3u", ".m3u8"})

# Потолки, чтобы случайно указанная папка «/» не повесила сервер.
MAX_PLAYLIST_FILES = 2000
MAX_DEPTH = 12
# Каталоги, которые не обходим: там нет плейлистов, но может быть мусор.
SKIP_DIRS = frozenset({
    ".git", ".svn", "__pycache__", "node_modules", "@eaDir", "#recycle",
    "$RECYCLE.BIN", "System Volume Information", "lost+found",
})


class ImportError_(Exception):
    """Ошибка импорта, пригодная для показа пользователю."""


@dataclass(slots=True)
class Candidate:
    """Вариант сопоставления записи плейлиста с треком библиотеки."""

    track_id: int
    path: str
    title: str
    artist: str | None
    album: str | None
    # "path" | "filename" | "title" | "prefix" | "title_artist" | "title_base" |
    # "filename_norm". Приблизительные варианты помечаются отдельно: интерфейс
    # показывает причину, чтобы список из похожих строк был понятен.
    match: str
    # Человекочитаемая причина: «по названию без feat», «по имени файла».
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "track_id": self.track_id,
            "path": self.path,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "match": self.match,
            "note": self.note,
        }


@dataclass(slots=True)
class UnmatchedEntry:
    """Запись, которую не удалось сопоставить однозначно."""

    raw_path: str
    title: str | None
    artist: str | None
    reason: str
    candidates: list[Candidate] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "raw_path": self.raw_path,
            "title": self.title,
            "artist": self.artist,
            "reason": self.reason,
            "candidates": [c.as_dict() for c in self.candidates],
        }


@dataclass(slots=True)
class PlaylistPlan:
    """Разобранный файл плейлиста и результат сопоставления."""

    path: str                    # путь к .m3u8
    name: str                    # имя файла без расширения
    total: int = 0
    # track_id по порядку записей; None — запись не сопоставлена.
    # Именно этот список задаёт порядок треков в плейлисте, поэтому он
    # выровнен с entries по индексу, а не просто «список найденных».
    resolutions: list[int | None] = field(default_factory=list)
    unmatched: list[UnmatchedEntry] = field(default_factory=list)
    error: str | None = None
    # Совпадает с существующим плейлистом того же владельца.
    existing_id: int | None = None
    existing_count: int = 0

    @property
    def matched(self) -> list[int]:
        return [tid for tid in self.resolutions if tid is not None]

    @property
    def matched_count(self) -> int:
        return len(self.matched)

    @property
    def ok(self) -> bool:
        return self.error is None

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "name": self.name,
            "total": self.total,
            "matched": len(self.matched),
            "unmatched": len(self.unmatched),
            "unmatched_items": [item.as_dict() for item in self.unmatched[:50]],
            "error": self.error,
            "existing_id": self.existing_id,
            "existing_count": self.existing_count,
        }


def normalize_path(raw: str) -> str:
    """Приводит путь к виду, в котором хранится библиотека.

    Обратные слэши → прямые, повторные слэши схлопываются, «..» разрешается
    лексически. Приводить к реальному пути через resolve() нельзя: файла
    может не быть, а сравнение всё равно нужно.
    """
    text = (raw or "").strip().replace("\\", "/")
    if not text:
        return ""
    if "://" in text:
        return text
    # PurePosixPath("/a/b").parts == ("/", "a", "b"): корень уже в списке,
    # поэтому отдельный префикс не нужен — иначе получалось «///music».
    parts = [
        part for part in PurePosixPath(text).parts
        if part not in ("", ".", "/")
    ]
    # «..» разрешаем вручную: Path.resolve() требует существования файла.
    resolved: list[str] = []
    for part in parts:
        if part == "..":
            if resolved and resolved[-1] != "..":
                resolved.pop()
            else:
                resolved.append(part)
        else:
            resolved.append(part)
    joined = "/".join(resolved)
    # Абсолютный путь восстанавливаем по исходной строке, а не по parts:
    # «..» в начале относительного пути не должен превращаться в абсолютный.
    is_absolute = text.startswith("/")
    return f"/{joined}" if is_absolute else joined


def apply_prefix(path: str, old_prefix: str, new_prefix: str) -> str:
    """Заменяет префикс пути, если он совпадает.

    Нужно для плейлистов с другой машины: `M:/Music/...` → `/music/...`.
    Сравнение без учёта регистра: Windows-пути регистр не различают.
    """
    if not old_prefix:
        return path
    normalized_old = normalize_path(old_prefix).rstrip("/")
    if not normalized_old:
        return path
    if path.lower().startswith(normalized_old.lower()):
        tail = path[len(normalized_old):].lstrip("/")
        base = normalize_path(new_prefix).rstrip("/")
        return f"{base}/{tail}" if base else tail
    return path


def find_playlist_files(root: Path) -> tuple[list[Path], list[str]]:
    """Рекурсивно собирает .m3u/.m3u8. Возвращает (файлы, предупреждения)."""
    if not root.exists():
        raise ImportError_(f"папка не найдена: {root}")
    if not root.is_dir():
        raise ImportError_(f"это не папка: {root}")

    found: list[Path] = []
    warnings: list[str] = []
    root_depth = len(root.parts)

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        # Ограничение глубины: случайная ссылка на корень ФС не должна
        # приводить к обходу всего диска.
        if len(current.parts) - root_depth > MAX_DEPTH:
            dirnames[:] = []
            warnings.append(f"глубина больше {MAX_DEPTH}, пропущено: {current}")
            continue
        # Служебные каталоги отсекаем на уровне обхода, а не после.
        dirnames[:] = [
            name for name in dirnames
            if name not in SKIP_DIRS and not name.startswith(".")
        ]
        for filename in filenames:
            if Path(filename).suffix.lower() not in PLAYLIST_SUFFIXES:
                continue
            found.append(current / filename)
            if len(found) >= MAX_PLAYLIST_FILES:
                warnings.append(
                    f"найдено больше {MAX_PLAYLIST_FILES} плейлистов, "
                    f"остальные пропущены"
                )
                return _dedupe_by_name(found, warnings)

    return _dedupe_by_name(found, warnings)


def _dedupe_by_name(
    files: list[Path], warnings: list[str]
) -> tuple[list[Path], list[str]]:
    """Схлопывает «X.m3u» и «X.m3u8» из одного каталога в один плейлист.

    Причина: многие программы сохраняют плейлист сразу в двух форматах,
    и без этой проверки один и тот же список импортировался дважды — под
    одинаковым именем. Имя плейлиста берётся из имени файла без расширения,
    поэтому «311.m3u» и «311.m3u8» давали два плейлиста «311».

    Предпочитаем .m3u8: он гарантированно UTF-8, тогда как .m3u часто
    в cp1251 и может быть пустым огрызком.
    """
    by_key: dict[tuple[Path, str], Path] = {}
    order: list[tuple[Path, str]] = []

    def rank(path: Path) -> int:
        # Больше — приоритетнее.
        return 1 if path.suffix.lower() == ".m3u8" else 0

    for path in files:
        key = (path.parent, path.stem.lower())
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = path
            order.append(key)
        elif rank(path) > rank(existing):
            # Нашли более предпочтительный формат того же плейлиста.
            by_key[key] = path
            warnings.append(
                f"дубликат имени «{path.stem}»: взят {path.name}, "
                f"пропущен {existing.name}"
            )
        else:
            warnings.append(
                f"дубликат имени «{path.stem}»: взят {existing.name}, "
                f"пропущен {path.name}"
            )

    return [by_key[key] for key in order], warnings


def read_playlist(path: Path, music_root: Path) -> tuple[list[M3UEntry], str | None]:
    """Читает и разбирает файл. Возвращает (записи, ошибка).

    Кодировки пробуем по очереди: UTF-8 (норма для m3u8), затем cp1251
    (старые Windows-плейлисты с русскими именами). Без этого такие файлы
    молча превращались бы в мусор.
    """
    for encoding in ("utf-8-sig", "utf-8", "cp1251", "latin-1"):
        try:
            text = path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
        except OSError as exc:
            return [], f"не удалось прочитать файл: {exc}"
        try:
            result = parse_m3u(text, source_dir=path.parent)
        except M3UError as exc:
            return [], str(exc)
        return result.entries, None
    return [], "не удалось определить кодировку файла"


async def match_entries(
    entries: list[M3UEntry],
    *,
    music_root: Path,
    find_by_path,
    find_by_filename,
    find_by_title,
    prefix_from: str = "",
    prefix_to: str = "",
    max_candidates: int = 5,
    index: TrackIndex | None = None,
) -> tuple[list[int | None], list[UnmatchedEntry]]:
    """Сопоставляет записи с библиотекой.

    `find_by_path` и остальные — асинхронные функции репозитория треков,
    передаются снаружи: модуль не знает про БД.

    Возвращает (список id или None по каждой записи, несопоставленные).
    Список выровнен с entries по индексу: порядок записей в файле задаёт
    порядок треков в плейлисте, и разрывать его нельзя.
    """
    matched: list[int | None] = []
    unmatched: list[UnmatchedEntry] = []

    for entry in entries:
        raw = entry.path
        normalized = normalize_path(raw)
        if prefix_from:
            normalized = apply_prefix(normalized, prefix_from, prefix_to)

        # 1. Точный путь.
        track = await find_by_path(normalized)
        if track is not None:
            matched.append(track.id)
            continue

        # Путь из m3u8 может быть относительно корня музыки: «All/x.mp3»
        # при корне /music. Пробуем приклеить корень.
        if not normalized.startswith("/"):
            joined = normalize_path(f"{music_root}/{normalized}")
            track = await find_by_path(joined)
            if track is not None:
                matched.append(track.id)
                continue

        # 2. По имени файла: путь не совпал, но файл с таким именем один.
        filename = PurePosixPath(normalized).name
        if filename:
            found = await find_by_filename(filename, limit=max_candidates + 1)
            if len(found) == 1:
                matched.append(found[0].id)
                continue
            if found:
                matched.append(None)
                unmatched.append(UnmatchedEntry(
                    raw_path=raw, title=entry.title, artist=entry.artist,
                    reason="путь не совпал, несколько файлов с таким именем",
                    candidates=[
                        Candidate(t.id, t.path, t.title, t.artist, t.album, "filename")
                        for t in found[:max_candidates]
                    ],
                ))
                continue

        # 3. По артисту и названию из #EXTINF.
        if entry.title:
            found = await find_by_title(
                entry.title, artist=entry.artist, limit=max_candidates
            )
            if len(found) == 1:
                matched.append(found[0].id)
                continue
            if found:
                matched.append(None)
                unmatched.append(UnmatchedEntry(
                    raw_path=raw, title=entry.title, artist=entry.artist,
                    reason="путь не совпал, найдено по названию, но неоднозначно",
                    candidates=[
                        Candidate(t.id, t.path, t.title, t.artist, t.album, "title")
                        for t in found
                    ],
                ))
                continue

        # 4. Приблизительные варианты.
        #
        # Сюда попадают пары, где теги разъехались: в плейлисте исполнитель
        # перечислением, в библиотеке он один, а соавторы вынесены в название.
        # Такие совпадения НЕ подставляем молча: показываем варианты с причиной,
        # решение принимает пользователь.
        if index is not None:
            suggestions = index.suggest(
                title=entry.title,
                artist=entry.artist,
                filename=filename or None,
                limit=max_candidates,
            )
            if suggestions:
                matched.append(None)
                unmatched.append(UnmatchedEntry(
                    raw_path=raw, title=entry.title, artist=entry.artist,
                    reason="похожие треки — выберите подходящий",
                    candidates=[
                        Candidate(
                            int(track["id"]), str(track["path"]),
                            str(track.get("title") or ""),
                            track.get("artist"), track.get("album"),
                            label, MATCH_LABELS.get(label, ""),
                        )
                        for track, label in suggestions
                    ],
                ))
                continue

        matched.append(None)
        unmatched.append(UnmatchedEntry(
            raw_path=raw, title=entry.title, artist=entry.artist,
            reason="не найдено в библиотеке",
        ))

    return matched, unmatched


def playlist_name(path: Path) -> str:
    """Имя плейлиста — имя файла без расширения.

    Пустое имя недопустимо: Subsonic-клиенты (Navidrome ловился на этом)
    показывают плейлисты с пустым названием как «без имени». Если файл
    называется «.m3u8», подставляем имя родительского каталога.
    """
    stem = path.stem.strip()
    # Path(".m3u8").stem возвращает ".m3u8": файл без имени, только расширение.
    if stem and not stem.startswith("."):
        return stem[:255]
    parent = path.parent.name.strip()
    return (parent or "Плейлист")[:255]
