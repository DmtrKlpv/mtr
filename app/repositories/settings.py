"""Настройки, изменяемые в рантайме (таблица `settings` в mtr_users.db).

Зачем отдельно от docker-compose.yml: часть параметров меняется в процессе работы
(интервал скана, лимит транскодов, формат по умолчанию), и требовать
перезапуск контейнера ради них — плохой опыт. Значения из БД главнее
переменных окружения, но только для белого списка ниже.

Что НЕ меняется намеренно:
  * MTR_SECRET_KEY — смена ломает расшифровку паролей (см. README);
  * MUSIC_DIR / DATA_DIR — пути монтируются в контейнер, менять на лету нельзя;
  * учётные данные администратора — это пользователи, не настройки.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.db.database import Database
from app.utils.timeutil import utcnow_iso


@dataclass(frozen=True, slots=True)
class SettingSpec:
    """Описание изменяемой настройки: тип, границы, значение по умолчанию."""

    key: str
    kind: str            # "int" | "bool" | "choice" | "text"
    default: Any
    label: str
    help: str
    minimum: int | None = None
    maximum: int | None = None
    choices: tuple[str, ...] = ()
    # Значение читается при старте. UI помечает такие пункты «после перезапуска»,
    # а apply_runtime_settings не подхватывает их на лету.
    restart: bool = False

    def coerce(self, raw: Any) -> Any:
        """Приводит значение к типу и проверяет границы.

        Бросает ValueError с понятным текстом: страница настроек показывает
        причину отказа, а не молча игнорирует ввод.
        """
        if self.kind == "bool":
            if isinstance(raw, bool):
                return raw
            text = str(raw).strip().lower()
            if text in ("1", "true", "yes", "on"):
                return True
            if text in ("0", "false", "no", "off"):
                return False
            raise ValueError(f"{self.label}: ожидалось да/нет")

        if self.kind == "int":
            try:
                value = int(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{self.label}: ожидалось целое число") from exc
            if self.minimum is not None and value < self.minimum:
                raise ValueError(f"{self.label}: минимум {self.minimum}")
            if self.maximum is not None and value > self.maximum:
                raise ValueError(f"{self.label}: максимум {self.maximum}")
            return value

        if self.kind == "text":
            # Свободная строка: путь к папке, префикс. Обрезаем пробелы и
            # ограничиваем длину, чтобы не хранить мегабайты в настройках.
            return str(raw if raw is not None else "").strip()[:512]

        if self.kind == "choice":
            text = str(raw)
            if text not in self.choices:
                raise ValueError(
                    f"{self.label}: допустимо {', '.join(self.choices)}"
                )
            return text

        raise ValueError(f"{self.label}: неизвестный тип настройки")


# Белый список: только эти ключи можно менять из интерфейса.
SPECS: dict[str, SettingSpec] = {
    "scan_interval_sec": SettingSpec(
        key="scan_interval_sec", kind="int", default=0,
        label="Интервал скана",
        help="Страховочный инкрементальный скан, секунд. 0 — выключен.",
        minimum=0, maximum=86400,
    ),
    "max_transcodes": SettingSpec(
        key="max_transcodes", kind="int", default=4,
        label="Лимит транскодов",
        help="Сколько ffmpeg-процессов одновременно. Применяется к новым стримам.",
        minimum=1, maximum=64,
    ),
    "cover_active_window_sec": SettingSpec(
        key="cover_active_window_sec", kind="int", default=3,
        label="Пауза обложек: окно активности, сек",
        help="Сколько секунд после последнего действия в интерфейсе фаза обложек ждёт. "
             "Меньше — обложки заполняются быстрее, но сильнее мешают просмотру. Применяется сразу.",
        minimum=1, maximum=60,
    ),
    "cover_max_wait_sec": SettingSpec(
        key="cover_max_wait_sec", kind="int", default=30,
        label="Пауза обложек: предельное ожидание, сек",
        help="Сколько фаза ждёт одного альбома из-за активности, потом идёт дальше, "
             "чтобы не остановиться совсем. Применяется сразу.",
        minimum=5, maximum=600,
    ),
    "scan_batch_tracks": SettingSpec(
        key="scan_batch_tracks", kind="int", default=0,
        label="Разбивать скан по треков",
        help="Сколько треков обрабатывать за один заход. Следующий заход начинается только "
             "после записи предыдущего. 0 — все сразу (по умолчанию). 100–500 — меньше "
             "блокировок и заметнее прогресс на большой библиотеке.",
        minimum=0, maximum=10000,
    ),
    "default_transcode": SettingSpec(
        key="default_transcode", kind="choice", default="raw",
        label="Формат по умолчанию",
        help="Формат отдачи, если клиент не указал свой.",
        choices=("raw", "mp3_128", "mp3_256", "mp3_320", "mp3_v0", "flac", "alac"),
    ),
    "library_view": SettingSpec(
        key="library_view", kind="choice", default="albums",
        label="Вид библиотеки по умолчанию",
        help="Что открывается при входе: исполнители, альбомы или плейлисты.",
        choices=("artists", "albums", "playlists"),
    ),
    "playlists_dir": SettingSpec(
        key="playlists_dir", kind="text", default="",
        label="Папка с плейлистами",
        help="Каталог внутри контейнера, где лежат .m3u/.m3u8 (обходится "
             "со всеми подпапками). Пусто — поиск по всей библиотеке.",
    ),
    "playlist_prefix_from": SettingSpec(
        key="playlist_prefix_from", kind="text", default="",
        label="Заменять префикс путей: было",
        help="Старый префикс из m3u8 (например M:/Music). Нужен, если "
             "плейлист создан на другой машине.",
    ),
    "playlist_prefix_to": SettingSpec(
        key="playlist_prefix_to", kind="text", default="/music",
        label="Заменять префикс путей: стало",
        help="На что заменить префикс (путь внутри контейнера).",
    ),
    "scan_concurrency": SettingSpec(
        key="scan_concurrency", kind="int", default=4,
        label="Параллельный разбор тегов",
        help="Сколько файлов читать одновременно при сканировании.",
        minimum=1, maximum=32,
    ),
    # --- То, что раньше задавалось только через .env ---------------------
    "watch_enabled": SettingSpec(
        key="watch_enabled", kind="bool", default=False,
        label="Наблюдение за библиотекой",
        help="Следить за появлением и изменением файлов (MTR_WATCH). Включается на лету.",
    ),
    "watch_polling": SettingSpec(
        key="watch_polling", kind="bool", default=False,
        label="Опрос вместо inotify",
        help="Нужно для Windows-монтирований и части сетевых томов (MTR_WATCH_POLL). "
             "Требует перезапуска контейнера.",
        restart=True,
    ),
    "watch_poll_ms": SettingSpec(
        key="watch_poll_ms", kind="int", default=1000,
        label="Период опроса, мс",
        help="Только при включённом опросе (MTR_WATCH_POLL_MS). 1000 — разумный компромисс. "
             "Требует перезапуска контейнера.",
        minimum=100, maximum=60000, restart=True,
    ),
    "log_level": SettingSpec(
        key="log_level", kind="choice", default="INFO",
        label="Уровень логов",
        help="Подробность записей в docker logs (MTR_LOG_LEVEL). Требует перезапуска контейнера.",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"), restart=True,
    ),
    "cookie_secure": SettingSpec(
        key="cookie_secure", kind="bool", default=False,
        label="Cookie только по HTTPS",
        help="Включайте, когда сервер доступен по HTTPS (MTR_COOKIE_SECURE). "
             "При обычном HTTP вход перестанет работать. Требует перезапуска контейнера.",
        restart=True,
    ),
    "subsonic_api_version": SettingSpec(
        key="subsonic_api_version", kind="text", default="1.16.1",
        label="Версия Subsonic API",
        help="Версия, которую сервер объявляет клиентам (MTR_SUBSONIC_VERSION). "
             "Требует перезапуска контейнера.",
        restart=True,
    ),
}


class SettingsRepository:
    """Чтение и запись переопределений настроек."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def all_overrides(self) -> dict[str, Any]:
        rows = await self._db.fetch_all("SELECT key, value FROM settings")
        result: dict[str, Any] = {}
        for row in rows:
            spec = SPECS.get(str(row["key"]))
            if spec is None or row["value"] is None:
                continue
            try:
                result[spec.key] = spec.coerce(row["value"])
            except ValueError:
                # Испорченное значение в БД не должно ломать старт:
                # работаем на значении из окружения.
                continue
        return result

    async def effective(self) -> dict[str, Any]:
        """Итоговые значения: переопределение из БД или дефолт спецификации."""
        overrides = await self.all_overrides()
        return {
            key: overrides.get(key, spec.default)
            for key, spec in SPECS.items()
        }

    async def set(self, key: str, raw: Any, *, user_id: int | None) -> Any:
        spec = SPECS.get(key)
        if spec is None:
            raise ValueError(f"неизвестная настройка: {key}")
        value = spec.coerce(raw)
        await self._db.execute(
            "INSERT INTO settings (key, value, updated_at, updated_by) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT (key) DO UPDATE SET "
            "value = excluded.value, updated_at = excluded.updated_at, "
            "updated_by = excluded.updated_by",
            (key, self._dump(value), utcnow_iso(), user_id),
        )
        return value

    async def reset(self, key: str) -> None:
        if key not in SPECS:
            raise ValueError(f"неизвестная настройка: {key}")
        await self._db.execute("DELETE FROM settings WHERE key = ?", (key,))

    @staticmethod
    def _dump(value: Any) -> str:
        if isinstance(value, bool):
            return "1" if value else "0"
        return str(value)

    async def describe(self) -> list[dict[str, Any]]:
        """Описание всех настроек с текущими значениями — для страницы."""
        overrides = await self.all_overrides()
        items = []
        for key, spec in SPECS.items():
            items.append({
                "key": key,
                "label": spec.label,
                "help": spec.help,
                "kind": spec.kind,
                "choices": list(spec.choices),
                "minimum": spec.minimum,
                "maximum": spec.maximum,
                "default": spec.default,
                "value": overrides.get(key, spec.default),
                "overridden": key in overrides,
                "restart": spec.restart,
            })
        return items
