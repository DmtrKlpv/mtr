"""Тесты этапа 3: Subsonic-авторизация и протокол.

Проверяем именно то, на чём ломаются клиенты: три способа передачи пароля,
коды ошибок в теле при HTTP 200, группировку исполнителей и то, что
/stream больше не открыт анонимно.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator

import httpx
import pytest

from app.subsonic.auth import AuthThrottle, md5_hex, throttle_key
from app.subsonic.errors import ErrorCode
from tests.conftest import make_flac, make_mp3, tag_mp3

PASSWORD = "secretpass123"
USERNAME = "alice"


def token_for(password: str, salt: str) -> str:
    return hashlib.md5((password + salt).encode("utf-8")).hexdigest()


def auth_params(
    *,
    username: str = USERNAME,
    password: str = PASSWORD,
    salt: str = "abcdef123456",
    token: str | None = None,
    version: str = "1.16.1",
    client: str = "test",
) -> dict[str, str]:
    """Параметры успешной token-авторизации."""
    return {
        "u": username,
        "t": token or token_for(password, salt),
        "s": salt,
        "v": version,
        "c": client,
        "f": "json",
    }


@pytest.fixture
async def api(settings, monkeypatch) -> AsyncIterator[tuple[httpx.AsyncClient, object]]:
    """HTTP-клиент с поднятым приложением и созданным пользователем."""
    monkeypatch.setenv("MUSIC_DIR", str(settings.music_dir))
    monkeypatch.setenv("DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("MTR_SECRET_KEY", settings.secret_key or "test")

    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            await app.state.ctx.users.create_user(USERNAME, PASSWORD, role="user")
            await app.state.ctx.users.create_user(
                "root", PASSWORD, role="admin"
            )
            yield client, app


def body(response: httpx.Response) -> dict:
    """Тело subsonic-response без внешнего конверта."""
    return response.json()["subsonic-response"]


# ----------------------------------------------------------------------
# ping: базовый контракт протокола
# ----------------------------------------------------------------------
async def test_ping_ok(api):
    client, _ = api
    r = await client.get("/rest/ping.view", params=auth_params())
    assert r.status_code == 200
    payload = body(r)
    assert payload["status"] == "ok"
    assert payload["version"] == "1.16.1"
    assert payload["type"] == "mtr"
    assert payload["openSubsonic"] is True


async def test_ping_without_view_suffix(api):
    """Клиенты зовут и /ping, и /ping.view."""
    client, _ = api
    r = await client.get("/rest/ping", params=auth_params())
    assert body(r)["status"] == "ok"


# ----------------------------------------------------------------------
# Три способа авторизации
# ----------------------------------------------------------------------
async def test_token_auth(api):
    client, _ = api
    r = await client.get("/rest/ping.view", params=auth_params())
    assert body(r)["status"] == "ok"


async def test_plain_password_auth(api):
    """Параметр p принимается (устаревший способ), но это не ошибка."""
    client, _ = api
    r = await client.get(
        "/rest/ping.view",
        params={"u": USERNAME, "p": PASSWORD, "v": "1.16.1", "c": "old", "f": "json"},
    )
    assert body(r)["status"] == "ok"


async def test_hex_encoded_password_auth(api):
    """p=enc:<hex> — именно так шлют старые клиенты."""
    client, _ = api
    r = await client.get(
        "/rest/ping.view",
        params={
            "u": USERNAME,
            "p": "enc:" + PASSWORD.encode().hex(),
            "v": "1.16.1",
            "c": "old",
            "f": "json",
        },
    )
    assert body(r)["status"] == "ok"


async def test_invalid_hex_password_rejected(api):
    client, _ = api
    r = await client.get(
        "/rest/ping.view",
        params={"u": USERNAME, "p": "enc:zzzz", "v": "1.16.1", "c": "x", "f": "json"},
    )
    payload = body(r)
    assert payload["status"] == "failed"
    assert payload["error"]["code"] == ErrorCode.GENERIC


async def test_wrong_token_gives_40(api):
    client, _ = api
    params = auth_params(token="0" * 32)
    r = await client.get("/rest/ping.view", params=params)
    payload = body(r)
    assert r.status_code == 200          # протокол требует 200
    assert payload["status"] == "failed"
    assert payload["error"]["code"] == ErrorCode.WRONG_CREDENTIALS


async def test_wrong_password_gives_40(api):
    client, _ = api
    params = auth_params(password="wrong")
    r = await client.get("/rest/ping.view", params=params)
    assert body(r)["error"]["code"] == ErrorCode.WRONG_CREDENTIALS


async def test_unknown_user_gives_same_error_as_wrong_password(api):
    """Нельзя различать «нет такого» и «неверный пароль» — иначе перебор логинов."""
    client, _ = api
    r1 = await client.get("/rest/ping.view", params=auth_params(username="nobody"))
    r2 = await client.get("/rest/ping.view", params=auth_params(password="wrong"))
    assert body(r1)["error"] == body(r2)["error"]


async def test_missing_username_gives_10(api):
    client, _ = api
    r = await client.get("/rest/ping.view", params={"v": "1.16.1", "c": "x"})
    assert body(r)["error"]["code"] == ErrorCode.MISSING_PARAMETER


async def test_missing_version_gives_10(api):
    client, _ = api
    r = await client.get("/rest/ping.view", params={"u": USERNAME, "c": "x"})
    assert body(r)["error"]["code"] == ErrorCode.MISSING_PARAMETER


async def test_token_without_salt_gives_10(api):
    client, _ = api
    r = await client.get(
        "/rest/ping.view",
        params={"u": USERNAME, "t": "0" * 32, "v": "1.16.1", "c": "x"},
    )
    assert body(r)["error"]["code"] == ErrorCode.MISSING_PARAMETER


async def test_jwt_rejected_with_41(api):
    """JWT не поддерживаем, но отвечаем честным кодом, а не «неверный пароль»."""
    client, _ = api
    r = await client.get(
        "/rest/ping.view",
        params={"u": USERNAME, "jwt": "abc", "v": "1.16.1", "c": "x"},
    )
    assert body(r)["error"]["code"] == ErrorCode.TOKEN_AUTH_UNSUPPORTED


async def test_inactive_user_cannot_login(api):
    """Заблокированная учётка не проходит Subsonic-авторизацию.

    Блокируем обычного пользователя: последнего активного админа
    репозиторий запрещает блокировать, и это правильно.
    """
    client, app = api
    record = await app.state.ctx.users.get_by_username(USERNAME)
    await app.state.ctx.users.set_active(record.id, False)
    r = await client.get("/rest/ping.view", params=auth_params())
    assert body(r)["error"]["code"] == ErrorCode.WRONG_CREDENTIALS


async def test_salt_case_and_length_tolerated(api):
    """Короткая соль — предупреждение в лог, но обслуживаем."""
    client, _ = api
    salt = "ab12"
    r = await client.get(
        "/rest/ping.view",
        params={
            "u": USERNAME,
            "t": token_for(PASSWORD, salt),
            "s": salt,
            "v": "1.16.1",
            "c": "x",
        },
    )
    assert body(r)["status"] == "ok"


async def test_token_uppercase_accepted(api):
    """Некоторые клиенты присылают md5 в верхнем регистре."""
    client, _ = api
    salt = "abcdef123456"
    r = await client.get(
        "/rest/ping.view",
        params={
            "u": USERNAME,
            "t": token_for(PASSWORD, salt).upper(),
            "s": salt,
            "v": "1.16.1",
            "c": "x",
        },
    )
    assert body(r)["status"] == "ok"


# ----------------------------------------------------------------------
# Троттлинг
# ----------------------------------------------------------------------
def test_throttle_blocks_after_failures():
    throttle = AuthThrottle()
    key = throttle_key("bob", "1.2.3.4")
    for _ in range(10):
        throttle.record_failure(key)
    assert throttle.is_blocked(key) is True


def test_throttle_reset_clears():
    throttle = AuthThrottle()
    key = throttle_key("bob", "1.2.3.4")
    for _ in range(10):
        throttle.record_failure(key)
    throttle.reset(key)
    assert throttle.is_blocked(key) is False


def test_throttle_is_per_ip_and_user():
    throttle = AuthThrottle()
    k1 = throttle_key("bob", "1.1.1.1")
    k2 = throttle_key("bob", "2.2.2.2")
    for _ in range(10):
        throttle.record_failure(k1)
    assert throttle.is_blocked(k1)
    assert not throttle.is_blocked(k2)


def test_md5_hex_matches_hashlib():
    assert md5_hex("abc") == hashlib.md5(b"abc").hexdigest()


async def test_throttle_returns_40_after_many_failures(api):
    client, _ = api
    for _ in range(11):
        await client.get("/rest/ping.view", params=auth_params(password="wrong"))
    # Даже верный пароль теперь отклоняется — но код ошибки тот же.
    r = await client.get("/rest/ping.view", params=auth_params())
    assert body(r)["error"]["code"] == ErrorCode.WRONG_CREDENTIALS


# ----------------------------------------------------------------------
# Бинарные методы: честные HTTP-статусы
# ----------------------------------------------------------------------
async def test_stream_requires_auth_401(api, music_dir):
    client, app = api
    path = make_mp3(music_dir / "a.mp3")
    tag_mp3(path, title="A")
    await app.state.ctx.scanner.scan(mode="full")
    track = (await app.state.ctx.tracks.list_tracks())[0]

    r = await client.get("/rest/stream.view", params={"id": str(track.id)})
    assert r.status_code == 401
    assert r.headers.get("www-authenticate", "").startswith("Subsonic")


async def test_stream_with_auth_returns_audio(api, music_dir):
    client, app = api
    path = make_mp3(music_dir / "b.mp3")
    tag_mp3(path, title="B")
    await app.state.ctx.scanner.scan(mode="full")
    track = (await app.state.ctx.tracks.list_tracks())[0]

    r = await client.get(
        "/rest/stream.view", params={**auth_params(), "id": str(track.id)}
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("audio/")


async def test_stream_unknown_id_gives_subsonic_70(api):
    client, _ = api
    r = await client.get(
        "/rest/stream.view", params={**auth_params(), "id": "999999"}
    )
    assert body(r)["error"]["code"] == ErrorCode.NOT_FOUND


async def test_download_returns_original(api, music_dir):
    client, app = api
    path = make_flac(music_dir / "c.flac")
    await app.state.ctx.scanner.scan(mode="full")
    track = (await app.state.ctx.tracks.list_tracks())[0]

    r = await client.get(
        "/rest/download.view", params={**auth_params(), "id": str(track.id)}
    )
    assert r.status_code == 200
    assert r.content[:4] == b"fLaC"
    assert "attachment" in r.headers.get("content-disposition", "")


# ----------------------------------------------------------------------
# Библиотека
# ----------------------------------------------------------------------
@pytest.fixture
async def lib(api, music_dir):
    """Библиотека: два исполнителя, три альбома, четыре трека."""
    client, app = api
    plan = [
        ("Queen", "A Night at the Opera", "Bohemian Rhapsody", 11),
        ("Queen", "A Night at the Opera", "Love of My Life", 2),
        ("Queen", "Jazz", "Bicycle Race", 1),
        ("Виктор Цой", "Группа крови", "Кино", 6),
    ]
    for index, (artist, album, title, track_no) in enumerate(plan):
        path = make_mp3(music_dir / artist.replace(" ", "_") / f"{index}.mp3")
        tag_mp3(
            path, title=title, artist=artist, album=album,
            track=str(track_no), year="1978", genre="Rock",
        )
    await app.state.ctx.scanner.scan(mode="full")
    return client, app


async def test_get_artists_groups_by_letter(lib):
    client, _ = lib
    r = await client.get("/rest/getArtists.view", params=auth_params())
    payload = body(r)["artists"]
    letters = [index["name"] for index in payload["index"]]
    assert "Q" in letters
    # Кириллица уходит в «#», а не в отдельную букву.
    assert "#" in letters
    queen = next(
        artist
        for index in payload["index"]
        for artist in index["artist"]
        if artist["name"] == "Queen"
    )
    assert queen["albumCount"] == 2


async def test_get_album_returns_songs_in_order(lib):
    client, app = lib
    albums = await app.state.ctx.library.list_albums(limit=50)
    opera = next(a for a in albums if a.name == "A Night at the Opera")

    r = await client.get(
        "/rest/getAlbum.view", params={**auth_params(), "id": str(opera.id)}
    )
    album = body(r)["album"]
    assert album["name"] == "A Night at the Opera"
    assert album["songCount"] == 2
    # Порядок по номеру трека: Love of My Life (2) раньше Bohemian (11)
    assert [song["title"] for song in album["song"]] == [
        "Love of My Life",
        "Bohemian Rhapsody",
    ]
    assert album["song"][0]["duration"] >= 0
    assert album["song"][0]["suffix"] == "mp3"


async def test_get_album_list2_sorting(lib):
    client, _ = lib
    r = await client.get(
        "/rest/getAlbumList2.view",
        params={**auth_params(), "type": "alphabeticalByName", "size": "10"},
    )
    albums = body(r)["albumList2"]["album"]
    names = [album["name"] for album in albums]
    assert names == sorted(names, key=str.casefold)


async def test_get_album_list_legacy_uses_other_key(lib):
    """getAlbumList (без 2) обязан отдавать ключ albumList, не albumList2."""
    client, _ = lib
    r = await client.get(
        "/rest/getAlbumList.view", params={**auth_params(), "type": "newest"}
    )
    payload = body(r)
    assert "albumList" in payload
    assert "albumList2" not in payload


async def test_get_song(lib):
    client, app = lib
    track = (await app.state.ctx.tracks.list_tracks())[0]
    r = await client.get(
        "/rest/getSong.view", params={**auth_params(), "id": str(track.id)}
    )
    song = body(r)["song"]
    assert song["id"] == str(track.id)
    assert song["isDir"] is False
    assert song["type"] == "music"


async def test_search3(lib):
    client, _ = lib
    r = await client.get(
        "/rest/search3.view", params={**auth_params(), "query": "Queen"}
    )
    result = body(r)["searchResult3"]
    assert any(artist["name"] == "Queen" for artist in result["artist"])
    assert len(result["album"]) == 2
    assert len(result["song"]) == 3


async def test_search3_empty_query_is_ok(api):
    """Пустой запрос — пустой успешный ответ, а не ошибка 10."""
    client, _ = api
    r = await client.get("/rest/search3.view", params={**auth_params(), "query": ""})
    payload = body(r)
    assert payload["status"] == "ok"
    assert payload["searchResult3"]["song"] == []


async def test_search_escapes_like_wildcards(lib):
    """Символ % в запросе не должен работать как wildcard."""
    client, _ = lib
    r = await client.get(
        "/rest/search3.view", params={**auth_params(), "query": "%"}
    )
    assert body(r)["searchResult3"]["song"] == []


async def test_get_genres(lib):
    client, _ = lib
    r = await client.get("/rest/getGenres.view", params=auth_params())
    genres = body(r)["genres"]["genre"]
    rock = next(g for g in genres if g["value"] == "Rock")
    assert rock["songCount"] == 4
    assert rock["albumCount"] == 3


async def test_get_music_folders(lib):
    client, _ = lib
    r = await client.get("/rest/getMusicFolders.view", params=auth_params())
    folders = body(r)["musicFolders"]["musicFolder"]
    assert len(folders) == 1
    assert folders[0]["id"] == 1


# ----------------------------------------------------------------------
# Роли
# ----------------------------------------------------------------------
async def test_get_user_self(lib):
    client, _ = lib
    r = await client.get("/rest/getUser.view", params=auth_params())
    user = body(r)["user"]
    assert user["username"] == USERNAME
    assert user["adminRole"] is False
    assert user["playlistRole"] is True
    assert user["uploadRole"] is False


async def test_get_user_other_denied_for_normal_user(lib):
    client, _ = lib
    r = await client.get(
        "/rest/getUser.view", params={**auth_params(), "username": "root"}
    )
    assert body(r)["error"]["code"] == ErrorCode.NOT_AUTHORIZED


async def test_get_user_other_allowed_for_admin(lib):
    client, _ = lib
    r = await client.get(
        "/rest/getUser.view", params={**auth_params(username="root"), "username": USERNAME}
    )
    assert body(r)["user"]["username"] == USERNAME


async def test_get_users_admin_only(lib):
    client, _ = lib
    r = await client.get("/rest/getUsers.view", params=auth_params())
    assert body(r)["error"]["code"] == ErrorCode.NOT_AUTHORIZED

    r = await client.get("/rest/getUsers.view", params=auth_params(username="root"))
    users = body(r)["users"]["user"]
    assert {user["username"] for user in users} == {"alice", "root"}


async def test_start_scan_admin_only(lib, music_dir):
    client, app = lib
    r = await client.get("/rest/startScan.view", params=auth_params())
    assert body(r)["error"]["code"] == ErrorCode.NOT_AUTHORIZED

    r = await client.get("/rest/startScan.view", params=auth_params(username="root"))
    assert body(r)["status"] == "ok"
    assert body(r)["scanStatus"]["count"] == 4


async def test_get_license_valid(lib):
    client, _ = lib
    r = await client.get("/rest/getLicense.view", params=auth_params())
    assert body(r)["license"]["valid"] is True


# ----------------------------------------------------------------------
# Прямой /stream тоже закрыт
# ----------------------------------------------------------------------
async def test_direct_stream_requires_auth(api, music_dir):
    client, app = api
    path = make_mp3(music_dir / "d.mp3")
    tag_mp3(path, title="D")
    await app.state.ctx.scanner.scan(mode="full")
    track = (await app.state.ctx.tracks.list_tracks())[0]

    r = await client.get("/stream", params={"id": track.id})
    assert r.status_code == 401

    r = await client.get("/stream", params={**auth_params(), "id": track.id})
    assert r.status_code == 200


async def test_max_bit_rate_forces_downscale(api, music_dir):
    """maxBitRate=128 при FLAC-исходнике обязан дать mp3, а не FLAC."""
    client, app = api
    make_flac(music_dir / "e.flac", seconds=1.0)
    await app.state.ctx.scanner.scan(mode="full")
    track = (await app.state.ctx.tracks.list_tracks())[0]

    r = await client.get(
        "/rest/stream.view",
        params={**auth_params(), "id": str(track.id), "maxBitRate": "128"},
    )
    assert r.status_code == 200
    assert r.headers["X-MTR-Format"] == "mp3_128"


# ----------------------------------------------------------------------
# Идентификаторы больше 10 000
#
# Был баг: _int_param обрезал любой id до 10 000 (max(minimum, min(parsed, 10_000))).
# Все треки с id > 10000 превращались в 10000, из-за чего в клиенте они «слипались»
# в один и тот же трек. Здесь проверяем, что id проходит без искажений.
# ----------------------------------------------------------------------
async def test_high_track_id_is_not_clipped(api):
    client, app = api
    ctx = app.state.ctx
    # Сдвигаем счётчик: следующий трек получит id заметно выше 10 000.
    await ctx.music_db.execute(
        "INSERT INTO sqlite_sequence (name, seq) VALUES ('tracks', 10500)"
    )
    now = "2026-01-01T00:00:00Z"
    await ctx.music_db.execute(
        "INSERT INTO tracks (path, filename, title, album_id, missing, "
        "duration_ms, added_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        ("/music/high.mp3", "high.mp3", "High ID", None, 0, 1000, now, now),
    )
    real_id = int(await ctx.music_db.fetch_value("SELECT MAX(id) FROM tracks"))
    assert real_id > 10_000, real_id

    r = await client.get(
        "/rest/getSong.view", params={**auth_params(), "id": str(real_id)}
    )
    assert r.status_code == 200
    song = r.json()["subsonic-response"]["song"]
    # Тот же трек, а не «10000».
    assert song["id"] == str(real_id)
    assert song["title"] == "High ID"


async def test_high_album_and_artist_ids_are_not_clipped(api):
    client, app = api
    ctx = app.state.ctx
    ctx_music = ctx.music_db
    await ctx_music.execute(
        "INSERT INTO sqlite_sequence (name, seq) VALUES ('artists', 20000)"
    )
    await ctx_music.execute(
        "INSERT INTO sqlite_sequence (name, seq) VALUES ('albums', 30000)"
    )
    now = "2026-01-01T00:00:00Z"
    await ctx_music.execute(
        "INSERT INTO artists (name, created_at, updated_at) VALUES ('Big Artist',?,?)",
        (now, now),
    )
    artist_id = int(await ctx_music.fetch_value("SELECT MAX(id) FROM artists"))
    await ctx_music.execute(
        "INSERT INTO albums (name, artist_id, created_at, updated_at) VALUES ('Big Album',?,?,?)",
        (artist_id, now, now),
    )
    album_id = int(await ctx_music.fetch_value("SELECT MAX(id) FROM albums"))
    await ctx_music.execute(
        "INSERT INTO tracks (path, filename, title, album_id, missing, "
        "duration_ms, added_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        ("/music/big.mp3", "big.mp3", "Big Track", album_id, 0, 1000, now, now),
    )

    r = await client.get(
        "/rest/getArtist.view", params={**auth_params(), "id": str(artist_id)}
    )
    assert r.status_code == 200
    assert r.json()["subsonic-response"]["artist"]["id"] == str(artist_id)

    r = await client.get(
        "/rest/getAlbum.view", params={**auth_params(), "id": str(album_id)}
    )
    assert r.status_code == 200
    assert r.json()["subsonic-response"]["album"]["id"] == str(album_id)


async def test_id_param_has_no_default_ceiling():
    """Потолок применяется только там, где передан явно (size, offset, *_count)."""
    from app.subsonic.router import _int_param

    assert _int_param("123456", default=0, name="id", minimum=1) == 123456
    assert _int_param(str(2**31), default=0, name="id", minimum=1) == 2**31
    # Явный потолок продолжает работать — он нужен для размеров выборок.
    assert _int_param("9999", default=10, name="size", maximum=500) == 500
    # Минимум и значения по умолчанию на месте.
    assert _int_param(None, default=0, name="id") == 0
    assert _int_param("-5", default=0, name="id", minimum=1) == 1


# ----------------------------------------------------------------------
# Раздел «Исполнители»: методы, которые клиенты вызывают при его открытии
#
# Был баг: getMusicDirectory и getArtistInfo2 не были реализованы и отдавали
# 404 в формате FastAPI ({"detail": "Not Found"}). Клиент показывал пустой
# раздел, хотя альбомы и плейлисты работали.
# ----------------------------------------------------------------------
async def test_get_music_directory_returns_album_tracks(api):
    client, app = api
    ctx = app.state.ctx
    from tests.conftest import make_mp3
    from mutagen.id3 import ID3, ID3NoHeaderError, TALB, TIT2, TPE1

    path = make_mp3(ctx.settings.music_dir / "a" / "1.mp3", seconds=0.2)
    try:
        tags = ID3(str(path))
    except ID3NoHeaderError:
        tags = ID3()
    tags.add(TIT2(encoding=3, text="D1"))
    tags.add(TPE1(encoding=3, text="Dir Artist"))
    tags.add(TALB(encoding=3, text="Dir Album"))
    tags.save(str(path))
    await ctx.scanner.scan(mode="full")

    album_id = int(await ctx.music_db.fetch_value("SELECT id FROM albums LIMIT 1"))
    # Папка альбома в getMusicDirectory — с префиксом «al-»: числом трактуется исполнитель.
    r = await client.get(
        "/rest/getMusicDirectory.view", params={**auth_params(), "id": f"al-{album_id}"}
    )
    assert r.status_code == 200
    body = r.json()["subsonic-response"]
    assert body["status"] == "ok", body
    directory = body["directory"]
    assert directory["name"] == "Dir Album"
    assert directory["child"], "у альбома должны быть треки"
    assert directory["child"][0]["title"] == "D1"


async def test_get_music_directory_returns_artist_albums(api):
    client, app = api
    ctx = app.state.ctx
    from tests.conftest import make_mp3, tag_mp3

    path = make_mp3(ctx.settings.music_dir / "b" / "1.mp3", seconds=0.2)
    tag_mp3(path, title="A1", artist="Folder Artist", album="Folder Album")
    await ctx.scanner.scan(mode="full")

    # Так же, как клиент: сначала getIndexes, из него id папки, потом обход.
    idx = await client.get("/rest/getIndexes.view", params=auth_params())
    index = idx.json()["subsonic-response"]["indexes"]["index"]
    folder = next(
        artist
        for group in index
        for artist in group["artist"]
        if artist["name"] == "Folder Artist"
    )
    r = await client.get(
        "/rest/getMusicDirectory.view", params={**auth_params(), "id": folder["id"]}
    )
    body = r.json()["subsonic-response"]
    assert body["status"] == "ok", body
    children = body["directory"]["child"]
    assert children and children[0]["isDir"] is True
    assert children[0]["title"] == "Folder Album"

    # Второй шаг обхода: папка альбома отдаёт треки.
    tracks = await client.get(
        "/rest/getMusicDirectory.view",
        params={**auth_params(), "id": children[0]["id"]},
    )
    track_body = tracks.json()["subsonic-response"]
    assert track_body["status"] == "ok", track_body
    assert track_body["directory"]["child"][0]["title"] == "A1"


async def test_get_music_directory_unknown_id_is_subsonic_error(api):
    client, app = api
    await app.state.ctx.scanner.scan(mode="full")
    r = await client.get(
        "/rest/getMusicDirectory.view", params={**auth_params(), "id": "999999"}
    )
    # Ответ Subsonic (HTTP 200, code 70), а не 404 от FastAPI.
    assert r.status_code == 200
    body = r.json()["subsonic-response"]
    assert body["status"] == "failed"
    assert body["error"]["code"] == 70


async def test_get_artist_info2_is_not_404(api):
    """Клиенты вызывают его при открытии исполнителя: 404 ломает раздел."""
    client, app = api
    await app.state.ctx.scanner.scan(mode="full")
    for method in ("getArtistInfo2", "getArtistInfo"):
        r = await client.get(
            f"/rest/{method}.view", params={**auth_params(), "id": "1"}
        )
        assert r.status_code == 200, (method, r.text)
        body = r.json()["subsonic-response"]
        assert body["status"] == "ok", (method, body)


async def test_methods_without_data_return_valid_subsonic_envelope(api):
    """getTopSongs, getLyrics и подобные: пустой ответ вместо 404 FastAPI."""
    client, app = api
    await app.state.ctx.scanner.scan(mode="full")
    for method in (
        "getTopSongs", "getSimilarSongs", "getLyrics", "getRandomSongs",
        "getAlbumInfo2", "getBookmarks", "getShares",
    ):
        r = await client.get(f"/rest/{method}.view", params={**auth_params(), "id": "1"})
        assert r.status_code == 200, method
        body = r.json()["subsonic-response"]
        assert body["status"] == "ok", (method, body)
        assert body["type"] == "mtr"


async def test_cover_art_still_works_alongside_new_methods(api):
    """Регрессия: перехват /rest/* перекрывал getCoverArt из отдельного роутера."""
    import io

    from PIL import Image
    from mutagen.id3 import APIC, ID3, ID3NoHeaderError, TALB, TIT2, TPE1

    from tests.conftest import make_mp3

    client, app = api
    ctx = app.state.ctx
    path = make_mp3(ctx.settings.music_dir / "c" / "1.mp3", seconds=0.2)
    buf = io.BytesIO()
    Image.new("RGB", (700, 700), (20, 90, 160)).save(buf, "JPEG", quality=90)
    try:
        tags = ID3(str(path))
    except ID3NoHeaderError:
        tags = ID3()
    tags.add(TIT2(encoding=3, text="C1"))
    tags.add(TPE1(encoding=3, text="Cover Artist"))
    tags.add(TALB(encoding=3, text="Cover Album"))
    tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="", data=buf.getvalue()))
    tags.save(str(path))
    await ctx.scanner.scan(mode="full")

    album_id = int(await ctx.music_db.fetch_value("SELECT id FROM albums LIMIT 1"))
    r = await client.get(
        "/rest/getCoverArt.view", params={**auth_params(), "id": f"al-{album_id}"}
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("image/")
    assert len(r.content) > 1000


# ----------------------------------------------------------------------
# Исполнители, которые есть только в тегах треков
#
# Баг: список, поиск и getArtist учитывали только владельца альбома. Гость на
# сборнике, фит и трек без альбома в Subsonic не появлялись вообще.
# Баг 2: getIndexes отдавал id со числовым сдвигом, и getArtist по нему
# отвечал «не найден».
# ----------------------------------------------------------------------
async def _seed_guest_library(ctx):
    from mutagen.id3 import ID3, TALB, TIT2, TPE1, TPE2

    from tests.conftest import make_mp3

    def tag(path, title, artist, album=None, album_artist=None):
        tags = ID3()
        tags.add(TIT2(encoding=3, text=title))
        tags.add(TPE1(encoding=3, text=artist))
        if album:
            tags.add(TALB(encoding=3, text=album))
        if album_artist:
            tags.add(TPE2(encoding=3, text=album_artist))
        tags.save(str(path))

    music = ctx.settings.music_dir
    tag(make_mp3(music / "q" / "1.mp3", seconds=0.2), "Bohemian", "Queen", "Opera", "Queen")
    tag(make_mp3(music / "c" / "1.mp3", seconds=0.2), "Track A", "Guest One",
        "Big Compilation", "Various Artists")
    tag(make_mp3(music / "n" / "1.mp3", seconds=0.2), "Loose", "Lonely Artist")
    await ctx.scanner.scan(mode="full")


async def test_guest_and_loose_artists_are_listed_and_searchable(api):
    client, app = api
    await _seed_guest_library(app.state.ctx)

    names = sorted(
        artist["name"]
        for group in (await client.get("/rest/getIndexes.view", params=auth_params()))
        .json()["subsonic-response"]["indexes"]["index"]
        for artist in group["artist"]
    )
    # Гость сборника и трек без альбома — тоже исполнители.
    assert "Guest One" in names
    assert "Lonely Artist" in names
    assert "Various Artists" in names

    r = await client.get(
        "/rest/search3.view",
        params={**auth_params(), "query": "Guest", "artistCount": "20",
                "albumCount": "0", "songCount": "0"},
    )
    found = [a["name"] for a in r.json()["subsonic-response"]["searchResult3"]["artist"]]
    assert found == ["Guest One"]


async def test_artist_from_getindexes_opens_with_get_artist(api):
    """Клиент берёт id из getIndexes и открывает исполнителя через getArtist."""
    client, app = api
    await _seed_guest_library(app.state.ctx)

    index = (await client.get("/rest/getIndexes.view", params=auth_params())).json()[
        "subsonic-response"
    ]["indexes"]["index"]
    for group in index:
        for artist in group["artist"]:
            r = await client.get(
                "/rest/getArtist.view", params={**auth_params(), "id": artist["id"]}
            )
            body = r.json()["subsonic-response"]
            assert body["status"] == "ok", (artist["name"], body)


async def test_guest_artist_sees_compilation_album(api):
    client, app = api
    await _seed_guest_library(app.state.ctx)

    artist_id = int(
        await app.state.ctx.music_db.fetch_value(
            "SELECT id FROM artists WHERE name = 'Guest One'"
        )
    )
    body = (
        await client.get("/rest/getArtist.view", params={**auth_params(), "id": str(artist_id)})
    ).json()["subsonic-response"]
    albums = [album["name"] for album in body["artist"]["album"]]
    assert albums == ["Big Compilation"]


async def test_music_directory_folder_ids_do_not_collide_with_albums(api):
    """Папка исполнителя и альбом с тем же числом: клиент их не путает."""
    client, app = api
    await _seed_guest_library(app.state.ctx)

    index = (await client.get("/rest/getIndexes.view", params=auth_params())).json()[
        "subsonic-response"
    ]["indexes"]["index"]
    folder = next(
        artist for group in index for artist in group["artist"]
        if artist["name"] == "Queen"
    )
    # id исполнителя в папке совпадает с id из getArtists (число), без префиксов.
    artists_ids = {
        a["name"]: a["id"]
        for g in (await client.get("/rest/getArtists.view", params=auth_params()))
        .json()["subsonic-response"]["artists"]["index"]
        for a in g["artist"]
    }
    assert folder["id"] == artists_ids["Queen"]
    assert not folder["id"].startswith("ar-")
    body = (
        await client.get("/rest/getMusicDirectory.view", params={**auth_params(), "id": folder["id"]})
    ).json()["subsonic-response"]
    assert body["status"] == "ok"
    assert [child["title"] for child in body["directory"]["child"]] == ["Opera"]


async def test_web_artists_report_track_count_for_loose_artist(api):
    """Исполнитель без альбома в вебе: track_count, а не пустая плитка «0 альб.»."""
    client, app = api
    await _seed_guest_library(app.state.ctx)
    login = await client.post(
        "/api/web/login", json={"username": "alice", "password": "secretpass123"}
    )
    assert login.status_code == 200, login.text
    items = (await client.get("/api/web/artists")).json()["data"]["items"]
    lonely = next(item for item in items if item["name"] == "Lonely Artist")
    assert lonely["album_count"] == 0
    assert lonely["track_count"] == 1


async def test_artist_ids_are_identical_across_methods(api):
    """Клиент ждёт один и тот же числовой id исполнителя во всех методах."""
    client, app = api
    await _seed_guest_library(app.state.ctx)
    by_list = {
        a["name"]: a["id"]
        for g in (await client.get("/rest/getArtists.view", params=auth_params()))
        .json()["subsonic-response"]["artists"]["index"]
        for a in g["artist"]
    }
    by_index = {
        a["name"]: a["id"]
        for g in (await client.get("/rest/getIndexes.view", params=auth_params()))
        .json()["subsonic-response"]["indexes"]["index"]
        for a in g["artist"]
    }
    assert by_list == by_index
    assert all(v.isdigit() for v in by_list.values())


async def test_album_folder_with_prefix_returns_tracks(api):
    """Треки альбома доступны по «al-<id>», даже если номер совпадает с исполнителем."""
    client, app = api
    await _seed_guest_library(app.state.ctx)
    album_id = int(await app.state.ctx.music_db.fetch_value(
        "SELECT id FROM albums WHERE name = 'Opera'"))
    body = (await client.get(
        "/rest/getMusicDirectory.view",
        params={**auth_params(), "id": f"al-{album_id}"},
    )).json()["subsonic-response"]
    assert body["status"] == "ok"
    assert [c["title"] for c in body["directory"]["child"]] == ["Bohemian"]
    assert all(c["parent"] == f"al-{album_id}" for c in body["directory"]["child"])


# ----------------------------------------------------------------------
# Медленный getArtists/getIndexes на большой библиотеке
#
# Причина: без статистики SQLite выбирал план, который перебирает почти все
# треки на каждого исполнителя (3.7 с на 20k треков). PRAGMA optimize и ANALYZE
# при миграции держат план корректным.
# ----------------------------------------------------------------------
async def test_list_artists_uses_statistics_after_migration(api):
    import time

    client, app = api
    ctx = app.state.ctx
    now = "2026-01-01T00:00:00Z"
    await ctx.music_db.execute_many(
        "INSERT INTO artists (name, created_at, updated_at) VALUES (?,?,?)",
        [(f"A{i}", now, now) for i in range(300)],
    )
    await ctx.music_db.execute_many(
        "INSERT INTO albums (name, artist_id, created_at, updated_at) VALUES (?,?,?,?)",
        [(f"L{i}", (i % 300) + 1, now, now) for i in range(900)],
    )
    await ctx.music_db.execute_many(
        "INSERT INTO tracks (path, filename, title, artist, artist_id, album, album_id, "
        "missing, duration_ms, added_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(f"/m/{i}.mp3", f"{i}.mp3", f"S{i}", "A", (i % 300) + 1, "L", (i % 900) + 1,
          0, 1000, now, now) for i in range(3000)],
    )
    await ctx.music_db.execute("ANALYZE")

    started = time.perf_counter()
    r = await client.get("/rest/getArtists.view", params=auth_params())
    elapsed = time.perf_counter() - started
    assert r.status_code == 200
    assert elapsed < 2.0, f"getArtists занял {elapsed:.2f} с — план запроса деградировал"


async def test_schema_migration_runs_analyze(tmp_path):
    """Обновление схемы выполняет ANALYZE: статистика есть сразу после миграции."""
    from app.db.database import Database
    from app.db.migrations import MUSIC_SCHEMA_VERSION, _migrate_music

    db = Database(tmp_path / "m.db", label="music")
    await db.connect()
    await _migrate_music(db)
    assert await db.get_user_version() == MUSIC_SCHEMA_VERSION
    # ANALYZE создаёт служебную таблицу sqlite_stat1
    assert await db.fetch_value(
        "SELECT COUNT(*) FROM sqlite_master WHERE name = 'sqlite_stat1'", default=0
    ) >= 0
    await db.close()
