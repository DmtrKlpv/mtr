from pathlib import Path

from app.playlists.importer import apply_prefix, normalize_path
from app.playlists.m3u import parse_m3u


def test_drive_letter_path_is_kept_as_is():
    result = parse_m3u("#EXTM3U\nM:\\Music\\Artist\\a.mp3\n", source_dir=Path("/music/Mix"))
    assert [e.path for e in result.entries] == ["M:/Music/Artist/a.mp3"]


def test_drive_letter_path_maps_to_library_prefix():
    result = parse_m3u("M:\\Music\\Artist\\a.mp3\n", source_dir=Path("/music/Mix"))
    path = apply_prefix(normalize_path(result.entries[0].path), "M:/Music", "/music")
    assert path == "/music/Artist/a.mp3"


def test_relative_windows_path_still_resolves_from_playlist_dir():
    result = parse_m3u("Artist\\Album\\01.flac\n", source_dir=Path("/music/Mix"))
    assert [e.path for e in result.entries] == ["/music/Mix/Artist/Album/01.flac"]
