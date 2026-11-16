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
    section: str
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
    "library_view": SettingSpec(
        key="library_view", kind="choice", default="albums", section="interface",
        label="Вид библиотеки при входе",
        help="Что открывается первым при входе в библиотеку: исполнители, альбомы или плейлисты.",
        choices=("artists", "albums", "playlists"),
    ),
    "accent_color": SettingSpec(
        key="accent_color", kind="choice", default="blue", section="interface",
        label="Цвет акцента",
        help="Цвет кнопок, активного пункта меню, полосы прогресса и подсветки текущего трека. "
             "Применяется сразу, без перезагрузки страницы.",
        choices=("blue", "green", "violet", "orange", "teal", "pink", "red"),
    ),
    "default_transcode": SettingSpec(
        key="default_transcode", kind="choice", default="raw", section="playback",
        label="Формат отдачи по умолчанию",
        help="Формат, в котором сервер отдаёт треки, если клиент не запросил свой. "
             "«Без перекодирования» — исходный файл.",
        choices=("raw", "mp3_128", "mp3_256", "mp3_320", "mp3_v0", "flac", "alac"),
    ),
    "max_transcodes": SettingSpec(
        key="max_transcodes", kind="int", default=4, section="playback",
        label="Лимит одновременных перекодирований",
        help="Сколько процессов ffmpeg могут перекодировать потоки одновременно. "
             "Сверх лимита новый поток сразу получает отказ (503), а не ждёт очереди. Применяется сразу.",
        minimum=1, maximum=64,
    ),
    "scan_interval_sec": SettingSpec(
        key="scan_interval_sec", kind="int", default=0, section="scan",
        label="Интервал проверки изменений, сек",
        help="Раз в столько секунд проверять библиотеку и обрабатывать только новые и изменённые файлы. "
             "Страховка на случай, если слежение за файлами что-то пропустило. 0 — выключено.",
        minimum=0, maximum=86400,
    ),
    "scan_batch_tracks": SettingSpec(
        key="scan_batch_tracks", kind="int", default=0, section="scan",
        label="Размер порции при скане, треков",
        help="Сколько треков обработать, прежде чем записать результат в базу. 0 — порциями по 100. "
             "Большая порция — меньше записей в базу, но больше памяти до записи.",
        minimum=0, maximum=10000,
    ),
    "scan_concurrency": SettingSpec(
        key="scan_concurrency", kind="int", default=4, section="scan",
        label="Файлов для разбора одновременно",
        help="Сколько файлов читать и разбирать теги параллельно при сканировании. "
             "Больше — скан быстрее, но выше нагрузка на диск и процессор.",
        minimum=1, maximum=32,
    ),
    "watch_enabled": SettingSpec(
        key="watch_enabled", kind="bool", default=False, section="watch",
        label="Следить за изменениями файлов",
        help="Сразу замечать появление и изменение файлов в папке с музыкой, без ручного скана. "
             "Включается без перезапуска.",
    ),
    "watch_polling": SettingSpec(
        key="watch_polling", kind="bool", default=False, section="watch",
        label="Проверять папку опросом",
        help="Нужно для Windows-томов и части сетевых дисков, где системные уведомления о файлах не приходят. "
             "Опрос нагружает систему сильнее. Требует перезапуска контейнера.",
        restart=True,
    ),
    "watch_poll_ms": SettingSpec(
        key="watch_poll_ms", kind="int", default=1000, section="watch",
        label="Период опроса, мс",
        help="Как часто проверять папку, если включён опрос. Меньше — быстрее реакция, но выше нагрузка. "
             "1000 — разумный компромисс. Требует перезапуска контейнера.",
        minimum=100, maximum=60000, restart=True,
    ),
    "cover_prefer_folder_image": SettingSpec(
        key="cover_prefer_folder_image", kind="bool", default=True, section="covers",
        label="Сначала обложка из файла в папке",
        help="Брать обложку из файла в папке с треком или в папке выше (cover, folder, front и другие), а не из тегов. "
             "Если такого файла нет, обложка берётся из тегов. Действует на новые обложки; "
             "уже готовые пересобираются кнопкой «Пересобрать обложки из папок».",
    ),
    "cover_active_window_sec": SettingSpec(
        key="cover_active_window_sec", kind="int", default=3, section="covers",
        label="Пауза обложек после действий, сек",
        help="Сколько секунд после последнего действия в интерфейсе фоновая загрузка обложек ждёт. "
             "Меньше — обложки заполняются быстрее, но сильнее мешают работе. Применяется сразу.",
        minimum=1, maximum=60,
    ),
    "cover_max_wait_sec": SettingSpec(
        key="cover_max_wait_sec", kind="int", default=30, section="covers",
        label="Максимальное ожидание одного альбома, сек",
        help="Фоновая загрузка обложек уступает действиям в интерфейсе, но не дольше этого времени: "
             "затем продолжает работу, даже если вы ещё активны. Применяется сразу.",
        minimum=5, maximum=600,
    ),
    "playlists_dir": SettingSpec(
        key="playlists_dir", kind="text", default="", section="playlists",
        label="Папка с плейлистами",
        help="Папка внутри контейнера, где лежат файлы .m3u и .m3u8. Поиск идёт и во вложенных папках. "
             "Если поле пустое, плейлисты ищутся по всей библиотеке.",
    ),
    "playlist_prefix_from": SettingSpec(
        key="playlist_prefix_from", kind="text", default="", section="playlists",
        label="Старое начало путей в плейлистах",
        help="Начало путей внутри .m3u-файлов, которое нужно заменить, например M:/Music. "
             "Нужно, если плейлист создан на другом компьютере.",
    ),
    "playlist_prefix_to": SettingSpec(
        key="playlist_prefix_to", kind="text", default="/music", section="playlists",
        label="Новое начало путей (внутри контейнера)",
        help="На что заменить старое начало пути. По умолчанию /music.",
    ),
    "log_level": SettingSpec(
        key="log_level", kind="choice", default="INFO", section="server",
        label="Подробность журнала",
        help="Какие записи попадают в журнал контейнера (docker logs): DEBUG — максимум деталей, "
             "ERROR — только ошибки. Для обычной работы хватает INFO. Требует перезапуска контейнера.",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        restart=True,
    ),
    "cookie_secure": SettingSpec(
        key="cookie_secure", kind="bool", default=False, section="server",
        label="Cookie только по HTTPS",
        help="Включайте, если сайт открывается по HTTPS. При обычном HTTP вход перестанет работать. "
             "Требует перезапуска контейнера.",
        restart=True,
    ),
    "subsonic_api_version": SettingSpec(
        key="subsonic_api_version", kind="text", default="1.16.1", section="server",
        label="Версия Subsonic API",
        help="Версия, которую сервер сообщает Subsonic-клиентам. Меняйте, только если приложение требует другую. "
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
                "section": spec.section,
            })
        return items
