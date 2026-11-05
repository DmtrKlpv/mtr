"""Репозиторий медиатеки (mtr_music.db): треки, исполнители, альбомы, сканы.

Здесь реализован главный инвариант переиндексации: трек идентифицируется
ПУТЁМ, а не автоинкрементным id. Повторное сканирование обновляет строку
на месте, поэтому playlist_tracks.track_id остаются валидными, а плейлисты
не рассыпаются при каждом полном проходе.

Исполнители и альбомы дедуплицируются через get-or-create с COLLATE NOCASE
в уникальных индексах, поэтому "The Beatles" и "the beatles" — одна запись.
"""

from __future__ import annotations

import json

import logging
import sqlite3
from dataclasses import dataclass, field

from app.db.database import Database
from app.scanner.tag_parser import TrackMetadata
from app.utils.timeutil import utcnow_iso

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TrackRow:
    """Плоское представление трека для API и внутренних нужд."""

    id: int
    path: str
    filename: str
    title: str
    artist: str | None
    album_artist: str | None
    album: str | None
    album_id: int | None
    artist_id: int | None
    track_number: int | None
    disc_number: int | None
    year: int | None
    genre: str | None
    duration_ms: int
    bitrate: int | None
    sample_rate: int | None
    channels: int | None
    format: str | None
    size_bytes: int
    mtime: float
    has_cover: bool
    missing: bool
    added_at: str
    updated_at: str

    @property
    def duration_sec(self) -> int:
        """Длительность в секундах — формат Subsonic (duration)."""
        return int(round(self.duration_ms / 1000))


@dataclass(slots=True)
class UpsertOutcome:
    """Итог обработки одного файла: что именно произошло."""

    track_id: int
    action: str  # "added" | "updated" | "unchanged"


# Сколько текстов ошибок хранить в истории скана. Полное число всё равно
# пишется в errors_total, поэтому обрезание не искажает счётчик.
MAX_STORED_ERRORS = 500


@dataclass(slots=True, frozen=True)
class ScanError:
    """Одна ошибка разбора: какой файл и почему не взят.

    Интерфейс показывает это таблицей «файл / причина», поэтому причина
    хранится отдельно от пути — склеенную строку пришлось бы разбирать
    обратно, а текст причины может сам содержать двоеточия.
    """

    path: str
    reason: str

    def as_text(self) -> str:
        return f"{self.path}: {self.reason}"

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path, "reason": self.reason}


@dataclass(slots=True)
class ScanCounters:
    """Счётчики прохода сканера."""

    seen: int = 0
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0
    failed: int = 0
    errors: list[ScanError] = field(default_factory=list)

    def add_error(self, path: str, reason: str) -> None:
        """Регистрирует ошибку файла.

        Полный список держим в памяти ограниченным: при массовом сбое (например,
        отвалился сетевой том) счётчик ошибок уходит в десятки тысяч, и хранить
        их все до конца прохода незачем — в историю всё равно попадает срез.
        """
        self.failed += 1
        if len(self.errors) < MAX_STORED_ERRORS:
            self.errors.append(ScanError(path=path, reason=reason))


def _row_to_track(row: sqlite3.Row) -> TrackRow:
    return TrackRow(
        id=int(row["id"]),
        path=str(row["path"]),
        filename=str(row["filename"]),
        title=str(row["title"]),
        artist=row["artist"],
        album_artist=row["album_artist"],
        album=row["album"],
        album_id=row["album_id"],
        artist_id=row["artist_id"],
        track_number=row["track_number"],
        disc_number=row["disc_number"],
        year=row["year"],
        genre=row["genre"],
        duration_ms=int(row["duration_ms"] or 0),
        bitrate=row["bitrate"],
        sample_rate=row["sample_rate"],
        channels=row["channels"],
        format=row["format"],
        size_bytes=int(row["size_bytes"] or 0),
        mtime=float(row["mtime"] or 0.0),
        has_cover=bool(row["has_cover"]),
        missing=bool(row["missing"]),
        added_at=str(row["added_at"]),
        updated_at=str(row["updated_at"]),
    )


class TrackRepository:
    """CRUD треков и справочников исполнителей/альбомов."""

    def __init__(self, db: Database) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Справочники
    # ------------------------------------------------------------------
    async def get_or_create_artist(self, name: str) -> int | None:
        """Возвращает id исполнителя, создавая его при необходимости."""
        from app.scanner.tag_parser import clean_text

        cleaned = clean_text(name)
        if not cleaned:
            return None

        row = await self._db.fetch_one(
            "SELECT id FROM artists WHERE name = ? COLLATE NOCASE", (cleaned,)
        )
        if row is not None:
            return int(row["id"])

        now = utcnow_iso()
        try:
            return await self._db.execute(
                "INSERT INTO artists (name, sort_name, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (cleaned, cleaned, now, now),
            )
        except Exception:
            # Гонка: другой корутин вставил того же исполнителя между
            # SELECT и INSERT. Уникальный индекс спас целостность —
            # достаточно перечитать.
            row = await self._db.fetch_one(
                "SELECT id FROM artists WHERE name = ? COLLATE NOCASE", (cleaned,)
            )
            if row is None:
                raise
            return int(row["id"])

    async def get_or_create_album(
        self, name: str, artist_id: int | None, *, year: int | None = None,
        genre: str | None = None,
    ) -> int | None:
        """Возвращает id альбома в рамках исполнителя."""
        from app.scanner.tag_parser import clean_text

        cleaned = clean_text(name)
        if not cleaned:
            return None

        row = await self._db.fetch_one(
            "SELECT id FROM albums WHERE name = ? COLLATE NOCASE AND "
            "((artist_id IS NULL AND ? IS NULL) OR artist_id = ?)",
            (cleaned, artist_id, artist_id),
        )
        if row is not None:
            return int(row["id"])

        now = utcnow_iso()
        try:
            return await self._db.execute(
                "INSERT INTO albums (name, artist_id, year, genre, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (cleaned, artist_id, year, clean_text(genre), now, now),
            )
        except Exception:
            row = await self._db.fetch_one(
                "SELECT id FROM albums WHERE name = ? COLLATE NOCASE AND "
                "((artist_id IS NULL AND ? IS NULL) OR artist_id = ?)",
                (cleaned, artist_id, artist_id),
            )
            if row is None:
                raise
            return int(row["id"])

    # ------------------------------------------------------------------
    # Чтение треков
    # ------------------------------------------------------------------
    async def get_by_id(self, track_id: int) -> TrackRow | None:
        row = await self._db.fetch_one("SELECT * FROM tracks WHERE id = ?", (track_id,))
        return _row_to_track(row) if row else None

    async def get_by_path(self, path: str) -> TrackRow | None:
        row = await self._db.fetch_one("SELECT * FROM tracks WHERE path = ?", (path,))
        return _row_to_track(row) if row else None

    async def list_tracks(
        self,
        *,
        limit: int = 500,
        offset: int = 0,
        include_missing: bool = False,
        artist: str | None = None,
        album: str | None = None,
        genre: str | None = None,
    ) -> list[TrackRow]:
        """Постраничная выдача с необязательными фильтрами."""
        # LIMIT/OFFSET санитизируем здесь же: они попадают в SQL как int.
        limit = max(1, min(int(limit), 5000))
        offset = max(0, int(offset))

        clauses: list[str] = []
        params: list[object] = []

        if not include_missing:
            clauses.append("missing = 0")
        if artist:
            clauses.append("artist = ? COLLATE NOCASE")
            params.append(artist)
        if album:
            clauses.append("album = ? COLLATE NOCASE")
            params.append(album)
        if genre:
            clauses.append("genre = ? COLLATE NOCASE")
            params.append(genre)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            f"SELECT * FROM tracks {where} "
            "ORDER BY artist COLLATE NOCASE, album COLLATE NOCASE, "
            "disc_number, track_number, title COLLATE NOCASE "
            "LIMIT ? OFFSET ?"
        )
        rows = await self._db.fetch_all(sql, (*params, limit, offset))
        return [_row_to_track(row) for row in rows]

    async def count_tracks(self, *, include_missing: bool = False) -> int:
        where = "" if include_missing else "WHERE missing = 0"
        return int(
            await self._db.fetch_value(f"SELECT COUNT(*) FROM tracks {where}", default=0)
            or 0
        )

    async def search(self, query: str, *, limit: int = 50) -> list[TrackRow]:
        """Поиск по названию/артисту/альбому. LIKE с ESCAPE от инъекций."""
        cleaned = (query or "").strip()
        if not cleaned:
            return []
        limit = max(1, min(int(limit), 500))
        # Экранируем и обратную косую: без неё запрос с "\\" ломает шаблон LIKE
        # (ESCAPE '\\' съедает следующий символ). Порядок замен важен: сначала "\\".
        escaped = cleaned.replace("\\", r"\\").replace("%", r"\%").replace("_", r"\_")
        like = f"%{escaped}%"
        rows = await self._db.fetch_all(
            """
            SELECT * FROM tracks
             WHERE missing = 0
               AND (title  LIKE ? ESCAPE '\\'
                 OR artist LIKE ? ESCAPE '\\'
                 OR album  LIKE ? ESCAPE '\\')
             ORDER BY title COLLATE NOCASE
             LIMIT ?
            """,
            (like, like, like, limit),
        )
        return [_row_to_track(row) for row in rows]

    async def known_paths(self) -> dict[str, tuple[int, int, float, bool]]:
        """Карта path -> (id, size_bytes, mtime, missing).

        Флаг missing нужен сканеру: файл мог вернуться на место с теми же
        size/mtime, и тогда diff по метаданным его не заметит — такой трек
        обязан попасть в очередь разбора, иначе он навсегда останется
        помеченным как отсутствующий.
        """
        rows = await self._db.fetch_all(
            "SELECT id, path, size_bytes, mtime, missing FROM tracks"
        )
        return {
            str(row["path"]): (
                int(row["id"]),
                int(row["size_bytes"] or 0),
                float(row["mtime"] or 0.0),
                bool(row["missing"]),
            )
            for row in rows
        }

    # ------------------------------------------------------------------
    # Запись
    # ------------------------------------------------------------------
    def batch(self):
        """Общая транзакция для пачки записей (см. library_scanner._parse_batch)."""
        return self._db.transaction()

    async def upsert_from_metadata(self, meta: TrackMetadata) -> UpsertOutcome:
        """Вставляет или обновляет трек по пути.

        Логика:
          * нет строки с таким path -> INSERT, вернуть "added";
          * есть, но (size, mtime) совпадают -> "unchanged", ничего не пишем;
          * есть и файл изменился -> UPDATE метаданных, снять missing.

        Всё внутри одной транзакции, включая разрешение artist/album,
        чтобы при сбое не осталось «висячего» исполнителя без трека.
        """
        existing = await self.get_by_path(meta.path)

        async with self._db.transaction():
            artist_id = await self.get_or_create_artist(meta.artist or "")
            album_artist_id = await self.get_or_create_artist(meta.album_artist or "")
            # Альбом группируем по album_artist, если он есть, иначе по artist.
            album_owner = album_artist_id or artist_id
            album_id = await self.get_or_create_album(
                meta.album or "", album_owner, year=meta.year, genre=meta.genre
            )

            now = utcnow_iso()

            if existing is None:
                track_id = await self._db.execute(
                    """
                    INSERT INTO tracks (
                        path, filename, title, artist, artist_id, album_artist,
                        album, album_id, track_number, disc_number, year, genre,
                        duration_ms, bitrate, sample_rate, channels, format,
                        size_bytes, mtime, has_cover, missing,
                        added_at, updated_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, 0, ?, ?
                    )
                    """,
                    (
                        meta.path,
                        meta.filename,
                        meta.title,
                        meta.artist,
                        artist_id,
                        meta.album_artist,
                        meta.album,
                        album_id,
                        meta.track_number,
                        meta.disc_number,
                        meta.year,
                        meta.genre,
                        meta.duration_ms,
                        meta.bitrate,
                        meta.sample_rate,
                        meta.channels,
                        meta.format,
                        meta.size_bytes,
                        meta.mtime,
                        1 if meta.has_cover else 0,
                        now,
                        now,
                    ),
                )
                # Создаём строку статистики сразу: смарт-плейлисты делают
                # JOIN play_stats и не должны терять свежие треки.
                await self._db.execute(
                    "INSERT OR IGNORE INTO play_stats (track_id, play_count, updated_at) "
                    "VALUES (?, 0, ?)",
                    (track_id, now),
                )
                return UpsertOutcome(track_id=track_id, action="added")

            if (
                existing.size_bytes == meta.size_bytes
                and abs(existing.mtime - meta.mtime) < 1e-6
                and not existing.missing
            ):
                return UpsertOutcome(track_id=existing.id, action="unchanged")

            await self._db.execute(
                """
                UPDATE tracks SET
                    filename = ?, title = ?, artist = ?, artist_id = ?,
                    album_artist = ?, album = ?, album_id = ?,
                    track_number = ?, disc_number = ?, year = ?, genre = ?,
                    duration_ms = ?, bitrate = ?, sample_rate = ?, channels = ?,
                    format = ?, size_bytes = ?, mtime = ?, has_cover = ?,
                    missing = 0, updated_at = ?
                 WHERE id = ?
                """,
                (
                    meta.filename,
                    meta.title,
                    meta.artist,
                    artist_id,
                    meta.album_artist,
                    meta.album,
                    album_id,
                    meta.track_number,
                    meta.disc_number,
                    meta.year,
                    meta.genre,
                    meta.duration_ms,
                    meta.bitrate,
                    meta.sample_rate,
                    meta.channels,
                    meta.format,
                    meta.size_bytes,
                    meta.mtime,
                    1 if meta.has_cover else 0,
                    now,
                    existing.id,
                ),
            )
            return UpsertOutcome(track_id=existing.id, action="updated")

    async def mark_missing(self, paths: list[str]) -> int:
        """Помечает треки как отсутствующие, НЕ удаляя их.

        Удаление строки разорвало бы плейлисты и историю прослушиваний.
        Флаг missing=1 убирает трек из выдачи, но сохраняет ссылки.
        Если файл вернётся, upsert снимет флаг автоматически.
        """
        if not paths:
            return 0
        now = utcnow_iso()
        # Пакетами по 500: SQLite ограничивает число плейсхолдеров.
        total = 0
        chunk = 500
        for start in range(0, len(paths), chunk):
            batch = paths[start : start + chunk]
            placeholders = ",".join("?" for _ in batch)
            affected = await self._db.execute_many(
                f"UPDATE tracks SET missing = 1, updated_at = ? "
                f"WHERE path IN ({placeholders}) AND missing = 0",
                [(now, *batch)],
            )
            total += affected
        return total

    async def purge_missing(self) -> int:
        """Удаляет треки, помеченные missing.

        Опасная операция: чистит playlist_tracks по каскаду. Вызывается
        только вручную из настроек, причём сервис обязан предупредить,
        что затронутые плейлисты потеряют эти позиции. Возвращает, сколько
        строк удалено.
        """
        count = int(
            await self._db.fetch_value(
                "SELECT COUNT(*) FROM tracks WHERE missing = 1", default=0
            )
            or 0
        )
        if count == 0:
            return 0
        await self._db.execute("DELETE FROM tracks WHERE missing = 1")
        logger.warning("удалено %d отсутствующих треков вместе с позициями плейлистов", count)
        return count

    # ------------------------------------------------------------------
    # Сканы
    # ------------------------------------------------------------------
    async def start_scan_run(self, mode: str) -> int:
        """Регистрирует начало сканирования и возвращает его id."""
        if mode not in ("full", "incremental", "watch"):
            raise ValueError(f"недопустимый режим сканирования: {mode!r}")
        return await self._db.execute(
            "INSERT INTO scan_runs (mode, status, started_at) VALUES (?, 'running', ?)",
            (mode, utcnow_iso()),
        )

    async def finish_scan_run(
        self,
        run_id: int,
        *,
        status: str,
        counters: ScanCounters,
        error: str | None = None,
    ) -> None:
        """Закрывает запись скана итогами."""
        if status not in ("completed", "failed", "cancelled"):
            raise ValueError(f"недопустимый статус скана: {status!r}")
        # Тексты ошибок могут быть тысячами: в историю кладём первые MAX_STORED_ERRORS,
        # а полное число храним отдельно, чтобы интерфейс не врал.
        stored = [error.as_dict() for error in counters.errors[:MAX_STORED_ERRORS]]
        errors_json = json.dumps(stored, ensure_ascii=False) if stored else None
        await self._db.execute(
            """
            UPDATE scan_runs SET
                status = ?, finished_at = ?, files_seen = ?, files_added = ?,
                files_updated = ?, files_removed = ?, files_failed = ?, error = ?,
                errors_total = ?, errors_json = ?
             WHERE id = ?
            """,
            (
                status,
                utcnow_iso(),
                counters.seen,
                counters.added,
                counters.updated,
                counters.removed,
                counters.failed,
                error,
                len(counters.errors),
                errors_json,
                run_id,
            ),
        )

    async def close_stale_runs(self) -> int:
        """Закрывает прогоны, оставшиеся в статусе running после падения.

        Если контейнер убили во время скана, запись в scan_runs остаётся
        running навсегда. Интерфейс по ней решает, что скан идёт, и показывает
        прогресс, которого нет. Закрываем такие записи при старте.
        """
        now = utcnow_iso()
        return await self._db.execute(
            "UPDATE scan_runs SET status = 'failed', finished_at = ?, "
            "error = COALESCE(error, 'прервано: сервер остановлен во время скана') "
            "WHERE status = 'running'",
            (now,),
        )

    async def library_stats(self) -> dict[str, int]:
        """Сводка по библиотеке для дашборда и Subsonic getScanStatus."""
        row = await self._db.fetch_one(
            """
            SELECT
                (SELECT COUNT(*) FROM tracks WHERE missing = 0)      AS tracks,
                (SELECT COUNT(*) FROM tracks WHERE missing = 1)      AS missing,
                (SELECT COUNT(*) FROM artists)                       AS artists,
                (SELECT COUNT(*) FROM albums)                        AS albums,
                (SELECT COUNT(*) FROM playlists)                     AS playlists,
                (SELECT COALESCE(SUM(duration_ms), 0) FROM tracks
                  WHERE missing = 0)                                  AS total_ms
            """
        )
        if row is None:
            return {"tracks": 0, "missing": 0, "artists": 0, "albums": 0,
                    "playlists": 0, "total_ms": 0}
        return {
            "tracks": int(row["tracks"] or 0),
            "missing": int(row["missing"] or 0),
            "artists": int(row["artists"] or 0),
            "albums": int(row["albums"] or 0),
            "playlists": int(row["playlists"] or 0),
            "total_ms": int(row["total_ms"] or 0),
        }

    # ------------------------------------------------------------------
    # Поиск для импорта плейлистов
    # ------------------------------------------------------------------
    async def find_by_path(self, path: str) -> TrackRow | None:
        """Ищет трек по точному пути. Отсутствующие файлы не возвращаем."""
        row = await self._db.fetch_one(
            "SELECT * FROM tracks WHERE path = ? AND missing = 0", (path,)
        )
        return None if row is None else _row_to_track(row)

    async def all_live_for_index(self) -> list[dict[str, object]]:
        """Все живые треки минимальным набором полей — для индекса сопоставления.

        Нужен построителю `app.playlists.matching.TrackIndex`: сравнение по
        базовому названию и нормализованному имени файла делается в памяти,
        иначе на 38 000 треков это были бы десятки тысяч запросов.
        """
        rows = await self._db.fetch_all(
            "SELECT id, path, filename, title, artist, album, album_id, "
            "duration_ms, missing FROM tracks WHERE missing = 0"
        )
        return [dict(row) for row in rows]

    async def find_by_filename(self, filename: str, *, limit: int = 5) -> list[TrackRow]:
        """Треки с таким именем файла.

        Сравнение без учёта регистра: плейлисты приходят с Windows, где
        `Track.MP3` и `track.mp3` — один файл. COLLATE NOCASE в SQLite
        работает только для ASCII, поэтому используем lower().
        """
        rows = await self._db.fetch_all(
            "SELECT * FROM tracks WHERE lower(filename) = lower(?) "
            "AND missing = 0 ORDER BY path LIMIT ?",
            (filename, max(1, min(int(limit), 50))),
        )
        return [_row_to_track(row) for row in rows]

    async def find_by_title(
        self, title: str, *, artist: str | None = None, limit: int = 5
    ) -> list[TrackRow]:
        """Треки по названию и (необязательно) исполнителю — запасной вариант."""
        params: list[object] = [title.strip().lower()]
        sql = "SELECT * FROM tracks WHERE lower(title) = ? AND missing = 0"
        if artist:
            sql += " AND lower(COALESCE(artist, '')) = ?"
            params.append(artist.strip().lower())
        sql += " ORDER BY path LIMIT ?"
        params.append(max(1, min(int(limit), 50)))
        rows = await self._db.fetch_all(sql, tuple(params))
        return [_row_to_track(row) for row in rows]

    # ------------------------------------------------------------------
    # История прослушиваний (главная страница)
    # ------------------------------------------------------------------
    async def recently_played(
        self, user_id: int, *, limit: int = 20
    ) -> list[TrackRow]:
        """Последние прослушанные треки пользователя, без повторов.

        `playback_log` хранит каждое прослушивание отдельной строкой, поэтому
        один и тот же трек мог бы встретиться несколько раз подряд. Группируем
        по track_id и берём максимальное время: в списке «недавно
        прослушанные» трек должен стоять один раз, на позиции последнего
        прослушивания.
        """
        # Порядок — по id записи журнала, а не по played_at: время хранится
        # с точностью до секунды, и два прослушивания подряд получают
        # одинаковую метку. Тогда «последнее» становится неопределённым,
        # а id растёт монотонно и задаёт порядок однозначно.
        rows = await self._db.fetch_all(
            "SELECT t.*, MAX(pb.id) AS last_id, MAX(pb.played_at) AS last_played "
            "FROM tracks t JOIN playback_log pb ON pb.track_id = t.id "
            "WHERE pb.user_id = ? AND t.missing = 0 "
            "GROUP BY t.id ORDER BY last_id DESC LIMIT ?",
            (user_id, max(1, min(int(limit), 100))),
        )
        return [_row_to_track(row) for row in rows]

    async def recently_added(
        self, *, limit: int = 20
    ) -> list[TrackRow]:
        """Последние добавленные в библиотеку треки — по added_at."""
        rows = await self._db.fetch_all(
            "SELECT * FROM tracks WHERE missing = 0 "
            "ORDER BY added_at DESC, id DESC LIMIT ?",
            (max(1, min(int(limit), 100)),),
        )
        return [_row_to_track(row) for row in rows]

    async def record_play(
        self,
        track_id: int,
        user_id: int,
        *,
        client: str = "web",
        played_ms: int = 0,
    ) -> None:
        """Записывает прослушивание: журнал + агрегат.

        Две таблицы, потому что задачи разные: `playback_log` нужен для
        «недавно прослушанных» (там важна история и пользователь), а
        `play_stats` — для счётчика проигрываний и сортировки «популярные».
        Считать COUNT(*) по журналу каждый раз дороже.
        """
        played_at = utcnow_iso()
        async with self._db.transaction():
            await self._db.execute(
                "INSERT INTO playback_log "
                "(track_id, user_id, played_at, client, played_ms) "
                "VALUES (?, ?, ?, ?, ?)",
                (track_id, user_id, played_at, client, played_ms),
            )
            await self._db.execute(
                "INSERT INTO play_stats (track_id, play_count, last_played_at, updated_at) "
                "VALUES (?, 1, ?, ?) "
                "ON CONFLICT (track_id) DO UPDATE SET "
                "play_count = play_count + 1, "
                "last_played_at = excluded.last_played_at, "
                "updated_at = excluded.updated_at",
                (track_id, played_at, played_at),
            )


async def _optimize(self) -> None:  # pragma: no cover - тонкая обёртка над Database
    await self._db.optimize()


TrackRepository.optimize = _optimize
