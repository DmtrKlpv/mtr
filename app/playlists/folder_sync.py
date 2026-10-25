"""Плейлист из папки и ресинк его с этой папкой.

Два сценария, оба работают только с БД медиатеки (mtr_music.db):

1. ``scan_folder_to_playlist`` — рекурсивно обходит папку (например,
   ``/music/Дискографии/Artist1``), находит аудиофайлы и собирает ОДИН плейлист.
   В плейлист пишутся ссылки на ``tracks.id``, а не пути. Файл сопоставляется
   с треком по относительному/абсолютному пути (батч-запросом ``IN``).
   Порядок: имя подпапки → номер диска → номер трека → имя файла.

2. ``build_resync_plan`` / ``apply_resync_plan`` — сверяет плейлист с папкой:
   * проверяет свежесть медиатеки (см. ``check_library_fresh``);
   * треки, чей файл пропал по старому пути, НЕ удаляются сразу: одним-двумя
     батч-запросами ищем их в БД по ``content_hash`` и по тегам
     ``(artist, album, title)``;
   * найденный трек → связь в плейлисте переключается на новый ``track_id``,
     позиция сохраняется;
   * не найденный никуда трек → кандидат на удаление. Удаление выполняется
     только в ``apply_resync_plan`` и только для id, которые пользователь
     подтвердил после preview.

Модуль не знает про HTTP. Ошибки — ``FolderSyncError`` с кодом для API.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from app.db.database import Database
from app.repositories.playlists import PlaylistRepository
from app.scanner.tag_parser import SUPPORTED_EXTENSIONS
from app.utils.timeutil import utcnow_iso

logger = logging.getLogger(__name__)

# Размер пачки для IN (...): SQLite ограничивает число параметров.
BATCH = 300
# Допуск по длительности, мс: при сопоставлении по тегам один и тот же трек
# может отличаться тегами, но не длительностью.
DURATION_TOLERANCE_MS = 1000
# Сколько живут превью ресинка, сек. Дальше подтверждение не принимаем.
PLAN_TTL_SEC = 15 * 60
# Точность finished_at — секунды. Допуск на округление при сравнении с mtime.
SCAN_CLOCK_SLACK_SEC = 1.0

STALE_MESSAGE = (
    "Медиатека сервера не обновлялась. Сначала запустите сканирование сервера"
)


class FolderSyncError(Exception):
    """Ошибка сценария. ``code`` — машиночитаемый код для клиента."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ----------------------------------------------------------------------
# Прогресс: общий объект для UI (опрос раз в секунду, как у прогрева обложек)
# ----------------------------------------------------------------------
@dataclass
class Job:
    id: str
    kind: str                      # "scan" | "resync"
    state: str = "running"         # running | done | error
    stage: str = "start"           # этап для текста в UI
    total: int = 0
    done: int = 0
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    result: dict[str, Any] | None = None
    error_code: str | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        end = self.finished_at or time.monotonic()
        percent = round(self.done / self.total * 100) if self.total else (
            100 if self.state == "done" else 0
        )
        return {
            "id": self.id,
            "kind": self.kind,
            "state": self.state,
            "stage": self.stage,
            "total": self.total,
            "done": self.done,
            "percent": percent,
            "elapsed_sec": round(end - self.started_at, 1),
            "result": self.result,
            "error_code": self.error_code,
            "error": self.error,
        }


class JobRegistry:
    """Хранит последние задачи в памяти процесса. Старые вытесняются."""

    def __init__(self, keep: int = 50) -> None:
        self._jobs: dict[str, Job] = {}
        self._keep = keep

    def new(self, kind: str) -> Job:
        job = Job(id=secrets.token_urlsafe(8), kind=kind)
        self._jobs[job.id] = job
        while len(self._jobs) > self._keep:
            self._jobs.pop(next(iter(self._jobs)))
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)


class PlanStore:
    """Превью ресинка, ждущие подтверждения. Токен одноразовый, с TTL."""

    def __init__(self) -> None:
        self._plans: dict[str, tuple[float, "ResyncPlan"]] = {}

    def put(self, plan: "ResyncPlan") -> str:
        self._prune()
        token = secrets.token_urlsafe(16)
        self._plans[token] = (time.monotonic() + PLAN_TTL_SEC, plan)
        return token

    def take(self, token: str) -> "ResyncPlan":
        """Забирает план (одноразово). Нет или истёк — FolderSyncError."""
        self._prune()
        entry = self._plans.pop(token, None)
        if entry is None:
            raise FolderSyncError(
                "plan_not_found",
                "Превью не найдено или устарело. Запустите проверку заново.",
            )
        return entry[1]

    def _prune(self) -> None:
        now = time.monotonic()
        for key in [k for k, (exp, _) in self._plans.items() if exp < now]:
            self._plans.pop(key, None)


# ----------------------------------------------------------------------
# Файловая часть (блокирующая — вызывается через asyncio.to_thread)
# ----------------------------------------------------------------------
def validate_folder(raw: str, music_root: Path) -> Path:
    """Проверяет, что путь — папка внутри корня медиатеки.

    Защита от выхода за пределы библиотеки: ``..`` и симлинки нормализуются
    через resolve(), после чего путь обязан лежать под ``music_root``.
    """
    value = (raw or "").strip()
    if not value:
        raise FolderSyncError("folder_empty", "Укажите путь к папке")
    candidate = Path(value)
    if not candidate.is_absolute():
        raise FolderSyncError(
            "folder_not_absolute", "Путь должен быть абсолютным, например /music/Artist1"
        )
    root = music_root.resolve()
    resolved = candidate.resolve()
    if resolved != root and root not in resolved.parents:
        raise FolderSyncError(
            "folder_outside_root", f"Папка должна лежать внутри {root}"
        )
    if not resolved.exists():
        raise FolderSyncError("folder_missing", f"Папка не найдена: {value}")
    if not resolved.is_dir():
        raise FolderSyncError("folder_not_dir", f"Это не папка: {value}")
    return resolved


def walk_audio(root: Path) -> list[tuple[str, float]]:
    """Все аудиофайлы под root (рекурсивно). Возвращает (путь, mtime).

    Скрытые файлы и папки (начинаются с точки) пропускаются. Путь —
    строка в том же виде, что tracks.path (абсолютный, POSIX).
    """
    found: list[tuple[str, float]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            if os.path.splitext(name)[1].lower() not in SUPPORTED_EXTENSIONS:
                continue
            full = os.path.join(dirpath, name)
            try:
                mtime = os.stat(full).st_mtime
            except OSError:
                continue
            found.append((full, mtime))
    return found


def tree_latest_mtime(root: Path) -> float:
    """Самое позднее изменение в дереве: файлы и папки.

    Папка меняет mtime при добавлении/удалении файлов, поэтому учитываем и её.
    """
    latest = 0.0
    for dirpath, dirnames, filenames in os.walk(root):
        try:
            latest = max(latest, os.stat(dirpath).st_mtime)
        except OSError:
            pass
        for name in filenames:
            try:
                latest = max(latest, os.stat(os.path.join(dirpath, name)).st_mtime)
            except OSError:
                continue
    return latest


# ----------------------------------------------------------------------
# БД: батч-запросы
# ----------------------------------------------------------------------
_TRACK_COLS = (
    "id, path, title, artist, album, album_id, track_number, disc_number, "
    "duration_ms, size_bytes, content_hash, missing"
)


# Фильтр missing = 0 в SQL не ставим: без ANALYZE планировщик SQLite выбирает
# индекс по missing (низкая селективность) вместо ix_tracks_content_hash /
# ix_tracks_tags и поиск деградирует до скана. Пропавшие отфильтровываем в Python.


def _chunks(items: list, size: int = BATCH):
    for start in range(0, len(items), size):
        yield items[start:start + size]


async def tracks_by_paths(db: Database, paths: Iterable[str]) -> dict[str, dict]:
    """Треки по путям. Один запрос на пачку, без цикла по файлам."""
    unique = list(dict.fromkeys(paths))
    result: dict[str, dict] = {}
    for batch in _chunks(unique):
        marks = ",".join("?" for _ in batch)
        rows = await db.fetch_all(
            f"SELECT {_TRACK_COLS} FROM tracks WHERE path IN ({marks})",
            tuple(batch),
        )
        for row in rows:
            result[row["path"]] = dict(row)
    return result


async def tracks_by_hashes(db: Database, hashes: Iterable[str]) -> list[dict]:
    """Живые треки с данным content_hash. Индекс ix_tracks_content_hash."""
    unique = [h for h in dict.fromkeys(hashes) if h]
    out: list[dict] = []
    for batch in _chunks(unique):
        marks = ",".join("?" for _ in batch)
        rows = await db.fetch_all(
            f"SELECT {_TRACK_COLS} FROM tracks WHERE content_hash IN ({marks})",
            tuple(batch),
        )
        out.extend(dict(row) for row in rows if not row["missing"])
    return out


async def tracks_by_tags(
    db: Database, keys: Iterable[tuple[str, str, str]]
) -> list[dict]:
    """Живые треки по (artist, album, title). Индекс ix_tracks_tags.

    Один запрос на пачку через row-value IN. Ключи с NULL сюда не передаём:
    NULL не сравнивается в IN, поэтому такие треки ищем только по хэшу.
    """
    unique = list(dict.fromkeys(keys))
    out: list[dict] = []
    for batch in _chunks(unique, size=BATCH // 3):
        values = ",".join("(?,?,?)" for _ in batch)
        params: list[Any] = []
        for artist, album, title in batch:
            params.extend((artist, album, title))
        rows = await db.fetch_all(
            f"SELECT {_TRACK_COLS} FROM tracks "
            f"WHERE (artist, album, title) IN (VALUES {values})",
            tuple(params),
        )
        out.extend(dict(row) for row in rows if not row["missing"])
    return out


# ----------------------------------------------------------------------
# Сортировка
# ----------------------------------------------------------------------
def subfolder_key(track_path: str, root: str) -> str:
    """Относительный путь папки трека от корня плейлиста ('' — лежит в корне)."""
    try:
        rel = os.path.relpath(os.path.dirname(track_path), root)
    except ValueError:
        return ""
    return "" if rel == "." else rel.replace(os.sep, "/")


def sort_key(row: dict, root: str) -> tuple:
    """Имя подпапки → номер диска → номер трека → имя файла.

    Пустые номера уходят в конец внутри своей группы: треки без тегов не
    перемешиваются с нумерованными.
    """
    disc = row.get("disc_number")
    track = row.get("track_number")
    return (
        subfolder_key(row["path"], root).casefold(),
        disc if disc is not None else 10**6,
        track if track is not None else 10**6,
        os.path.basename(row["path"]).casefold(),
    )


# ----------------------------------------------------------------------
# Сценарий 1: папка → плейлист
# ----------------------------------------------------------------------
@dataclass
class ScanResult:
    playlist_id: int
    name: str
    folder: str
    files_found: int
    matched: int
    unmatched: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "playlist_id": self.playlist_id,
            "name": self.name,
            "folder": self.folder,
            "files_found": self.files_found,
            "matched": self.matched,
            "unmatched_count": len(self.unmatched),
            "unmatched": self.unmatched[:100],
        }


async def scan_folder_to_playlist(
    *,
    db: Database,
    playlists: PlaylistRepository,
    music_root: Path,
    folder: str,
    owner_id: int,
    name: str | None = None,
    job: Job | None = None,
) -> ScanResult:
    """Собирает один плейлист из всех аудиофайлов папки.

    Файлы, которых нет в медиатеке, не добавляются, а перечисляются в
    ``unmatched``: обычно это значит, что сервер ещё не просканировал папку.
    """
    root = validate_folder(folder, music_root)
    root_str = str(root)
    playlist_name = (name or "").strip() or root.name

    if job:
        job.stage = "Обход папки…"
    files = await asyncio.to_thread(walk_audio, root)
    paths = [p for p, _ in files]
    if job:
        job.total = len(paths)
        job.stage = "Сопоставление с медиатекой…"

    found: dict[str, dict] = {}
    for batch in _chunks(paths):
        found.update(await tracks_by_paths(db, batch))
        if job:
            job.done = min(job.done + len(batch), len(paths))

    rows = [found[p] for p in paths if p in found and not found[p]["missing"]]
    unmatched = [p for p in paths if p not in found or found[p]["missing"]]
    rows.sort(key=lambda r: sort_key(r, root_str))

    if job:
        job.stage = "Создание плейлиста…"
    track_ids = [int(r["id"]) for r in rows]
    async with db.transaction():
        playlist = await playlists.create(
            owner_id, playlist_name, track_ids=track_ids,
        )
        await db.execute(
            "UPDATE playlists SET origin_folder_path = ?, synced_at = ? WHERE id = ?",
            (root_str, utcnow_iso(), playlist.id),
        )

    logger.info(
        "плейлист %r из %s: %d треков, не в медиатеке %d",
        playlist_name, root_str, len(track_ids), len(unmatched),
    )
    return ScanResult(
        playlist_id=playlist.id,
        name=playlist_name,
        folder=root_str,
        files_found=len(paths),
        matched=len(track_ids),
        unmatched=unmatched,
    )


# ----------------------------------------------------------------------
# Сценарий 2: ресинк
# ----------------------------------------------------------------------
def _parse_iso(value: str | None) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


async def check_library_fresh(
    db: Database, folder_mtime: float
) -> None:
    """Бросает library_stale, если папка изменилась после последнего скана.

    Берём время завершения последнего успешного скана. Если сканов не было
    вовсе — тоже считаем медиатеку устаревшей.
    """
    last = await db.fetch_value(
        "SELECT MAX(finished_at) FROM scan_runs WHERE status = 'completed'",
        default=None,
    )
    scanned_at = _parse_iso(last)
    # finished_at хранится с точностью до секунды, а mtime — дробный.
    # Без допуска файл, изменённый в ту же секунду до конца скана, давал бы ложную тревогу.
    if scanned_at is None or folder_mtime > scanned_at + SCAN_CLOCK_SLACK_SEC:
        raise FolderSyncError("library_stale", STALE_MESSAGE)


@dataclass
class ResyncPlan:
    playlist_id: int
    folder: str
    # Переключить связь на другой трек: позиция сохраняется.
    relocate: list[dict[str, Any]] = field(default_factory=list)
    # Треки не найдены нигде в БД. Удаляются только после подтверждения.
    remove: list[dict[str, Any]] = field(default_factory=list)
    # Файлы в папке, которых ещё нет в плейлисте: добавляем в конец.
    add: list[dict[str, Any]] = field(default_factory=list)
    # Найдено несколько кандидатов и ни один не выделился. Не трогаем.
    ambiguous: list[dict[str, Any]] = field(default_factory=list)
    # Файлы в папке, которых нет в медиатеке.
    unmatched_files: list[str] = field(default_factory=list)
    kept: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "playlist_id": self.playlist_id,
            "folder": self.folder,
            "kept": self.kept,
            "relocate": self.relocate,
            "remove": self.remove,
            "add": self.add,
            "ambiguous": self.ambiguous,
            "unmatched_files": self.unmatched_files[:100],
            "unmatched_count": len(self.unmatched_files),
        }


def _pick_candidate(
    entry: dict, hash_hits: list[dict], tag_hits: list[dict], taken: set[int]
) -> tuple[str, dict | None, list[dict]]:
    """Выбирает новый трек для пропавшего. Возвращает (статус, трек, кандидаты).

    Статусы: ``relocate`` — однозначно нашли; ``remove`` — кандидатов нет;
    ``ambiguous`` — несколько, и длительность не помогла.
    """
    own_hash = entry.get("content_hash")
    by_hash = [c for c in hash_hits if own_hash and c["content_hash"] == own_hash]
    by_tags = tag_hits

    # Один и тот же файл, перемещённый или переименованный, — самый сильный сигнал.
    pool: dict[int, dict] = {}
    for cand in by_hash + by_tags:
        if cand["id"] == entry["track_id"]:
            continue
        pool[cand["id"]] = cand
    candidates = list(pool.values())
    if not candidates:
        return "remove", None, []

    # Предпочитаем треки, которых ещё нет в плейлисте.
    fresh = [c for c in candidates if c["id"] not in taken] or candidates

    if len(fresh) == 1:
        return "relocate", fresh[0], candidates

    if own_hash:
        exact = [c for c in fresh if c["content_hash"] == own_hash]
        if len(exact) == 1:
            return "relocate", exact[0], candidates

    duration = entry.get("duration_ms") or 0
    close = [
        c for c in fresh
        if abs((c.get("duration_ms") or 0) - duration) <= DURATION_TOLERANCE_MS
    ]
    if len(close) == 1:
        return "relocate", close[0], candidates
    return "ambiguous", None, candidates


async def build_resync_plan(
    *,
    db: Database,
    playlists: PlaylistRepository,
    music_root: Path,
    playlist_id: int,
    user_id: int,
    is_admin: bool,
    job: Job | None = None,
) -> ResyncPlan:
    """Считает, что изменится при ресинке. Ничего не пишет в БД."""
    playlist = await playlists.get(playlist_id)
    if playlist is None:
        raise FolderSyncError("not_found", "Плейлист не найден")
    if not PlaylistRepository.can_edit(playlist, user_id, is_admin=is_admin):
        raise FolderSyncError("forbidden", "Нет прав на редактирование плейлиста")

    origin = await db.fetch_value(
        "SELECT origin_folder_path FROM playlists WHERE id = ?",
        (playlist_id,), default=None,
    )
    if not origin:
        raise FolderSyncError(
            "no_origin", "Плейлист не создан из папки — ресинк невозможен"
        )

    if job:
        job.stage = "Проверка медиатеки…"
    root = validate_folder(origin, music_root)
    root_str = str(root)
    folder_mtime = await asyncio.to_thread(tree_latest_mtime, root)
    await check_library_fresh(db, folder_mtime)

    if job:
        job.stage = "Чтение папки…"
    files = await asyncio.to_thread(walk_audio, root)
    file_paths = {p for p, _ in files}

    entries = [dict(r) for r in await db.fetch_all(
        "SELECT pt.id AS link_id, pt.position, t.id AS track_id, t.path, "
        "t.title, t.artist, t.album, t.duration_ms, t.content_hash, t.missing "
        "FROM playlist_tracks pt JOIN tracks t ON t.id = pt.track_id "
        "WHERE pt.playlist_id = ? ORDER BY pt.position, pt.id",
        (playlist_id,),
    )]

    # Управляем только треками из этой папки. Чужие добавления не трогаем.
    managed = [e for e in entries if e["path"].startswith(root_str + "/")]
    kept_ids = {e["link_id"] for e in managed if e["path"] in file_paths and not e["missing"]}
    gone = [e for e in managed if e["link_id"] not in kept_ids]

    plan = ResyncPlan(playlist_id=playlist_id, folder=root_str, kept=len(kept_ids))
    if job:
        job.total = len(gone) + len(file_paths)
        job.stage = "Поиск перемещённых треков…"

    # Новые файлы: есть в папке, нет в плейлисте.
    in_playlist_paths = {e["path"] for e in entries}
    new_paths = [p for p in sorted(file_paths) if p not in in_playlist_paths]
    new_found = await tracks_by_paths(db, new_paths)
    new_rows = [new_found[p] for p in new_paths if p in new_found and not new_found[p]["missing"]]
    new_rows.sort(key=lambda r: sort_key(r, root_str))
    plan.add = [{"track_id": int(r["id"]), "path": r["path"], "title": r["title"]} for r in new_rows]
    plan.unmatched_files = [p for p in new_paths if p not in new_found or new_found[p]["missing"]]

    if gone:
        # Два батча на всех пропавших, а не запрос на каждый трек.
        hashes = [e["content_hash"] for e in gone if e.get("content_hash")]
        tag_keys = [
            (e["artist"], e["album"], e["title"])
            for e in gone
            if e.get("artist") and e.get("album") and e.get("title")
        ]
        hash_hits = await tracks_by_hashes(db, hashes) if hashes else []
        tag_hits = await tracks_by_tags(db, tag_keys) if tag_keys else []

        hash_index: dict[str, list[dict]] = {}
        for hit in hash_hits:
            hash_index.setdefault(hit["content_hash"], []).append(hit)
        tag_index: dict[tuple, list[dict]] = {}
        for hit in tag_hits:
            tag_index.setdefault((hit["artist"], hit["album"], hit["title"]), []).append(hit)

        taken = {e["track_id"] for e in managed if e["link_id"] in kept_ids}
        for entry in gone:
            same_hash = hash_index.get(entry.get("content_hash") or "", [])
            same_tags = tag_index.get(
                (entry.get("artist"), entry.get("album"), entry.get("title")), []
            )
            status, target, candidates = _pick_candidate(
                entry, same_hash, same_tags, taken,
            )
            base = {
                "link_id": entry["link_id"],
                "position": entry["position"],
                "old_track_id": entry["track_id"],
                "old_path": entry["path"],
                "title": entry["title"],
                "artist": entry["artist"],
                "album": entry["album"],
            }
            if status == "relocate" and target is not None:
                taken.add(target["id"])
                plan.relocate.append({
                    **base,
                    "new_track_id": int(target["id"]),
                    "new_path": target["path"],
                })
            elif status == "remove":
                plan.remove.append(base)
            else:
                plan.ambiguous.append({
                    **base,
                    "candidates": [c["path"] for c in candidates[:10]],
                })
            if job:
                job.done += 1

    # Файл, на который уже переключаем пропавший трек, не добавляем второй раз.
    relocated_targets = {r["new_track_id"] for r in plan.relocate}
    plan.add = [a for a in plan.add if a["track_id"] not in relocated_targets]

    if job:
        job.done = job.total
        job.stage = "Готово"
    return plan


async def apply_resync_plan(
    *,
    db: Database,
    playlists: PlaylistRepository,
    plan: ResyncPlan,
    remove_link_ids: list[int],
) -> dict[str, int]:
    """Применяет план. Удаляются только ``remove_link_ids`` из плана.

    Перемещения и добавления применяются всегда: они не теряют данных.
    Позиции перенумеровываются в конце, чтобы не было дыр от удалённых.
    """
    allowed_remove = {e["link_id"] for e in plan.remove}
    to_remove = [lid for lid in remove_link_ids if lid in allowed_remove]

    async with db.transaction():
        for item in plan.relocate:
            # Позиция не трогаем: перепривязываем ту же строку к новому треку.
            await db.execute(
                "UPDATE playlist_tracks SET track_id = ? "
                "WHERE id = ? AND playlist_id = ?",
                (item["new_track_id"], item["link_id"], plan.playlist_id),
            )
        if to_remove:
            await db.execute_many(
                "DELETE FROM playlist_tracks WHERE id = ? AND playlist_id = ?",
                [(lid, plan.playlist_id) for lid in to_remove],
            )
        if plan.add:
            start = await db.fetch_value(
                "SELECT COALESCE(MAX(position), -1) FROM playlist_tracks "
                "WHERE playlist_id = ?",
                (plan.playlist_id,), default=-1,
            )
            now = utcnow_iso()
            await db.execute_many(
                "INSERT INTO playlist_tracks (playlist_id, track_id, position, added_at) "
                "VALUES (?, ?, ?, ?)",
                [
                    (plan.playlist_id, item["track_id"], int(start) + 1 + i, now)
                    for i, item in enumerate(plan.add)
                ],
            )
        await playlists._renumber(plan.playlist_id)
        await db.execute(
            "UPDATE playlists SET synced_at = ?, updated_at = ? WHERE id = ?",
            (utcnow_iso(), utcnow_iso(), plan.playlist_id),
        )

    return {
        "relocated": len(plan.relocate),
        "removed": len(to_remove),
        "added": len(plan.add),
    }
