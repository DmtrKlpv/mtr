"""Конфигурация приложения.

Все значения читаются из переменных окружения (12-factor), с безопасными
дефолтами, рассчитанными на запуск внутри Docker-контейнера, где музыка
проброшена в /music, а состояние (БД, ключи, кэш обложек) — в /data.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Значение по умолчанию для каталога с музыкой (Docker volume).
DEFAULT_MUSIC_DIR = "/music"
# Значение по умолчанию для каталога состояния (БД, ключ шифрования, кэш).
DEFAULT_DATA_DIR = "/data"

USERS_DB_NAME = "mtr_users.db"
MUSIC_DB_NAME = "mtr_music.db"


def _env_str(key: str, default: str) -> str:
    """Читает строковую переменную окружения, отбрасывая пустые значения."""
    raw = os.environ.get(key)
    if raw is None or not raw.strip():
        return default
    return raw.strip()


def _env_int(key: str, default: int, *, minimum: int, maximum: int) -> int:
    """Целочисленная переменная с ограничением диапазона.

    Некорректное значение не роняет запуск — возвращается значение по умолчанию.
    """
    raw = _env_str(key, "")
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, min(value, maximum))


def _env_bool(key: str, default: bool) -> bool:
    """Булева переменная: 1/true/yes/on — истина."""
    raw = _env_str(key, "")
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def _env_path(key: str, default: Path) -> Path:
    """Читает переменную окружения как путь."""
    return Path(_env_str(key, str(default))).expanduser()


@dataclass(frozen=True, slots=True)
class Settings:
    """Неизменяемый снимок конфигурации.

    Создаётся один раз при старте приложения и передаётся в сервисы явно,
    чтобы не тащить глобальное состояние и упростить тестирование.
    """

    music_dir: Path
    data_dir: Path
    users_db: Path
    music_db: Path
    covers_dir: Path
    secret_key: str | None
    secret_key_file: Path
    # Версия Subsonic API, которую мы декларируем клиентам.
    subsonic_api_version: str = "1.16.1"
    # Формат транскодирования по умолчанию (используется на этапе 2).
    default_transcode: str = "raw"
    # Максимум одновременных процессов ffmpeg. Защита от исчерпания CPU:
    # каждый транскод занимает ядро, а сервер не должен падать от 20 вкладок.
    max_transcodes: int = 4
    # Наблюдение за каталогом (watchfiles). Выключено по умолчанию.
    watch_enabled: bool = False
    # Принудительный опрос вместо inotify — для Windows-монтирований.
    watch_polling: bool = False
    # Период опроса в мс (только при watch_polling). 1000 — разумный
    # компромисс: задержка ~1-3 с при 0.2% ядра на 200 файлах.
    watch_poll_ms: int = 1000
    # Страховочный инкрементальный скан, секунд; 0 — выключен.
    scan_interval_sec: int = 0
    # Secure-флаг на cookie сессии: включать при работе за HTTPS.
    cookie_secure: bool = False
    # Уровень логов приложения и uvicorn.
    log_level: str = "INFO"
    # Сколько файлов разбирать параллельно при сканировании.
    scan_concurrency: int = 4

    @classmethod
    def from_env(cls) -> "Settings":
        """Собирает конфигурацию из окружения.

        MUSIC_DIR   — каталог с музыкальной библиотекой (read-only по смыслу).
        DATA_DIR    — каталог состояния: обе БД, ключ шифрования, кэш обложек.
        USERS_DB    — путь к mtr_users.db (по умолчанию DATA_DIR/mtr_users.db).
        MUSIC_DB    — путь к mtr_music.db (по умолчанию DATA_DIR/mtr_music.db).
        MTR_SECRET_KEY_FILE — файл с ключом шифрования. По умолчанию
                              DATA_DIR/secret.key, создаётся автоматически при
                              первом старте. Задавать переменную не нужно:
                              ключ живёт в томе с данными и переживает
                              пересоздание контейнера.
        """
        music_dir = _env_path("MUSIC_DIR", Path(DEFAULT_MUSIC_DIR))
        data_dir = _env_path("DATA_DIR", Path(DEFAULT_DATA_DIR))

        users_db = _env_path("USERS_DB", data_dir / USERS_DB_NAME)
        music_db = _env_path("MUSIC_DB", data_dir / MUSIC_DB_NAME)
        covers_dir = _env_path("COVERS_DIR", data_dir / "covers")

        return cls(
            music_dir=music_dir,
            data_dir=data_dir,
            users_db=users_db,
            music_db=music_db,
            covers_dir=covers_dir,
            secret_key=os.environ.get("MTR_SECRET_KEY") or None,
            secret_key_file=_env_path("MTR_SECRET_KEY_FILE", data_dir / "secret.key"),
            subsonic_api_version=_env_str("MTR_SUBSONIC_VERSION", "1.16.1"),
            default_transcode=_env_str("MTR_DEFAULT_TRANSCODE", "raw"),
            max_transcodes=_env_int("MTR_MAX_TRANSCODES", 4, minimum=1, maximum=64),
            watch_enabled=_env_bool("MTR_WATCH", False),
            watch_polling=_env_bool("MTR_WATCH_POLL", False),
            watch_poll_ms=_env_int("MTR_WATCH_POLL_MS", 1000, minimum=100, maximum=60000),
            scan_interval_sec=_env_int("MTR_SCAN_INTERVAL", 0, minimum=0, maximum=86400),
            cookie_secure=_env_bool("MTR_COOKIE_SECURE", False),
            log_level=_env_str("MTR_LOG_LEVEL", "INFO").upper(),
            scan_concurrency=_env_int("MTR_SCAN_CONCURRENCY", 4, minimum=1, maximum=32),
        )

    def ensure_directories(self) -> None:
        """Создаёт каталоги состояния, если их ещё нет.

        Каталог с музыкой намеренно НЕ создаётся: если volume не проброшен,
        мы хотим это заметить, а не молча получить пустую библиотеку.
        """
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.users_db.parent.mkdir(parents=True, exist_ok=True)
        self.music_db.parent.mkdir(parents=True, exist_ok=True)
        self.covers_dir.mkdir(parents=True, exist_ok=True)
