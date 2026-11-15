"""Поиск на главной и хлебные крошки (0.23.1)."""

from __future__ import annotations

from pathlib import Path

from app.repositories.library import LibraryRepository, _like
from app.repositories.tracks import TrackRepository
from tests.test_web import login_admin, web  # noqa: F401


async def test_search_finds_tracks_albums_artists(web):
    client, app = web
    await login_admin(client)
    data = (await client.get("/api/web/search?q=Queen")).json()["data"]
    # В фикстуре есть альбомы Queen: поиск обязан вернуть хоть что-то из альбомов.
    assert data["albums"], data
    names = " ".join(a["name"] for a in data["albums"]).lower()
    assert "queen" in names or any("queen" in (a.get("artist") or "").lower() for a in data["albums"])


async def test_search_empty_query_returns_empty(web):
    client, _ = web
    await login_admin(client)
    data = (await client.get("/api/web/search?q=%20%20")).json()["data"]
    assert data == {"tracks": [], "albums": [], "artists": []}


async def test_search_survives_special_characters(web):
    client, _ = web
    await login_admin(client)
    for query in ("%", "_", "\\", "'", "\"", "%_%"):
        r = await client.get(f"/api/web/search?q={query}")
        assert r.status_code == 200, (query, r.text)
        assert "tracks" in r.json()["data"]


def test_like_escapes_backslash_first():
    """Порядок замен важен: '\\' экранируется раньше '%' и '_'."""
    assert _like("100%") == "%100\\%%"
    assert _like("a_b") == "%a\\_b%"
    assert _like("a\\b") == "%a\\\\b%"
    assert _like("   ") is None


async def test_search_albums_and_artists_ignore_empty_albums(ctx, music_dir: Path):
    """Альбом без живых треков не попадает в поиск, исполнитель тоже."""
    from tests.conftest import make_mp3, tag_mp3

    lib: LibraryRepository = ctx.library
    music = ctx.settings.music_dir
    p = make_mp3(music / "a" / "t.mp3", seconds=0.2)
    tag_mp3(p, title="T", artist="Searchable", album="Findable")
    await ctx.scanner.scan(mode="full")

    albums = await lib.search_albums("Findable", limit=10)
    assert [a.name for a in albums] == ["Findable"]
    artists = await lib.search_artists("Searchable", limit=10)
    assert [a.name for a in artists] == ["Searchable"]

    await ctx.music_db.execute("UPDATE tracks SET missing = 1")
    assert await lib.search_albums("Findable", limit=10) == []
    assert await lib.search_artists("Searchable", limit=10) == []


async def test_tracks_search_does_not_break_on_backslash(ctx):
    tr: TrackRepository = ctx.tracks
    assert await tr.search("\\", limit=5) == []
    assert await tr.search("%", limit=5) == []


# ----------------------------------------------------------------------
# е и ё — одна буква, регистр не важен (0.25.0)
# ----------------------------------------------------------------------
def test_fold_unifies_yo_and_register():
    from app.utils.alphabet import fold

    assert fold("Ёлка") == fold("елка") == "елка"
    assert fold("ЁЖИК") == fold("ёжик") == "ежик"
    assert fold("Мёд") == "мед"
    assert fold("QUEEN") == fold("queen") == "queen"
    assert fold(None) == ""


async def test_search_finds_yo_by_ye(ctx, music_dir: Path):
    """«елка» находит «Ёлка»: раньше кириллица в LIKE зависела от регистра и ё."""
    from tests.conftest import make_mp3, tag_mp3

    path = make_mp3(music_dir / "Yo" / "Alb" / "01.mp3", seconds=0.2)
    tag_mp3(path, title="Ёлка в лесу", artist="Ёжик", album="Мёд")
    await ctx.scanner.scan(mode="full")
    ctx.cover_pass.cancel_nowait()
    await ctx.cover_pass.stop()

    found = await ctx.tracks.search("елка")
    assert any("Ёлка" in (t.title or "") for t in found), [t.title for t in found]

    # И наоборот: запрос с ё находит текст с е.
    found = await ctx.tracks.search("мед")
    assert any("Мёд" in (t.album or "") for t in found)


async def test_search_ignores_register_for_cyrillic(ctx, music_dir: Path):
    from tests.conftest import make_mp3, tag_mp3

    path = make_mp3(music_dir / "Reg" / "Alb2" / "01.mp3", seconds=0.2)
    tag_mp3(path, title="Ледокол", artist="Мумий Тролль", album="Морская")
    await ctx.scanner.scan(mode="full")
    ctx.cover_pass.cancel_nowait()
    await ctx.cover_pass.stop()

    for query in ("Ледокол", "ледокол", "ЛЕДОКОЛ"):
        found = await ctx.tracks.search(query)
        assert any(t.title == "Ледокол" for t in found), query


# ----------------------------------------------------------------------
# Цвет акцента из настроек (0.25.0)
# ----------------------------------------------------------------------
async def test_accent_color_setting_applied_to_page(web):  # noqa: F811
    """Выбранный цвет попадает в data-accent на <html> ещё до загрузки JS."""
    client, app = web
    csrf = await login_admin(client)
    ctx = app.state.ctx

    page = (await client.get("/app")).text
    assert 'data-accent="blue"' in page, "по умолчанию синий"

    r = await client.post(
        "/api/web/settings",
        json={"values": {"accent_color": "green"}},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200, r.text
    assert ctx.settings.accent_color == "green"

    page = (await client.get("/app")).text
    assert 'data-accent="green"' in page, "выбранный цвет должен применяться на странице"


async def test_accent_color_rejects_unknown_preset(web):  # noqa: F811
    client, app = web
    csrf = await login_admin(client)
    r = await client.post(
        "/api/web/settings",
        json={"values": {"accent_color": "rainbow"}},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code >= 400, "неизвестный пресет должен отклоняться"
    assert app.state.ctx.settings.accent_color == "blue"


def test_every_accent_preset_has_css_block():
    """Каждый вариант из настроек должен быть описан в theme.css, иначе выбор не даст эффекта."""
    from pathlib import Path

    from app.repositories.settings import SPECS

    css = (Path(__file__).resolve().parent.parent
           / "app" / "web" / "static" / "theme.css").read_text(encoding="utf-8")
    spec = SPECS["accent_color"]
    for preset in spec.choices:
        assert f'data-accent="{preset}"' in css, preset
    assert spec.default in spec.choices


# ----------------------------------------------------------------------
# Плеер: поля для переходов (0.25.0)
# ----------------------------------------------------------------------
async def test_album_tracks_carry_artist_and_album_ids(web):  # noqa: F811
    """Из плеера нужны оба id: по обложке и альбому — альбом, по исполнителю — его альбомы."""
    client, _ = web
    await login_admin(client)
    albums = (await client.get("/api/web/albums?limit=5")).json()["data"]["items"]
    assert albums, "в фикстуре есть альбомы"

    detail = (await client.get(f"/api/web/albums/{albums[0]['id']}")).json()["data"]
    assert detail["artist_id"], "у альбома должен быть artist_id"
    for track in detail["tracks"]:
        assert "album_id" in track and track["album_id"] == detail["id"]
        assert "artist_id" in track


async def test_search_tracks_carry_ids(web):  # noqa: F811
    client, _ = web
    await login_admin(client)
    data = (await client.get("/api/web/search?q=Queen")).json()["data"]
    assert data["tracks"], data
    for track in data["tracks"]:
        assert "album_id" in track and "artist_id" in track
