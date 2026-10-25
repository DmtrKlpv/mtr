"""Замер стоимости обложек: холодный путь против кеша.

Отвечает на вопрос «сколько стоит показать сетку» и «что даёт прогрев».
Замер идёт на РЕАЛЬНОЙ библиотеке: берутся альбомы, у которых превью ещё нет,
замеряется извлечение из файла + уменьшение + запись, затем повторный проход
по тем же альбомам — он должен попасть в кеш.

Запуск в контейнере:

    docker exec -it mtr-app python -m app.cli bench-covers
    docker exec -it mtr-app python -m app.cli bench-covers --albums 100

Скрипт ничего не удаляет: он только читает файлы и создаёт недостающие превью.
Альбомы, у которых превью уже есть, пропускаются — их стоимость нулевая по
определению, и включать их в замер холодного пути было бы враньём.
"""

from __future__ import annotations

import logging
import statistics
import time

from app.bootstrap import AppContext

logger = logging.getLogger(__name__)

# Сколько карточек в одном экране сетки — столько запросов обложек уходит
# при открытии «Альбомы». Нужно, чтобы перевести «мс на альбом» в «секунды
# до появления картинок на экране».
GRID_PAGE = 60


def _percentile(values: list[float], share: float) -> float:
    """Простой перцентиль без внешних зависимостей."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * share))
    return ordered[index]


async def bench_covers(ctx: AppContext, albums: int = 60) -> dict[str, object]:
    """Меряет холодное извлечение и чтение из кеша на выборке альбомов."""
    # Выборка альбомов с треками. Прогрева больше нет, поэтому берём
    # первые альбомы библиотеки: холодный прогон идёт по ним при первом чтении.
    rows = await ctx.music_db.fetch_all(
        "SELECT DISTINCT album_id FROM tracks "
        "WHERE album_id IS NOT NULL AND missing = 0 ORDER BY album_id LIMIT ?",
        (albums,),
    )
    pending = [int(row["album_id"]) for row in rows]
    if not pending:
        raise SystemExit("в библиотеке нет альбомов с треками — сначала запустите скан")
    cold: list[float] = []
    db_before = await ctx.covers.stats()
    sizes: list[int] = []

    # --- холодный путь: чтение файла, разбор тегов, уменьшение, запись ---
    started = time.monotonic()
    made = absent = failed = 0
    for album_id in pending:
        tick = time.monotonic()
        try:
            cover = await ctx.covers.for_album(album_id)
        except Exception:  # noqa: BLE001
            logger.exception("замер: ошибка на альбоме %d", album_id)
            failed += 1
            continue
        cold.append((time.monotonic() - tick) * 1000)
        if cover is None:
            absent += 1
        else:
            made += 1
    total_cold = time.monotonic() - started

    # --- стоимость одного превью на диске ---
    if pending:
        placeholders = ",".join("?" for _ in pending)
        rows = await ctx.music_db.fetch_all(
            f"SELECT size_bytes FROM cover_cache "
            f"WHERE key IN ({placeholders})",
            tuple(f"al-{album_id}" for album_id in pending),
        )
        sizes = [int(row["size_bytes"] or 0) for row in rows]

    # --- тёплый путь: те же альбомы, но уже из кеша ---
    warm: list[float] = []
    for album_id in pending:
        tick = time.monotonic()
        try:
            await ctx.covers.for_album(album_id)
        except Exception:  # noqa: BLE001
            continue
        warm.append((time.monotonic() - tick) * 1000)

    stats = await ctx.covers.stats()
    library = await ctx.tracks.library_stats()

    return {
        "albums": len(pending),
        "made": made,
        "absent": absent,
        "failed": failed,
        "cold": cold,
        "warm": warm,
        "total_cold_sec": total_cold,
        "sizes": sizes,
        "cache_before": db_before,
        "cache_after": stats,
        "library_albums": int(library.get("albums", 0)),
    }


def format_bench(result: dict[str, object]) -> str:
    """Человекочитаемый отчёт."""
    cold = result["cold"]  # type: ignore[assignment]
    warm = result["warm"]  # type: ignore[assignment]
    sizes = result["sizes"]  # type: ignore[assignment]
    albums = int(result["albums"])  # type: ignore[arg-type]

    lines: list[str] = []
    lines.append(f"Альбомов в выборке: {albums}")
    lines.append(
        f"  превью создано: {result['made']}, без картинки: {result['absent']}, "
        f"ошибок: {result['failed']}"
    )
    lines.append("")

    if cold:
        total = sum(cold) / 1000
        lines.append("ХОЛОДНЫЙ путь (файл → теги → уменьшение → запись):")
        lines.append(f"  всего: {total:.1f} с на {len(cold)} альбомов")
        lines.append(
            f"  среднее: {statistics.mean(cold):.0f} мс, "
            f"медиана: {statistics.median(cold):.0f} мс, "
            f"p90: {_percentile(cold, 0.9):.0f} мс, "
            f"максимум: {max(cold):.0f} мс"
        )
    else:
        lines.append("ХОЛОДНЫЙ путь: не измерялся (кеш уже заполнен).")

    if warm:
        lines.append("")
        lines.append("ТЁПЛЫЙ путь (готовый файл из кеша):")
        lines.append(
            f"  среднее: {statistics.mean(warm):.1f} мс, "
            f"медиана: {statistics.median(warm):.1f} мс, "
            f"максимум: {max(warm):.1f} мс"
        )

    if cold and warm:
        speedup = statistics.mean(cold) / max(0.01, statistics.mean(warm))
        lines.append("")
        lines.append(f"Ускорение на повторном показе: в {speedup:.0f} раз")

    if sizes:
        lines.append("")
        lines.append("Размер превью на диске:")
        lines.append(
            f"  среднее: {statistics.mean(sizes) / 1024:.0f} КБ, "
            f"медиана: {statistics.median(sizes) / 1024:.0f} КБ, "
            f"максимум: {max(sizes) / 1024:.0f} КБ"
        )

    # --- экстраполяция на всю библиотеку ---
    total_albums = int(result["library_albums"])
    if cold and total_albums:
        per_album = statistics.mean(cold) / 1000
        warm_all = per_album * total_albums
        if warm_all < 90:
            human = f"{warm_all:.0f} с"
        elif warm_all < 5400:
            human = f"{warm_all / 60:.0f} мин"
        else:
            human = f"{warm_all / 3600:.1f} ч"
        lines.append("")
        lines.append(
            f"Экстраполяция на всю библиотеку ({total_albums} альбомов): "
            f"прогрев ≈ {human}"
        )
        grid_cold = per_album * GRID_PAGE
        grid_warm = statistics.mean(warm) / 1000 * GRID_PAGE if warm else 0
        lines.append(
            f"Открытие сетки ({GRID_PAGE} карточек): холодное ≈ {grid_cold:.1f} с, "
            f"из кеша ≈ {grid_warm:.2f} с"
        )
        lines.append(
            "  (это последовательная оценка; сервер делает 2 извлечения "
            "параллельно, поэтому по времени будет примерно вдвое меньше)"
        )

    before = int(result["cache_before"].get("count", 0))  # type: ignore[union-attr]
    after = int(result["cache_after"].get("count", 0))  # type: ignore[union-attr]
    lines.append("")
    lines.append(f"Превью в кеше: было {before}, стало {after}")

    return "\n".join(lines)
