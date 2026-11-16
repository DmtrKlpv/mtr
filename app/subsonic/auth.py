"""Авторизация Subsonic API.

Поддерживаемые способы передачи пароля (по спецификации 1.16.1):

1. `t` + `s` — токен: `t = md5(password + s)`, `s` — случайная соль от клиента.
   Основной и рекомендуемый способ.
2. `p=enc:<hex>` — пароль в hex-кодировке. Устаревший, но всё ещё используется
   старыми клиентами.
3. `p=<plain>` — открытый пароль. Разрешён протоколом, но небезопасен:
   логируем предупреждение, чтобы это было видно при разборе инцидентов.

JWT (`jwt`) не поддерживаем — отвечаем кодом 41, а не молчаливым отказом,
чтобы клиент переключился на токен.

Почему нужен расшифрованный пароль: md5(password + salt) невычислим из
bcrypt/PBKDF2-хэша. Поэтому у нас хранится Fernet-шифр (см. security/crypto.py),
и расшифровка выполняется ровно в момент проверки — результат не кэшируется.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from app.bootstrap import AppContext
from app.repositories.users import UserRecord
from app.subsonic.errors import ErrorCode, SubsonicError, missing

logger = logging.getLogger(__name__)

# Соль короче этого значения — нарушение спецификации, но клиент всё равно
# обслужим: ломать воспроизведение из-за педантичности хуже, чем принять.
MIN_SALT_LEN = 6

# Защита от подбора пароля: N неудач за окно — временная блокировка ключа
# «username|ip». Считаем в памяти процесса: при нескольких воркерах лимит
# будет кратно выше, но это всё равно на порядки лучше полного отсутствия.
THROTTLE_WINDOW_SEC = 300
THROTTLE_MAX_FAILURES = 10
THROTTLE_MAX_KEYS = 10_000

# Как часто обновлять last_login_at. Писать в БД на каждый запрос плеера
# (а их десятки в минуту) — бессмысленная нагрузка на единственного писателя.
LAST_LOGIN_UPDATE_SEC = 300


@dataclass(slots=True)
class AuthContext:
    """Результат успешной аутентификации."""

    user: UserRecord
    client: str
    api_version: str
    params: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_admin(self) -> bool:
        return self.user.role == "admin"


class AuthThrottle:
    """Счётчик неудачных попыток по ключу username|ip."""

    def __init__(self) -> None:
        self._failures: dict[str, list[float]] = {}
        self._last_login: dict[int, float] = {}

    def is_blocked(self, key: str) -> bool:
        self._prune(key)
        return len(self._failures.get(key, ())) >= THROTTLE_MAX_FAILURES

    def record_failure(self, key: str) -> None:
        now = time.monotonic()
        bucket = self._failures.setdefault(key, [])
        bucket.append(now)
        # Не даём словарю расти безгранично при распределённом переборе.
        if len(self._failures) > THROTTLE_MAX_KEYS:
            self._evict_oldest()

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)

    def should_touch_login(self, user_id: int) -> bool:
        """True, если пора записать last_login_at (не чаще раза в 5 минут)."""
        now = time.monotonic()
        previous = self._last_login.get(user_id)
        if previous is not None and now - previous < LAST_LOGIN_UPDATE_SEC:
            return False
        self._last_login[user_id] = now
        return True

    def _prune(self, key: str) -> None:
        cutoff = time.monotonic() - THROTTLE_WINDOW_SEC
        bucket = self._failures.get(key)
        if not bucket:
            return
        fresh = [stamp for stamp in bucket if stamp >= cutoff]
        if fresh:
            self._failures[key] = fresh
        else:
            self._failures.pop(key, None)

    def _evict_oldest(self) -> None:
        """Выкидываем десятую часть самых старых ключей."""
        items = sorted(self._failures.items(), key=lambda pair: pair[1][-1] if pair[1] else 0)
        for key, _ in items[: max(1, len(items) // 10)]:
            self._failures.pop(key, None)


def throttle_key(username: str, ip: str | None) -> str:
    """Ключ блокировки. Без IP (например, unix-сокет) — только логин."""
    return f"{(username or '').lower()}|{ip or '-'}"


def md5_hex(value: str) -> str:
    """md5 в нижнем регистре — формат Subsonic-токена."""
    return hashlib.md5(value.encode("utf-8")).hexdigest()


def _validate_api_version(version: str) -> str:
    """Проверяем, что версия выглядит как версия и не «слишком новая».

    Возвращаем нашу версию: клиент получит в ответе то, что мы реально
    поддерживаем, а не то, что он запросил.
    """
    parts = version.split(".")
    if not all(part.isdigit() for part in parts) or not 1 <= len(parts) <= 3:
        raise SubsonicError(
            ErrorCode.CLIENT_TOO_OLD, f"Invalid API version: {version!r}"
        )
    return version


async def authenticate(
    ctx: AppContext,
    params: Mapping[str, Any],
    *,
    ip: str | None = None,
    throttle: AuthThrottle | None = None,
) -> AuthContext:
    """Проверяет учётные данные запроса. Бросает SubsonicError при отказе."""
    username = (params.get("u") or "").strip()
    if not username:
        raise missing("u")

    api_version = (params.get("v") or "").strip()
    if not api_version:
        raise missing("v")
    _validate_api_version(api_version)

    client = (params.get("c") or "unknown").strip()[:64]
    key = throttle_key(username, ip)
    if throttle is not None and throttle.is_blocked(key):
        logger.warning(
            "аутентификация заблокирована после серии неудач: user=%s ip=%s",
            username, ip,
        )
        raise SubsonicError(ErrorCode.WRONG_CREDENTIALS)

    try:
        user = await _resolve_user(ctx, params, username)
    except SubsonicError:
        if throttle is not None:
            throttle.record_failure(key)
        raise

    if throttle is not None:
        throttle.reset(key)
        if throttle.should_touch_login(user.id):
            try:
                await ctx.users.touch_login(user.id)
            except Exception:  # noqa: BLE001 - метка входа не критична
                logger.debug("не удалось обновить last_login_at", exc_info=True)

    return AuthContext(
        user=user, client=client, api_version=api_version, params=params
    )


async def _resolve_user(
    ctx: AppContext, params: Mapping[str, Any], username: str
) -> UserRecord:
    """Находит пользователя и сверяет пароль выбранным способом."""
    token = (params.get("t") or "").strip()
    salt = (params.get("s") or "").strip()
    password = params.get("p")
    jwt = (params.get("jwt") or "").strip()

    if not token and not password:
        if jwt:
            raise SubsonicError(
                ErrorCode.TOKEN_AUTH_UNSUPPORTED,
                "JWT authentication is not supported, use token (t + s)",
            )
        raise SubsonicError(
            ErrorCode.MISSING_PARAMETER,
            "Required parameter is missing: either (t and s) or p",
        )

    if token and not salt:
        raise missing("s")

    # Пользователь читается до проверки пароля: иначе по разнице ответов
    # («нет такого» vs «неверный пароль») можно перечислять логины.
    record = await ctx.users.get_by_username(username)
    if record is None or not record.is_active:
        raise SubsonicError(ErrorCode.WRONG_CREDENTIALS)

    # --- Ветка токена: обязательна расшифровка обратимой копии -------
    if token:
        expected = await ctx.users.get_plain_password(record.id)
        if expected is None:
            # Ключ MTR_SECRET_KEY не совпадает с тем, которым записан пароль.
            # Клиенту отдаём тот же 40 (не раскрываем состояние сервера),
            # оператору — громкая запись в лог с путём восстановления.
            logger.error(
                "нет доступа к паролю пользователя %r: ключ MTR_SECRET_KEY "
                "не совпадает с тем, которым база была зашифрована. "
                "Восстановление: python -m app.cli doctor", username,
            )
            raise SubsonicError(ErrorCode.WRONG_CREDENTIALS)

        if len(salt) < MIN_SALT_LEN:
            logger.warning(
                "клиент %s прислал соль короче %d символов: %r",
                params.get("c", "?"), MIN_SALT_LEN, salt,
            )
        computed = md5_hex(expected + salt)
        if not hmac.compare_digest(computed, token.lower()):
            raise SubsonicError(ErrorCode.WRONG_CREDENTIALS)
        return record

    # --- Ветка пароля: проверяем по PBKDF2, ключ шифрования НЕ нужен ---
    # Это принципиально: даже при смене MTR_SECRET_KEY вход по паролю
    # продолжает работать, и через него можно восстановить token-авторизацию.
    assert password is not None
    supplied = str(password)
    if supplied.startswith("enc:"):
        hex_part = supplied[4:]
        try:
            decoded = bytes.fromhex(hex_part).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise SubsonicError(
                ErrorCode.GENERIC, "Invalid hex encoding in parameter p"
            ) from exc
        supplied = decoded
    else:
        logger.warning(
            "клиент %s передал пароль в открытом виде (параметр p) — "
            "используйте токен t+s", params.get("c", "?"),
        )

    if not ctx.cipher.verify_password(supplied, record.password_hash):
        raise SubsonicError(ErrorCode.WRONG_CREDENTIALS)

    # Пароль верен, но обратимая копия не читается текущим ключом —
    # перешифровываем прямо здесь. Самовосстановление: после одного входа
    # с паролем token-авторизация снова работает, удалять базу не нужно.
    if await ctx.users.get_plain_password(record.id) is None:
        try:
            await ctx.users.reencrypt_password(record.id, supplied)
            logger.warning(
                "пароль пользователя %r перешифрован текущим MTR_SECRET_KEY — "
                "token-авторизация восстановлена", username,
            )
        except Exception:  # noqa: BLE001 - вход важнее восстановления
            logger.exception("не удалось перешифровать пароль %r", username)

    return record
