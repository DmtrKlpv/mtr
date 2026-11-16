"""Тесты пользователей: крипто, роли, защита последнего админа."""

from __future__ import annotations

import pytest

from app.repositories.users import UserError
from app.security.crypto import CryptoError, PasswordCipher


# Крипто
def test_encrypt_decrypt_roundtrip():
    cipher = PasswordCipher("my-secret")
    token = cipher.encrypt("S3cret-pass")
    assert token != "S3cret-pass"
    assert cipher.decrypt(token) == "S3cret-pass"


def test_encrypt_is_not_deterministic():
    """Fernet использует IV: два шифрования одного пароля различаются."""
    cipher = PasswordCipher("my-secret")
    assert cipher.encrypt("same") != cipher.encrypt("same")


def test_decrypt_with_wrong_key_raises():
    token = PasswordCipher("key-one").encrypt("pw")
    with pytest.raises(CryptoError):
        PasswordCipher("key-two").decrypt(token)


def test_hash_verify():
    stored = PasswordCipher.hash_password("пароль123")
    assert stored.startswith("pbkdf2_sha256$")
    assert PasswordCipher.verify_password("пароль123", stored)
    assert not PasswordCipher.verify_password("пароль124", stored)


def test_verify_rejects_malformed_hash():
    assert not PasswordCipher.verify_password("x", "not-a-hash")
    assert not PasswordCipher.verify_password("x", "")
    assert not PasswordCipher.verify_password("x", "md5$1$a$b")


def test_key_file_created_with_restrictive_perms(tmp_path):
    from app.security.crypto import load_or_create_secret

    key_file = tmp_path / "secret.key"
    secret = load_or_create_secret(None, key_file)
    assert key_file.exists()
    assert key_file.read_text().strip() == secret
    assert oct(key_file.stat().st_mode)[-3:] == "600"

    # Повторный вызов читает тот же ключ, а не создаёт новый.
    assert load_or_create_secret(None, key_file) == secret


def test_env_secret_wins(tmp_path):
    from app.security.crypto import load_or_create_secret

    key_file = tmp_path / "secret.key"
    assert load_or_create_secret("from-env", key_file) == "from-env"
    assert not key_file.exists()


# Пользователи
async def test_create_and_read_user(ctx):
    created = await ctx.users.create_user("bob", "password123", role="user")
    assert created.id > 0
    assert created.username == "bob"
    assert created.role == "user"

    record = await ctx.users.get_by_id(created.id)
    assert record is not None
    # Пароль хранится в двух формах, обе не равны открытому тексту.
    assert record.password_enc != "password123"
    assert record.password_hash != "password123"


async def test_create_user_stores_decryptable_password(ctx):
    """Ключевой инвариант для Subsonic: сервер может достать пароль."""
    created = await ctx.users.create_user("carol", "password123")
    assert await ctx.users.get_plain_password(created.id) == "password123"


async def test_duplicate_username_rejected(ctx):
    await ctx.users.create_user("dave", "password123")
    with pytest.raises(UserError, match="уже существует"):
        await ctx.users.create_user("DAVE", "password456")


@pytest.mark.parametrize("name", ["ab", "a b", "юзер", "x" * 40, ""])
async def test_invalid_username_rejected(ctx, name):
    with pytest.raises(UserError):
        await ctx.users.create_user(name, "password123")


@pytest.mark.parametrize("password", ["short", "", "x" * 300])
async def test_invalid_password_rejected(ctx, password):
    with pytest.raises(UserError):
        await ctx.users.create_user("erin", password)


async def test_verify_credentials(ctx):
    await ctx.users.create_user("frank", "password123")
    assert await ctx.users.verify_credentials("frank", "password123") is not None
    assert await ctx.users.verify_credentials("frank", "wrong") is None
    assert await ctx.users.verify_credentials("nobody", "password123") is None


async def test_inactive_user_cannot_authenticate(ctx):
    user = await ctx.users.create_user("gina", "password123")
    await ctx.users.set_active(user.id, False)

    assert await ctx.users.verify_credentials("gina", "password123") is None
    # Subsonic-путь тоже обязан отказать.
    assert await ctx.users.get_plain_password(user.id) is None


async def test_change_password_requires_current(ctx):
    user = await ctx.users.create_user("hana", "password123")
    with pytest.raises(UserError, match="неверен"):
        await ctx.users.change_password(user.id, "wrong", "newpassword1")

    await ctx.users.change_password(user.id, "password123", "newpassword1")
    assert await ctx.users.verify_credentials("hana", "newpassword1") is not None
    assert await ctx.users.verify_credentials("hana", "password123") is None
    # Расшифрованная форма тоже обновилась — иначе Subsonic сломался бы.
    assert await ctx.users.get_plain_password(user.id) == "newpassword1"


async def test_cannot_block_last_admin(ctx):
    admin = await ctx.users.create_user("root", "password123", role="admin")
    with pytest.raises(UserError, match="последнего"):
        await ctx.users.set_active(admin.id, False)


async def test_can_block_admin_if_another_exists(ctx):
    first = await ctx.users.create_user("root1", "password123", role="admin")
    await ctx.users.create_user("root2", "password123", role="admin")
    updated = await ctx.users.set_active(first.id, False)
    assert updated.is_active is False


async def test_cannot_demote_last_admin(ctx):
    admin = await ctx.users.create_user("solo", "password123", role="admin")
    with pytest.raises(UserError, match="последнего"):
        await ctx.users.set_role(admin.id, "user")


async def test_cannot_delete_last_admin(ctx):
    admin = await ctx.users.create_user("keeper", "password123", role="admin")
    with pytest.raises(UserError, match="последнего"):
        await ctx.users.delete_user(admin.id)


async def test_delete_user(ctx):
    user = await ctx.users.create_user("temp", "password123")
    await ctx.users.delete_user(user.id)
    assert await ctx.users.get_by_id(user.id) is None


async def test_public_projection_hides_secrets(ctx):
    user = await ctx.users.create_user("ivan", "password123")
    record = await ctx.users.get_by_id(user.id)
    public = record.to_public()
    assert not hasattr(public, "password_enc")
    assert not hasattr(public, "password_hash")


async def test_touch_login_updates_timestamp(ctx):
    user = await ctx.users.create_user("jack", "password123")
    assert (await ctx.users.get_by_id(user.id)).last_login_at is None
    await ctx.users.touch_login(user.id)
    assert (await ctx.users.get_by_id(user.id)).last_login_at is not None


async def test_bootstrap_admin_only_when_empty(ctx):
    from app.bootstrap import ensure_bootstrap_admin

    assert await ensure_bootstrap_admin(
        ctx.users, username="admin", password="password123"
    ) is True
    # Второй раз — база уже не пуста.
    assert await ensure_bootstrap_admin(
        ctx.users, username="admin2", password="password123"
    ) is False
    assert await ctx.users.count_users() == 1
