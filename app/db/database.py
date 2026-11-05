"""Асинхронный слой доступа к SQLite на aiosqlite.

Задачи модуля:
  * одно долгоживущее соединение на базу (SQLite плохо любит много писателей);
  * WAL-режим, чтобы сканер мог писать, пока API читает;
  * ретраи на SQLITE_BUSY / SQLITE_LOCKED с экспоненциальной паузой;
  * строгая дисциплина транзакций: транзакция принадлежит ОДНОЙ задаче
    и на время её жизни блокирует запись из других задач.

Почему дисциплина транзакций вынесена в отдельный слой
-----------------------------------------------------
У SQLite транзакция — свойство соединения, а не логической операции. Если
две корутины делят одно соединение, между их `await` транзакции
перемешиваются. Наивный счётчик глубины (`_tx_depth`) это НЕ решает:

    A: transaction() -> BEGIN, depth=1
    B: transaction() -> видит depth=1, своего BEGIN не делает, depth=2
    B: выходит      -> depth=1, COMMIT не делает (is_outer=False)
    A: падает       -> ROLLBACK откатывает и работу B

Итог: B получила «успех», её данные потеряны, а сканер отчитался
added=N. Ровно поэтому здесь используется блокировка с владельцем:

  * `transaction()` берёт `_tx_lock` и запоминает задачу-владельца;
  * повторный вход ИЗ ТОЙ ЖЕ задачи считается вложенным (реентерабельность);
  * записи из чужих задач ждут на `_tx_lock`, а не влезают в чужую транзакцию;
  * `isolation_level=None` отключает неявный BEGIN модуля sqlite3, иначе
    Python открывает транзакцию сам и получается «cannot start a transaction
    within a transaction».
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from collections.abc import AsyncIterator, Iterable, Sequence
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

# Сколько раз повторять запрос, если база занята другим соединением.
BUSY_RETRIES = 5
# Базовая пауза между ретраями (удваивается).
BUSY_BASE_DELAY = 0.05

_BUSY_CODES = (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)


class DatabaseError(RuntimeError):
    """Оборачивает ошибки SQLite, чтобы наружу не летели сырые исключения."""


class Database:
    """Обёртка над одним файлом SQLite.

    Экземпляр создаётся в единственном числе на базу и живёт всё время
    работы приложения. Перед использованием обязателен `await connect()`.
    """

    def __init__(self, path: Path, *, label: str) -> None:
        self.path = Path(path)
        self.label = label
        self._conn: aiosqlite.Connection | None = None
        # Отдельное соединение только для чтения (WAL): чтения веба не стоят в очереди
        # за записями сканера в одном потоке aiosqlite.
        self._reader_conn: aiosqlite.Connection | None = None

        # Сериализация записи и владение транзакцией.
        self._tx_lock = asyncio.Lock()
        self._tx_owner: asyncio.Task[Any] | None = None
        # Глубина вложенности — имеет смысл ТОЛЬКО для задачи-владельца.
        self._tx_depth = 0

    # ------------------------------------------------------------------
    # Жизненный цикл
    # ------------------------------------------------------------------
    async def connect(self) -> None:
        """Открывает соединение и выставляет PRAGMA."""
        if self._conn is not None:
            return

        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # isolation_level=None — режим autocommit: sqlite3 не вставляет
            # неявный BEGIN перед DML. Транзакциями управляем только мы,
            # через явный BEGIN IMMEDIATE в transaction().
            conn = await aiosqlite.connect(self.path, isolation_level=None)
        except sqlite3.Error as exc:  # pragma: no cover - только при сбое ФС
            raise DatabaseError(f"[{self.label}] не удалось открыть {self.path}") from exc

        conn.row_factory = aiosqlite.Row
        self._conn = conn

        # WAL: конкурентное чтение при записи. NORMAL — разумный компромисс
        # между долговечностью и скоростью для медиасервера.
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        # Таймаут ожидания блокировки на уровне SQLite (мс).
        await conn.execute("PRAGMA busy_timeout=5000")
        await conn.execute("PRAGMA temp_store=MEMORY")
        if str(self.path) != ":memory:":
            reader = await aiosqlite.connect(self.path, isolation_level=None)
            reader.row_factory = aiosqlite.Row
            await reader.execute("PRAGMA busy_timeout=5000")
            await reader.execute("PRAGMA query_only=ON")
            self._reader_conn = reader
        logger.info("[%s] подключена база %s", self.label, self.path)

    async def close(self) -> None:
        """Закрывает соединение, дожидаясь сброса WAL."""
        if self._conn is None:
            return
        if self._reader_conn is not None:
            await self._reader_conn.close()
            self._reader_conn = None
        with contextlib.suppress(sqlite3.Error):
            # TRUNCATE сворачивает -wal файл; полезно при остановке контейнера.
            await self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        await self._conn.close()
        self._conn = None
        self._tx_owner = None
        self._tx_depth = 0
        logger.info("[%s] соединение закрыто", self.label)

    @property
    def conn(self) -> aiosqlite.Connection:
        """Соединение или понятная ошибка, если забыли connect()."""
        if self._conn is None:
            raise DatabaseError(
                f"[{self.label}] соединение не открыто — вызовите await connect()"
            )
        return self._conn

    def _reader(self) -> aiosqlite.Connection:
        """Соединение для чтения.

        Владелец открытой транзакции читает через writer: ему нужны его же незакоммиченные
        строки. Остальные читают через читающее соединение и видят последний закоммиченный
        снимок, не дожидаясь записи в очереди.
        """
        if self._reader_conn is None or self._owns_transaction():
            return self.conn
        return self._reader_conn

    @property
    def in_transaction(self) -> bool:
        """Открыта ли транзакция (для диагностики и тестов)."""
        return self._tx_owner is not None

    # ------------------------------------------------------------------
    # Дисциплина записи
    # ------------------------------------------------------------------
    def _owns_transaction(self) -> bool:
        """Принадлежит ли текущая транзакция вызывающей задаче."""
        task = asyncio.current_task()
        return task is not None and self._tx_owner is task

    @contextlib.asynccontextmanager
    async def _write_scope(self) -> AsyncIterator[None]:
        """Сериализует запись относительно других задач.

        Реентерабельно: если задача уже владеет транзакцией, повторный вход
        не берёт блокировку (иначе был бы самоблокировка на asyncio.Lock).
        Чужая задача ждёт освобождения, а не пишет внутрь чужой транзакции.
        """
        if self._owns_transaction():
            yield
            return

        async with self._tx_lock:
            self._tx_owner = asyncio.current_task()
            self._tx_depth = 0
            try:
                yield
            finally:
                self._tx_owner = None
                self._tx_depth = 0

    # ------------------------------------------------------------------
    # Низкоуровневые операции
    # ------------------------------------------------------------------
    async def _run_with_retry(self, factory, *, what: str):
        """Повторяет операцию при SQLITE_BUSY/LOCKED."""
        delay = BUSY_BASE_DELAY
        last_exc: Exception | None = None

        for attempt in range(1, BUSY_RETRIES + 1):
            try:
                return await factory()
            except sqlite3.OperationalError as exc:
                code = getattr(exc, "sqlite_errorcode", None)
                message = str(exc).lower()
                is_busy = (
                    code in _BUSY_CODES
                    or "database is locked" in message
                    or "database table is locked" in message
                )
                if not is_busy or attempt == BUSY_RETRIES:
                    last_exc = exc
                    break
                logger.debug(
                    "[%s] %s: база занята, ретрай %d/%d через %.2fs",
                    self.label, what, attempt, BUSY_RETRIES, delay,
                )
                await asyncio.sleep(delay)
                delay *= 2
            except sqlite3.Error as exc:
                last_exc = exc
                break

        raise DatabaseError(
            f"[{self.label}] ошибка при {what}: {last_exc}"
        ) from last_exc

    async def execute(self, sql: str, params: Sequence[Any] | None = None) -> int:
        """Выполняет одиночный запрос, возвращает lastrowid.

        В режиме autocommit (isolation_level=None) запись фиксируется сразу,
        если мы не внутри явной транзакции. Внутри транзакции фиксацию делает
        её владелец — здесь COMMIT не вызывается никогда, иначе транзакция
        развалилась бы на середине.
        """

        async def _do() -> int:
            cursor = await self.conn.execute(sql, tuple(params or ()))
            last_id = cursor.lastrowid or 0
            await cursor.close()
            return int(last_id)

        async with self._write_scope():
            return await self._run_with_retry(_do, what="execute")

    async def execute_many(
        self, sql: str, seq_of_params: Iterable[Sequence[Any]]
    ) -> int:
        """Пакетная вставка/обновление, возвращает число затронутых строк."""

        async def _do() -> int:
            cursor = await self.conn.executemany(
                sql, [tuple(p) for p in seq_of_params]
            )
            affected = cursor.rowcount if cursor.rowcount != -1 else 0
            await cursor.close()
            return int(affected)

        async with self._write_scope():
            return await self._run_with_retry(_do, what="execute_many")

    async def executescript(self, script: str) -> None:
        """Выполняет SQL-скрипт целиком (DDL, миграции)."""

        async def _do() -> None:
            await self.conn.executescript(script)

        async with self._write_scope():
            await self._run_with_retry(_do, what="executescript")

    async def fetch_one(
        self, sql: str, params: Sequence[Any] | None = None
    ) -> aiosqlite.Row | None:
        """Возвращает первую строку или None."""

        async def _do():
            cursor = await self._reader().execute(sql, tuple(params or ()))
            try:
                return await cursor.fetchone()
            finally:
                await cursor.close()

        return await self._run_with_retry(_do, what="fetch_one")

    async def fetch_all(
        self, sql: str, params: Sequence[Any] | None = None
    ) -> list[aiosqlite.Row]:
        """Возвращает все строки."""

        async def _do():
            cursor = await self._reader().execute(sql, tuple(params or ()))
            try:
                return await cursor.fetchall()
            finally:
                await cursor.close()

        return await self._run_with_retry(_do, what="fetch_all")

    async def fetch_value(
        self, sql: str, params: Sequence[Any] | None = None, *, default: Any = None
    ) -> Any:
        """Возвращает первое поле первой строки или default."""
        row = await self.fetch_one(sql, params)
        if row is None:
            return default
        return row[0]

    # ------------------------------------------------------------------
    # Транзакции
    # ------------------------------------------------------------------
    @contextlib.asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        """Атомарный блок, принадлежащий одной задаче.

        Три уровня поведения:

        1. Вызов из ЧУЖОЙ задачи — ждёт на `_tx_lock` и открывает свою
           транзакцию. Именно это устраняет «cannot start a transaction
           within a transaction» и потерю чужих данных.

        2. Вложенный вызов из ТОЙ ЖЕ задачи — оборачивается в SAVEPOINT.
           Откат внутреннего блока отменяет только его работу, внешние
           изменения остаются. Без SAVEPOINT откат внутреннего блока
           уничтожил бы всё, что внешний уже сделал.

        3. Ошибка COMMIT/ROLLBACK больше НЕ проглатывается: если фиксация
           не удалась, вызывающий код обязан об этом узнать, иначе скан
           отрапортует added=N при фактически потерянных данных.
        """
        if self._owns_transaction():
            # Вложенный уровень: SAVEPOINT вместо нового BEGIN.
            self._tx_depth += 1
            savepoint = f"mtr_sp_{self._tx_depth}"
            await self.conn.execute(f"SAVEPOINT {savepoint}")
            try:
                yield
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    await self.conn.execute(f"ROLLBACK TO {savepoint}")
                with contextlib.suppress(sqlite3.Error):
                    await self.conn.execute(f"RELEASE {savepoint}")
                raise
            else:
                with contextlib.suppress(sqlite3.Error):
                    await self.conn.execute(f"RELEASE {savepoint}")
            finally:
                self._tx_depth -= 1
            return

        async with self._tx_lock:
            self._tx_owner = asyncio.current_task()
            self._tx_depth = 1
            try:
                await self.conn.execute("BEGIN IMMEDIATE")
                try:
                    yield
                except BaseException:
                    await self._rollback_outer()
                    raise
                else:
                    await self._commit_outer()
            finally:
                self._tx_owner = None
                self._tx_depth = 0

    async def _commit_outer(self) -> None:
        """COMMIT с явной обработкой сбоя — молча терять данные нельзя."""
        try:
            await self.conn.execute("COMMIT")
        except sqlite3.Error as exc:
            logger.error("[%s] COMMIT не удался: %s", self.label, exc)
            # Транзакция могла остаться открытой: пробуем откатить,
            # чтобы соединение не залипло в состоянии с блокировкой записи.
            with contextlib.suppress(sqlite3.Error):
                await self.conn.execute("ROLLBACK")
            raise DatabaseError(
                f"[{self.label}] не удалось зафиксировать транзакцию: {exc}"
            ) from exc

    async def _rollback_outer(self) -> None:
        """ROLLBACK с логированием: сбой отката оставляет соединение грязным."""
        try:
            await self.conn.execute("ROLLBACK")
        except sqlite3.Error as exc:
            # Откат падает, если транзакции уже нет (например, SQLite сам
            # откатил её при грубой ошибке) — это не повод ронять скан.
            logger.warning("[%s] ROLLBACK не выполнен: %s", self.label, exc)

    # ------------------------------------------------------------------
    # Миграции
    # ------------------------------------------------------------------
    async def optimize(self) -> None:
        """Лёгкое обновление статистики планировщика (PRAGMA optimize).

        Вызывается после записи больших объёмов (скан), чтобы планы запросов
        не деградировали на растущей библиотеке. Ошибки не критичны.
        """
        try:
            await self.execute("PRAGMA optimize")
        except Exception:  # noqa: BLE001 - оптимизация не должна ронять запись
            logger.debug("[%s] PRAGMA optimize не выполнен", self.label, exc_info=True)

    async def get_user_version(self) -> int:
        """Текущая версия схемы (PRAGMA user_version)."""
        return int(await self.fetch_value("PRAGMA user_version", default=0) or 0)

    async def set_user_version(self, version: int) -> None:
        """Фиксирует версию схемы. PRAGMA не принимает плейсхолдеры."""
        if version < 0:
            raise ValueError("версия схемы не может быть отрицательной")
        async with self._write_scope():
            await self.conn.execute(f"PRAGMA user_version={int(version)}")
