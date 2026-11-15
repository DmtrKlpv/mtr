"""Сопоставление записей плейлиста с библиотекой, когда теги разъехались.

Зачем. В плейлистах исполнитель часто записан перечислением, а в библиотеке он
один, зато соавторы вынесены в название::

    плейлист:  artist="Fallaout boy, Idx, Korasakov"  title="My favorite game"
    библиотека: artist="Fallaout boy"  title="My favorite game (feat. Idx, Korasakov)"

Точное сравнение по (artist, title) такие пары не находит: исполнитель длиннее,
название с хвостом. Запись оставалась «не найдено» с пустым списком вариантов —
выбрать вручную было нечего.

Что здесь есть:

* :func:`artist_variants` — все части перечисления **плюс** исходная строка.
  Полный набор разделителей (`feat`, `ft`, `with`, `via`, `vs`, `&`, `,`, `/`,
  `x`), потому что титульный исполнитель может стоять не первым. Исходная
  строка тоже остаётся вариантом: `Simon & Garfunkel` не должно теряться.
* :func:`base_title` — название без хвоста соавторов: `(feat. …)`, `(via …)`,
  `(with …)`, `(prod. …)`, а также без ` feat. …` в конце без скобок.
* :func:`normalize_filename` — имя файла без расширения, ведущего номера трека,
  скобок и разделителей вроде `_`.

Главное правило: совпадения по этим правилам **приблизительные**. Поэтому
:meth:`TrackIndex.suggest` только предлагает варианты, а вызывающая сторона
обязана показывать их пользователю на выбор и никогда не подставлять молча.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Iterable

# Слова, которые в скобках означают «здесь перечислены соавторы».
_COLLAB_WORDS = (
    "feat", "ft", "featuring", "with", "via", "prod", "vs", "&", "remix by",
)

# Разделители перечисления исполнителей. Порядок не важен: regex с чередованием.
_ARTIST_SPLIT = re.compile(
    r"\s*(?:"
    r"\bfeaturing\b|\bfeat\.?(?=\s)|\bft\.?(?=\s)|\bwith\b|\bvia\b|"
    r"\bvs\.?(?=\s)|\bprod\.?(?=\s)|\bx(?=\s)|\band\b|"
    r"[,;/&+×]"
    r")\s*",
    re.IGNORECASE,
)

# Хвост «… feat. X» без скобок.
_INLINE_COLLAB = re.compile(
    r"\s+(?:feat\.?|ft\.?|featuring|with|via|prod\.?)\s+.*$",
    re.IGNORECASE,
)

# Группа в скобках в конце строки: «(feat. X)», «[Official]».
_TRAILING_GROUP = re.compile(r"\s*[\(\[\{]([^\)\]\}]*)[\)\]\}]\s*$")

# Ведущий номер трека в имени файла: «01 - », «01. », «1_».
_LEADING_TRACK_NO = re.compile(r"^\s*\d{1,3}\s*[-–—.)\]]?\s+")

_PUNCT = re.compile(r"[.,;:!?'\"«»“”()\[\]{}]+")
_SPACES = re.compile(r"\s+")


def _clean(text: str) -> str:
    """Нижний регистр, без пунктуации, одиночные пробелы."""
    lowered = text.replace("ё", "е").lower()
    lowered = _PUNCT.sub(" ", lowered)
    lowered = lowered.replace("_", " ")
    return _SPACES.sub(" ", lowered).strip()


def _strip_trailing_groups(text: str, *, only_collab: bool) -> tuple[str, list[str]]:
    """Срезает группы в скобках в конце строки.

    Возвращает (остаток, содержимое срезанных групп). При ``only_collab``
    срезаются только группы с соавторами: `(Official Video)` в названии трека
    может быть частью названия, а вот `(feat. X)` — точно хвост.
    """
    removed: list[str] = []
    current = text.strip()
    while True:
        match = _TRAILING_GROUP.search(current)
        if match is None:
            break
        inner = match.group(1)
        if only_collab and not _looks_like_collab(inner):
            break
        removed.append(inner)
        current = current[: match.start()].rstrip()
    return current, removed


def _looks_like_collab(inner: str) -> bool:
    lowered = inner.lower()
    if any(word in lowered for word in _COLLAB_WORDS):
        return True
    # «(Idx, Korasakov)» — перечисление без ключевого слова.
    return "," in inner


def artist_variants(value: str | None) -> list[str]:
    """Все варианты имени исполнителя из строки плейлиста.

    ``"Fallaout boy, Idx, Korasakov"`` → ``["fallaout boy", "idx", "korasakov"]``.
    Исходная строка попадает в результат последней, чтобы точное имя вроде
    ``Simon & Garfunkel`` тоже было вариантом.
    """
    if not value or not value.strip():
        return []
    text, removed = _strip_trailing_groups(value.strip(), only_collab=False)
    pieces: list[str] = [text, *removed]
    out: list[str] = []
    for piece in pieces:
        for part in _ARTIST_SPLIT.split(piece):
            cleaned = _clean(part)
            if cleaned and cleaned not in out:
                out.append(cleaned)
    whole = _clean(text)
    if whole and whole not in out:
        out.append(whole)
    return out


def base_title(value: str | None) -> str:
    """Название без хвоста соавторов — ключ для сравнения.

    ``"My favorite game (feat. Idx, Korasakov)"`` → ``"my favorite game"``.
    ``"Song (Official Video)"`` не трогаем: это часть названия, а не соавторы.
    """
    if not value or not value.strip():
        return ""
    text, _ = _strip_trailing_groups(value.strip(), only_collab=True)
    text = _INLINE_COLLAB.sub("", text)
    return _clean(text)


def normalize_filename(name: str) -> list[str]:
    """Варианты нормализованного имени файла для сравнения.

    ``"02 - Fallaout boy - My favorite game.mp3"`` →
    ``["fallaout boy my favorite game", "my favorite game", "fallaout boy"]``.

    Расширение, ведущий номер трека, скобки и подчёркивания отбрасываются.
    Вариант «только название» нужен, когда в плейлисте файл назван без
    исполнителя, а в библиотеке — с ним (и наоборот).
    """
    if not name:
        return []
    stem = PurePosixPath(name.replace("\\", "/")).name
    stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", stem)
    stem = stem.replace("_", " ")
    stem = _LEADING_TRACK_NO.sub("", stem)
    # Хвост соавторов в имени файла тоже срезаем: «Song (feat. X).mp3».
    stem, _ = _strip_trailing_groups(stem, only_collab=True)
    stem = _INLINE_COLLAB.sub("", stem)
    # Дефисы в «полном» варианте — разделители полей, а не часть слова.
    full = _clean(re.sub(r"[-–—]+", " ", stem))
    if not full:
        return []
    out = [full]

    # «Артист - Название»: пробуем обе половины отдельно.
    parts = [p for p in re.split(r"\s+[-–—]\s+", stem) if p.strip()]
    if len(parts) >= 2:
        title_part = _clean(parts[-1])
        artist_part = _clean(parts[0])
        for piece in (title_part, artist_part):
            if piece and piece not in out:
                out.append(piece)
    return out


# ----------------------------------------------------------------------
# Индекс библиотеки
# ----------------------------------------------------------------------
# Метка варианта: её видит пользователь в списке выбора.
MATCH_BY_TITLE_ARTIST = "title_artist"
MATCH_BY_TITLE_BASE = "title_base"
MATCH_BY_FILENAME = "filename_norm"
MATCH_BY_FILENAME_EXACT = "filename"
MATCH_BY_TITLE_EXACT = "title"

MATCH_LABELS = {
    MATCH_BY_TITLE_ARTIST: "по названию и первому артисту",
    MATCH_BY_TITLE_BASE: "по названию без feat",
    MATCH_BY_FILENAME: "по имени файла",
    MATCH_BY_FILENAME_EXACT: "по имени файла (точно)",
    MATCH_BY_TITLE_EXACT: "по названию (точно)",
}


@dataclass(slots=True)
class TrackIndex:
    """Индекс живых треков для приблизительного поиска.

    Строится один раз на импорт или ресинк: сравнение идёт в памяти, а не
    N запросами к SQLite. На библиотеке 38 000 треков это ~10 МБ и один
    проход по таблице.
    """

    by_title: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    by_filename: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    size: int = 0

    @classmethod
    def build(cls, rows: Iterable[dict[str, Any]]) -> "TrackIndex":
        index = cls()
        for row in rows:
            if row_dict := dict(row):
                if row_dict.get("missing"):
                    continue
                index._add(row_dict)
        return index

    def _add(self, row: dict[str, Any]) -> None:
        self.size += 1
        key = base_title(row.get("title"))
        if key:
            self.by_title.setdefault(key, []).append(row)
        for variant in normalize_filename(str(row.get("filename") or row.get("path") or "")):
            self.by_filename.setdefault(variant, []).append(row)

    def suggest(
        self,
        *,
        title: str | None,
        artist: str | None,
        filename: str | None = None,
        limit: int = 5,
        exclude: set[int] | None = None,
    ) -> list[tuple[dict[str, Any], str]]:
        """Варианты трека для записи плейлиста, в порядке убывания точности.

        Возвращает пары (трек, метка совпадения). Пустой список — вариантов нет.
        Ничего не выбирает сама: решение за пользователем.
        """
        if limit <= 0:
            return []
        skip = exclude or set()
        seen: dict[int, tuple[dict[str, Any], str]] = {}

        wanted_title = base_title(title)
        artists = artist_variants(artist)

        def add(row: dict[str, Any], label: str) -> None:
            track_id = int(row.get("id") or 0)
            if not track_id or track_id in skip or track_id in seen:
                return
            seen[track_id] = (row, label)

        # 1. Базовое название и исполнитель из перечисления.
        if wanted_title and artists:
            for row in self.by_title.get(wanted_title, ()):
                row_artist = _clean(str(row.get("artist") or ""))
                if row_artist and row_artist in artists:
                    add(row, MATCH_BY_TITLE_ARTIST)

        # 2. Базовое название, исполнитель любой: пусть пользователь посмотрит.
        if wanted_title:
            for row in self.by_title.get(wanted_title, ()):
                add(row, MATCH_BY_TITLE_BASE)

        # 3. Нормализованное имя файла из пути плейлиста.
        if filename:
            for variant in normalize_filename(filename):
                for row in self.by_filename.get(variant, ()):
                    add(row, MATCH_BY_FILENAME)

        ordered = sorted(
            seen.values(),
            key=lambda pair: (
                (MATCH_BY_TITLE_ARTIST, MATCH_BY_TITLE_BASE, MATCH_BY_FILENAME).index(pair[1]),
                str(pair[0].get("path") or ""),
            ),
        )
        return ordered[:limit]

    def suggest_many(
        self,
        entries: Iterable[dict[str, Any]],
        *,
        limit: int = 5,
        exclude: set[int] | None = None,
    ) -> dict[str, list[tuple[dict[str, Any], str]]]:
        """Варианты сразу для пачки записей: ключ — поле ``raw_path``.

        Записи без ``raw_path`` пропускаются.
        """
        out: dict[str, list[tuple[dict[str, Any], str]]] = {}
        for entry in entries:
            key = str(entry.get("raw_path") or "")
            if not key:
                continue
            out[key] = self.suggest(
                title=entry.get("title"),
                artist=entry.get("artist"),
                filename=key,
                limit=limit,
                exclude=exclude,
            )
        return out
