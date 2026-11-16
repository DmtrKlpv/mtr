"""Репозиторий пользователей (mtr_users.db).

Единственное место, где живут SQL-запросы к таблице users. Сервисы и API
работают только через эти методы — так проще удержать контроль доступа
в одном файле и не размазать проверки ролей по хендлерам.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass

from app.db.database import Database, DatabaseError
from app.security.crypto import CryptoError, PasswordCipher
from app.utils.timeutil import utcnow_iso

logger = logging.getLogger(__name__)

# Требования к username: буквы/цифры/._- , 3-32 символа, без пробелов.
USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")
MIN_PASSWORD_LEN = 8
MAX_PASSWORD_LEN = 256

VALID_ROLES = ("admin", "user")


class UserError(Exception):
    """Нарушение бизнес-правил работы с пользователями."""


@dataclass(slots=True)
class UserRecord:
    """Полная запись пользователя — только для внутреннего использования."""

    id: int
    username: str
    password_enc: str
    password_hash: str
    role: str
    is_active: bool
    email: str | None
    display_name: str | None
    created_at: str
    updated_at: str
    last_login_at: str | None
    password_changed_at: str | None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def to_public(self) -> "UserPublic":
        """Проекция без секретов — то, что можно отдать в API."""
        return UserPublic(
            id=self.id,
            username=self.username,
            role=self.role,
            is_active=self.is_active,
            email=self.email,
            display_name=self.display_name,
            created_at=self.created_at,
            last_login_at=self.last_login_at,
        )


@dataclass(slots=True)
class UserPublic:
    """Безопасное представление пользователя для ответов API."""

    id: int
    username: str
    role: str
    is_active: bool
    email: str | None
    display_name: str | None
    created_at: str
    last_login_at: str | None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def _row_to_record(row: sqlite3.Row) -> UserRecord:
    """Маппинг строки SQLite в UserRecord с приведением типов."""
    return UserRecord(
        id=int(row["id"]),
        username=str(row["username"]),
        password_enc=str(row["password_enc"]),
        password_hash=str(row["password_hash"]),
        role=str(row["role"]),
        is_active=bool(row["is_active"]),
        email=row["email"],
        display_name=row["display_name"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        last_login_at=row["last_login_at"],
        password_changed_at=row["password_changed_at"],
    )


class UserRepository:
    """CRUD над users + проверка учётных данных."""

    def __init__(self, db: Database, cipher: PasswordCipher) -> None:
        self._db = db
        self._cipher = cipher

    # Чтение
    async def get_by_id(self, user_id: int) -> UserRecord | None:
        row = await self._db.fetch_one("SELECT * FROM users WHERE id = ?", (user_id,))
        return _row_to_record(row) if row else None

    async def get_by_username(self, username: str) -> UserRecord | None:
        # COLLATE NOCASE в схеме даёт регистронезависимый поиск.
        row = await self._db.fetch_one(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)
        )
        return _row_to_record(row) if row else None

    async def list_users(self) -> list[UserPublic]:
        rows = await self._db.fetch_all(
            "SELECT * FROM users ORDER BY username COLLATE NOCASE"
        )
        return [_row_to_record(row).to_public() for row in rows]

    async def count_users(self) -> int:
        return int(await self._db.fetch_value("SELECT COUNT(*) FROM users", default=0) or 0)

    async def count_admins(self) -> int:
        return int(
            await self._db.fetch_value(
                "SELECT COUNT(*) FROM users WHERE role = 'admin' AND is_active = 1",
                default=0,
            )
            or 0
        )

    # Запись
    async def create_user(
        self,
        username: str,
        password: str,
        *,
        role: str = "user",
        email: str | None = None,
        display_name: str | None = None,
    ) -> UserPublic:
        """Создаёт пользователя. Пароль шифруется И хэшируется."""
        username = (username or "").strip()
        if not USERNAME_RE.match(username):
            raise UserError(
                "имя пользователя: 3-32 символа, только латиница, цифры, '.', '_', '-'"
            )
        self._validate_password(password)
        if role not in VALID_ROLES:
            raise UserError(f"недопустимая роль: {role!r}")
        if email and ("@" not in email or len(email) > 254):
            raise UserError("некорректный email")

        encrypted = self._cipher.encrypt(password)
        hashed = self._cipher.hash_password(password)
        now = utcnow_iso()

        try:
            user_id = await self._db.execute(
                """
                INSERT INTO users (
                    username, password_enc, password_hash, role, is_active,
                    email, display_name, created_at, updated_at,
                    password_changed_at
                ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                """,
                (
                    username,
                    encrypted,
                    hashed,
                    role,
                    email,
                    display_name or username,
                    now,
                    now,
                    now,
                ),
            )
        except DatabaseError as exc:
            # UNIQUE-нарушение означает занятое имя — отдаём понятную ошибку.
            if "UNIQUE" in str(exc).upper() or "unique" in str(exc):
                raise UserError(f"пользователь {username!r} уже существует") from exc
            raise

        logger.info("создан пользователь %s (id=%d, role=%s)", username, user_id, role)
        record = await self.get_by_id(user_id)
        if record is None:  # pragma: no cover - защита от гонки
            raise UserError("пользователь создан, но не читается")
        return record.to_public()

    async def set_password(self, user_id: int, new_password: str) -> None:
        """Меняет пароль: перешифровывает и перехэширует заново."""
        self._validate_password(new_password)
        record = await self.get_by_id(user_id)
        if record is None:
            raise UserError("пользователь не найден")

        now = utcnow_iso()
        await self._db.execute(
            """
            UPDATE users
               SET password_enc = ?, password_hash = ?,
                   password_changed_at = ?, updated_at = ?
             WHERE id = ?
            """,
            (
                self._cipher.encrypt(new_password),
                self._cipher.hash_password(new_password),
                now,
                now,
                user_id,
            ),
        )
        logger.info("пароль пользователя id=%d изменён", user_id)

    async def change_password(
        self, user_id: int, current_password: str, new_password: str
    ) -> None:
        """Смена пароля самим пользователем — требует знания текущего."""
        record = await self.get_by_id(user_id)
        if record is None:
            raise UserError("пользователь не найден")
        if not self._cipher.verify_password(current_password, record.password_hash):
            raise UserError("текущий пароль неверен")
        if current_password == new_password:
            raise UserError("новый пароль совпадает с текущим")
        await self.set_password(user_id, new_password)

    async def reencrypt_password(self, user_id: int, plain_password: str) -> bool:
        """Перешифровывает обратимую копию пароля текущим ключом.

        Нужно, когда MTR_SECRET_KEY сменился: PBKDF2-хэш остался валидным
        (вход по паролю работает), а Fernet-копия не читается, из-за чего
        Subsonic token-авторизация возвращает код 40.

        Вызывается ТОЛЬКО после успешной проверки пароля по PBKDF2-хэшу —
        иначе любой мог бы перезаписать чужой пароль.
        """
        record = await self.get_by_id(user_id)
        if record is None:
            raise UserError("пользователь не найден")
        if not self._cipher.verify_password(plain_password, record.password_hash):
            raise UserError("пароль не соответствует сохранённому хэшу")

        await self._db.execute(
            "UPDATE users SET password_enc = ?, updated_at = ? WHERE id = ?",
            (self._cipher.encrypt(plain_password), utcnow_iso(), user_id),
        )
        logger.info(
            "обратимая копия пароля пользователя id=%d перешифрована "
            "текущим ключом", user_id,
        )
        return True

    async def reset_password(self, user_id: int, new_password: str) -> None:
        """Принудительная смена пароля без знания текущего (CLI админа).

        Отдельный метод, а не set_password, чтобы намерение было явным:
        используется при восстановлении доступа, когда старый пароль
        неизвестен.
        """
        await self.set_password(user_id, new_password)

    async def set_active(self, user_id: int, active: bool) -> UserPublic:
        """Блокировка/разблокировка. Последнего активного админа не блокируем."""
        record = await self.get_by_id(user_id)
        if record is None:
            raise UserError("пользователь не найден")

        if record.role == "admin" and record.is_active and not active:
            if await self.count_admins() <= 1:
                raise UserError(
                    "нельзя заблокировать последнего активного администратора"
                )

        await self._db.execute(
            "UPDATE users SET is_active = ?, updated_at = ? WHERE id = ?",
            (1 if active else 0, utcnow_iso(), user_id),
        )
        updated = await self.get_by_id(user_id)
        if updated is None:  # pragma: no cover
            raise UserError("пользователь исчез после обновления")
        return updated.to_public()

    async def set_role(self, user_id: int, role: str) -> UserPublic:
        """Смена роли. Понижение последнего админа запрещено."""
        if role not in VALID_ROLES:
            raise UserError(f"недопустимая роль: {role!r}")
        record = await self.get_by_id(user_id)
        if record is None:
            raise UserError("пользователь не найден")

        if record.role == "admin" and role != "admin":
            if await self.count_admins() <= 1:
                raise UserError("нельзя понизить последнего активного администратора")

        await self._db.execute(
            "UPDATE users SET role = ?, updated_at = ? WHERE id = ?",
            (role, utcnow_iso(), user_id),
        )
        updated = await self.get_by_id(user_id)
        if updated is None:  # pragma: no cover
            raise UserError("пользователь исчез после обновления")
        return updated.to_public()

    async def delete_user(self, user_id: int) -> None:
        """Удаляет пользователя из users.db.

        Плейлисты в mtr_music.db при этом НЕ трогаются: между базами нет FK.
        Очистка/передача плейлистов — отдельная двухфазная операция на
        уровне сервиса (этап 4), здесь мы только убираем учётку.
        """
        record = await self.get_by_id(user_id)
        if record is None:
            raise UserError("пользователь не найден")
        if record.role == "admin" and await self.count_admins() <= 1:
            raise UserError("нельзя удалить последнего активного администратора")

        await self._db.execute("DELETE FROM users WHERE id = ?", (user_id,))
        logger.info("удалён пользователь id=%d (%s)", user_id, record.username)

    async def touch_login(self, user_id: int) -> None:
        """Отмечает успешный вход."""
        await self._db.execute(
            "UPDATE users SET last_login_at = ?, updated_at = ? WHERE id = ?",
            (utcnow_iso(), utcnow_iso(), user_id),
        )

    # Аутентификация
    async def verify_credentials(self, username: str, password: str) -> UserRecord | None:
        """Проверка логина/пароля для веб-входа. Возвращает только активных."""
        record = await self.get_by_username(username)
        if record is None:
            return None
        if not record.is_active:
            logger.info("вход заблокированного пользователя %s отклонён", username)
            return None
        if not self._cipher.verify_password(password, record.password_hash):
            return None
        return record

    async def get_plain_password(self, user_id: int) -> str | None:
        """Расшифрованный пароль — ТОЛЬКО для Subsonic token-проверки.

        Расшифровка нужна потому, что md5(password + salt) невычислим из
        хэша. Вызывать этот метод следует лишь в момент проверки подписи
        запроса, не кэшировать и не логировать результат.
        """
        record = await self.get_by_id(user_id)
        if record is None or not record.is_active:
            return None
        try:
            return self._cipher.decrypt(record.password_enc)
        except CryptoError:
            logger.error(
                "не удалось расшифровать пароль пользователя id=%d — "
                "Subsonic-авторизация для него невозможна", user_id,
            )
            return None

    # Валидация
    @staticmethod
    def _validate_password(password: str) -> None:
        if not isinstance(password, str):
            raise UserError("пароль должен быть строкой")
        if len(password) < MIN_PASSWORD_LEN:
            raise UserError(f"пароль короче {MIN_PASSWORD_LEN} символов")
        if len(password) > MAX_PASSWORD_LEN:
            raise UserError(f"пароль длиннее {MAX_PASSWORD_LEN} символов")
