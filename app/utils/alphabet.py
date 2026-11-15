"""Рубрики алфавита для списков исполнителей, альбомов и плейлистов."""

from __future__ import annotations

import unicodedata
from typing import Final

DIGITS: Final = "0-9"
OTHER: Final = "Другие"

LATIN: Final = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
# Ё в рубрикаторе — это Е, как в словарях.
CYRILLIC: Final = "АБВГДЕЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"

BUCKETS: Final[tuple[str, ...]] = (DIGITS, *LATIN, *CYRILLIC, OTHER)
VALID: Final[frozenset[str]] = frozenset(BUCKETS)

_LATIN_SET: Final = frozenset(LATIN)
_CYRILLIC_SET: Final = frozenset(CYRILLIC)

# Латинские буквы, которые NFD не раскладывает на базовую: æ не станет a + e.
_LATIN_FOLD: Final = {
    "Æ": "A", "Œ": "O", "Ø": "O", "Đ": "D", "Ð": "D",
    "Ł": "L", "Þ": "P", "ß": "S", "Ŧ": "T",
}


def _base_letter(ch: str) -> str:
    upper = ch.upper()
    # NFD разложил бы Й в И, а это отдельная буква.
    if upper in _CYRILLIC_SET or upper == "Ё":
        return upper
    if upper in _LATIN_FOLD:
        return _LATIN_FOLD[upper]
    decomposed = unicodedata.normalize("NFD", upper)
    return decomposed[0] if decomposed else upper


def letter_of(name: str | None) -> str:
    """Рубрика имени: «0-9», буква или «Другие». Спецсимволы пропускаются."""
    for ch in name or "":
        if not ch.isalnum():
            continue
        if ch.isnumeric():
            return DIGITS
        base = _base_letter(ch)
        if base == "Ё":
            return "Е"
        if base in _LATIN_SET or base in _CYRILLIC_SET:
            return base
        return OTHER
    return OTHER


def fold(text: str | None) -> str:
    """Приводит строку к виду для поиска: нижний регистр и ё -> е.

    Зачем: «елка» и «ёлка» для пользователя одно слово, а в базе это разные
    строки. Заодно убирается разница регистра у латиницы и кириллицы.
    Регистр снимается через casefold: он корректно обрабатывает «SS» -> «ss»
    и другие неоднозначные символы, чего .lower() не делает.
    """
    return (text or "").casefold().replace("ё", "е")
