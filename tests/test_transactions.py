"""Регрессионные тесты дисциплины транзакций SQLite.

Здесь зафиксирован дефект, из-за которого сканирование могло отрапортовать
added=N при фактически потерянных треках. Три сценария:

1. Разные задачи не вклиниваются в чужую транзакцию (иначе откат одной
   задачи уничтожает уже «успешную» работу другой).
2. Транзакция из чужой задачи не падает с «cannot start a transaction
   within a transaction» (причина — неявный BEGIN модуля sqlite3, который
   отключён через isolation_level=None).
3. Вложенный блок из той же задачи откатывается через SAVEPOINT и не
   затрагивает внешние изменения.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from app.db.database import Database, DatabaseError


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "tx.db", label="tx")
    await database.connect()
    await database.execute("CREATE TABLE items (n INTEGER, tag TEXT)")
    try:
        yield database
    finally:
        await database.close()


async def _names(db: Database, tag: str) -> list[int]:
    rows = await db.fetch_all(
        "SELECT n FROM items WHERE tag = ? ORDER BY n", (tag,)
    )
    return [int(row["n"]) for row in rows]


# 1. Изоляция задач: падение A не уносит работу B
async def test_failed_transaction_does_not_rollback_other_task(db: Database):
    """Ключевой инвариант: успешная запись B выживает при откате A."""
    async def task_a() -> None:
        async with db.transaction():
            await db.execute("INSERT INTO items VALUES (1, 'a')")
            await asyncio.sleep(0.05)          # даём B войти в ожидание
            raise RuntimeError("сбой в A")

    async def task_b() -> None:
        await asyncio.sleep(0.01)              # входим, пока A в транзакции
        async with db.transaction():
            await db.execute("INSERT INTO items VALUES (2, 'b')")

    results = await asyncio.gather(task_a(), task_b(), return_exceptions=True)
    assert isinstance(results[0], RuntimeError)
    assert results[1] is None, "B не должна видеть исключение"

    # B зафиксирована, A откачена.
    assert await _names(db, "b") == [2]
    assert await _names(db, "a") == []


# 2. Нет «cannot start a transaction within a transaction»
async def test_no_implicit_transaction_conflict(db: Database):
    """isolation_level=None: sqlite3 не открывает транзакцию сам.

    С дефолтным isolation_level="" любой INSERT оставлял бы соединение
    внутри неявной транзакции, и следующий BEGIN IMMEDIATE падал бы с
    OperationalError. Проверяем именно это состояние.
    """
    assert db.conn.isolation_level is None
    await db.execute("INSERT INTO items VALUES (10, 'x')")
    assert db.conn.in_transaction is False, "autocommit должен фиксировать сразу"

    # И явная транзакция после одиночной записи работает.
    async with db.transaction():
        await db.execute("INSERT INTO items VALUES (11, 'x')")
    assert sorted(await _names(db, "x")) == [10, 11]


async def test_many_concurrent_transactions_all_commit(db: Database):
    """200 конкурентных транзакций: ни одной ошибки и ни одной потери."""
    errors: list[str] = []

    async def writer(n: int) -> None:
        try:
            async with db.transaction():
                await db.execute("INSERT INTO items VALUES (?, 'c')", (n,))
                await asyncio.sleep(0)          # точка переключения задач
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")

    await asyncio.gather(*(writer(n) for n in range(200)))

    assert errors == []
    assert len(await _names(db, "c")) == 200


async def test_mixed_single_and_transactional_writes(db: Database):
    """Одиночные записи и транзакции вперемешку не мешают друг другу."""
    errors: list[str] = []

    async def tx(n: int) -> None:
        try:
            async with db.transaction():
                await db.execute("INSERT INTO items VALUES (?, 'tx')", (n,))
                await asyncio.sleep(0)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"tx{n}: {exc}")

    async def single(n: int) -> None:
        try:
            await db.execute("INSERT INTO items VALUES (?, 'single')", (n,))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"single{n}: {exc}")

    await asyncio.gather(*(fn(i) for i in range(40) for fn in (tx, single)))

    assert errors == []
    assert len(await _names(db, "tx")) == 40
    assert len(await _names(db, "single")) == 40


# 3. Вложенные транзакции через SAVEPOINT
async def test_nested_rollback_preserves_outer_changes(db: Database):
    """Откат внутреннего блока не отменяет то, что сделал внешний."""
    async with db.transaction():
        await db.execute("INSERT INTO items VALUES (100, 'outer')")
        with pytest.raises(ValueError):
            async with db.transaction():
                await db.execute("INSERT INTO items VALUES (101, 'inner')")
                raise ValueError("сбой внутри вложенного")
        await db.execute("INSERT INTO items VALUES (102, 'outer')")

    assert await _names(db, "outer") == [100, 102]
    assert await _names(db, "inner") == []


async def test_nested_commit_persists_with_outer(db: Database):
    """Успешный вложенный блок фиксируется вместе с внешним."""
    async with db.transaction():
        await db.execute("INSERT INTO items VALUES (1, 'o')")
        async with db.transaction():
            await db.execute("INSERT INTO items VALUES (2, 'i')")
        async with db.transaction():
            await db.execute("INSERT INTO items VALUES (3, 'i')")

    assert await _names(db, "i") == [2, 3]
    assert await _names(db, "o") == [1]


async def test_outer_failure_discards_nested_commits(db: Database):
    """Вложенные блоки не фиксируются, если внешний откатился."""
    with pytest.raises(RuntimeError):
        async with db.transaction():
            await db.execute("INSERT INTO items VALUES (1, 'o')")
            async with db.transaction():
                await db.execute("INSERT INTO items VALUES (2, 'i')")
            raise RuntimeError("внешний откат")

    assert await _names(db, "o") == []
    assert await _names(db, "i") == []


# 4. Сбой COMMIT не должен молча терять данные
async def test_commit_failure_raises_instead_of_swallowing(db: Database):
    """Если COMMIT не удался, вызывающий код обязан узнать об этом.

    Раньше COMMIT был обёрнут в suppress(sqlite3.Error): транзакция
    считалась успешной при фактически потерянных данных.

    Подмена делается присваиванием атрибута экземпляра, а не monkeypatch:
    execute у aiosqlite.Connection — слот-дескриптор, и setattr через
    monkeypatch его не перекрывает.
    """
    real_conn = db.conn
    original_execute = real_conn.execute

    async def execute_failing_commit(sql, *args, **kwargs):
        # Падает только COMMIT, всё остальное проходит штатно.
        if isinstance(sql, str) and sql.strip().upper() == "COMMIT":
            raise sqlite3.OperationalError("disk I/O error")
        return await original_execute(sql, *args, **kwargs)

    real_conn.execute = execute_failing_commit
    try:
        with pytest.raises(DatabaseError, match="зафиксировать"):
            async with db.transaction():
                await db.execute("INSERT INTO items VALUES (1, 'x')")
    finally:
        real_conn.execute = original_execute

    # После сбоя соединение не залипло в открытой транзакции.
    assert db.conn.in_transaction is False


