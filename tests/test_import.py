"""Тесты импорта плейлистов из папки и нормализации путей."""

from __future__ import annotations


import pytest

from app.playlists.importer import (
    ImportError_,
    apply_prefix,
    find_playlist_files,
    normalize_path,
    playlist_name,
    read_playlist,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("/music/All/x.mp3", "/music/All/x.mp3"),
        ("M:\\Music\\All\\x.mp3", "M:/Music/All/x.mp3"),
        ("/music//a///b.mp3", "/music/a/b.mp3"),
        ("/music/All/../All/y.mp3", "/music/All/y.mp3"),
        ("All/z.mp3", "All/z.mp3"),
        ("", ""),
    ],
)
def test_normalize_path(raw, expected):
    assert normalize_path(raw) == expected


def test_apply_prefix_replaces_case_insensitively():
    assert apply_prefix("M:/Music/All/x.mp3", "M:/Music", "/music") == "/music/All/x.mp3"
    assert apply_prefix("m:/music/A/b.mp3", "M:/Music", "/music") == "/music/A/b.mp3"


def test_apply_prefix_leaves_foreign_paths():
    assert apply_prefix("/other/x.mp3", "M:/Music", "/music") == "/other/x.mp3"


def test_playlist_name_from_file(tmp_path):
    assert playlist_name(tmp_path / "Рок 2020.m3u8") == "Рок 2020"


def test_playlist_name_falls_back_to_folder(tmp_path):
    folder = tmp_path / "MyList"
    folder.mkdir()
    # Файл без имени: только расширение.
    assert playlist_name(folder / ".m3u8") == "MyList"


def test_find_playlists_recurses_subfolders(tmp_path):
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    (tmp_path / "top.m3u").write_text("#EXTM3U\n/x.mp3\n", encoding="utf-8")
    (deep / "deep.m3u8").write_text("#EXTM3U\n/y.mp3\n", encoding="utf-8")
    (tmp_path / "note.txt").write_text("не плейлист", encoding="utf-8")

    found, warnings = find_playlist_files(tmp_path)
    assert {f.name for f in found} == {"top.m3u", "deep.m3u8"}
    assert warnings == []


def test_find_playlists_skips_service_dirs(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "x.m3u").write_text("#EXTM3U\n/a.mp3\n", encoding="utf-8")
    (tmp_path / "ok.m3u").write_text("#EXTM3U\n/b.mp3\n", encoding="utf-8")
    found, _ = find_playlist_files(tmp_path)
    assert [f.name for f in found] == ["ok.m3u"]


def test_find_playlists_missing_dir_raises(tmp_path):
    with pytest.raises(ImportError_):
        find_playlist_files(tmp_path / "nope")


def test_read_playlist_parses_user_example(tmp_path):
    """Формат из задачи: абсолютные пути внутри контейнера."""
    content = (
        "#EXTM3U\n"
        "/music/All/All Eyes On Africa/Alcina - JORO.mp3\n"
        "/music/All/All Eyes On Africa/Burna Boy - Anybody.mp3\n"
        "/music/All/All Eyes On Africa/DAMSO - Œveillé.mp3\n"
    )
    path = tmp_path / "All Eyes On Africa.m3u8"
    path.write_text(content, encoding="utf-8")

    entries, error = read_playlist(path, tmp_path)
    assert error is None
    assert len(entries) == 3
    assert entries[0].path == "/music/All/All Eyes On Africa/Alcina - JORO.mp3"
    assert entries[2].path.endswith("DAMSO - Œveillé.mp3")


def test_read_playlist_cp1251_fallback(tmp_path):
    """Старые Windows-плейлисты в cp1251 не должны превращаться в мусор."""
    path = tmp_path / "old.m3u"
    path.write_bytes("#EXTM3U\n/music/Песня.mp3\n".encode("cp1251"))
    entries, error = read_playlist(path, tmp_path)
    assert error is None
    assert entries[0].path.endswith("Песня.mp3")


def test_read_playlist_empty_gives_error(tmp_path):
    path = tmp_path / "empty.m3u8"
    path.write_text("#EXTM3U\n", encoding="utf-8")
    entries, error = read_playlist(path, tmp_path)
    assert entries == []
    assert error


# Регрессия: плейлисты импортировались дважды
def test_same_name_m3u_and_m3u8_collapse_to_one(tmp_path):
    """X.m3u и X.m3u8 — один плейлист, а не два.

    Многие программы пишут плейлист сразу в двух форматах. Имя плейлиста
    берётся из имени файла без расширения, поэтому оба давали «X».
    """
    content = "#EXTM3U\n/music/a.mp3\n"
    (tmp_path / "311.m3u").write_text(content, encoding="utf-8")
    (tmp_path / "311.m3u8").write_text(content, encoding="utf-8")

    found, warnings = find_playlist_files(tmp_path)
    assert len(found) == 1, [f.name for f in found]
    # .m3u8 приоритетнее: гарантированно UTF-8.
    assert found[0].suffix == ".m3u8"
    assert any("дубликат" in w for w in warnings)


def test_same_name_in_different_dirs_stays_separate(tmp_path):
    """Одинаковые имена в разных каталогах — разные плейлисты."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "Mix.m3u8").write_text("#EXTM3U\n/x.mp3\n", encoding="utf-8")
    (tmp_path / "b" / "Mix.m3u8").write_text("#EXTM3U\n/y.mp3\n", encoding="utf-8")

    found, _ = find_playlist_files(tmp_path)
    assert len(found) == 2
    assert {playlist_name(f) for f in found} == {"Mix"}
