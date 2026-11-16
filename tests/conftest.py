"""Общие фикстуры: реальные аудиофайлы, изолированные базы, AppContext.

Аудио генерируется настоящим ffmpeg (синусоида), а теги проставляются
mutagen — так тесты проверяют именно тот код, который пойдёт в прод,
а не заглушки.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import imageio_ffmpeg
import pytest

from app.bootstrap import AppContext
from app.config import Settings

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


def _run_ffmpeg(*args: str) -> None:
    """Вызов ffmpeg с падением при ошибке и захватом stderr."""
    proc = subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", *args],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg упал: {proc.stderr}")


def make_mp3(path: Path, *, seconds: float = 1.0, bitrate: str = "128k") -> Path:
    """Генерирует валидный MP3 с синусоидой."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg(
        "-f", "lavfi",
        "-i", f"sine=frequency=440:duration={seconds}",
        "-c:a", "libmp3lame",
        "-b:a", bitrate,
        str(path),
    )
    return path


def make_flac(path: Path, *, seconds: float = 1.0) -> Path:
    """Генерирует валидный FLAC."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg(
        "-f", "lavfi",
        "-i", f"sine=frequency=440:duration={seconds}",
        "-c:a", "flac",
        str(path),
    )
    return path


def make_m4a(path: Path, *, seconds: float = 1.0) -> Path:
    """Генерирует валидный M4A (AAC)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg(
        "-f", "lavfi",
        "-i", f"sine=frequency=440:duration={seconds}",
        "-c:a", "aac",
        "-b:a", "128k",
        str(path),
    )
    return path


def tag_mp3(
    path: Path,
    *,
    title: str,
    artist: str | None = None,
    album: str | None = None,
    track: str | None = None,
    year: str | None = None,
    genre: str | None = None,
    cover: bytes | None = None,
) -> None:
    """Проставляет ID3-теги через mutagen."""
    from mutagen.id3 import ID3, ID3NoHeaderError, TALB, TCON, TDRC, TIT2, TPE1, TRCK
    from mutagen.id3 import APIC

    try:
        tags = ID3(str(path))
    except ID3NoHeaderError:
        tags = ID3()

    tags.delall("TIT2")
    tags.add(TIT2(encoding=3, text=title))
    if artist:
        tags.delall("TPE1")
        tags.add(TPE1(encoding=3, text=artist))
    if album:
        tags.delall("TALB")
        tags.add(TALB(encoding=3, text=album))
    if track:
        tags.delall("TRCK")
        tags.add(TRCK(encoding=3, text=track))
    if year:
        tags.delall("TDRC")
        tags.add(TDRC(encoding=3, text=year))
    if genre:
        tags.delall("TCON")
        tags.add(TCON(encoding=3, text=genre))
    if cover:
        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover))
    tags.save(str(path), v2_version=3)


def tag_flac(
    path: Path,
    *,
    title: str,
    artist: str | None = None,
    album: str | None = None,
    track: str | None = None,
    year: str | None = None,
    genre: str | None = None,
) -> None:
    """Проставляет Vorbis-комментарии во FLAC."""
    from mutagen.flac import FLAC

    audio = FLAC(str(path))
    audio.delete()
    audio["title"] = title
    if artist:
        audio["artist"] = artist
    if album:
        audio["album"] = album
    if track:
        audio["tracknumber"] = track
    if year:
        audio["date"] = year
    if genre:
        audio["genre"] = genre
    audio.save()


@pytest.fixture
def music_dir(tmp_path: Path) -> Path:
    """Изолированный каталог «библиотеки»."""
    root = tmp_path / "music"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def settings(tmp_path: Path, music_dir: Path) -> Settings:
    """Конфиг, указывающий на временные БД и каталог музыки."""
    data_dir = tmp_path / "data"
    return Settings(
        music_dir=music_dir,
        data_dir=data_dir,
        users_db=data_dir / "mtr_users.db",
        music_db=data_dir / "mtr_music.db",
        covers_dir=data_dir / "covers",
        secret_key="test-secret-key-do-not-use-in-prod",
        secret_key_file=data_dir / "secret.key",
    )


@pytest.fixture
async def ctx(settings: Settings) -> AppContext:
    """Полностью инициализированный контекст на временных файлах."""
    context = await AppContext.create(settings)
    try:
        yield context
    finally:
        await context.close()


@pytest.fixture(scope="session", autouse=True)
def _ffmpeg_on_path(tmp_path_factory) -> None:
    """Команда 'ffmpeg' должна резолвиться в PATH (как в Docker).

    В тестовом окружении без системного ffmpeg подкладываем симлинк
    на бинарник из imageio-ffmpeg. Продуктовый код не меняется.
    """
    if shutil.which("ffmpeg"):
        return
    bin_dir = tmp_path_factory.mktemp("ffbin")
    (bin_dir / "ffmpeg").symlink_to(FFMPEG)
    os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
