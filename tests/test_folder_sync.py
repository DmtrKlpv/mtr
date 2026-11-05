"""Импорт плейлиста из папки и ресинк с папкой (app/playlists/folder_sync.py)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.playlists import folder_sync as fs
from app.playlists.folder_sync import FolderSyncError, PlanStore
from tests.conftest import make_mp3, tag_mp3

OWNER = 1


def _make_track(path: Path, *, title: str, artist: str, album: str, track: str) -> Path:
    make_mp3(path, seconds=1.0)
    tag_mp3(path, title=title, artist=artist, album=album, track=track)
    return path


@pytest.fixture
def layout(music_dir: Path) -> dict[str, Path]:
    """Artist1/Album A (2 трека) + Artist1/Single (1 трек)."""
    base = music_dir / "Artist1"
    files = {
        "a2": _make_track(base / "Album A" / "02.mp3", title="Second", artist="Artist1", album="Album A", track="2"),
        "a1": _make_track(base / "Album A" / "01.mp3", title="First", artist="Artist1", album="Album A", track="1"),
        "s": _make_track(base / "Single" / "single.mp3", title="Solo", artist="Artist1", album="Single", track="1"),
    }
    return files


async def _scan(ctx) -> None:
    await ctx.scanner.scan(mode="incremental")


async def test_scan_builds_one_playlist_sorted_by_folder_disc_track(ctx, music_dir, layout):
    await _scan(ctx)
    result = await fs.scan_folder_to_playlist(
        db=ctx.music_db, playlists=ctx.playlists, music_root=music_dir,
        folder=str(music_dir / "Artist1"), owner_id=OWNER,
    )
    assert result.name == "Artist1"  # по умолчанию — имя корневой папки
    assert result.matched == 3 and result.unmatched == []

    tracks = await ctx.playlists.tracks(await ctx.playlists.get(result.playlist_id))
    titles = [t.title for t in tracks]
    # «Album A» < «Single» по имени подпапки; внутри альбома — по номеру трека.
    assert titles == ["First", "Second", "Solo"]


async def test_scan_stores_links_to_tracks_not_paths(ctx, music_dir, layout):
    await _scan(ctx)
    result = await fs.scan_folder_to_playlist(
        db=ctx.music_db, playlists=ctx.playlists, music_root=music_dir,
        folder=str(music_dir / "Artist1"), owner_id=OWNER,
    )
    rows = await ctx.music_db.fetch_all(
        "SELECT track_id FROM playlist_tracks WHERE playlist_id = ? ORDER BY position",
        (result.playlist_id,),
    )
    assert all(isinstance(r["track_id"], int) for r in rows)
    assert len(rows) == 3


async def test_scan_reports_files_not_in_library(ctx, music_dir, layout):
    # Сервер ещё не сканировал папку: ничего не найдено, всё — в unmatched.
    result = await fs.scan_folder_to_playlist(
        db=ctx.music_db, playlists=ctx.playlists, music_root=music_dir,
        folder=str(music_dir / "Artist1"), owner_id=OWNER,
    )
    assert result.matched == 0
    assert len(result.unmatched) == 3


@pytest.mark.parametrize("raw, code", [
    ("", "folder_empty"),
    ("relative/Artist1", "folder_not_absolute"),
    ("/etc", "folder_outside_root"),
    ("/music/../etc", "folder_outside_root"),
])
async def test_validate_folder_rejects(ctx, music_dir, raw, code):
    with pytest.raises(FolderSyncError) as info:
        fs.validate_folder(raw, music_dir)
    assert info.value.code == code


async def test_resync_refuses_when_library_is_stale(ctx, music_dir, layout):
    await _scan(ctx)
    pl = await fs.scan_folder_to_playlist(
        db=ctx.music_db, playlists=ctx.playlists, music_root=music_dir,
        folder=str(music_dir / "Artist1"), owner_id=OWNER,
    )
    # Меняем папку ПОСЛЕ последнего скана.
    later = max(os.stat(music_dir / "Artist1").st_mtime, 0) + 100
    os.utime(music_dir / "Artist1" / "Single", (later, later))

    with pytest.raises(FolderSyncError) as info:
        await fs.build_resync_plan(
            db=ctx.music_db, playlists=ctx.playlists, music_root=music_dir,
            playlist_id=pl.playlist_id, user_id=OWNER, is_admin=False,
        )
    assert info.value.code == "library_stale"
    assert info.value.message == fs.STALE_MESSAGE


async def test_resync_relocates_moved_track_and_keeps_position(ctx, music_dir, layout):
    await _scan(ctx)
    pl = await fs.scan_folder_to_playlist(
        db=ctx.music_db, playlists=ctx.playlists, music_root=music_dir,
        folder=str(music_dir / "Artist1"), owner_id=OWNER,
    )
    # Переносим трек из альбома в Single ДО скана — сервер его проиндексирует.
    moved = music_dir / "Artist1" / "Single" / "01-moved.mp3"
    os.replace(layout["a1"], moved)
    await _scan(ctx)

    plan = await fs.build_resync_plan(
        db=ctx.music_db, playlists=ctx.playlists, music_root=music_dir,
        playlist_id=pl.playlist_id, user_id=OWNER, is_admin=False,
    )
    assert len(plan.relocate) == 1
    assert plan.remove == []
    item = plan.relocate[0]
    assert item["new_path"] == str(moved)
    position_before = item["position"]

    await fs.apply_resync_plan(
        db=ctx.music_db, playlists=ctx.playlists, plan=plan, remove_link_ids=[],
    )
    row = await ctx.music_db.fetch_one(
        "SELECT track_id, position FROM playlist_tracks WHERE id = ?", (item["link_id"],),
    )
    assert int(row["track_id"]) == item["new_track_id"]
    assert int(row["position"]) == position_before


async def test_resync_removes_only_after_confirmation(ctx, music_dir, layout):
    await _scan(ctx)
    pl = await fs.scan_folder_to_playlist(
        db=ctx.music_db, playlists=ctx.playlists, music_root=music_dir,
        folder=str(music_dir / "Artist1"), owner_id=OWNER,
    )
    os.remove(layout["s"])
    await _scan(ctx)

    plan = await fs.build_resync_plan(
        db=ctx.music_db, playlists=ctx.playlists, music_root=music_dir,
        playlist_id=pl.playlist_id, user_id=OWNER, is_admin=False,
    )
    assert len(plan.remove) == 1 and plan.relocate == []
    link_id = plan.remove[0]["link_id"]

    # Без подтверждения ничего не удаляется.
    await fs.apply_resync_plan(
        db=ctx.music_db, playlists=ctx.playlists, plan=plan, remove_link_ids=[],
    )
    assert await ctx.music_db.fetch_value(
        "SELECT COUNT(*) FROM playlist_tracks WHERE playlist_id = ?", (pl.playlist_id,)
    ) == 3

    # После подтверждения — удаляется, позиции перенумерованы без дыр.
    result = await fs.apply_resync_plan(
        db=ctx.music_db, playlists=ctx.playlists, plan=plan, remove_link_ids=[link_id],
    )
    assert result["removed"] == 1
    positions = [r["position"] for r in await ctx.music_db.fetch_all(
        "SELECT position FROM playlist_tracks WHERE playlist_id = ? ORDER BY position",
        (pl.playlist_id,),
    )]
    assert positions == [0, 1]


async def test_resync_adds_new_files(ctx, music_dir, layout):
    await _scan(ctx)
    pl = await fs.scan_folder_to_playlist(
        db=ctx.music_db, playlists=ctx.playlists, music_root=music_dir,
        folder=str(music_dir / "Artist1"), owner_id=OWNER,
    )
    _make_track(music_dir / "Artist1" / "Single" / "02.mp3", title="New", artist="Artist1",
                album="Single", track="2")
    await _scan(ctx)
    plan = await fs.build_resync_plan(
        db=ctx.music_db, playlists=ctx.playlists, music_root=music_dir,
        playlist_id=pl.playlist_id, user_id=OWNER, is_admin=False,
    )
    assert len(plan.add) == 1
    await fs.apply_resync_plan(db=ctx.music_db, playlists=ctx.playlists, plan=plan, remove_link_ids=[])
    tracks = await ctx.playlists.tracks(await ctx.playlists.get(pl.playlist_id))
    assert tracks[-1].title == "New"


async def test_resync_rejects_playlist_without_origin(ctx, music_dir, layout):
    await _scan(ctx)
    manual = await ctx.playlists.create(OWNER, "manual")
    with pytest.raises(FolderSyncError) as info:
        await fs.build_resync_plan(
            db=ctx.music_db, playlists=ctx.playlists, music_root=music_dir,
            playlist_id=manual.id, user_id=OWNER, is_admin=False,
        )
    assert info.value.code == "no_origin"


async def test_plan_is_single_use_and_expires(ctx):
    store = PlanStore()
    token = store.put(fs.ResyncPlan(playlist_id=1, folder="/music/x"))
    assert store.take(token).playlist_id == 1
    with pytest.raises(FolderSyncError):
        store.take(token)  # второй раз — нельзя
    with pytest.raises(FolderSyncError):
        store.take("nope")


def test_sort_key_orders_disc_then_track(music_dir):
    root = str(music_dir / "A")
    rows = [
        {"path": f"{root}/CD2/01.mp3", "disc_number": 2, "track_number": 1},
        {"path": f"{root}/CD1/02.mp3", "disc_number": 1, "track_number": 2},
        {"path": f"{root}/CD1/01.mp3", "disc_number": 1, "track_number": 1},
    ]
    rows.sort(key=lambda r: fs.sort_key(r, root))
    assert [r["path"].split("/")[-1] for r in rows] == ["01.mp3", "02.mp3", "01.mp3"]
    assert [r["path"].split("/")[-2] for r in rows] == ["CD1", "CD1", "CD2"]


async def test_music_migration_v4_upgrades_v3_database(tmp_path):
    """Старая база (v3) получает колонки и индексы v4 и версию 4 — ровно один раз."""
    from app.db.database import Database
    from app.db.migrations import MUSIC_SCHEMA_VERSION, _migrate_music, _read_schema

    assert MUSIC_SCHEMA_VERSION == 6
    db = Database(tmp_path / "old.db", label="music")
    await db.connect()
    try:
        # Имитируем v3: базовая схема без колонок v4.
        await db.executescript(_read_schema("schema_music.sql"))
        await db.set_user_version(3)

        await _migrate_music(db)
        assert await db.get_user_version() == 6

        cols = {r["name"] for r in await db.fetch_all("PRAGMA table_info(playlists)")}
        assert {"origin_folder_path", "synced_at"} <= cols
        idx = {r["name"] for r in await db.fetch_all("PRAGMA index_list(tracks)")}
        assert "ix_tracks_tags" in idx and "ix_tracks_content_hash" in idx
        pl_idx = {r["name"] for r in await db.fetch_all("PRAGMA index_list(playlist_tracks)")}
        assert "ix_playlist_tracks_pos" in pl_idx

        # Повторный запуск ничего не ломает (ALTER не выполняется дважды).
        await _migrate_music(db)
        assert await db.get_user_version() == 6
    finally:
        await db.close()


async def test_batch_lookups_use_indexes_not_scan(tmp_path):
    """Батч-запросы идут по ix_tracks_tags / ix_tracks_content_hash, а не по ix_tracks_missing.

    Фильтр missing в SQL убран специально (см. комментарий в folder_sync.py):
    с ним планировщик выбирал индекс по missing.
    """
    from app.db.database import Database
    from app.db.migrations import _migrate_music

    db = Database(tmp_path / "plan.db", label="music")
    await db.connect()
    try:
        await _migrate_music(db)
        tags_sql = (
            "SELECT id FROM tracks WHERE (artist, album, title) IN (VALUES (?,?,?),(?,?,?))"
        )
        plan = " ".join(str(r[3]) for r in await db.fetch_all(
            "EXPLAIN QUERY PLAN " + tags_sql, ("a", "b", "c", "d", "e", "f")))
        assert "ix_tracks_tags" in plan, plan
        assert "ix_tracks_missing" not in plan, plan

        hash_sql = "SELECT id FROM tracks WHERE content_hash IN (?,?)"
        plan = " ".join(str(r[3]) for r in await db.fetch_all(
            "EXPLAIN QUERY PLAN " + hash_sql, ("h1", "h2")))
        assert "ix_tracks_content_hash" in plan, plan
    finally:
        await db.close()
