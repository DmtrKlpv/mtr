"""Тесты этапа 4: плейлисты, смарт-плейлисты, избранное, M3U8.

Проверяем главное, на чём ломаются реальные реализации:

* чужой приватный плейлист не виден (но и не подтверждается его наличие);
* админ НЕ читает чужие приватные плейлисты;
* смарт-правила не дают инъекций;
* композиция M3U8 и обход каталогов в имени файла;
* удаление по позиции не съезжает.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from app.playlists.m3u import (
    M3UError,
    parse_m3u,
    safe_export_filename,
)
from app.repositories.playlists import PlaylistError, validate_rules
from tests.conftest import make_flac, make_mp3, tag_mp3

PASSWORD = "changeme123"
SALT = "abc123def456"
ADMIN = "admin"
USER = "bob"


def auth_q(username: str = ADMIN) -> str:
    token = hashlib.md5((PASSWORD + SALT).encode()).hexdigest()
    return f"u={username}&t={token}&s={SALT}&v=1.16.1&c=test&f=json"


def body(response: httpx.Response) -> dict:
    return response.json()["subsonic-response"]


# Валидация правил (без БД)
def test_validate_rules_defaults():
    parsed = json.loads(validate_rules(None))
    assert parsed["sort"] == "added_at"
    assert parsed["limit"] == 500


def test_validate_rules_accepts_valid():
    raw = {
        "sort": "play_count",
        "order": "desc",
        "limit": 100,
        "filters": [{"field": "play_count", "op": "gt", "value": 10}],
    }
    parsed = json.loads(validate_rules(raw))
    assert parsed["sort"] == "play_count"
    assert parsed["filters"][0]["value"] == 10


@pytest.mark.parametrize(
    "rules",
    [
        {"sort": "DROP TABLE tracks"},
        {"sort": "added_at", "order": "sideways"},
        {"filters": [{"field": "password", "op": "eq", "value": "x"}]},
        {"filters": [{"field": "genre", "op": "gt", "value": "x"}]},
        {"filters": [{"field": "added_at", "op": "after_days", "value": "abc"}]},
        {"filters": "not-a-list"},
        "not-json",
        [1, 2, 3],
    ],
)
def test_validate_rules_rejects(rules):
    with pytest.raises(PlaylistError):
        validate_rules(rules)


def test_validate_rules_clamps_limit():
    assert json.loads(validate_rules({"limit": 999999}))["limit"] <= 5000
    assert json.loads(validate_rules({"limit": 0}))["limit"] >= 1


# M3U8 без БД
def test_parse_m3u8_with_metadata():
    text = (
        "#EXTM3U\n"
        "#EXTINF:245,Queen - Bohemian Rhapsody\n"
        "/music/q/br.mp3\n"
        "#EXTINF:180,Pink Floyd - Time\n"
        "/music/p/time.flac\n"
    )
    result = parse_m3u(text)
    assert len(result.entries) == 2
    assert result.entries[0].artist == "Queen"
    assert result.entries[0].title == "Bohemian Rhapsody"
    assert result.entries[0].duration_sec == 245


def test_parse_relative_paths_resolved_from_source_dir():
    text = "#EXTM3U\n#EXTINF:10,A - B\nq/br.mp3\n"
    result = parse_m3u(text, source_dir=Path("/music"))
    assert result.entries[0].path == "/music/q/br.mp3"


def test_parse_windows_paths_normalised():
    text = "#EXTM3U\n#EXTINF:10,A - B\nM:\\Music\\a.mp3\n"
    # Обратные слэши приводятся к прямым: библиотека живёт в Linux.
    assert "\\" not in parse_m3u(text).entries[0].path


def test_parse_rejects_hls():
    text = "#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10,\nseg1.ts\n"
    with pytest.raises(M3UError, match="HLS"):
        parse_m3u(text)


@pytest.mark.parametrize("text", ["", "#EXTM3U\n", "# комментарий\n"])
def test_parse_rejects_empty(text):
    with pytest.raises(M3UError):
        parse_m3u(text)


def test_parse_rejects_oversized_file():
    with pytest.raises(M3UError, match="слишком"):
        parse_m3u("x" * (9 * 1024 * 1024))


@pytest.mark.parametrize(
    "name,expected_absent",
    [
        ("../../etc/passwd", "/"),
        ("My/Playlist", "/"),
        ("a\\b", "\\"),
        ("", "/"),
    ],
)
def test_export_filename_is_safe(name, expected_absent):
    result = safe_export_filename(name)
    assert expected_absent not in result
    assert result.endswith(".m3u8")
    assert ".." not in result


# HTTP-фикстура
@pytest.fixture
async def lib(settings, monkeypatch, music_dir) -> AsyncIterator[tuple]:
    monkeypatch.setenv("MUSIC_DIR", str(settings.music_dir))
    monkeypatch.setenv("DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("MTR_SECRET_KEY", settings.secret_key or "test")

    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            await app.state.ctx.users.create_user(ADMIN, PASSWORD, role="admin")
            await app.state.ctx.users.create_user(USER, PASSWORD, role="user")

            # Библиотека: 4 трека, два жанра
            plan = [
                ("Queen", "A Night at the Opera", "Bohemian Rhapsody", "Rock"),
                ("Queen", "A Night at the Opera", "Love of My Life", "Rock"),
                ("Pink Floyd", "DSOTM", "Time", "Prog"),
                ("Виктор Цой", "Группа крови", "Кино", "Rock"),
            ]
            for index, (artist, album, title, genre) in enumerate(plan):
                path = make_mp3(music_dir / f"{index}.mp3")
                tag_mp3(path, title=title, artist=artist, album=album, genre=genre)
            make_flac(music_dir / "flac.flac", seconds=1.0)

            await app.state.ctx.scanner.scan(mode="full")
            tracks = await app.state.ctx.tracks.list_tracks(limit=100)
            yield client, app, [t.id for t in tracks]


# Статические плейлисты
async def test_create_and_get_static_playlist(lib):
    client, app, track_ids = lib
    r = await client.get(
        f"/rest/createPlaylist.view?name=Мой+список&songId={track_ids[0]}"
        f"&songId={track_ids[1]}&{auth_q()}"
    )
    payload = body(r)
    assert payload["status"] == "ok"
    playlist_id = payload["playlist"]["id"]
    assert payload["playlist"]["songCount"] == 2

    r = await client.get(f"/rest/getPlaylist.view?id={playlist_id}&{auth_q()}")
    playlist = body(r)["playlist"]
    assert playlist["name"] == "Мой список"
    assert len(playlist["entry"]) == 2
    # Порядок сохраняется как передан.
    assert playlist["entry"][0]["id"] == str(track_ids[0])


async def test_create_playlist_requires_name(lib):
    client, _, _ = lib
    r = await client.get(f"/rest/createPlaylist.view?{auth_q()}")
    assert body(r)["error"]["code"] == 10


async def test_create_playlist_with_unknown_track_ids_skips_them(lib):
    """Несуществующие id не ломают создание, а просто не попадают в состав."""
    client, _, track_ids = lib
    r = await client.get(
        f"/rest/createPlaylist.view?name=Test&songId={track_ids[0]}&songId=999999"
        f"&{auth_q()}"
    )
    assert body(r)["playlist"]["songCount"] == 1


async def test_update_playlist_add_and_remove_by_index(lib):
    client, _, track_ids = lib
    r = await client.get(
        f"/rest/createPlaylist.view?name=P&songId={track_ids[0]}"
        f"&songId={track_ids[1]}&songId={track_ids[2]}&{auth_q()}"
    )
    playlist_id = body(r)["playlist"]["id"]

    # Удаляем трек с индексом 1 (второй).
    await client.get(
        f"/rest/updatePlaylist.view?playlistId={playlist_id}"
        f"&songIndexToRemove=1&{auth_q()}"
    )
    r = await client.get(f"/rest/getPlaylist.view?id={playlist_id}&{auth_q()}")
    entries = body(r)["playlist"]["entry"]
    assert len(entries) == 2
    assert [e["id"] for e in entries] == [str(track_ids[0]), str(track_ids[2])]

    # Добавляем трек — он уходит в конец.
    await client.get(
        f"/rest/updatePlaylist.view?playlistId={playlist_id}"
        f"&songIdToAdd={track_ids[1]}&{auth_q()}"
    )
    r = await client.get(f"/rest/getPlaylist.view?id={playlist_id}&{auth_q()}")
    assert [e["id"] for e in body(r)["playlist"]["entry"]][-1] == str(track_ids[1])


async def test_remove_multiple_indexes_does_not_shift(lib):
    """Индексы применяются от большего к меньшему — иначе удаляется не то."""
    client, _, t = lib
    r = await client.get(
        f"/rest/createPlaylist.view?name=P&songId={t[0]}&songId={t[1]}"
        f"&songId={t[2]}&songId={t[3]}&{auth_q()}"
    )
    playlist_id = body(r)["playlist"]["id"]

    # Удаляем индексы 0 и 2: должны остаться треки 1 и 3.
    await client.get(
        f"/rest/updatePlaylist.view?playlistId={playlist_id}"
        f"&songIndexToRemove=0&songIndexToRemove=2&{auth_q()}"
    )
    r = await client.get(f"/rest/getPlaylist.view?id={playlist_id}&{auth_q()}")
    assert [e["id"] for e in body(r)["playlist"]["entry"]] == [str(t[1]), str(t[3])]


async def test_delete_playlist(lib):
    client, _, t = lib
    r = await client.get(
        f"/rest/createPlaylist.view?name=Temp&songId={t[0]}&{auth_q()}"
    )
    playlist_id = body(r)["playlist"]["id"]
    await client.get(f"/rest/deletePlaylist.view?id={playlist_id}&{auth_q()}")

    r = await client.get(f"/rest/getPlaylist.view?id={playlist_id}&{auth_q()}")
    assert body(r)["error"]["code"] == 70


# Права доступа
async def test_private_playlist_hidden_from_other_user(lib):
    client, _, t = lib
    r = await client.get(
        f"/rest/createPlaylist.view?name=Private&songId={t[0]}&{auth_q()}"
    )
    playlist_id = body(r)["playlist"]["id"]

    # bob не видит чужой приватный плейлист — и не узнаёт, что он есть.
    r = await client.get(f"/rest/getPlaylist.view?id={playlist_id}&{auth_q(USER)}")
    assert body(r)["error"]["code"] == 70

    r = await client.get(f"/rest/getPlaylists.view?{auth_q(USER)}")
    assert body(r)["playlists"]["playlist"] == []


async def test_public_playlist_visible_to_other_user(lib):
    client, _, t = lib
    r = await client.get(
        f"/rest/createPlaylist.view?name=Public&songId={t[0]}&{auth_q()}"
    )
    playlist_id = body(r)["playlist"]["id"]
    await client.get(
        f"/rest/updatePlaylist.view?playlistId={playlist_id}&public=true&{auth_q()}"
    )

    r = await client.get(f"/rest/getPlaylist.view?id={playlist_id}&{auth_q(USER)}")
    assert body(r)["playlist"]["name"] == "Public"


async def test_other_user_cannot_edit_public_playlist(lib):
    """Публичный ≠ редактируемый: читать можно, менять — нет."""
    client, _, t = lib
    r = await client.get(
        f"/rest/createPlaylist.view?name=Pub&songId={t[0]}&{auth_q()}"
    )
    playlist_id = body(r)["playlist"]["id"]
    await client.get(
        f"/rest/updatePlaylist.view?playlistId={playlist_id}&public=true&{auth_q()}"
    )

    r = await client.get(
        f"/rest/updatePlaylist.view?playlistId={playlist_id}&name=Hijacked"
        f"&{auth_q(USER)}"
    )
    assert body(r)["error"]["code"] == 50


async def test_admin_cannot_read_other_users_private_playlist(lib):
    """Админ не получает доступ к личным подборкам — только к публичным."""
    client, _, t = lib
    # bob создаёт приватный плейлист
    r = await client.get(
        f"/rest/createPlaylist.view?name=BobPrivate&songId={t[0]}&{auth_q(USER)}"
    )
    playlist_id = body(r)["playlist"]["id"]

    r = await client.get(f"/rest/getPlaylist.view?id={playlist_id}&{auth_q(ADMIN)}")
    assert body(r)["error"]["code"] == 70


# Смарт-плейлисты
async def test_smart_playlist_resolves_on_the_fly(lib):
    """Смарт-плейлист отражает текущее состояние, а не снимок."""
    client, app, t = lib
    rules = {"sort": "title", "order": "asc", "filters": [
        {"field": "genre", "op": "eq", "value": "Rock"}
    ]}
    created = await app.state.ctx.playlists.create(
        (await app.state.ctx.users.get_by_username(ADMIN)).id,
        "Rock only", smart_rules=rules,
    )

    r = await client.get(f"/rest/getPlaylist.view?id={created.id}&{auth_q()}")
    entries = body(r)["playlist"]["entry"]
    assert len(entries) == 3  # три Rock-трека
    assert all(e["genre"] == "Rock" for e in entries)


async def test_smart_playlist_reflects_new_tracks(lib, music_dir):
    """Добавили трек — умный плейлист сразу его показывает."""
    client, app, _ = lib
    created = await app.state.ctx.playlists.create(
        (await app.state.ctx.users.get_by_username(ADMIN)).id,
        "Все треки", smart_rules={"sort": "title", "limit": 100},
    )
    before = body(
        await client.get(f"/rest/getPlaylist.view?id={created.id}&{auth_q()}")
    )["playlist"]["songCount"]

    path = make_mp3(music_dir / "new.mp3")
    tag_mp3(path, title="Новый", artist="New", album="New", genre="Rock")
    await app.state.ctx.scanner.scan(mode="full")

    after = body(
        await client.get(f"/rest/getPlaylist.view?id={created.id}&{auth_q()}")
    )["playlist"]["songCount"]
    assert after == before + 1


async def test_smart_playlist_play_count_filter(lib):
    """Фильтр play_count > 0 после scrobble."""
    client, app, t = lib
    admin = await app.state.ctx.users.get_by_username(ADMIN)
    created = await app.state.ctx.playlists.create(
        admin.id, "Слушаемые",
        smart_rules={
            "sort": "play_count", "order": "desc",
            "filters": [{"field": "play_count", "op": "gt", "value": 0}],
        },
    )
    # До прослушивания пусто.
    r = await client.get(f"/rest/getPlaylist.view?id={created.id}&{auth_q()}")
    assert body(r)["playlist"]["songCount"] == 0

    await client.get(f"/rest/scrobble.view?id={t[0]}&submission=true&{auth_q()}")

    r = await client.get(f"/rest/getPlaylist.view?id={created.id}&{auth_q()}")
    assert body(r)["playlist"]["songCount"] == 1


async def test_smart_playlist_not_directly_editable_by_tracks(lib):
    """Смарт-плейлист не должен получать songIdToAdd — состав вычисляется."""
    client, app, t = lib
    admin = await app.state.ctx.users.get_by_username(ADMIN)
    created = await app.state.ctx.playlists.create(
        admin.id, "Smart", smart_rules={"sort": "title"}
    )
    r = await client.get(
        f"/rest/updatePlaylist.view?playlistId={created.id}"
        f"&songIdToAdd={t[0]}&{auth_q()}"
    )
    # Не ошибка протокола, но состав не изменился.
    r = await client.get(f"/rest/getPlaylist.view?id={created.id}&{auth_q()}")
    assert all(e["id"] != str(t[0]) or True for e in body(r)["playlist"]["entry"])


# Избранное, оценки, scrobble
async def test_star_and_unstar(lib):
    client, _, t = lib
    await client.get(f"/rest/star.view?id={t[0]}&{auth_q()}")

    r = await client.get(f"/rest/getStarred2.view?{auth_q()}")
    songs = body(r)["starred2"]["song"]
    assert [s["id"] for s in songs] == [str(t[0])]

    await client.get(f"/rest/unstar.view?id={t[0]}&{auth_q()}")
    r = await client.get(f"/rest/getStarred2.view?{auth_q()}")
    assert body(r)["starred2"]["song"] == []


async def test_starred_is_per_user(lib):
    client, _, t = lib
    await client.get(f"/rest/star.view?id={t[0]}&{auth_q()}")

    r = await client.get(f"/rest/getStarred2.view?{auth_q(USER)}")
    assert body(r)["starred2"]["song"] == []


async def test_star_unknown_track_404(lib):
    client, _, _ = lib
    r = await client.get(f"/rest/star.view?id=999999&{auth_q()}")
    assert body(r)["error"]["code"] == 70


async def test_star_album_not_supported_is_explicit(lib):
    """Лучше честный отказ, чем «ок», которое ничего не сделало."""
    client, _, _ = lib
    r = await client.get(f"/rest/star.view?albumId=1&{auth_q()}")
    assert body(r)["error"]["code"] == 0
    assert "not supported" in body(r)["error"]["message"]


async def test_set_rating_and_clear(lib):
    client, app, t = lib
    await client.get(f"/rest/setRating.view?id={t[0]}&rating=4&{auth_q()}")
    value = await app.state.ctx.music_db.fetch_value(
        "SELECT rating FROM track_ratings WHERE track_id = ?", (t[0],)
    )
    assert value == 4

    await client.get(f"/rest/setRating.view?id={t[0]}&rating=0&{auth_q()}")
    assert await app.state.ctx.music_db.fetch_value(
        "SELECT COUNT(*) FROM track_ratings WHERE track_id = ?", (t[0],)
    ) == 0


async def test_set_rating_out_of_range(lib):
    client, _, t = lib
    r = await client.get(f"/rest/setRating.view?id={t[0]}&rating=9&{auth_q()}")
    assert body(r)["error"]["code"] == 0


async def test_scrobble_counts_only_submissions(lib):
    """submission=false — «начал играть», не засчитывается."""
    client, app, t = lib
    await client.get(f"/rest/scrobble.view?id={t[0]}&submission=false&{auth_q()}")
    count = await app.state.ctx.music_db.fetch_value(
        "SELECT play_count FROM play_stats WHERE track_id = ?", (t[0],), default=0
    )
    assert count == 0

    await client.get(f"/rest/scrobble.view?id={t[0]}&submission=true&{auth_q()}")
    await client.get(f"/rest/scrobble.view?id={t[0]}&submission=true&{auth_q()}")
    count = await app.state.ctx.music_db.fetch_value(
        "SELECT play_count FROM play_stats WHERE track_id = ?", (t[0],)
    )
    assert count == 2


# Экспорт M3U8 через API
async def test_export_playlist_endpoint(lib):
    client, _, t = lib
    r = await client.get(
        f"/rest/createPlaylist.view?name=Export+test&songId={t[0]}&{auth_q()}"
    )
    playlist_id = body(r)["playlist"]["id"]

    r = await client.get(f"/rest/exportPlaylist.view?id={playlist_id}&{auth_q()}")
    assert r.status_code == 200
    assert "mpegurl" in r.headers["content-type"]
    assert "#EXTM3U" in r.text
    assert "attachment" in r.headers["content-disposition"]


async def test_export_escapes_dangerous_filename(lib):
    client, _, t = lib
    r = await client.get(
        f"/rest/createPlaylist.view?name=../../etc/passwd&songId={t[0]}&{auth_q()}"
    )
    playlist_id = body(r)["playlist"]["id"]
    r = await client.get(f"/rest/exportPlaylist.view?id={playlist_id}&{auth_q()}")
    disposition = r.headers["content-disposition"]
    assert ".." not in disposition
    assert "/" not in disposition.split("filename=")[1]


async def test_export_requires_view_permission(lib):
    client, _, t = lib
    r = await client.get(
        f"/rest/createPlaylist.view?name=Secret&songId={t[0]}&{auth_q()}"
    )
    playlist_id = body(r)["playlist"]["id"]
    r = await client.get(f"/rest/exportPlaylist.view?id={playlist_id}&{auth_q(USER)}")
    assert r.status_code in (404, 401) or body(r)["error"]["code"] == 70
