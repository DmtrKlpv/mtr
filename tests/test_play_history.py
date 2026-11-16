"""История прослушиваний для главной страницы.

`playback_log` уже существовал и писался из Subsonic scrobble; здесь
проверяется, что веб-плеер пишет туда же и что выборка не дублирует треки.
"""

from __future__ import annotations


import httpx

from tests.test_web import PASSWORD, USER_PASSWORD, login, login_admin, web  # noqa: F401


async def test_recently_played_empty_for_new_user(web):
    """Без истории — пустой список, а не чужие треки."""
    client, _ = web
    await login_admin(client)
    data = (await client.get("/api/web/recently-played")).json()["data"]
    assert data["items"] == []


async def test_play_event_records_history(web):
    client, app = web
    csrf = await login_admin(client)
    track = (await app.state.ctx.tracks.list_tracks(limit=1))[0]

    response = await client.post(
        "/api/web/play-event", json={"track_id": track.id},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200, response.text

    items = (await client.get("/api/web/recently-played")).json()["data"]["items"]
    assert [item["id"] for item in items] == [track.id]


async def test_repeated_play_does_not_duplicate(web):
    """Один трек в списке один раз, на позиции последнего прослушивания."""
    client, app = web
    csrf = await login_admin(client)
    tracks = await app.state.ctx.tracks.list_tracks(limit=2)
    for track in tracks[:2]:
        await client.post("/api/web/play-event", json={"track_id": track.id},
                          headers={"X-CSRF-Token": csrf})
    # Повторно играем первый: он должен подняться наверх, но не задвоиться.
    await client.post("/api/web/play-event", json={"track_id": tracks[0].id},
                      headers={"X-CSRF-Token": csrf})

    items = (await client.get("/api/web/recently-played")).json()["data"]["items"]
    ids = [item["id"] for item in items]
    assert len(ids) == 2, ids
    assert ids[0] == tracks[0].id, ids


async def test_history_is_per_user(web):
    client, app = web
    csrf = await login_admin(client)
    track = (await app.state.ctx.tracks.list_tracks(limit=1))[0]
    await client.post("/api/web/play-event", json={"track_id": track.id},
                      headers={"X-CSRF-Token": csrf})

    bob = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    await login(bob, "bob", USER_PASSWORD)
    items = (await bob.get("/api/web/recently-played")).json()["data"]["items"]
    assert items == []
    await bob.aclose()


async def test_play_event_requires_csrf(web):
    client, app = web
    await login_admin(client)
    track = (await app.state.ctx.tracks.list_tracks(limit=1))[0]
    response = await client.post(
        "/api/web/play-event", json={"track_id": track.id}
    )
    assert response.status_code == 403


async def test_play_event_unknown_track_404(web):
    client, _ = web
    csrf = await login_admin(client)
    response = await client.post(
        "/api/web/play-event", json={"track_id": 999999},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 404


async def test_recently_added_returns_tracks_and_albums(web):
    client, _ = web
    await login_admin(client)
    data = (await client.get("/api/web/recently-added")).json()["data"]
    assert data["items"], "треки должны быть"
    assert data["albums"], "альбомы должны быть"


async def test_artists_include_album_id_for_avatar(web):
    """Аватар берёт обложку первого альбома — id должен быть в ответе."""
    client, _ = web
    await login_admin(client)
    artists = (await client.get("/api/web/artists")).json()["data"]["items"]
    assert artists
    assert any(artist.get("album_id") for artist in artists), artists
