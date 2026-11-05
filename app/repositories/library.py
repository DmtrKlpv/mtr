"""Репозиторий альбомов и исполнителей (mtr_music.db).

JOIN между albums/artists/tracks здесь полностью законен: все три таблицы
живут в одном файле. Запрет на JOIN касается только пары mtr_users.db и
mtr_music.db — это разные SQLite-базы, и связывать их можно лишь в Python.

Все запросы фильтруют missing = 0: трек, чей файл исчез, не должен
попадать в выдачу, но строка в БД остаётся ради плейлистов.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from app.db.database import Database
from app.repositories.tracks import TrackRow, _row_to_track

logger = logging.getLogger(__name__)

# Допустимые порядки сортировки для getAlbumList2.
# Список альбомов: агрегаты по трекам считаются один раз (GROUP BY), а не подзапросом
# на каждый альбом. Старый вариант на 1200 альбомах занимал ~5.5 с, на большой базе —
# минуты, и страница «Альбомы» не открывалась. Порядок колонок и алиасов тот же.
_ALBUM_LIST_SELECT = """
WITH live AS (
    SELECT album_id,
           COUNT(*) AS song_count,
           COALESCE(SUM(duration_ms), 0) AS duration_ms
      FROM tracks
     WHERE missing = 0
     GROUP BY album_id
),
played AS (
    SELECT t.album_id, SUM(ps.play_count) AS plays
      FROM tracks t JOIN play_stats ps ON ps.track_id = t.id
     GROUP BY t.album_id
),
last_played AS (
    SELECT t.album_id, MAX(pb.played_at) AS last_play
      FROM tracks t JOIN playback_log pb ON pb.track_id = t.id
     GROUP BY t.album_id
)
SELECT
    a.id, a.name, a.artist_id, a.year, a.genre, a.created_at,
    ar.name AS artist_name,
    l.song_count AS song_count,
    l.duration_ms AS duration_ms,
    COALESCE(p.plays, 0) AS plays,
    lp.last_play AS last_play
  FROM albums a
  JOIN live l ON l.album_id = a.id
  JOIN artists ar ON ar.id = a.artist_id
  LEFT JOIN played p ON p.album_id = a.id
  LEFT JOIN last_played lp ON lp.album_id = a.id
"""

ALBUM_SORTS: dict[str, str] = {
    "newest": "a.created_at DESC, a.id DESC",
    "recent": "COALESCE(last_play, '') DESC, a.id DESC",
    "frequent": "COALESCE(plays, 0) DESC, a.id DESC",
    "random": "RANDOM()",
    "alphabeticalbyname": "a.name COLLATE NOCASE ASC",
    "alphabeticalbyartist": "ar.name COLLATE NOCASE ASC, a.year, a.name COLLATE NOCASE",
    "byyear": "a.year DESC, a.name COLLATE NOCASE",
    "bygenre": "a.genre COLLATE NOCASE, a.name COLLATE NOCASE",
}

# Общая часть выборки альбома: счётчики и агрегаты считаются подзапросами,
# потому что JOIN с tracks размножил бы строки при подсчёте.
_ALBUM_SELECT = """
SELECT
    a.id, a.name, a.artist_id, a.year, a.genre, a.created_at,
    ar.name AS artist_name,
    (SELECT COUNT(*) FROM tracks t WHERE t.album_id = a.id AND t.missing = 0)
        AS song_count,
    (SELECT COALESCE(SUM(t.duration_ms), 0) FROM tracks t
      WHERE t.album_id = a.id AND t.missing = 0) AS duration_ms,
    (SELECT COALESCE(SUM(ps.play_count), 0)
       FROM tracks t JOIN play_stats ps ON ps.track_id = t.id
      WHERE t.album_id = a.id) AS plays,
    (SELECT MAX(pb.played_at)
       FROM tracks t JOIN playback_log pb ON pb.track_id = t.id
      WHERE t.album_id = a.id) AS last_play
  FROM albums a
  LEFT JOIN artists ar ON ar.id = a.artist_id
"""


@dataclass(slots=True)
class AlbumRow:
    """Альбом с агрегатами для Subsonic-ответа."""

    id: int
    name: str
    artist_id: int | None
    artist_name: str | None
    year: int | None
    genre: str | None
    created_at: str
    song_count: int
    duration_ms: int
    play_count: int = 0

    @property
    def duration_sec(self) -> int:
        return int(round(self.duration_ms / 1000))


@dataclass(slots=True)
class ArtistRow:
    """Исполнитель с числом альбомов."""

    id: int
    name: str
    album_count: int

    @property
    def sort_letter(self) -> str:
        """Первая буква для группировки в getArtists.

        Не-латинские имена (кириллица, CJK) попадают в «#»: раскладывать их
        по Unicode-блокам бессмысленно для навигации, а клиенты ожидают
        именно однобуквенный индекс.
        """
        if not self.name:
            return "#"
        first = self.name.lstrip()[0].upper()
        return first if "A" <= first <= "Z" else "#"


def _row_to_album(row: sqlite3.Row) -> AlbumRow:
    return AlbumRow(
        id=int(row["id"]),
        name=str(row["name"]),
        artist_id=row["artist_id"],
        artist_name=row["artist_name"],
        year=row["year"],
        genre=row["genre"],
        created_at=str(row["created_at"]),
        song_count=int(row["song_count"] or 0),
        duration_ms=int(row["duration_ms"] or 0),
        play_count=int(row["plays"] or 0),
    )


# Связи «исполнитель — альбом» для разделов исполнителей.
#
# Исполнитель попадает в список, в поиск и в getArtist в двух случаях:
#   1) он владелец альбома (albums.artist_id);
#   2) у него есть живой трек (tracks.artist_id) — гость на сборнике,
#      фит, трек без альбома.
# Раньше учитывался только первый случай, и гости сборников, фиты и одиночные
# треки не были видны вообще: их не находил поиск и не показывал список.
_LIVE_LINKS = """
WITH live_links AS (
    SELECT t.artist_id AS artist_id, t.album_id AS album_id
      FROM tracks t
     WHERE t.missing = 0 AND t.artist_id IS NOT NULL
    UNION
    SELECT a.artist_id AS artist_id, a.id AS album_id
      FROM albums a
      JOIN tracks t ON t.album_id = a.id AND t.missing = 0
     WHERE a.artist_id IS NOT NULL
)
"""

class LibraryRepository:
    """Чтение альбомов, исполнителей и жанров."""

    def __init__(self, db: Database) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Альбомы
    # ------------------------------------------------------------------
    async def list_albums(
        self,
        *,
        sort: str = "alphabeticalByArtist",
        limit: int = 10,
        offset: int = 0,
        genre: str | None = None,
        from_year: int | None = None,
        to_year: int | None = None,
    ) -> list[AlbumRow]:
        """Список альбомов по типу сортировки из getAlbumList2."""
        order = ALBUM_SORTS.get(sort.lower())
        if order is None:
            order = ALBUM_SORTS["alphabeticalbyartist"]

        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))

        clauses = [
            "(SELECT COUNT(*) FROM tracks t WHERE t.album_id = a.id "
            "AND t.missing = 0) > 0"
        ]
        params: list[object] = []
        if genre:
            clauses.append("a.genre = ? COLLATE NOCASE")
            params.append(genre)
        if from_year is not None:
            clauses.append("a.year >= ?")
            params.append(int(from_year))
        if to_year is not None:
            clauses.append("a.year <= ?")
            params.append(int(to_year))

        # Фильтры по альбому: live-треки уже в JOIN live, поэтому здесь только атрибуты альбома.
        attr_clauses = [c for c in clauses if not c.startswith("(SELECT COUNT")]
        where = f" WHERE {' AND '.join(attr_clauses)}" if attr_clauses else ""
        sql = (
            f"{_ALBUM_LIST_SELECT}{where} "
            f"ORDER BY {order} LIMIT ? OFFSET ?"
        )
        rows = await self._db.fetch_all(sql, (*params, limit, offset))
        return [_row_to_album(row) for row in rows]

    async def first_album_id(self, artist_id: int) -> int | None:
        """id первого непустого альбома исполнителя — для аватара.

        Обложек у исполнителей в схеме нет, поэтому берём обложку альбома.
        Пустые альбомы (без треков) пропускаем: у них нет и картинки.
        """
        row = await self._db.fetch_one(
            "SELECT a.id FROM albums a WHERE a.artist_id = ? "
            "AND (SELECT COUNT(*) FROM tracks t "
            "     WHERE t.album_id = a.id AND t.missing = 0) > 0 "
            "ORDER BY a.year, a.name COLLATE NOCASE LIMIT 1",
            (artist_id,),
        )
        return None if row is None else int(row["id"])

    async def get_album(self, album_id: int) -> AlbumRow | None:
        row = await self._db.fetch_one(f"{_ALBUM_SELECT} WHERE a.id = ?", (album_id,))
        return _row_to_album(row) if row else None

    async def album_tracks(self, album_id: int) -> list[TrackRow]:
        """Треки альбома в порядке диска и номера."""
        rows = await self._db.fetch_all(
            "SELECT * FROM tracks WHERE album_id = ? AND missing = 0 "
            "ORDER BY disc_number, track_number, title COLLATE NOCASE",
            (album_id,),
        )
        return [_row_to_track(row) for row in rows]

    async def count_albums(self) -> int:
        # Альбомы с хотя бы одним живым треком: один проход по tracks, без подзапроса на альбом.
        return int(
            await self._db.fetch_value(
                "SELECT COUNT(DISTINCT album_id) FROM tracks "
                "WHERE missing = 0 AND album_id IS NOT NULL",
                default=0,
            )
            or 0
        )

    # ------------------------------------------------------------------
    # Исполнители
    # ------------------------------------------------------------------
    async def list_artists(
        self, *, limit: int | None = None, offset: int = 0
    ) -> list[ArtistRow]:
        """Исполнители с хотя бы одним живым треком: владельцы и гости.

        Страницы идут через LIMIT/OFFSET; агрегаты считаются одним проходом.
        """
        sql = (
            _LIVE_LINKS
            + """
            SELECT ar.id, ar.name,
                   COUNT(DISTINCT ll.album_id) AS album_count
              FROM live_links ll
              JOIN artists ar ON ar.id = ll.artist_id
             GROUP BY ar.id, ar.name
             ORDER BY ar.name COLLATE NOCASE
            """
        )
        params: list[object] = []
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params = [max(1, min(int(limit), 500)), max(0, int(offset))]
        rows = await self._db.fetch_all(sql, tuple(params))
        return [
            ArtistRow(
                id=int(row["id"]),
                name=str(row["name"]),
                album_count=int(row["album_count"] or 0),
            )
            for row in rows
        ]

    async def count_artists(self) -> int:
        """Исполнители с хотя бы одним живым треком — для пагинации списка."""
        return int(
            await self._db.fetch_value(
                _LIVE_LINKS
                + "SELECT COUNT(DISTINCT artist_id) FROM live_links"
                " WHERE artist_id IS NOT NULL",
                default=0,
            )
            or 0
        )

    async def artist_track_counts(self, artist_ids: list[int]) -> dict[int, int]:
        """Число живых треков каждого исполнителя (включая гостевые) — одним запросом."""
        if not artist_ids:
            return {}
        placeholders = ",".join("?" for _ in artist_ids)
        rows = await self._db.fetch_all(
            f"""
            SELECT t.artist_id AS artist_id, COUNT(*) AS n
              FROM tracks t
             WHERE t.missing = 0 AND t.artist_id IN ({placeholders})
             GROUP BY t.artist_id
            """,
            tuple(artist_ids),
        )
        return {int(row["artist_id"]): int(row["n"]) for row in rows}

    async def artist_album_map(self, artist_ids: list[int]) -> dict[int, int]:
        """id первого непустого альбома для каждого исполнителя — одним запросом.

        Раньше на каждого исполнителя делался отдельный запрос (first_album_id),
        и на 5000 исполнителей это были 5000 обращений в SQLite подряд.
        """
        if not artist_ids:
            return {}
        placeholders = ",".join("?" for _ in artist_ids)
        rows = await self._db.fetch_all(
            f"""
            SELECT a.artist_id AS artist_id, MIN(a.id) AS album_id
              FROM albums a
             WHERE a.artist_id IN ({placeholders})
               AND EXISTS (SELECT 1 FROM tracks t
                            WHERE t.album_id = a.id AND t.missing = 0)
             GROUP BY a.artist_id
            """,
            tuple(artist_ids),
        )
        return {
            int(row["artist_id"]): int(row["album_id"])
            for row in rows
            if row["album_id"] is not None
        }

    async def get_artist(self, artist_id: int) -> ArtistRow | None:
        row = await self._db.fetch_one(
            _LIVE_LINKS
            + """
            SELECT ar.id, ar.name,
                   (SELECT COUNT(DISTINCT ll.album_id) FROM live_links ll
                     WHERE ll.artist_id = ar.id) AS album_count
              FROM artists ar WHERE ar.id = ?
            """,
            (artist_id,),
        )
        if row is None:
            return None
        return ArtistRow(
            id=int(row["id"]),
            name=str(row["name"]),
            album_count=int(row["album_count"] or 0),
        )

    async def artist_albums(self, artist_id: int) -> list[AlbumRow]:
        # Альбомы исполнителя: где он владелец или есть его трек (гость, фит).
        rows = await self._db.fetch_all(
            _LIVE_LINKS
            + f"{_ALBUM_SELECT} WHERE a.id IN ("
            "  SELECT album_id FROM live_links"
            "   WHERE artist_id = ? AND album_id IS NOT NULL"
            ") ORDER BY a.year, a.name COLLATE NOCASE",
            (artist_id,),
        )
        return [_row_to_album(row) for row in rows]

    # ------------------------------------------------------------------
    # Жанры и поиск
    # ------------------------------------------------------------------
    async def list_genres(self) -> list[tuple[str, int, int]]:
        """(жанр, число треков, число альбомов) — для getGenres."""
        rows = await self._db.fetch_all(
            """
            SELECT genre,
                   COUNT(*) AS song_count,
                   COUNT(DISTINCT album_id) AS album_count
              FROM tracks
             WHERE missing = 0 AND genre IS NOT NULL AND TRIM(genre) <> ''
             GROUP BY genre COLLATE NOCASE
             ORDER BY genre COLLATE NOCASE
            """
        )
        return [
            (str(row["genre"]), int(row["song_count"]), int(row["album_count"]))
            for row in rows
        ]

    async def search_albums(self, query: str, *, limit: int = 20) -> list[AlbumRow]:
        like = _like(query)
        if like is None:
            return []
        # Ищем по названию альбома И по имени исполнителя: запрос "Queen"
        # должен находить альбомы Queen, даже если слово не в названии.
        #
        # Порядок важен для скорости: сначала отбираем подходящие альбомы по LIKE
        # (их единицы), и только потом считаем агрегаты по трекам — один раз
        # GROUP BY, а не подзапросом на каждый альбом. Прежний вариант на
        # 8000 альбомах и 120000 треках занимал 3.2 с и выглядел как зависание.
        limit = max(1, min(int(limit), 200))
        rows = await self._db.fetch_all(
            """
            WITH matched AS (
                SELECT a.id, a.name
                  FROM albums a
                  JOIN artists ar ON ar.id = a.artist_id
                 WHERE (a.name  LIKE ? ESCAPE '\\'
                     OR ar.name LIKE ? ESCAPE '\\')
                 ORDER BY a.name COLLATE NOCASE
                 LIMIT ?
            ),
            live AS (
                SELECT album_id,
                       COUNT(*) AS song_count,
                       COALESCE(SUM(duration_ms), 0) AS duration_ms
                  FROM tracks
                 WHERE missing = 0
                 GROUP BY album_id
            )
            SELECT a.id, a.name, a.artist_id, a.year, a.genre, a.created_at,
                   ar.name AS artist_name,
                   live.song_count AS song_count,
                   live.duration_ms AS duration_ms,
                   0 AS plays,
                   NULL AS last_play
              FROM matched m
              JOIN albums a ON a.id = m.id
              JOIN artists ar ON ar.id = a.artist_id
              -- JOIN, а не LEFT JOIN: альбом без живых треков в поиск не попадает
              JOIN live ON live.album_id = a.id
             ORDER BY a.name COLLATE NOCASE
            """,
            (like, like, limit),
        )
        return [_row_to_album(row) for row in rows]

    async def search_artists(self, query: str, *, limit: int = 20) -> list[ArtistRow]:
        like = _like(query)
        if like is None:
            return []
        limit = max(1, min(int(limit), 200))
        # Сначала отбор по имени среди исполнителей с живыми треками, затем агрегат.
        rows = await self._db.fetch_all(
            _LIVE_LINKS
            + """
            , matched AS (
                SELECT ar.id, ar.name
                  FROM artists ar
                 WHERE ar.name LIKE ? ESCAPE '\\'
                   AND ar.id IN (SELECT artist_id FROM live_links)
                 ORDER BY ar.name COLLATE NOCASE
                 LIMIT ?
            )
            SELECT m.id AS id, m.name AS name,
                   COUNT(DISTINCT ll.album_id) AS album_count
              FROM matched m
              JOIN live_links ll ON ll.artist_id = m.id
             GROUP BY m.id, m.name
             ORDER BY m.name COLLATE NOCASE
            """,
            (like, limit),
        )
        return [
            ArtistRow(int(row["id"]), str(row["name"]), int(row["album_count"] or 0))
            for row in rows
        ]


def _like(query: str) -> str | None:
    """Экранированный LIKE-шаблон. None для пустого запроса."""
    cleaned = (query or "").strip()
    if not cleaned:
        return None
    escaped = cleaned.replace("\\", r"\\").replace("%", r"\%").replace("_", r"\_")
    return f"%{escaped}%"
