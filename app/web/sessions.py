"""Сессии веб-интерфейса.

Почему не JWT и не Subsonic-токены:

* Subsonic-токен живёт в URL. В веб-интерфейсе это означало бы пароль
  в истории браузера и в Referer — уже проходили на этапе 3.
* JWT не отзывается: заблокировали пользователя — а его токен работает
  до истечения срока.

Поэтому серверные сессии в `mtr_users.db`:

  * в cookie лежит случайный токен (32 байта), в БД — только его SHA-256.
    Утечка базы не даёт войти по чужой сессии;
  * удаление строки = мгновенный выход на всех устройствах;
  * CSRF-токен привязан к сессии и передаётся отдельно в заголовке.

Cookie: `HttpOnly` (JS не прочитает), `SameSite=Lax` (защита от
межсайтовых POST), `Secure` включается через `MTR_COOKIE_SECURE=1`
при работе за HTTPS-прокси.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.db.database import Database
from app.repositories.users import UserRecord, UserRepository
from app.utils.timeutil import utcnow_iso

logger = logging.getLogger(__name__)

SESSION_COOKIE = "mtr_session"
CSRF_COOKIE = "mtr_csrf"
CSRF_HEADER = "x-csrf-token"

# Срок жизни сессии. 30 дней — компромисс: не заставлять входить каждый
# день, но и не держать вечную сессию на чужом устройстве.
DEFAULT_TTL_SEC = 30 * 24 * 3600
# Не обновлять last_seen_at на каждый запрос: это запись в БД при каждом
# клике в интерфейсе.
TOUCH_INTERVAL_SEC = 300
# Потолок числа сессий на пользователя: старые вытесняются.
MAX_SESSIONS_PER_USER = 10


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _iso_after(seconds: int) -> str:
    moment = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(slots=True)
class SessionRecord:
    """Активная сессия вместе с пользователем."""

    id: int
    user_id: int
    csrf_token: str
    expires_at: str
    user: UserRecord

    @property
    def is_admin(self) -> bool:
        return self.user.role == "admin"


class SessionRepository:
    """Создание, проверка и отзыв сессий."""

    def __init__(self, db: Database, users: UserRepository) -> None:
        self._db = db
        self._users = users

    # Создание
    async def create(
        self,
        user_id: int,
        *,
        user_agent: str | None = None,
        ip: str | None = None,
        ttl_sec: int = DEFAULT_TTL_SEC,
    ) -> tuple[str, str]:
        """Создаёт сессию. Возвращает (токен для cookie, csrf-токен)."""
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        now = utcnow_iso()

        await self.purge_expired()
        await self._evict_excess(user_id)

        await self._db.execute(
            "INSERT INTO sessions "
            "(token_hash, user_id, csrf_token, created_at, expires_at, "
            " last_seen_at, user_agent, ip) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _hash_token(token), user_id, csrf, now,
                _iso_after(ttl_sec), now,
                (user_agent or "")[:255] or None,
                ip,
            ),
        )
        logger.info("сессия создана для user_id=%d (ip=%s)", user_id, ip)
        return token, csrf

    async def _evict_excess(self, user_id: int) -> None:
        """Оставляет не больше MAX_SESSIONS_PER_USER последних сессий."""
        rows = await self._db.fetch_all(
            "SELECT id FROM sessions WHERE user_id = ? "
            "ORDER BY last_seen_at DESC, id DESC",
            (user_id,),
        )
        excess = [int(row["id"]) for row in rows[MAX_SESSIONS_PER_USER:]]
        for session_id in excess:
            await self._db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    # Проверка
    async def resolve(self, token: str | None) -> SessionRecord | None:
        """Возвращает сессию по токену из cookie. None — недействительна."""
        if not token:
            return None
        row = await self._db.fetch_one(
            "SELECT id, user_id, csrf_token, expires_at, last_seen_at "
            "FROM sessions WHERE token_hash = ?",
            (_hash_token(token),),
        )
        if row is None:
            return None

        # Сравнение сроков строками безопасно: формат ISO-8601 UTC
        # лексикографически совпадает с хронологическим.
        if str(row["expires_at"]) <= utcnow_iso():
            await self._db.execute(
                "DELETE FROM sessions WHERE id = ?", (int(row["id"]),)
            )
            return None

        user = await self._users.get_by_id(int(row["user_id"]))
        if user is None or not user.is_active:
            # Пользователь заблокирован или удалён — сессия недействительна.
            await self.revoke_token(token)
            return None

        await self._touch(int(row["id"]), str(row["last_seen_at"]))
        return SessionRecord(
            id=int(row["id"]),
            user_id=int(row["user_id"]),
            csrf_token=str(row["csrf_token"]),
            expires_at=str(row["expires_at"]),
            user=user,
        )

    async def _touch(self, session_id: int, last_seen: str) -> None:
        """Обновляет last_seen_at не чаще TOUCH_INTERVAL_SEC."""
        try:
            seen = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
        except ValueError:
            seen = None
        now = datetime.now(timezone.utc)
        if seen is not None and (now - seen).total_seconds() < TOUCH_INTERVAL_SEC:
            return
        await self._db.execute(
            "UPDATE sessions SET last_seen_at = ? WHERE id = ?",
            (utcnow_iso(), session_id),
        )

    # Отзыв
    async def revoke_token(self, token: str) -> None:
        await self._db.execute(
            "DELETE FROM sessions WHERE token_hash = ?", (_hash_token(token),)
        )

    async def revoke_by_id(self, session_id: int) -> None:
        """Удаляет сессию по внутреннему id."""
        await self._db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    async def revoke_all_for_user(self, user_id: int) -> int:
        """Выход со всех устройств: смена пароля, блокировка, удаление."""
        row = await self._db.fetch_one(
            "SELECT COUNT(*) AS count FROM sessions WHERE user_id = ?", (user_id,)
        )
        await self._db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        count = int(row["count"]) if row else 0
        if count:
            logger.info("отозвано сессий пользователя %d: %d", user_id, count)
        return count

    async def revoke_others(self, user_id: int, keep_session_id: int) -> None:
        """Сменить пароль — и выйти везде, кроме текущего устройства."""
        await self._db.execute(
            "DELETE FROM sessions WHERE user_id = ? AND id != ?",
            (user_id, keep_session_id),
        )

    async def purge_expired(self) -> int:
        await self._db.execute(
            "DELETE FROM sessions WHERE expires_at <= ?", (utcnow_iso(),)
        )
        return 0

    async def list_for_user(self, user_id: int) -> list[dict]:
        rows = await self._db.fetch_all(
            "SELECT id, created_at, last_seen_at, expires_at, user_agent, ip "
            "FROM sessions WHERE user_id = ? ORDER BY last_seen_at DESC",
            (user_id,),
        )
        return [
            {
                "id": int(row["id"]),
                "created_at": str(row["created_at"]),
                "last_seen_at": str(row["last_seen_at"]),
                "expires_at": str(row["expires_at"]),
                "user_agent": row["user_agent"],
                "ip": row["ip"],
            }
            for row in rows
        ]
