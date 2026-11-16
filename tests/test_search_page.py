"""Страница поиска: теги, видимость плейлистов, кнопка в рейле."""

from __future__ import annotations

import httpx

from tests.test_web import PASSWORD, USER_PASSWORD, login, login_admin, web  # noqa: F401


async def _search(client: httpx.AsyncClient, q: str) -> dict:
    response = await client.get("/api/web/search", params={"q": q})
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def test_artists_carry_album_count_and_cover_album(web):
    client, _ = web
    await login_admin(client)
    data = await _search(client, "Queen")
    queen = next(a for a in data["artists"] if a["name"] == "Queen")
    assert queen["album_count"] == 1
    assert queen["album_id"] is not None


async def test_genre_tag_finds_track_album_and_artist(web):
    client, app = web
    await login_admin(client)
    await app.state.ctx.music_db.execute(
        "UPDATE tracks SET genre = ? WHERE title = ?", ("Ambient", "Alone")
    )
    data = await _search(client, "ambient")
    assert [t["title"] for t in data["tracks"]] == ["Alone"]
    assert [a["name"] for a in data["albums"]] == ["X"]
    assert [a["name"] for a in data["artists"]] == ["Solo"]


async def test_playlist_search_ignores_case_and_yo(web):
    client, _ = web
    csrf = await login_admin(client)
    created = await client.post(
        "/api/web/playlists", json={"name": "Ёлка 2025"}, headers={"X-CSRF-Token": csrf}
    )
    assert created.status_code == 201, created.text
    data = await _search(client, "ЕЛКА")
    assert [p["name"] for p in data["playlists"]] == ["Ёлка 2025"]


async def test_search_hides_other_users_private_playlists(web):
    client, _ = web
    csrf = (await login(client, "bob", USER_PASSWORD))[1]
    private = await client.post(
        "/api/web/playlists", json={"name": "Секрет боба"}, headers={"X-CSRF-Token": csrf}
    )
    public = await client.post(
        "/api/web/playlists",
        json={"name": "Общий боба", "is_public": True},
        headers={"X-CSRF-Token": csrf},
    )
    assert private.status_code == 201 and public.status_code == 201
    bob_sees = [p["name"] for p in (await _search(client, "боба"))["playlists"]]
    assert bob_sees == ["Общий боба", "Секрет боба"]

    await login_admin(client)
    admin_sees = [p["name"] for p in (await _search(client, "боба"))["playlists"]]
    assert admin_sees == ["Общий боба"]


async def test_rail_search_sits_between_library_and_playlists(web):
    client, _ = web
    await login_admin(client)
    html = (await client.get("/app")).text
    library = html.index('data-view="library"')
    search = html.index('data-view="search"')
    playlists = html.index('data-view="playlists"')
    assert library < search < playlists
    assert "/static/icons/search.svg" in html
