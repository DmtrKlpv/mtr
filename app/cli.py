"""CLI этапа 1-3: инициализация баз, пользователи, скан, диагностика.

Запуск из контейнера:
    python -m app.cli initdb
    python -m app.cli createuser --username admin --role admin
    python -m app.cli setpassword --username admin
    python -m app.cli doctor
    python -m app.cli scan --mode full
    python -m app.cli stats
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import sys

from app.bootstrap import AppContext, ensure_bootstrap_admin
from app.config import Settings
from app.diagnostics import format_report, run_doctor
from app.security.crypto import load_or_create_secret


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _read_password(args: argparse.Namespace, *, confirm: bool = True) -> str | None:
    """Пароль из аргумента или интерактивно. None — отказ."""
    if args.password:
        return args.password
    password = getpass.getpass("пароль: ")
    if confirm and password != getpass.getpass("повторите: "):
        print("пароли не совпадают", file=sys.stderr)
        return None
    return password


async def _cmd_initdb(args: argparse.Namespace) -> int:
    ctx = await AppContext.create(Settings.from_env())
    try:
        if args.bootstrap_admin:
            created = await ensure_bootstrap_admin(
                ctx.users,
                username=args.bootstrap_admin,
                password=args.bootstrap_password or "changeme123",
            )
            print("стартовый админ создан" if created else "пользователи уже есть")
        stats = await ctx.tracks.library_stats()
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        print(f"users.db: {ctx.settings.users_db}")
        print(f"music.db: {ctx.settings.music_db}")
    finally:
        await ctx.close()
    return 0


async def _cmd_createuser(args: argparse.Namespace) -> int:
    password = _read_password(args)
    if password is None:
        return 2

    ctx = await AppContext.create(Settings.from_env())
    try:
        user = await ctx.users.create_user(
            args.username,
            password,
            role=args.role,
            email=args.email,
            display_name=args.display_name,
        )
        print(f"создан id={user.id} username={user.username} role={user.role}")
    except Exception as exc:  # noqa: BLE001 - CLI показывает причину как есть
        print(f"ошибка: {exc}", file=sys.stderr)
        return 1
    finally:
        await ctx.close()
    return 0


async def _cmd_setpassword(args: argparse.Namespace) -> int:
    """Задаёт пароль заново — без знания текущего.

    Нужно, когда пароль забыт или когда MTR_SECRET_KEY сменился и обратимая
    копия стала нечитаемой. Перешифровывает обе формы (PBKDF2 и Fernet),
    поэтому после команды работают и вход по паролю, и Subsonic-токен.
    """
    password = _read_password(args, confirm=False if args.password else True)
    if password is None:
        return 2

    ctx = await AppContext.create(Settings.from_env())
    try:
        record = await ctx.users.get_by_username(args.username)
        if record is None:
            print(f"пользователь {args.username!r} не найден", file=sys.stderr)
            return 1
        await ctx.users.reset_password(record.id, password)
        print(
            f"пароль пользователя {record.username!r} обновлён; "
            "Subsonic token-авторизация снова доступна"
        )
    except Exception as exc:  # noqa: BLE001
        print(f"ошибка: {exc}", file=sys.stderr)
        return 1
    finally:
        await ctx.close()
    return 0


async def _cmd_doctor(args: argparse.Namespace) -> int:
    """Проверка конфигурации. Код возврата 1, если есть проблемы."""
    settings = Settings.from_env()
    ctx = await AppContext.create(settings)
    try:
        try:
            secret = load_or_create_secret(settings.secret_key, settings.secret_key_file)
        except Exception:  # noqa: BLE001 - ключ может быть недоступен
            secret = settings.secret_key
        report = await run_doctor(ctx, secret=secret)
        print(format_report(report))
        return 0 if report.healthy else 1
    finally:
        await ctx.close()


async def _cmd_scan(args: argparse.Namespace) -> int:
    ctx = await AppContext.create(Settings.from_env())
    try:
        result = await ctx.scanner.scan(mode=args.mode)
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
        return 0 if result.status == "completed" else 1
    finally:
        await ctx.close()


async def _cmd_stats(args: argparse.Namespace) -> int:
    ctx = await AppContext.create(Settings.from_env())
    try:
        stats = await ctx.tracks.library_stats()
        stats["users"] = await ctx.users.count_users()
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    finally:
        await ctx.close()
    return 0


async def _cmd_purge_missing(args: argparse.Namespace) -> int:
    ctx = await AppContext.create(Settings.from_env())
    try:
        removed = await ctx.tracks.purge_missing()
        print(f"удалено треков, помеченных отсутствующими: {removed}")
        if removed:
            print(
                "ВНИМАНИЕ: их позиции удалены из плейлистов "
                "(каскадное удаление playlist_tracks)",
                file=sys.stderr,
            )
    finally:
        await ctx.close()
    return 0


async def _cmd_covers(args: argparse.Namespace) -> int:
    """Очистка кеша обложек. Фонового прогрева больше нет.

    Обложка извлекается при первом запросе getCoverArt и кладётся в кеш.
    Поэтому единственная команда здесь — сброс кеша.
    """
    if not args.purge:
        print("фонового прогрева обложек нет: обложка извлекается при запросе.")
        print("для сброса кеша: python -m app.cli covers --purge")
        return 0
    ctx = await AppContext.create(Settings.from_env())
    try:
        removed = await ctx.covers.purge_all()
        print(f"удалено записей кеша: {removed}")
    finally:
        await ctx.close()
    return 0


def _human_size(size: int) -> str:
    """Размер по-человечески: 900 КБ, 12 МБ, 1.4 ГБ."""
    value = float(max(0, size))
    if value < 1024:
        return f"{value:.0f} Б"
    for unit in ("КБ", "МБ"):
        value /= 1024
        if value < 1024:
            return f"{value:.0f} {unit}"
    return f"{value / 1024:.1f} ГБ"


def _human_duration(seconds: float) -> str:
    """Длительность по-человечески: 40 с, 12 мин, 1 ч 20 мин."""
    seconds = max(0.0, seconds)
    if seconds < 1:
        return "меньше секунды"
    if seconds < 10:
        return f"{seconds:.1f} с"
    if seconds < 90:
        return f"{seconds:.0f} с"
    minutes = seconds / 60
    # Порог «до 90 минут» здесь не годится: 3600 с — это ровно час, и он
    # показывался как «60 мин». В часы переходим с 60 минут.
    if minutes < 60:
        return f"{minutes:.0f} мин"
    hours = int(minutes // 60)
    rest = int(minutes % 60)
    if rest == 0:
        return f"{hours} ч"
    return f"{hours} ч {rest} мин"


async def _cmd_bench_covers(args: argparse.Namespace) -> int:
    """Меряет стоимость обложек: холодный путь против кеша."""
    from app.bench_covers import bench_covers, format_bench

    ctx = await AppContext.create(Settings.from_env())
    try:
        result = await bench_covers(ctx, albums=max(1, int(args.albums)))
        print(format_bench(result))
    finally:
        await ctx.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mtr", description="MTR music server CLI")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("initdb", help="создать/мигрировать обе базы")
    p_init.add_argument("--bootstrap-admin", metavar="USERNAME")
    p_init.add_argument("--bootstrap-password", metavar="PASSWORD")
    p_init.set_defaults(func=_cmd_initdb)

    p_user = sub.add_parser("createuser", help="создать пользователя")
    p_user.add_argument("--username", required=True)
    p_user.add_argument("--password", help="если не задан — спросит интерактивно")
    p_user.add_argument("--role", default="user", choices=("admin", "user"))
    p_user.add_argument("--email")
    p_user.add_argument("--display-name")
    p_user.set_defaults(func=_cmd_createuser)

    p_set = sub.add_parser(
        "setpassword", help="задать пароль заново (сброс, без знания текущего)"
    )
    p_set.add_argument("--username", required=True)
    p_set.add_argument("--password", help="если не задан — спросит интерактивно")
    p_set.set_defaults(func=_cmd_setpassword)

    p_doc = sub.add_parser("doctor", help="проверить конфигурацию и ключ шифрования")
    p_doc.set_defaults(func=_cmd_doctor)

    p_scan = sub.add_parser("scan", help="проиндексировать библиотеку")
    p_scan.add_argument("--mode", default="incremental",
                        choices=("full", "incremental"))
    p_scan.set_defaults(func=_cmd_scan)

    p_stats = sub.add_parser("stats", help="сводка по библиотеке")
    p_stats.set_defaults(func=_cmd_stats)

    p_purge = sub.add_parser(
        "purge-missing", help="удалить треки, чьи файлы исчезли"
    )
    p_purge.set_defaults(func=_cmd_purge_missing)

    p_covers = sub.add_parser(
        "covers", help="сбросить кеш обложек"
    )
    p_covers.add_argument(
        "--purge", action="store_true",
        help="очистить кеш обложек вместо прогрева",
    )
    p_covers.set_defaults(func=_cmd_covers)

    p_bench = sub.add_parser(
        "bench-covers",
        help="замерить стоимость извлечения обложек и выигрыш от кеша",
    )
    p_bench.add_argument(
        "--albums", type=int, default=60,
        help="сколько альбомов взять в выборку (по умолчанию 60)",
    )
    p_bench.set_defaults(func=_cmd_bench_covers)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return asyncio.run(args.func(args))
    except KeyboardInterrupt:
        print("прервано", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
