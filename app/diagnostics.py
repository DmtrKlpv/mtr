"""Диагностика состояния сервера.

Отдельный модуль, потому что проверки нетривиальны и нужны в двух местах:
в CLI (`python -m app.cli doctor`) и при старте сервера (короткая версия),
чтобы оператор увидел проблему в логе, а не гадал над кодом 40.

Главный сценарий, ради которого это написано: MTR_SECRET_KEY сменился,
PBKDF2-хэш пароля остался валидным, а Fernet-копия не расшифровывается.
Вход с паролем работает, Subsonic token-авторизация — нет, и оба случая
выглядят для клиента одинаково (код 40). Такое нужно показывать явно.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.bootstrap import AppContext
from app.security.crypto import CryptoError

logger = logging.getLogger(__name__)

# Минимальная длина секрета, при которой он считается хоть сколько-то стойким.
MIN_SECRET_LEN = 16


@dataclass(slots=True)
class DoctorReport:
    """Результат проверки. `problems` — то, что требует действий оператора."""

    users_total: int = 0
    users_with_broken_cipher: list[str] = field(default_factory=list)
    users_inactive: list[str] = field(default_factory=list)
    admins_active: int = 0
    tracks: int = 0
    tracks_missing: int = 0
    music_dir_exists: bool = False
    # Признак того, что /music смонтирован, но пуст, а база при этом непустая:
    # классическая ситуация «создался анонимный том вместо папки на хосте».
    music_mount_suspicious: bool = False
    secret_len: int = 0
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return not self.problems


async def run_doctor(ctx: AppContext, *, secret: str | None = None) -> DoctorReport:
    """Проверяет конфигурацию, ключ шифрования и состояние библиотеки."""
    report = DoctorReport()
    settings = ctx.settings

    # --- музыкальный каталог ---
    report.music_dir_exists = settings.music_dir.exists()
    if not report.music_dir_exists:
        report.problems.append(
            f"каталог музыки {settings.music_dir} не существует — "
            "проверьте, что volume проброшен в контейнер"
        )

    # --- ключ шифрования ---
    if secret is not None:
        report.secret_len = len(secret)
        if report.secret_len < MIN_SECRET_LEN:
            report.notes.append(
                f"MTR_SECRET_KEY короче {MIN_SECRET_LEN} символов — "
                "для продакшена сгенерируйте стойкий: "
                'python -c "import secrets;print(secrets.token_urlsafe(48))"'
            )

    # --- пользователи ---
    users = await ctx.users.list_users()
    report.users_total = len(users)
    report.admins_active = await ctx.users.count_admins()

    for user in users:
        if not user.is_active:
            report.users_inactive.append(user.username)
        # Пробуем расшифровать обратимою копию: именно она нужна Subsonic.
        record = await ctx.users.get_by_id(user.id)
        if record is None:
            continue
        try:
            ctx.cipher.decrypt(record.password_enc)
        except CryptoError:
            report.users_with_broken_cipher.append(user.username)

    if report.users_with_broken_cipher:
        names = ", ".join(report.users_with_broken_cipher)
        report.problems.append(
            f"пароли пользователей не расшифровываются текущим MTR_SECRET_KEY: {names}. "
            "Вход по паролю работает, но Subsonic token-авторизация вернёт код 40. "
            "Лечение: войти любым из этих пользователей по паролю "
            "(параметр p=) — обратимая копия перешифруется автоматически; "
            "либо задать пароль заново: python -m app.cli setpassword --username <имя>"
        )

    if report.admins_active == 0:
        report.problems.append(
            "нет ни одного активного администратора — управление сервером недоступно"
        )

    # --- библиотека ---
    stats = await ctx.tracks.library_stats()
    report.tracks = stats.get("tracks", 0)
    report.tracks_missing = stats.get("missing", 0)
    if report.tracks == 0:
        report.notes.append(
            "в библиотеке нет треков — запустите: python -m app.cli scan --mode full"
        )
    if report.tracks_missing:
        report.notes.append(
            f"{report.tracks_missing} треков помечены отсутствующими (файлы удалены); "
            "почистить: python -m app.cli purge-missing"
        )

    # --- проверка монтирования ---
    await _check_mounts(ctx, report)

    return report


async def _check_mounts(ctx: AppContext, report: DoctorReport) -> None:
    """Ищет признак того, что вместо папки на хосте подключился пустой том.

    Docker считает `-v ":/music"` валидной записью и создаёт анонимный том с
    именем из хеша. Ошибки нет, но музыки внутри нет. Симптом: база полна
    треков, а каталог пуст. Лечится указанием абсолютного пути на хосте.
    """
    music = ctx.settings.music_dir
    if not music.exists():
        return

    try:
        entries = sum(1 for _ in music.iterdir())
    except OSError:
        return

    if report.tracks == 0:
        return

    # База помнит треки, а каталог пуст: почти наверняка смонтирован не тот том.
    if entries == 0:
        report.music_mount_suspicious = True
        report.problems.append(
            f"база знает {report.tracks} треков, но каталог {music} пуст — "
            "похоже, вместо папки с музыкой смонтирован пустой том. "
            "Проверьте docker inspect: источник у /music должен быть путём на "
            "хосте (/share/...), а не .../volumes/<хеш>/_data"
        )
        return

    # Дополнительный признак: файлов на диске заметно меньше, чем треков в базе.
    if report.tracks > 50 and entries < report.tracks // 100:
        report.music_mount_suspicious = True
        report.problems.append(
            f"в каталоге {music} всего {entries} записей при {report.tracks} "
            "треках в базе — возможно, смонтирована не та папка"
        )


def format_report(report: DoctorReport) -> str:
    """Человекочитаемый отчёт для CLI."""
    lines: list[str] = []
    lines.append(f"Пользователей: {report.users_total}, "
                 f"активных админов: {report.admins_active}")
    lines.append(f"Треков: {report.tracks}, отсутствующих: {report.tracks_missing}")
    lines.append(f"Каталог музыки: {'найден' if report.music_dir_exists else 'НЕ НАЙДЕН'}")
    if report.secret_len:
        lines.append(f"Длина MTR_SECRET_KEY: {report.secret_len} символов")
    if report.users_inactive:
        lines.append(f"Заблокированы: {', '.join(report.users_inactive)}")

    if report.problems:
        lines.append("")
        lines.append("ПРОБЛЕМЫ:")
        for problem in report.problems:
            lines.append(f"  [!] {problem}")
    else:
        lines.append("")
        lines.append("Критических проблем не найдено.")

    if report.notes:
        lines.append("")
        lines.append("Замечания:")
        for note in report.notes:
            lines.append(f"  [i] {note}")

    return "\n".join(lines)


async def log_startup_warnings(ctx: AppContext, *, secret: str | None) -> None:
    """Короткая версия doctor при старте сервера.

    Не мешает запуску: только пишет в лог то, из-за чего потом будет
    необъяснимый код 40.
    """
    try:
        report = await run_doctor(ctx, secret=secret)
    except Exception:  # noqa: BLE001 - диагностика не должна ронять старт
        logger.exception("диагностика при старте не удалась")
        return

    for problem in report.problems:
        logger.error("ПРОБЛЕМА: %s", problem)
    for note in report.notes:
        logger.info("%s", note)
