"""Импорт m3u в фоне с прогрессом.

Раньше разбор папки был одним синхронным запросом: пока он шёл, интерфейс
показывал «Разбираю папку…» и ничего больше, а на большой библиотеке это
минуты. Нажатие «Импортировать» выглядело как зависание.

Теперь разбор идёт фоновой задачей в том же процессе, а интерфейс опрашивает
прогресс: общий этап (обход папки → разбор плейлистов → готово) и состояние
каждого плейлиста отдельно. Плейлисты обрабатываются строго последовательно:
следующий начинается только после предыдущего, поэтому прогресс монотонный.

Планы (результат сопоставления) хранятся в задаче: применение берёт их оттуда
и не разбирает папку повторно — иначе на «Применить» было бы то же зависание.
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Awaitable

from app.playlists import importer

logger = logging.getLogger(__name__)

# Сколько задач держим в памяти. Старые вытесняются.
KEEP_JOBS = 20


@dataclass
class ImportItem:
    """Состояние одного плейлиста в процессе разбора."""

    path: str
    name: str
    state: str = "pending"   # pending | parsing | matching | done | error
    total: int = 0
    matched: int = 0
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "name": self.name,
            "state": self.state,
            "total": self.total,
            "matched": self.matched,
            "error": self.error,
        }


@dataclass
class ImportJob:
    """Задача импорта: общий прогресс + состояние по каждому плейлисту."""

    id: str
    state: str = "running"          # running | done | error
    stage: str = "walk"             # walk | playlists | done | error
    total: int = 0                  # сколько плейлистов найдено
    done: int = 0                   # сколько обработано
    current: str = ""               # имя текущего плейлиста
    items: list[ImportItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    plans: list = field(default_factory=list)
    error: str | None = None
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None

    @property
    def percent(self) -> int:
        if self.state == "done":
            return 100
        if not self.total:
            return 0
        return min(99, round(self.done / self.total * 100))

    def as_dict(self, *, with_items: bool = True) -> dict[str, Any]:
        end = self.finished_at or time.monotonic()
        payload: dict[str, Any] = {
            "id": self.id,
            "state": self.state,
            "stage": self.stage,
            "total": self.total,
            "done": self.done,
            "percent": self.percent,
            "current": self.current,
            "elapsed_sec": round(end - self.started_at, 1),
            "warnings": self.warnings,
            "params": self.params,
            "error": self.error,
        }
        if with_items:
            payload["items"] = [item.as_dict() for item in self.items]
        if self.result is not None:
            payload["result"] = self.result
        return payload


class ImportJobs:
    """Реестр задач импорта в памяти процесса."""

    def __init__(self, keep: int = KEEP_JOBS) -> None:
        self._jobs: dict[str, ImportJob] = {}
        self._keep = keep

    def new(self) -> ImportJob:
        job = ImportJob(id=secrets.token_urlsafe(8))
        self._jobs[job.id] = job
        while len(self._jobs) > self._keep:
            self._jobs.pop(next(iter(self._jobs)))
        return job

    def get(self, job_id: str) -> ImportJob | None:
        return self._jobs.get(job_id)


async def run_import(
    job: ImportJob,
    *,
    build_plans: Callable[..., Awaitable[tuple[list, list[str], dict]]],
    dir_override: str | None,
    visible_playlists: Callable[[], Awaitable[list]],
) -> None:
    """Разбирает папку с прогрессом. Ничего не пишет в БД.

    Порядок строго последовательный: обход папки, затем по одному плейлисту.
    Так прогресс не скачет назад, а нагрузка на диск ровная.
    """
    try:
        job.stage = "walk"
        job.current = ""
        # Шаг 1: обход папки. Список файлов нужен заранее, чтобы показать общий счётчик.
        plans, warnings, params = await build_plans(
            dir_override=dir_override, job=job
        )
        job.warnings = warnings
        job.params = params
        job.total = len(plans)
        job.done = 0

        # Шаг 2: состояние по каждому плейлисту + сопоставление с существующими.
        visible = await visible_playlists()
        by_name = {p.name.strip().lower(): p for p in visible}
        job.items = [
            ImportItem(path=plan.path, name=plan.name) for plan in plans
        ]

        job.stage = "playlists"
        for index, plan in enumerate(plans):
            item = job.items[index]
            job.current = plan.name
            if plan.error:
                item.state = "error"
                item.error = plan.error
            else:
                item.state = "done"
                item.total = plan.total
                item.matched = plan.matched_count
            found = by_name.get(plan.name.strip().lower())
            if found is not None:
                plan.existing_id = found.id
                plan.existing_count = found.song_count
            job.done = index + 1

        job.plans = plans
        job.stage = "done"
        job.state = "done"
        job.current = ""
        job.result = {
            "params": params,
            "warnings": warnings,
            "playlists": [plan.as_dict() for plan in plans],
            "total_files": len(plans),
            "total_matched": sum(p.matched_count for p in plans),
            "total_unmatched": sum(len(p.unmatched) for p in plans),
        }
    except importer.ImportError_ as exc:
        job.state, job.stage, job.error = "error", "error", str(exc)
    except OSError as exc:
        job.state, job.stage = "error", "error"
        job.error = f"ошибка чтения папки: {exc}"
    except Exception as exc:  # noqa: BLE001 - задача не должна умирать молча
        logger.exception("импорт плейлистов упал")
        job.state, job.stage, job.error = "error", "error", str(exc)
    finally:
        job.finished_at = time.monotonic()


def search_root_for(raw_dir: str, music_root: Path) -> Path:
    """Корень поиска: пустая настройка = вся библиотека."""
    return Path(raw_dir) if raw_dir else music_root
