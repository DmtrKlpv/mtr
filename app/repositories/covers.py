"""Репозиторий кэша обложек (mtr_music.db + COVERS_DIR).

Таблица `cover_cache` уже есть в схеме v1, менять структуру не нужно.
Здесь — операции над ней и логика «откуда взять обложку».

Порядок для альбома:
  1. записи кэша с ключом `al-<id>` — если файл на диске ещё существует;
  2. поиск обложки у треков альбома (первый, у кого нашлась);
  3. файл cover.jpg в каталоге альбома.

Для трека (`tr-<id>`) — только его собственные данные: подставлять чужую
обложку к конкретному треку нельзя, клиент ждёт картинку именно этого файла.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from pathlib import Path

from app.covers.service import (
    CoverData,
    CoverResolver,
    extract_cover,
    extract_from_directory,
    make_thumbnail,
)
from app.db.database import Database
from app.utils.timeutil import utcnow_iso

logger = logging.getLogger(__name__)

# Сколько обложек извлекать одновременно. Извлечение — это чтение файла и
# разбор тегов: на слабом ARM десяток параллельных чтений уже насыщает диск,
# а браузер запрашивает картинки пачками по 6 штук на соединение.
COVER_EXTRACT_CONCURRENCY = 2

# Сколько ключей помнить как «обложки нет». Без этого повторный заход в каталог
# заново перечитывал бы десятки файлов на каждый альбом без картинки.
NEGATIVE_CACHE_LIMIT = 20000


def _search_cover_files(
    paths: list[Path], with_cover: list[Path]
) -> CoverData | None:
    """Ищет обложку среди файлов альбома. Выполняется в рабочем потоке.

    Сначала картинка-файл в каталоге (одно чтение каталога), затем встроенные
    обложки — только у треков, где это обещает флаг has_cover.
    """
    if paths:
        cover = extract_from_directory(paths[0])
        if cover is not None:
            return cover
    for path in with_cover:
        cover = extract_cover(path)
        if cover is not None:
            return cover
    return None


def _is_file(file_path: str) -> bool:
    try:
        return Path(file_path).is_file()
    except OSError:
        return False


class CoverRepository:
    """Кэш обложек: метаданные в БД, байты в COVERS_DIR."""

    def __init__(self, db: Database, resolver: CoverResolver) -> None:
        self._db = db
        self._resolver = resolver
        # Ограничитель именно для извлечения: чтение с диска не должно
        # занимать event loop и не должно идти сотнями потоков.
        self._semaphore = asyncio.Semaphore(COVER_EXTRACT_CONCURRENCY)
        # Отрицательный кеш: ключ -> True. Храним в памяти процесса.
        self._missing: OrderedDict[str, None] = OrderedDict()

    # ------------------------------------------------------------------
    # Чтение
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Отрицательный кеш
    # ------------------------------------------------------------------
    def _remember_missing(self, key: str) -> None:
        """Запоминает, что обложки у сущности нет.

        Без этого каждый заход в каталог заново перечитывал бы все файлы
        альбома в поисках картинки, которой там нет. Сбрасывается при
        изменении тегов и при очистке кеша.
        """
        self._missing[key] = None
        self._missing.move_to_end(key)
        while len(self._missing) > NEGATIVE_CACHE_LIMIT:
            self._missing.popitem(last=False)

    def _is_known_missing(self, key: str) -> bool:
        return key in self._missing

    def forget_missing(self) -> None:
        """Сбрасывает отрицательный кеш (после скана или очистки обложек)."""
        self._missing.clear()

    async def get_cached(self, key: str) -> CoverData | None:
        """Возвращает обложку из кеша, если файл ещё на месте."""
        row = await self._db.fetch_one(
            "SELECT file_path, mime_type FROM cover_cache WHERE key = ?", (key,)
        )
        if row is None:
            return None
        if not str(row["file_path"] or ""):
            # Маркер «проверено, картинки нет» — файла не существует.
            self._remember_missing(key)
            return None
        # Чтение файла блокирует поток: уводим в рабочий, иначе на медленном
        # томе встаёт весь event loop и интерфейс перестаёт отвечать.
        cover = await asyncio.to_thread(
            self._resolver.read_cached, str(row["file_path"])
        )
        if cover is None:
            # Файл пропал (например, почистили volume) — запись устарела.
            await self._db.execute("DELETE FROM cover_cache WHERE key = ?", (key,))
            return None
        # MIME из БД авторитетнее угадывания по сигнатуре.
        return CoverData(cover.data, str(row["mime_type"]), "cache")

    async def store(self, key: str, cover: CoverData) -> None:
        """Сохраняет обложку и запись кэша (upsert по ключу)."""
        # Уменьшаем один раз, при первом извлечении: дальше на диске лежит
        # готовое превью, и отдача в сетку не требует ни разбора тегов, ни
        # пересжатия на каждый запрос. Если Pillow не смог разобрать формат,
        # сохраняем оригинал как есть.
        thumb = await asyncio.to_thread(make_thumbnail, cover.data)
        if thumb is not None:
            data, mime = thumb
            cover = CoverData(data, mime, cover.source)

        # Запись на диск тоже блокирующая.
        file_path, size = await asyncio.to_thread(
            self._resolver.store, key, cover
        )
        self._missing.pop(key, None)
        await self._db.execute(
            "INSERT INTO cover_cache "
            "(key, file_path, mime_type, size_bytes, source, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (key) DO UPDATE SET "
            "file_path = excluded.file_path, "
            "mime_type = excluded.mime_type, "
            "size_bytes = excluded.size_bytes, "
            "source = excluded.source, "
            "created_at = excluded.created_at",
            (key, file_path, cover.mime, size, cover.source, utcnow_iso()),
        )

    # ------------------------------------------------------------------
    # Разрешение для альбома
    # ------------------------------------------------------------------
    async def for_album(self, album_id: int) -> CoverData | None:
        """Обложка альбома: кэш, затем файл в каталоге, затем теги."""
        return await self._for_album(album_id)

    async def _for_album(self, album_id: int) -> CoverData | None:
        """Обложка альбома: кеш, затем файл в каталоге, затем встроенные.

        Порядок выбран по стоимости: `cover.jpg` рядом с треками находится
        одним чтением каталога, а разбор тегов — это открытие и парсинг
        каждого файла. Поэтому встроенные обложки ищутся вторыми и только у
        треков, где их наличие подтверждает has_cover.
        """
        key = f"al-{album_id}"
        cached = await self.get_cached(key)
        if cached is not None:
            return cached
        if self._is_known_missing(key):
            return None

        rows = await self._db.fetch_all(
            "SELECT path, has_cover FROM tracks "
            "WHERE album_id = ? AND missing = 0 "
            "ORDER BY has_cover DESC, disc_number, track_number LIMIT 50",
            (album_id,),
        )
        if not rows:
            self._remember_missing(key)
            return None

        paths = [
            Path(str(row["path"])) for row in rows
            if self._resolver.is_inside_music(Path(str(row["path"])))
        ]
        with_cover = [
            Path(str(row["path"])) for row in rows
            if int(row["has_cover"] or 0)
            and self._resolver.is_inside_music(Path(str(row["path"])))
        ]

        # Извлечение — блокирующее чтение файлов: рабочий поток плюс
        # ограничитель, чтобы десятки параллельных запросов не забили диск.
        async with self._semaphore:
            cover = await asyncio.to_thread(
                _search_cover_files, paths, with_cover
            )

        if cover is None:
            self._remember_missing(key)
            await self.mark_absent(key)
            return None
        await self.store(key, cover)
        logger.debug("обложка альбома %d (%s)", album_id, cover.source)
        return cover

    async def pending_album_ids(
        self, *, mode: str = "incremental", limit: int | None = None
    ) -> list[int]:
        """Альбомы, которым нужна обложка.

        Инкрементальный проход берёт:
          1. альбомы без записи в cover_cache (новые);
          2. альбомы, у которых трек обновлён позже, чем записана обложка;
          3. альбомы, у которых запись есть, но файл превью пропал с диска.

        Полный проход снимает маркеры «картинки нет» (это делает CoverPass) и
        берёт те же альбомы; разница в том, что маркеров уже нет.
        """
        sql = (
            "SELECT a.id AS id FROM albums a "
            "WHERE EXISTS (SELECT 1 FROM tracks t "
            "              WHERE t.album_id = a.id AND t.missing = 0) "
            "  AND ("
            "    NOT EXISTS (SELECT 1 FROM cover_cache c "
            "                WHERE c.key = 'al-' || a.id)"
        )
        if mode != "full":
            sql += (
                " OR EXISTS (SELECT 1 FROM cover_cache c "
                "            WHERE c.key = 'al-' || a.id "
                "              AND c.file_path <> '' "
                "              AND EXISTS (SELECT 1 FROM tracks t2 "
                "                          WHERE t2.album_id = a.id "
                "                            AND t2.missing = 0 "
                "                            AND t2.updated_at > c.created_at))"
            )
        sql += ") ORDER BY a.id"
        rows = await self._db.fetch_all(sql)
        ids = {int(row["id"]) for row in rows}

        if mode != "full":
            cached = await self._db.fetch_all(
                "SELECT key, file_path FROM cover_cache "
                "WHERE file_path <> '' AND key LIKE 'al-%'"
            )
            for row in cached:
                try:
                    album_id = int(str(row["key"])[3:])
                except ValueError:
                    continue
                if album_id in ids:
                    continue
                if not await asyncio.to_thread(_is_file, str(row["file_path"])):
                    ids.add(album_id)

        result = sorted(ids)
        if limit is not None:
            result = result[: max(1, int(limit))]
        return result

    async def mark_absent(self, key: str) -> None:
        """Пишет в БД маркер «картинки нет» (file_path пустой, см. get_cached)."""
        await self._db.execute(
            "INSERT INTO cover_cache (key, file_path, mime_type, size_bytes, source, created_at) "
            "VALUES (?, '', '', 0, 'absent', ?) "
            "ON CONFLICT(key) DO UPDATE SET file_path = '', size_bytes = 0, source = 'absent'",
            (key, utcnow_iso()),
        )

    async def clear_absent_markers(self) -> int:
        """Снимает маркеры «картинки нет»: после полного скана тегов стоит перепроверить."""
        count = int(await self._db.fetch_value(
            "SELECT COUNT(*) FROM cover_cache WHERE file_path = ''", default=0,
        ) or 0)
        await self._db.execute("DELETE FROM cover_cache WHERE file_path = ''")
        self.forget_missing()
        return count

    async def for_track(self, track_id: int) -> CoverData | None:
        """Обложка трека: кэш, затем теги самого файла, затем обложка альбома."""
        return await self._for_track(track_id)

    async def _for_track(self, track_id: int) -> CoverData | None:
        """Обложка конкретного трека (для `tr-<id>`)."""
        key = f"tr-{track_id}"
        cached = await self.get_cached(key)
        if cached is not None:
            return cached

        row = await self._db.fetch_one(
            "SELECT path, has_cover, album_id FROM tracks WHERE id = ? AND missing = 0",
            (track_id,),
        )
        if row is None:
            return None

        path = Path(str(row["path"]))
        if not self._resolver.is_inside_music(path):
            return None

        cover = None
        if not self._is_known_missing(key):
            async with self._semaphore:
                cover = await asyncio.to_thread(extract_cover, path)
        if cover is None:
            # У трека своей картинки нет — отдаём обложку альбома,
            # но НЕ кэшируем под ключом трека: иначе при появлении
            # собственной обложки кэш останется неверным.
            album_id = row["album_id"]
            if album_id is None:
                return None
            return await self.for_album(int(album_id))

        await self.store(key, cover)
        return cover

    async def invalidate_album(self, album_id: int) -> None:
        """Сбрасывает кэш альбома (вызывается при изменении тегов)."""
        key = f"al-{album_id}"
        self._missing.pop(key, None)
        await self._db.execute("DELETE FROM cover_cache WHERE key = ?", (key,))

    # ------------------------------------------------------------------
    # Маркер «обложки нет» и прогрев
    # ------------------------------------------------------------------
    async def _mark_absent(self, key: str) -> None:
        """Пишет в кеш запись «проверено, обложки нет».

        Нужна прогреву: без неё он при каждом запуске заново перебирал бы
        тысячи альбомов без картинок. Пустой file_path — признак маркера.
        """
        await self._db.execute(
            "INSERT INTO cover_cache "
            "(key, file_path, mime_type, size_bytes, source, created_at) "
            "VALUES (?, '', 'none', 0, 'none', ?) "
            "ON CONFLICT (key) DO NOTHING",
            (key, utcnow_iso()),
        )
        self._remember_missing(key)

    async def purge_all(self) -> int:
        """Удаляет все превью: файлы в COVERS_DIR и записи cover_cache.

        Файлы удаляются только внутри COVERS_DIR — проверка через relative_to,
        чтобы ошибочный путь в БД не снёс что-то за пределами кеша.
        """
        rows = await self._db.fetch_all(
            "SELECT file_path FROM cover_cache WHERE file_path <> ''"
        )
        removed = 0
        base = self._resolver.covers_dir.resolve()
        for row in rows:
            raw = str(row["file_path"])
            candidate = (base / raw.split("/")[-1]).resolve()
            try:
                candidate.relative_to(base)
            except ValueError:
                continue
            try:
                if candidate.is_file():
                    candidate.unlink()
                    removed += 1
            except OSError:
                continue
        await self._db.execute("DELETE FROM cover_cache")
        self.forget_missing()
        return removed

    async def ready_keys(self, keys: list[str]) -> dict[str, str]:
        """Состояние обложек пачкой, без извлечения и без чтения файлов.

        Возвращает {key: "ready" | "absent"} только для ключей, у которых уже есть
        запись в cover_cache. Ключей без записи в словаре нет: клиент их не знает
        и ничего не запрашивает, пока фоновая фаза не дойдёт до них.
        Читается только таблица БД: это дешёвый запрос даже во время скана.
        """
        unique = list(dict.fromkeys(k for k in keys if k))[:500]
        if not unique:
            return {}
        marks = ",".join("?" for _ in unique)
        rows = await self._db.fetch_all(
            f"SELECT key, file_path FROM cover_cache WHERE key IN ({marks})",
            tuple(unique),
        )
        result: dict[str, str] = {}
        for row in rows:
            key = str(row["key"])
            # Пустой file_path — маркер «картинки нет».
            result[key] = "ready" if str(row["file_path"] or "") else "absent"
        return result

    async def stats(self) -> dict[str, int]:
        """Сколько обложек в кэше и сколько занимают."""
        row = await self._db.fetch_one(
            "SELECT COUNT(*) AS count, COALESCE(SUM(size_bytes), 0) AS total "
            "FROM cover_cache WHERE file_path <> ''"
        )
        if row is None:
            return {"count": 0, "total_bytes": 0}
        return {
            "count": int(row["count"] or 0),
            "total_bytes": int(row["total"] or 0),
        }
