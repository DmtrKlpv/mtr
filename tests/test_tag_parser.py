"""Тесты парсера тегов на настоящих файлах, сгенерированных ffmpeg."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.scanner.tag_parser import (
    MAX_STRING_LEN,
    clean_genre,
    clean_text,
    is_supported_audio,
    parse_track,
    to_int,
    to_year,
)
from tests.conftest import make_flac, make_m4a, make_mp3, tag_flac, tag_mp3

pytestmark = pytest.mark.skipif(
    not Path(__import__("imageio_ffmpeg").get_ffmpeg_exe()).exists(),
    reason="ffmpeg недоступен",
)


# Чистые нормализаторы (без файлов)
@pytest.mark.parametrize(
    "value,expected",
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("Title", "Title"),
        (["A", "B"], "A, B"),
        (["A", "A"], "A"),
        ([], None),
        ("\x00bad\x07value", "badvalue"),
        ("a" * (MAX_STRING_LEN + 100), "a" * MAX_STRING_LEN),
        (b"bytes", "bytes"),
        (123, "123"),
    ],
)
def test_clean_text(value, expected):
    assert clean_text(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [("3", 3), ("03", 3), ("3/12", 3), ("3 of 12", 3), ("", None), (None, None),
     ("abc", None), ("-1", None)],
)
def test_to_int(value, expected):
    assert to_int(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [("1998", 1998), ("1998-05-04", 1998), ("", None), ("nope", None),
     ("999", None), ("3000", None)],
)
def test_to_year(value, expected):
    assert to_year(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [("(17)Rock", "Rock"), ("(17)", None), ("Metal", "Metal"), ("", None)],
)
def test_clean_genre(value, expected):
    assert clean_genre(value) == expected


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/music/a/song.mp3", True),
        ("/music/a/song.FLAC", True),
        ("/music/a/song.opus", True),
        ("/music/a/song.txt", False),
        ("/music/a/.hidden.mp3", False),
        ("/music/a/cover.jpg", False),
    ],
)
def test_is_supported_audio(path, expected):
    assert is_supported_audio(path) is expected


# MP3 / ID3
def test_mp3_full_tags(music_dir: Path):
    path = make_mp3(music_dir / "artist" / "album" / "01-song.mp3", seconds=1.5)
    tag_mp3(
        path,
        title="Тестовая Песня",
        artist="Исполнитель",
        album="Альбом",
        track="3/12",
        year="1998-05-04",
        genre="(17)Rock",
    )

    meta = parse_track(path)

    assert meta.ok is True
    assert meta.error is None
    assert meta.title == "Тестовая Песня"
    assert meta.artist == "Исполнитель"
    assert meta.album == "Альбом"
    assert meta.album_artist == "Исполнитель"  # fallback от artist
    assert meta.track_number == 3
    assert meta.year == 1998
    assert meta.genre == "Rock"
    assert meta.path == str(path)
    assert meta.filename == "01-song.mp3"
    assert meta.size_bytes > 0
    assert meta.mtime > 0
    assert meta.duration_ms > 1000  # 1.5 c, с допуском на энкодер
    assert meta.bitrate and meta.bitrate > 0
    assert meta.sample_rate and meta.sample_rate > 0
    assert meta.channels == 1  # sine моно
    assert meta.format  # mime или расширение
    assert meta.has_cover is False


def test_mp3_without_tags_falls_back_to_filename(music_dir: Path):
    path = make_mp3(music_dir / "Untagged Track.mp3")
    meta = parse_track(path)

    assert meta.ok is True
    assert meta.title == "Untagged Track"
    assert meta.artist is None
    assert meta.album is None
    assert meta.year is None


def test_mp3_with_cover_flags_has_cover(music_dir: Path):
    path = make_mp3(music_dir / "with-cover.mp3")
    # Минимальный валидный JPEG-заголовок — mutagen не проверяет содержимое.
    fake_jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9"
    tag_mp3(path, title="Cover", cover=fake_jpeg)

    meta = parse_track(path)
    assert meta.has_cover is True
    assert meta.cover_mime == "image/jpeg"


def test_mp3_unicode_tags_survive(music_dir: Path):
    """Кириллица, эмодзи и длинные строки не должны ломать разбор."""
    path = make_mp3(music_dir / "unicode.mp3")
    long_title = "Ж" * 400
    tag_mp3(path, title=long_title, artist="Штиль 🌊", album="Альбом №1")

    meta = parse_track(path)
    assert meta.ok
    assert meta.artist == "Штиль 🌊"
    assert meta.album == "Альбом №1"
    assert len(meta.title) == 400


# FLAC / Vorbis
def test_flac_tags(music_dir: Path):
    path = make_flac(music_dir / "lossless.flac")
    tag_flac(
        path,
        title="Lossless",
        artist="FLAC Artist",
        album="FLAC Album",
        track="7",
        year="2001",
        genre="Ambient",
    )

    meta = parse_track(path)
    assert meta.ok
    assert meta.title == "Lossless"
    assert meta.artist == "FLAC Artist"
    assert meta.album == "FLAC Album"
    assert meta.track_number == 7
    assert meta.year == 2001
    assert meta.genre == "Ambient"
    assert meta.duration_ms > 0
    assert meta.bitrate is not None  # FLAC тоже отдаёт bitrate


# M4A / AAC
def test_m4a_tags(music_dir: Path):
    path = make_m4a(music_dir / "aac.m4a")
    from mutagen.mp4 import MP4

    audio = MP4(str(path))
    audio["\xa9nam"] = ["AAC Title"]
    audio["\xa9ART"] = ["AAC Artist"]
    audio["\xa9alb"] = ["AAC Album"]
    audio["trkn"] = [(4, 10)]
    audio["disk"] = [(1, 2)]
    audio["\xa9day"] = ["2015"]
    audio.save()

    meta = parse_track(path)
    assert meta.ok
    assert meta.title == "AAC Title"
    assert meta.artist == "AAC Artist"
    assert meta.album == "AAC Album"
    assert meta.track_number == 4
    assert meta.disc_number == 1
    assert meta.year == 2015


# Отказоустойчивость
def test_missing_file_returns_error(music_dir: Path):
    meta = parse_track(music_dir / "nope.mp3")
    assert meta.ok is False
    assert "не найден" in (meta.error or "")


def test_empty_file_returns_error(music_dir: Path):
    path = music_dir / "empty.mp3"
    path.write_bytes(b"")
    meta = parse_track(path)
    assert meta.ok is False
    assert "нулевого размера" in (meta.error or "")


def test_garbage_mp3_returns_error_not_exception(music_dir: Path):
    """Мусорные байты не должны выбрасывать исключение наружу."""
    path = music_dir / "garbage.mp3"
    path.write_bytes(b"this is definitely not an mp3 frame" * 100)

    meta = parse_track(path)
    # mutagen с большой вероятностью не откроет файл -> ok=False.
    # Если всё же открыл, метаданные должны быть согласованы.
    if not meta.ok:
        assert meta.error
    else:
        assert meta.title


def test_truncated_mp3_still_parsed(music_dir: Path):
    """Обрезанный файл: длительность может быть 0, но трек не теряется."""
    path = make_mp3(music_dir / "truncated.mp3", seconds=2.0)
    tag_mp3(path, title="Truncated", artist="A")
    data = path.read_bytes()
    path.write_bytes(data[: len(data) // 2])

    meta = parse_track(path)
    assert meta.ok is True
    assert meta.title == "Truncated"
    assert meta.duration_ms >= 0


def test_directory_is_rejected(music_dir: Path):
    meta = parse_track(music_dir)
    assert meta.ok is False
    assert "обычным файлом" in (meta.error or "")


def test_parser_never_raises_on_random_suffix(music_dir: Path):
    """Неизвестное расширение: универсальный путь не должен падать."""
    path = music_dir / "weird.xyz"
    path.write_bytes(b"random")
    meta = parse_track(path)  # не должно бросить
    assert isinstance(meta.ok, bool)


# Регрессия: парсер не привязан к расширению при чтении info
def test_duration_is_milliseconds_int(music_dir: Path):
    path = make_mp3(music_dir / "dur.mp3", seconds=3.0)
    meta = parse_track(path)
    assert isinstance(meta.duration_ms, int)
    assert 2500 < meta.duration_ms < 3500
