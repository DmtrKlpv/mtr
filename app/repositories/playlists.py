"""Репозиторий плейлистов (mtr_music.db).

Два типа плейлистов:

**Статические** — список треков в `playlist_tracks` с полем `position`.
Порядок задаётся явно; переупорядочивание переписывает позиции пачкой
в одной транзакции.

**Умные (smart)** — правила в JSON (`smart_rules`), выборка строится на лету.
Треки в БД не дублируются: изменились теги — изменился и результат.

Правила смарт-плейлиста:

    {
      "sort": "play_count" | "added_at" | "title" | "random" | "duration_ms",
      "order": "asc" | "desc",
      "limit": 100,
      "filters": [
        {"field": "play_count", "op": "gt",  "value": 10},
        {"field": "added_at",   "op": "after_days", "value": 30},
        {"field": "genre",      "op": "eq",  "value": "Rock"}
      ]
    }

Безопасность: `field`, `op` и `sort` берутся ТОЛЬКО из белых списков ниже.
Значения подставляются плейсхолдерами. Это критично — правила приходят
от пользователя через API и попадают в SQL.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from typing import Any

from app.db.database import Database
from app.repositories.tracks import TrackRow, _row_to_track
from app.utils.alphabet import VALID
from app.utils.timeutil import days_ago_iso, utcnow_iso

logger = logging.getLogger(__name__)

# --- Белые списки: единственный способ попасть в динамический SQL -------
SORT_FIELDS: dict[str, str] = {
    "title": "t.title COLLATE NOCASE",
    "artist": "t.artist COLLATE NOCASE",
    "album": "t.album COLLATE NOCASE",
    "play_count": "COALESCE(ps.play_count, 0)",
    "added_at": "t.added_at",
    "duration_ms": "t.duration_ms",
    "last_played": "COALESCE(ps.last_played_at, '')",
    "random": "RANDOM()",
}

FILTER_FIELDS: dict[str, str] = {
    "play_count": "COALESCE(ps.play_count, 0)",
    "added_at": "t.added_at",
    "last_played": "COALESCE(ps.last_played_at, '')",
    "duration_ms": "t.duration_ms",
    "year": "t.year",
    "genre": "t.genre",
    "artist": "t.artist",
    "album": "t.album",
}

NUMERIC_OPS: dict[str, str] = {
    "eq": "=", "ne": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<=",
}

TEXT_OPS: dict[str, str] = {"eq": "=", "ne": "!="}

DEFAULT_LIMIT = 500
MAX_LIMIT = 5000


class PlaylistError(Exception):
    """Нарушение бизнес-правил плейлиста (ответ 400/50 клиенту)."""


@dataclass(slots=True)
class PlaylistRow:
    """Плейлист с агрегатами для Subsonic-ответа."""

    id: int
    owner_id: int
    name: str
    comment: str | None
    is_public: bool
    is_smart: bool
    smart_rules: str | None
    created_at: str
    updated_at: str
    song_count: int = 0
    duration_ms: int = 0

    @property
    def duration_sec(self) -> int:
        return int(round(self.duration_ms / 1000))

    @property
    def rules(self) -> dict[str, Any]:
        """Разобранные правила. Пустой dict для статического плейлиста."""
        if not self.smart_rules:
            return {}
        try:
            parsed = json.loads(self.smart_rules)
        except (ValueError, TypeError):
            logger.warning("плейлист %d: повреждённый smart_rules", self.id)
            return {}
        return parsed if isinstance(parsed, dict) else {}


def _row_to_playlist(row: sqlite3.Row) -> PlaylistRow:
    return PlaylistRow(
        id=int(row["id"]),
        owner_id=int(row["owner_id"]),
        name=str(row["name"]),
        comment=row["comment"],
        is_public=bool(row["is_public"]),
        is_smart=bool(row["is_smart"]),
        smart_rules=row["smart_rules"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        song_count=int(row["song_count"] or 0),
        duration_ms=int(row["duration_ms"] or 0),
    )


def validate_rules(raw: Any) -> str:
    """Проверяет правила смарт-плейлиста и возвращает JSON-строку.

    Отклоняем всё, чего нет в белых списках: неизвестное поле в SQL
    превратилось бы в инъекцию или в ошибку запроса.
    """
    if raw is None:
        return json.dumps({"sort": "added_at", "order": "desc",
                           "limit": DEFAULT_LIMIT, "filters": []})

    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise PlaylistError(f"smart_rules: некорректный JSON: {exc}") from exc

    if not isinstance(raw, dict):
        raise PlaylistError("smart_rules: ожидался объект")

    sort = str(raw.get("sort", "added_at")).lower()
    if sort not in SORT_FIELDS:
        raise PlaylistError(
            f"smart_rules: недопустимая сортировка {sort!r}, "
            f"допустимы: {', '.join(sorted(SORT_FIELDS))}"
        )

    order = str(raw.get("order", "desc")).lower()
    if order not in ("asc", "desc"):
        raise PlaylistError("smart_rules: order должен быть asc или desc")

    try:
        limit = int(raw.get("limit", DEFAULT_LIMIT))
    except (TypeError, ValueError) as exc:
        raise PlaylistError("smart_rules: limit должен быть числом") from exc
    limit = max(1, min(limit, MAX_LIMIT))

    filters_in = raw.get("filters", [])
    if not isinstance(filters_in, list):
        raise PlaylistError("smart_rules: filters должен быть массивом")

    filters: list[dict[str, Any]] = []
    for item in filters_in:
        if not isinstance(item, dict):
            raise PlaylistError("smart_rules: каждый фильтр должен быть объектом")
        field = str(item.get("field", "")).lower()
        op = str(item.get("op", "")).lower()
        if field not in FILTER_FIELDS:
            raise PlaylistError(
                f"smart_rules: недопустимое поле {field!r}, "
                f"допустимы: {', '.join(sorted(FILTER_FIELDS))}"
            )
        # after_days/before_days — относительные даты, удобные в UI.
        if op in ("after_days", "before_days"):
            try:
                days = int(item.get("value", 0))
            except (TypeError, ValueError) as exc:
                raise PlaylistError(
                    f"smart_rules: {op} требует число дней"
                ) from exc
            filters.append({"field": field, "op": op, "value": max(0, days)})
            continue
        allowed = NUMERIC_OPS if field in (
            "play_count", "duration_ms", "year"
        ) else TEXT_OPS
        if op not in allowed:
            raise PlaylistError(
                f"smart_rules: недопустимая операция {op!r} для поля {field!r}"
            )
        filters.append({"field": field, "op": op, "value": item.get("value")})

    return json.dumps(
        {"sort": sort, "order": order, "limit": limit, "filters": filters},
        ensure_ascii=False,
    )


class PlaylistRepository:
    """CRUD плейлистов и разрешение прав доступа."""

    def __init__(self, db: Database) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Чтение
    # ------------------------------------------------------------------
    async def get(self, playlist_id: int) -> PlaylistRow | None:
        row = await self._db.fetch_one(
            """
            SELECT p.*,
                   (SELECT COUNT(*) FROM playlist_tracks pt
                     WHERE pt.playlist_id = p.id) AS song_count,
                   (SELECT COALESCE(SUM(t.duration_ms), 0)
                      FROM playlist_tracks pt JOIN tracks t ON t.id = pt.track_id
                     WHERE pt.playlist_id = p.id) AS duration_ms
              FROM playlists p WHERE p.id = ?
            """,
            (playlist_id,),
        )
        return _row_to_playlist(row) if row else None

    async def list_visible(
        self, user_id: int, *, username: str | None = None,
        letter: str | None = None,
    ) -> list[PlaylistRow]:
        """Плейлисты, видимые пользователю: свои + публичные.

        Владелец проверяется по `owner_id`. Осиротевшие плейлисты
        (владелец удалён) видны только если публичные — иначе они
        превратились бы в ничей мусор, к которому есть доступ.
        """
        # Скобки обязательны: без них AND прилипнет только к is_public.
        where = "(p.owner_id = ? OR p.is_public = 1)"
        params: list[object] = [user_id]
        if letter and letter in VALID:
            where += " AND mtr_letter(p.name) = ?"
            params.append(letter)
        rows = await self._db.fetch_all(
            """
            SELECT p.*,
                   CASE WHEN p.is_smart = 1 THEN
                        COALESCE(json_array_length(p.smart_rules, '$.filters'), 0)
                   ELSE
                        (SELECT COUNT(*) FROM playlist_tracks pt
                          WHERE pt.playlist_id = p.id)
                   END AS song_count,
                   CASE WHEN p.is_smart = 1 THEN 0 ELSE
                        (SELECT COALESCE(SUM(t.duration_ms), 0)
                           FROM playlist_tracks pt
                           JOIN tracks t ON t.id = pt.track_id
                          WHERE pt.playlist_id = p.id)
                   END AS duration_ms
              FROM playlists p
             WHERE """ + where + """
             ORDER BY p.name COLLATE NOCASE
            """,
            tuple(params),
        )
        return [_row_to_playlist(row) for row in rows]

    async def tracks(self, playlist: PlaylistRow) -> list[TrackRow]:
        """Треки плейлиста: статического — по позиции, умного — по правилам."""
        if playlist.is_smart:
            return await self._resolve_smart(playlist)
        rows = await self._db.fetch_all(
            """
            SELECT t.* FROM playlist_tracks pt
              JOIN tracks t ON t.id = pt.track_id
             WHERE pt.playlist_id = ? AND t.missing = 0
             ORDER BY pt.position, pt.id
            """,
            (playlist.id,),
        )
        return [_row_to_track(row) for row in rows]

    async def _resolve_smart(self, playlist: PlaylistRow) -> list[TrackRow]:
        """Строит выборку по правилам. Все значения — плейсхолдеры."""
        rules = playlist.rules
        sort_key = str(rules.get("sort", "added_at")).lower()
        order_sql = SORT_FIELDS.get(sort_key, SORT_FIELDS["added_at"])
        direction = "ASC" if str(rules.get("order", "desc")).lower() == "asc" else "DESC"
        try:
            limit = max(1, min(int(rules.get("limit", DEFAULT_LIMIT)), MAX_LIMIT))
        except (TypeError, ValueError):
            limit = DEFAULT_LIMIT

        clauses = ["t.missing = 0"]
        params: list[Any] = []

        for item in rules.get("filters", []) or []:
            if not isinstance(item, dict):
                continue
            field = str(item.get("field", "")).lower()
            column = FILTER_FIELDS.get(field)
            if column is None:
                continue
            op = str(item.get("op", "")).lower()
            value = item.get("value")

            if op in ("after_days", "before_days"):
                try:
                    days = int(value)
                except (TypeError, ValueError):
                    continue
                boundary = days_ago_iso(days)
                comparator = ">=" if op == "after_days" else "<="
                clauses.append(f"{column} {comparator} ?")
                params.append(boundary)
                continue

            sql_op = NUMERIC_OPS.get(op) or TEXT_OPS.get(op)
            if sql_op is None:
                continue
            clauses.append(f"{column} {sql_op} ?")
            params.append(value)

        sql = (
            "SELECT t.* FROM tracks t "
            "LEFT JOIN play_stats ps ON ps.track_id = t.id "
            f"WHERE {' AND '.join(clauses)} "
            f"ORDER BY {order_sql} {direction} LIMIT ?"
        )
        rows = await self._db.fetch_all(sql, (*params, limit))
        return [_row_to_track(row) for row in rows]

    # ------------------------------------------------------------------
    # Права
    # ------------------------------------------------------------------
    @staticmethod
    def can_view(playlist: PlaylistRow, user_id: int) -> bool:
        return playlist.is_public or playlist.owner_id == user_id

    @staticmethod
    def can_edit(playlist: PlaylistRow, user_id: int, *, is_admin: bool) -> bool:
        """Редактировать может владелец; админ — только публичные.

        Админ НЕ получает доступ к чужим приватным плейлистам: иначе
        привилегия превращалась бы в чтение личных подборок.
        """
        if playlist.owner_id == user_id:
            return True
        return is_admin and playlist.is_public

    # ------------------------------------------------------------------
    # Запись
    # ------------------------------------------------------------------
    async def create(
        self,
        owner_id: int,
        name: str,
        *,
        comment: str | None = None,
        is_public: bool = False,
        smart_rules: Any = None,
        track_ids: list[int] | None = None,
    ) -> PlaylistRow:
        """Создаёт плейлист. Пустые имена отклоняются (NOT NULL + пустота)."""
        cleaned = (name or "").strip()
        if not cleaned:
            raise PlaylistError("название плейлиста не может быть пустым")
        if len(cleaned) > 255:
            raise PlaylistError("название плейлиста длиннее 255 символов")

        is_smart = smart_rules is not None
        rules_json = validate_rules(smart_rules) if is_smart else None
        now = utcnow_iso()

        async with self._db.transaction():
            playlist_id = await self._db.execute(
                """
                INSERT INTO playlists
                    (owner_id, name, comment, is_public, is_smart, smart_rules,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (owner_id, cleaned, comment, 1 if is_public else 0,
                 1 if is_smart else 0, rules_json, now, now),
            )
            if track_ids and not is_smart:
                await self._replace_tracks(playlist_id, track_ids)

        created = await self.get(playlist_id)
        if created is None:  # pragma: no cover
            raise PlaylistError("плейлист создан, но не читается")
        return created

    async def update(
        self,
        playlist_id: int,
        *,
        name: str | None = None,
        comment: str | None = None,
        is_public: bool | None = None,
        smart_rules: Any = None,
        track_ids: list[int] | None = None,
        append_track_ids: list[int] | None = None,
    ) -> PlaylistRow:
        """Обновляет плейлист. `None` означает «не менять»."""
        playlist = await self.get(playlist_id)
        if playlist is None:
            raise PlaylistError("плейлист не найден")

        sets: list[str] = []
        params: list[Any] = []

        if name is not None:
            cleaned = name.strip()
            if not cleaned:
                raise PlaylistError("название плейлиста не может быть пустым")
            sets.append("name = ?")
            params.append(cleaned[:255])
        if comment is not None:
            sets.append("comment = ?")
            params.append(comment)
        if is_public is not None:
            sets.append("is_public = ?")
            params.append(1 if is_public else 0)
        if smart_rules is not None:
            sets.append("smart_rules = ?")
            params.append(validate_rules(smart_rules))
            sets.append("is_smart = 1")

        async with self._db.transaction():
            if sets:
                sets.append("updated_at = ?")
                params.append(utcnow_iso())
                await self._db.execute(
                    f"UPDATE playlists SET {', '.join(sets)} WHERE id = ?",
                    (*params, playlist_id),
                )
            if track_ids is not None:
                await self._replace_tracks(playlist_id, track_ids)
            elif append_track_ids:
                await self._append_tracks(playlist_id, append_track_ids)

        updated = await self.get(playlist_id)
        if updated is None:  # pragma: no cover
            raise PlaylistError("плейлист исчез после обновления")
        return updated

    async def delete(self, playlist_id: int) -> None:
        """Удаляет плейлист; позиции чистятся каскадом внутри music.db."""
        await self._db.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))

    async def set_public(self, playlist_id: int, is_public: bool) -> None:
        await self._db.execute(
            "UPDATE playlists SET is_public = ?, updated_at = ? WHERE id = ?",
            (1 if is_public else 0, utcnow_iso(), playlist_id),
        )

    # ------------------------------------------------------------------
    # Состав
    # ------------------------------------------------------------------
    async def _replace_tracks(self, playlist_id: int, track_ids: list[int]) -> int:
        """Полностью заменяет состав. Несуществующие треки отбрасываются.

        Фильтрация по `missing = 0` важна: клиент может прислать id трека,
        чей файл уже удалён, и тогда в плейлисте появилась бы дыра.
        """
        valid = await self._filter_existing(track_ids)
        await self._db.execute(
            "DELETE FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,)
        )
        if valid:
            now = utcnow_iso()
            await self._db.execute_many(
                "INSERT INTO playlist_tracks (playlist_id, track_id, position, added_at) "
                "VALUES (?, ?, ?, ?)",
                [(playlist_id, track_id, position, now)
                 for position, track_id in enumerate(valid)],
            )
        return len(valid)

    async def _append_tracks(self, playlist_id: int, track_ids: list[int]) -> int:
        """Добавляет треки в конец, сохраняя текущий порядок."""
        valid = await self._filter_existing(track_ids)
        if not valid:
            return 0
        current = await self._db.fetch_value(
            "SELECT COALESCE(MAX(position), -1) FROM playlist_tracks "
            "WHERE playlist_id = ?",
            (playlist_id,),
            default=-1,
        )
        start = int(current if current is not None else -1) + 1
        now = utcnow_iso()
        await self._db.execute_many(
            "INSERT INTO playlist_tracks (playlist_id, track_id, position, added_at) "
            "VALUES (?, ?, ?, ?)",
            [(playlist_id, track_id, start + offset, now)
             for offset, track_id in enumerate(valid)],
        )
        return len(valid)

    async def _filter_existing(self, track_ids: list[int]) -> list[int]:
        """Убирает несуществующие и отсутствующие треки, сохраняя порядок."""
        if not track_ids:
            return []
        # Приводим к int явно: id приходят из JSON и могут быть строками.
        normalized: list[int] = []
        for value in track_ids:
            try:
                normalized.append(int(value))
            except (TypeError, ValueError):
                continue
        if not normalized:
            return []

        # Пакетами: SQLite ограничивает число плейсхолдеров.
        found: set[int] = set()
        chunk = 400
        for start in range(0, len(normalized), chunk):
            batch = normalized[start:start + chunk]
            placeholders = ",".join("?" for _ in batch)
            rows = await self._db.fetch_all(
                f"SELECT id FROM tracks WHERE id IN ({placeholders}) AND missing = 0",
                tuple(batch),
            )
            found.update(int(row["id"]) for row in rows)

        return [track_id for track_id in normalized if track_id in found]

    async def remove_track(self, playlist_id: int, track_id: int) -> bool:
        """Удаляет одно вхождение трека и пересобирает позиции."""
        removed = await self._db.execute_many(
            "DELETE FROM playlist_tracks WHERE playlist_id = ? AND track_id = ? "
            "AND position = (SELECT MIN(position) FROM playlist_tracks "
            "                WHERE playlist_id = ? AND track_id = ?)",
            [(playlist_id, track_id, playlist_id, track_id)],
        )
        if removed:
            await self._renumber(playlist_id)
        return bool(removed)

    async def _renumber(self, playlist_id: int) -> None:
        """Переписывает position как 0..n-1, сохраняя текущий порядок."""
        rows = await self._db.fetch_all(
            "SELECT id FROM playlist_tracks WHERE playlist_id = ? "
            "ORDER BY position, id",
            (playlist_id,),
        )
        if not rows:
            return
        await self._db.execute_many(
            "UPDATE playlist_tracks SET position = ? WHERE id = ?",
            [(position, int(row["id"])) for position, row in enumerate(rows)],
        )
