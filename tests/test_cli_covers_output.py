"""Форматирование вывода команды covers.

Проверяем не саму обработку обложек (она в tests/test_covers_*), а то,
что читается в консоли: «0 с» вместо «меньше секунды» и «0 МБ» —
это не косметика, а признак ошибки в расчёте.
"""

from __future__ import annotations

from app.cli import _human_duration, _human_size


def test_human_duration_short_values():
    assert _human_duration(0) == "меньше секунды"
    assert _human_duration(0.4) == "меньше секунды"
    assert _human_duration(2.35) == "2.4 с"
    assert _human_duration(45) == "45 с"


def test_human_duration_minutes_and_hours():
    assert _human_duration(600) == "10 мин"
    assert _human_duration(3600) == "1 ч"
    assert _human_duration(4800) == "1 ч 20 мин"


def test_human_size_small_is_kilobytes_not_zero_megabytes():
    """90 КБ не должны превращаться в «0 МБ»."""
    assert _human_size(90 * 1024) == "90 КБ"
    assert _human_size(500) == "500 Б"


def test_human_size_large():
    assert _human_size(12 * 1024 * 1024) == "12 МБ"
    assert _human_size(1500 * 1024 * 1024) == "1.5 ГБ"
