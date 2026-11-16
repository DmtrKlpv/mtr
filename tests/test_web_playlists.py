"""Тесты веб-API этапа 6.2: состав плейлистов, избранное, права."""

from __future__ import annotations


import httpx

from tests.test_web import PASSWORD, USER_PASSWORD, login, login_admin, web  # noqa: F401


async def _playlist_with_tracks(client, app, csrf: str) -> tuple[int, list[int]]:
    tracks = await app.state.ctx.tracks.list_tracks(limit=10)
    ids = [track.id for track in tracks]
    created = await client.post(
        "/api/web/playlists", json={"name": "Mix"}, headers={"X-CSRF-Token": csrf}
    )
    assert created.status_code == 201, created.text
    return created.json()["data"]["id"], ids


async def test_add_tracks_and_read_back(web):
    client, app = web
    csrf = await login_admin(client)
    pl_id, ids = await _playlist_with_tracks(client, app, csrf)
    r = await client.post(
        f"/api/web/playlists/{pl_id}/tracks",
        json={"track_ids": ids[:2]},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200, r.text
    detail = (await client.get(f"/api/web/playlists/{pl_id}")).json()["data"]
    assert [t["id"] for t in detail["tracks"]] == ids[:2]
    assert detail["can_edit"] is True


async def test_remove_track_from_playlist(web):
    client, app = web
    csrf = await login_admin(client)
    pl_id, ids = await _playlist_with_tracks(client, app, csrf)
    await client.post(
        f"/api/web/playlists/{pl_id}/tracks",
        json={"track_ids": ids[:2]},
        headers={"X-CSRF-Token": csrf},
    )
    r = await client.delete(
        f"/api/web/playlists/{pl_id}/tracks/{ids[0]}",
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200
    detail = (await client.get(f"/api/web/playlists/{pl_id}")).json()["data"]
    assert [t["id"] for t in detail["tracks"]] == [ids[1]]


async def test_add_requires_csrf(web):
    client, app = web
    csrf = await login_admin(client)
    pl_id, ids = await _playlist_with_tracks(client, app, csrf)
    r = await client.post(
        f"/api/web/playlists/{pl_id}/tracks", json={"track_ids": ids[:1]}
    )
    assert r.status_code == 403


async def test_foreign_private_playlist_is_hidden(web):
    """Чужой приватный плейлист — 404, не 403: не подтверждаем существование."""
    client, app = web
    csrf = await login_admin(client)
    pl_id, _ = await _playlist_with_tracks(client, app, csrf)

    bob = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    await login(bob, "bob", USER_PASSWORD)
    assert (await bob.get(f"/api/web/playlists/{pl_id}")).status_code == 404
    await bob.aclose()


async def test_public_playlist_readable_but_not_editable_by_other(web):
    client, app = web
    csrf = await login_admin(client)
    pl_id, ids = await _playlist_with_tracks(client, app, csrf)
    await client.post(
        f"/api/web/playlists/{pl_id}/public",
        json={"is_public": True}, headers={"X-CSRF-Token": csrf},
    )

    bob = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    _, bob_csrf = await login(bob, "bob", USER_PASSWORD)
    detail = (await bob.get(f"/api/web/playlists/{pl_id}")).json()["data"]
    assert detail["can_edit"] is False

    r = await bob.post(
        f"/api/web/playlists/{pl_id}/tracks",
        json={"track_ids": ids[:1]}, headers={"X-CSRF-Token": bob_csrf},
    )
    assert r.status_code == 403
    await bob.aclose()


async def test_smart_playlist_rejects_manual_tracks(web):
    client, app = web
    csrf = await login_admin(client)
    admin = await app.state.ctx.users.get_by_username("admin")
    smart = await app.state.ctx.playlists.create(
        admin.id, "Smart", smart_rules={"sort": "title"}
    )
    r = await client.post(
        f"/api/web/playlists/{smart.id}/tracks",
        json={"track_ids": [1]}, headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 400


async def test_rename_playlist(web):
    client, app = web
    csrf = await login_admin(client)
    pl_id, _ = await _playlist_with_tracks(client, app, csrf)
    r = await client.post(
        f"/api/web/playlists/{pl_id}/rename",
        json={"name": "Renamed"}, headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200
    assert r.json()["data"]["name"] == "Renamed"


async def test_star_roundtrip(web):
    client, app = web
    csrf = await login_admin(client)
    track = (await app.state.ctx.tracks.list_tracks(limit=1))[0]

    await client.post(f"/api/web/tracks/{track.id}/star",
                      json={"starred": True}, headers={"X-CSRF-Token": csrf})
    ids = (await client.get("/api/web/starred-ids")).json()["data"]["ids"]
    assert track.id in ids

    items = (await client.get("/api/web/starred")).json()["data"]["items"]
    assert [t["id"] for t in items] == [track.id]

    await client.post(f"/api/web/tracks/{track.id}/star",
                      json={"starred": False}, headers={"X-CSRF-Token": csrf})
    assert track.id not in (await client.get("/api/web/starred-ids")).json()["data"]["ids"]


async def test_star_is_per_user(web):
    client, app = web
    csrf = await login_admin(client)
    track = (await app.state.ctx.tracks.list_tracks(limit=1))[0]
    await client.post(f"/api/web/tracks/{track.id}/star",
                      json={"starred": True}, headers={"X-CSRF-Token": csrf})

    bob = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    await login(bob, "bob", USER_PASSWORD)
    assert (await bob.get("/api/web/starred-ids")).json()["data"]["ids"] == []
    await bob.aclose()


async def test_star_unknown_track_404(web):
    client, _ = web
    csrf = await login_admin(client)
    r = await client.post("/api/web/tracks/999999/star",
                          json={"starred": True}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 404


async def test_import_does_not_create_duplicates_for_same_name(web):
    """Два файла с одинаковым именем из разных каталогов не дают дубля.

    Регрессия: `by_name` строился один раз до цикла, поэтому плейлист,
    созданный в этом же запросе, не находился, и второй файл с тем же
    именем создавал второй плейлист.
    """
    client, app = web
    csrf = await login_admin(client)
    root = app.state.ctx.settings.music_dir

    # Реальный трек из библиотеки: пустой m3u8 теперь отбрасывается как
    # ошибка разбора, и плейлист не создался бы вовсе.
    track = (await app.state.ctx.tracks.list_tracks(limit=1))[0]
    content = f"#EXTM3U\n{track.path}\n"

    def write_playlist(folder: str) -> str:
        target = root / folder
        target.mkdir(parents=True, exist_ok=True)
        path = target / "DupCheck.m3u8"
        path.write_text(content, encoding="utf-8")
        return str(path)

    first = write_playlist("one")
    second = write_playlist("two")

    # Оба файла в одном запросе: второй должен увидеть первый и создать
    # плейлист только один раз.
    body = {"dir": str(root), "items": [
        {"path": first, "action": "create"},
        {"path": second, "action": "create"},
    ]}
    response = await client.post(
        "/api/web/playlists/import/apply", json=body,
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200, response.text

    listed = (await client.get("/api/web/playlists")).json()["data"]["items"]
    same_name = [p for p in listed if p["name"] == "DupCheck"]
    assert len(same_name) == 1, [p["name"] for p in listed]


# Массовое удаление плейлистов
async def _create_playlists(client, app, names, *, with_track=True) -> list[int]:
    csrf = await login_admin(client)
    admin = await app.state.ctx.users.get_by_username("admin")
    track = (await app.state.ctx.tracks.list_tracks(limit=1))[0]
    ids = []
    for name in names:
        playlist = await app.state.ctx.playlists.create(
            admin.id, name,
            track_ids=[track.id] if with_track else [],
        )
        ids.append(playlist.id)
    return ids, csrf


async def test_purge_deletes_all_own_playlists(web):
    client, app = web
    await _create_playlists(client, app, ["A", "B", "C"])

    csrf = await login_admin(client)
    response = await client.post(
        "/api/web/playlists/purge", json={"scope": "mine"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["deleted"] == 3

    listed = (await client.get("/api/web/playlists")).json()["data"]["items"]
    assert [p["name"] for p in listed] == []


async def test_purge_empty_only(web):
    client, app = web
    await _create_playlists(client, app, ["Full"], with_track=True)
    await _create_playlists(client, app, ["Empty1", "Empty2"], with_track=False)

    csrf = await login_admin(client)
    response = await client.post(
        "/api/web/playlists/purge", json={"scope": "empty"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    assert response.json()["data"]["deleted"] == 2

    listed = (await client.get("/api/web/playlists")).json()["data"]["items"]
    assert [p["name"] for p in listed] == ["Full"]


async def test_purge_does_not_touch_foreign_without_admin_scope(web):
    """Обычный пользователь не должен удалять чужие плейлисты."""
    client, app = web
    await login_admin(client)
    admin = await app.state.ctx.users.get_by_username("admin")
    await app.state.ctx.playlists.create(admin.id, "AdminList")

    bob = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    _, bob_csrf = await login(bob, "bob", USER_PASSWORD)
    response = await bob.post(
        "/api/web/playlists/purge", json={"scope": "mine"},
        headers={"X-CSRF-Token": bob_csrf},
    )
    assert response.status_code == 200
    assert response.json()["data"]["deleted"] == 0

    # Плейлист админа на месте.
    assert (await client.get("/api/web/playlists")).status_code == 200
    listed = (await client.get("/api/web/playlists")).json()["data"]["items"]
    assert any(p["name"] == "AdminList" for p in listed)
    await bob.aclose()


async def test_purge_all_requires_admin(web):
    client, app = web
    bob = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    _, bob_csrf = await login(bob, "bob", USER_PASSWORD)
    response = await bob.post(
        "/api/web/playlists/purge", json={"scope": "all"},
        headers={"X-CSRF-Token": bob_csrf},
    )
    assert response.status_code == 403
    await bob.aclose()


async def test_purge_requires_csrf(web):
    client, _ = web
    await login_admin(client)
    response = await client.post(
        "/api/web/playlists/purge", json={"scope": "mine"}
    )
    assert response.status_code == 403


async def test_delete_many_in_a_row(web):
    """Регрессия: после нескольких удалений кнопка перестаёт работать.

    Причина была в нативных диалогах: Chrome блокирует confirm() после
    нескольких вызовов. Здесь проверяем, что API выдерживает серию
    удалений без побочных эффектов.
    """
    client, app = web
    ids, csrf = await _create_playlists(client, app, ["P1", "P2", "P3", "P4"])

    for playlist_id in ids[:3]:
        response = await client.delete(
            f"/api/web/playlists/{playlist_id}", headers={"X-CSRF-Token": csrf}
        )
        assert response.status_code == 200, response.text

    listed = (await client.get("/api/web/playlists")).json()["data"]["items"]
    assert [p["name"] for p in listed] == ["P4"]

    # Последний тоже удаляется.
    response = await client.delete(
        f"/api/web/playlists/{ids[3]}", headers={"X-CSRF-Token": csrf}
    )
    assert response.status_code == 200
    listed = (await client.get("/api/web/playlists")).json()["data"]["items"]
    assert listed == []
