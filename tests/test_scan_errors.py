"""Ошибки сканирования: структура «файл / причина» и отдача в API.

Раньше в историю писалась склеенная строка «путь: причина», и интерфейс
не мог показать её таблицей, не разбирая текст обратно.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.repositories.tracks import ScanCounters, ScanError
from app.web.api import _scan_errors


# ----------------------------------------------------------------------
# Счётчики
# ----------------------------------------------------------------------
def test_add_error_keeps_path_and_reason():
    counters = ScanCounters()
    counters.add_error("/music/a.mp3", "HeaderNotFoundError: can't sync")

    assert counters.failed == 1
    assert len(counters.errors) == 1
    assert counters.errors[0].path == "/music/a.mp3"
    assert counters.errors[0].reason.startswith("HeaderNotFoundError")


def test_add_error_does_not_grow_without_limit():
    """При массовом сбое список не должен расти бесконечно."""
    from app.repositories.tracks import MAX_STORED_ERRORS

    counters = ScanCounters()
    for i in range(MAX_STORED_ERRORS + 50):
        counters.add_error(f"/music/{i}.mp3", "битый файл")

    # Счётчик полный, список — обрезан.
    assert counters.failed == MAX_STORED_ERRORS + 50
    assert len(counters.errors) == MAX_STORED_ERRORS


# ----------------------------------------------------------------------
# Разбор из БД
# ----------------------------------------------------------------------
class _Row(dict):
    def keys(self):
        return super().keys()


def _row(payload: str | None, total: int = 0, failed: int = 0) -> _Row:
    return _Row(errors_json=payload, errors_total=total, files_failed=failed)


def test_parses_structured_errors():
    payload = json.dumps([{"path": "/music/a.flac", "reason": "не FLAC"}])
    total, items = _scan_errors(_row(payload, total=1))

    assert total == 1
    assert items == [{"path": "/music/a.flac", "reason": "не FLAC"}]


def test_parses_legacy_string_format():
    """Записи, сделанные до перехода на структуру, тоже должны читаться."""
    payload = json.dumps(["/music/a.mp3: FLACNoHeaderError: not valid"])
    total, items = _scan_errors(_row(payload, total=1))

    assert total == 1
    assert items[0]["path"] == "/music/a.mp3"
    assert items[0]["reason"] == "FLACNoHeaderError: not valid"


def test_legacy_string_with_colons_in_reason():
    """Причина сама содержит двоеточия — режем только по первому."""
    payload = json.dumps(["/music/a.mp3: ошибка: ещё: детали"])
    _, items = _scan_errors(_row(payload, total=1))

    assert items[0]["path"] == "/music/a.mp3"
    assert items[0]["reason"] == "ошибка: ещё: детали"


def test_counts_fall_back_to_files_failed():
    """У записей без errors_total число берём из files_failed."""
    total, items = _scan_errors(_row(None, total=0, failed=3))

    assert total == 3
    assert items == []


def test_broken_json_does_not_crash():
    total, items = _scan_errors(_row("{не json", total=2))

    assert total == 2
    assert items == []


# ----------------------------------------------------------------------
# Запись в историю
# ----------------------------------------------------------------------
async def test_scan_writes_error_objects_to_db(ctx, music_dir: Path):
    (music_dir / "broken.mp3").write_bytes(b"not audio at all")

    result = await ctx.scanner.scan(mode="full")

    row = await ctx.music_db.fetch_one(
        "SELECT errors_total, errors_json FROM scan_runs WHERE id = ?",
        (result.run_id,),
    )
    assert int(row["errors_total"]) == 1

    stored = json.loads(row["errors_json"])
    assert isinstance(stored, list) and len(stored) == 1
    assert stored[0]["path"].endswith("broken.mp3")
    assert stored[0]["reason"]

    # И в ответе на запуск — та же структура.
    assert result.as_dict()["errors"][0]["path"].endswith("broken.mp3")


async def test_finish_scan_run_accepts_scan_error_objects(ctx):
    run_id = await ctx.tracks.start_scan_run("full")
    counters = ScanCounters()
    counters.seen = 10
    counters.add_error("/music/x.mp3", "тестовая причина")

    await ctx.tracks.finish_scan_run(run_id, status="completed", counters=counters)

    row = await ctx.music_db.fetch_one(
        "SELECT errors_total, errors_json FROM scan_runs WHERE id = ?", (run_id,)
    )
    assert int(row["errors_total"]) == 1
    assert json.loads(row["errors_json"])[0]["reason"] == "тестовая причина"


def test_scan_error_as_text():
    error = ScanError(path="/m/a.mp3", reason="битый")
    assert error.as_text() == "/m/a.mp3: битый"
    assert error.as_dict() == {"path": "/m/a.mp3", "reason": "битый"}
