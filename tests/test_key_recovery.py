"""Тесты восстановления после смены MTR_SECRET_KEY.

Сценарий из практики: ключ шифрования сменился между запусками контейнера.
PBKDF2-хэш пароля остаётся валидным, а Fernet-копия перестаёт читаться.
Следствие: вход по паролю должен работать, а token-авторизация — падать
с кодом 40, пока обратимая копия не будет перешифрована.

Ключевой инвариант: проверка пароля НЕ зависит от ключа шифрования.
"""

from __future__ import annotations

import hashlib

import pytest

from app.bootstrap import AppContext
from app.config import Settings
from app.security.crypto import PasswordCipher
from app.subsonic.auth import authenticate
from app.subsonic.errors import ErrorCode, SubsonicError

PASSWORD = "changeme123"
SALT = "abc123def456"
TOKEN = hashlib.md5((PASSWORD + SALT).encode()).hexdigest()

TOKEN_PARAMS = {"u": "admin", "t": TOKEN, "s": SALT, "v": "1.16.1", "c": "test"}
PLAIN_PARAMS = {"u": "admin", "p": PASSWORD, "v": "1.16.1", "c": "test"}
HEX_PARAMS = {
    "u": "admin", "p": "enc:" + PASSWORD.encode().hex(),
    "v": "1.16.1", "c": "test",
}


@pytest.fixture
async def stale_key_ctx(tmp_path, settings):
    """База, записанная одним ключом, открытая другим.

    Имитирует ровно вашу ситуацию: старый контейнер работал с ключом А,
    затем в .env прописали ключ Б.
    """
    old_secret = "old-secret-key-1234567890"
    new_secret = "new-secret-key-9876543210"

    # Запись старым ключом.
    old_settings = Settings(
        music_dir=settings.music_dir,
        data_dir=settings.data_dir,
        users_db=settings.users_db,
        music_db=settings.music_db,
        covers_dir=settings.covers_dir,
        secret_key=old_secret,
        secret_key_file=settings.secret_key_file,
    )
    ctx_old = await AppContext.create(old_settings)
    await ctx_old.users.create_user("admin", PASSWORD, role="admin")
    await ctx_old.close()

    # Открытие новым ключом.
    new_settings = Settings(
        music_dir=settings.music_dir,
        data_dir=settings.data_dir,
        users_db=settings.users_db,
        music_db=settings.music_db,
        covers_dir=settings.covers_dir,
        secret_key=new_secret,
        secret_key_file=settings.secret_key_file,
    )
    ctx = await AppContext.create(new_settings)
    try:
        yield ctx
    finally:
        await ctx.close()


# Слой крипто: почему это вообще возможно
def test_pbkdf2_verification_ignores_key():
    """Хэш PBKDF2 не зависит от ключа — основа восстановления."""
    stored = PasswordCipher.hash_password(PASSWORD)
    # Сверка работает без всякого ключа: соль и хэш лежат в самой строке.
    assert PasswordCipher.verify_password(PASSWORD, stored)
    assert not PasswordCipher.verify_password("wrong", stored)


def test_fernet_copy_is_key_dependent():
    """Обратимая копия ключом и ломается — собственно причина кода 40."""
    token = PasswordCipher("key-one").encrypt(PASSWORD)
    with pytest.raises(Exception):
        PasswordCipher("key-two").decrypt(token)


# Поведение аутентификации при сменённом ключе
async def test_plain_password_works_after_key_change(stale_key_ctx):
    """Вход по паролю обязан работать: он проверяется по PBKDF2."""
    auth = await authenticate(stale_key_ctx, PLAIN_PARAMS, ip="1.1.1.1")
    assert auth.user.username == "admin"


async def test_hex_password_works_after_key_change(stale_key_ctx):
    auth = await authenticate(stale_key_ctx, HEX_PARAMS, ip="1.1.1.1")
    assert auth.user.username == "admin"


async def test_token_fails_before_recovery(stale_key_ctx):
    """До перешифровки токен невозможен: нужен расшифрованный пароль."""
    with pytest.raises(SubsonicError) as exc:
        await authenticate(stale_key_ctx, TOKEN_PARAMS, ip="1.1.1.1")
    assert exc.value.code == ErrorCode.WRONG_CREDENTIALS


async def test_plain_login_recovers_token_auth(stale_key_ctx):
    """Главный сценарий: один вход паролем чинит token-авторизацию."""
    # До восстановления токен не работает.
    with pytest.raises(SubsonicError):
        await authenticate(stale_key_ctx, TOKEN_PARAMS, ip="1.1.1.1")

    # Вход паролем перешифровывает обратимую копию.
    await authenticate(stale_key_ctx, PLAIN_PARAMS, ip="1.1.1.1")

    # После — токен работает, и веб-логин не пострадал.
    auth = await authenticate(stale_key_ctx, TOKEN_PARAMS, ip="1.1.1.1")
    assert auth.user.username == "admin"
    assert await stale_key_ctx.users.verify_credentials("admin", PASSWORD) is not None


async def test_wrong_password_does_not_recover(stale_key_ctx):
    """Неверный пароль не должен перешифровывать чужой пароль."""
    with pytest.raises(SubsonicError) as exc:
        await authenticate(
            stale_key_ctx, {**PLAIN_PARAMS, "p": "wrongpass"}, ip="1.1.1.1"
        )
    assert exc.value.code == ErrorCode.WRONG_CREDENTIALS

    # Старая (нечитаемая) копия осталась нетронутой: токен всё ещё не работает.
    with pytest.raises(SubsonicError):
        await authenticate(stale_key_ctx, TOKEN_PARAMS, ip="1.1.1.1")


async def test_reencrypt_requires_matching_password(stale_key_ctx):
    """reencrypt_password отказывает, если пароль не соответствует хэшу."""
    from app.repositories.users import UserError

    record = await stale_key_ctx.users.get_by_username("admin")
    with pytest.raises(UserError, match="не соответствует"):
        await stale_key_ctx.users.reencrypt_password(record.id, "totally-wrong")


# CLI-эквивалент: принудительный сброс
async def test_reset_password_fixes_both_paths(stale_key_ctx):
    """reset_password (CLI setpassword) чинит и пароль, и токен."""
    record = await stale_key_ctx.users.get_by_username("admin")
    await stale_key_ctx.users.reset_password(record.id, "brand-new-pass-1")

    # Вход по паролю.
    auth = await authenticate(
        stale_key_ctx,
        {"u": "admin", "p": "brand-new-pass-1", "v": "1.16.1", "c": "test"},
        ip="1.1.1.1",
    )
    assert auth.user.username == "admin"

    # И токен с новым паролем.
    new_token = hashlib.md5(("brand-new-pass-1" + SALT).encode()).hexdigest()
    await authenticate(
        stale_key_ctx,
        {"u": "admin", "t": new_token, "s": SALT, "v": "1.16.1", "c": "test"},
        ip="1.1.1.1",
    )


# Диагностика
async def test_doctor_detects_broken_cipher(stale_key_ctx):
    from app.diagnostics import run_doctor

    report = await run_doctor(stale_key_ctx, secret="new-secret-key-9876543210")
    assert report.users_with_broken_cipher == ["admin"]
    assert not report.healthy
    assert any("не расшифровываются" in problem for problem in report.problems)


async def test_doctor_clean_after_reset(stale_key_ctx):
    from app.diagnostics import run_doctor

    record = await stale_key_ctx.users.get_by_username("admin")
    await stale_key_ctx.users.reset_password(record.id, "fresh-pass-12345")

    report = await run_doctor(stale_key_ctx, secret="new-secret-key-9876543210")
    assert report.users_with_broken_cipher == []
    assert report.healthy


async def test_doctor_warns_short_secret(stale_key_ctx):
    from app.diagnostics import run_doctor

    report = await run_doctor(stale_key_ctx, secret="short")
    assert any("короче" in note for note in report.notes)


# Bootstrap: env-пароль не применяется к существующим пользователям
async def test_bootstrap_warns_when_users_exist(ctx, caplog):
    from app.bootstrap import ensure_bootstrap_admin

    await ctx.users.create_user("existing", "password123")
    with caplog.at_level("WARNING"):
        created = await ensure_bootstrap_admin(
            ctx.users, username="other", password="password123"
        )
    assert created is False
    assert any("проигнорирован" in record.message for record in caplog.records)
