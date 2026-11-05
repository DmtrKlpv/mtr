"""Инициализация и миграции двух баз данных.

Версионирование через PRAGMA user_version:
    0 — пустая база;
    1 — базовая схема (mtr_users / mtr_music);
    2 — сессии веб-интерфейса (только mtr_users);

Миграции применяются по порядку и идемпотентны: повторный запуск
на уже актуальной базе ничего не делает. DDL внутри каждого шага
выполняется под транзакцией, а версия выставляется только после успеха.
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.config import MUSIC_DB_NAME, USERS_DB_NAME
from app.db.database import Database, DatabaseError

logger = logging.getLogger(__name__)

SCHEMA_DIR = Path(__file__).resolve().parent

# Текущая целевая версия схемы mtr_users.db.
SCHEMA_VERSION = 2
# У музыкальной базы своя версия: она меняется чаще, и поднимать из-за этого
# версию пользовательской базы (с паролями и сессиями) незачем.
MUSIC_SCHEMA_VERSION = 6

# v2: сессии веб-интерфейса. В v1 таблица sessions уже есть, но хранит
# открытый id и не умеет CSRF. Миграция перестраивает её:
#   * token_hash  — SHA-256 токена из cookie (открытый токен не храним);
#   * csrf_token  — привязка CSRF к сессии;
#   * id INTEGER  — внутренний ключ вместо TEXT.
# Старые сессии удаляются: они всё равно недействительны без token_hash,
# а таблица на этом этапе пуста (веб-интерфейса ещё не было).
MIGRATION_V2_SESSIONS = """
DROP TABLE IF EXISTS sessions;
CREATE TABLE sessions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash   TEXT    NOT NULL UNIQUE,
    user_id      INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    csrf_token   TEXT    NOT NULL,
    created_at   TEXT    NOT NULL,
    expires_at   TEXT    NOT NULL,
    last_seen_at TEXT    NOT NULL,
    user_agent   TEXT,
    ip           TEXT
);
CREATE INDEX IF NOT EXISTS ix_sessions_user ON sessions (user_id);
CREATE INDEX IF NOT EXISTS ix_sessions_expires ON sessions (expires_at);
"""

# v3 (только mtr_music.db): тексты ошибок сканирования.
# Раньше в scan_runs попадали только счётчики, поэтому «ошибок 1» в интерфейсе
# нельзя было раскрыть до конкретного файла. Теперь храним и текст.
# errors_json — список строк, ограниченный при записи (см. TrackRepository).
MIGRATION_MUSIC_V3_ERRORS = """
ALTER TABLE scan_runs ADD COLUMN errors_total INTEGER NOT NULL DEFAULT 0;
ALTER TABLE scan_runs ADD COLUMN errors_json TEXT;
"""

# v5 (только mtr_music.db): очередь команд для worker-процесса (скан, перечитать настройки).
MIGRATION_MUSIC_V5_WORKER = """
CREATE TABLE IF NOT EXISTS worker_commands (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT    NOT NULL,
    payload     TEXT,
    created_at  TEXT    NOT NULL,
    handled_at  TEXT
);
CREATE INDEX IF NOT EXISTS ix_worker_commands_pending ON worker_commands (handled_at, id);
"""

# v4 (только mtr_music.db): импорт плейлистов из папки и ресинк.
#   * tracks: индекс по content_hash (поиск перемещённых файлов по хэшу);
#   * tracks: составной индекс (artist, album, title) — запасной ключ поиска,
#     когда хэш не посчитан (он ленивый). Не уникальный: дубли в библиотеке реальны;
#   * playlists: origin_folder_path — папка, из которой собран плейлист;
#     synced_at — когда последний раз проверяли папку;
#   * playlist_tracks(playlist_id, position) — порядок и выборка для ресинка.
MIGRATION_MUSIC_V4_FOLDER_SYNC = """
CREATE INDEX IF NOT EXISTS ix_tracks_content_hash ON tracks (content_hash);
CREATE INDEX IF NOT EXISTS ix_tracks_tags ON tracks (artist, album, title);
ALTER TABLE playlists ADD COLUMN origin_folder_path TEXT;
ALTER TABLE playlists ADD COLUMN synced_at TEXT;
CREATE INDEX IF NOT EXISTS ix_playlists_origin ON playlists (origin_folder_path);
CREATE INDEX IF NOT EXISTS ix_playlist_tracks_pos ON playlist_tracks (playlist_id, position);
"""


def _read_schema(filename: str) -> str:
    """Читает SQL-скрипт схемы рядом с этим модулем."""
    path = SCHEMA_DIR / filename
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DatabaseError(f"не удалось прочитать схему {path}: {exc}") from exc


async def _migrate_users(db: Database) -> None:
    """Приводит mtr_users.db к SCHEMA_VERSION."""
    current = await db.get_user_version()
    if current > SCHEMA_VERSION:
        raise DatabaseError(
            f"mtr_users.db имеет версию схемы {current}, а код поддерживает "
            f"только {SCHEMA_VERSION}. База создана более новой версией."
        )
    if current == SCHEMA_VERSION and current != 0:
        logger.debug("mtr_users.db уже на версии %d", current)
        return

    if current < 1:
        logger.info("mtr_users.db: применяю базовую схему (v1)")
        await db.executescript(_read_schema("schema_users.sql"))
    if current < 2:
        logger.info("mtr_users.db: применяю миграцию v2 (сессии)")
        await db.executescript(MIGRATION_V2_SESSIONS)

    await db.set_user_version(SCHEMA_VERSION)
    logger.info("mtr_users.db готова, версия схемы %d", SCHEMA_VERSION)


async def _migrate_music(db: Database) -> None:
    """Приводит mtr_music.db к MUSIC_SCHEMA_VERSION."""
    current = await db.get_user_version()
    if current > MUSIC_SCHEMA_VERSION:
        raise DatabaseError(
            f"mtr_music.db имеет версию схемы {current}, а код поддерживает "
            f"только {MUSIC_SCHEMA_VERSION}. База создана более новой версией."
        )
    if current == MUSIC_SCHEMA_VERSION and current != 0:
        logger.debug("mtr_music.db уже на версии %d", current)
        return

    # Ветки именно elif: свежая база получает нужные колонки из schema_music.sql,
    # и повторный ALTER упал бы с «duplicate column name».
    if current < 1:
        logger.info("mtr_music.db: применяю базовую схему (v3)")
        await db.executescript(_read_schema("schema_music.sql"))
    elif current < 3:
        logger.info("mtr_music.db: применяю миграцию v3 (ошибки сканирования)")
        await db.executescript(MIGRATION_MUSIC_V3_ERRORS)
    if current < 4:
        # Для свежей базы сюда попадаем после базовой схемы (она v3-эквивалентна),
        # поэтому ALTER из v4 применяется ровно один раз в любом пути.
        logger.info("mtr_music.db: применяю миграцию v4 (импорт папок, индексы)")
        await db.executescript(MIGRATION_MUSIC_V4_FOLDER_SYNC)
    if current < 5:
        logger.info("mtr_music.db: применяю миграцию v5 (очередь команд worker)")
        await db.executescript(MIGRATION_MUSIC_V5_WORKER)

    # Статистика для планировщика SQLite. Без неё план запросов по исполнителям
    # выбирается вслепую: индекс tracks(missing) с двумя значениями кажется
    # селективным, и каждый запрос перебирает почти всю таблицу треков.
    # Замер: list_artists на 20k треков — 3.7 с без статистики, 23 мс с ней.
    # ANALYZE один раз при обновлении схемы; дальше PRAGMA optimize держит статистику.
    if current < MUSIC_SCHEMA_VERSION:
        await db.execute("ANALYZE")

    await db.set_user_version(MUSIC_SCHEMA_VERSION)
    logger.info("mtr_music.db готова, версия схемы %d", MUSIC_SCHEMA_VERSION)


async def init_databases(users_db: Database, music_db: Database) -> None:
    """Открывает и мигрирует обе базы.

    Открытие идёт последовательно и с явными именами в логах: если сломается
    одна база, сообщение сразу скажет какая, а вторая останется нетронутой.
    """
    await users_db.connect()
    try:
        await _migrate_users(users_db)
    except Exception:
        await users_db.close()
        raise

    await music_db.connect()
    try:
        await _migrate_music(music_db)
    except Exception:
        await music_db.close()
        await users_db.close()
        raise

    logger.info(
        "обе базы инициализированы: %s, %s",
        users_db.path.name or USERS_DB_NAME,
        music_db.path.name or MUSIC_DB_NAME,
    )
