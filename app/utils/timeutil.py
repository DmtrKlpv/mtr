"""Единые функции времени.

Все временные метки в базах хранятся как UTC ISO-8601 со суффиксом Z.
Это делает их сравнимыми как строки (что важно для SQL-фильтров
«добавлено за последние 30 дней») и не зависит от таймзоны контейнера.
"""

from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    """Текущее время в UTC с таймзоной."""
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    """Текущее время как строка 'YYYY-MM-DDTHH:MM:SSZ'."""
    return to_iso(utcnow())


def to_iso(dt: datetime) -> str:
    """Приводит datetime к UTC-строке с суффиксом Z.

    Naive-datetime трактуется как UTC, а не как локальное время: иначе
    в контейнере с TZ=Europe/Moscow метки уехали бы на 3 часа.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def days_ago_iso(days: int) -> str:
    """ISO-строка для момента N дней назад (для смарт-плейлистов)."""
    from datetime import timedelta

    return to_iso(utcnow() - timedelta(days=days))
