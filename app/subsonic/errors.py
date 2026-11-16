"""Коды ошибок Subsonic API и исключение для их передачи.

HTTP-статус при ошибке остаётся 200 — так требует протокол: клиенты
(Navidrome-совместимые, DSub, Symfonium, play:Sub) читают код из тела
`subsonic-response.error`, а не из заголовков. Исключение составляют
бинарные эндпоинты (stream, download, getCoverArt): там реальный HTTP-статус
важнее, потому что плеер решает по нему, пытаться ли снова.

Тексты — стандартные англоязычные из спецификации, клиенты иногда показывают
их пользователю как есть, поэтому не переводим.
"""

from __future__ import annotations

from enum import IntEnum


class ErrorCode(IntEnum):
    """Официальные коды ошибок Subsonic API 1.16.1."""

    GENERIC = 0
    MISSING_PARAMETER = 10
    CLIENT_TOO_OLD = 20
    SERVER_TOO_OLD = 30
    WRONG_CREDENTIALS = 40
    TOKEN_AUTH_UNSUPPORTED = 41
    NOT_AUTHORIZED = 50
    TRIAL_EXPIRED = 60
    NOT_FOUND = 70


DEFAULT_MESSAGES: dict[int, str] = {
    ErrorCode.GENERIC: "A generic error.",
    ErrorCode.MISSING_PARAMETER: "Required parameter is missing.",
    ErrorCode.CLIENT_TOO_OLD: "Incompatible Subsonic REST protocol version. "
                              "Client must upgrade.",
    ErrorCode.SERVER_TOO_OLD: "Incompatible Subsonic REST protocol version. "
                              "Server must upgrade.",
    ErrorCode.WRONG_CREDENTIALS: "Wrong username or password.",
    ErrorCode.TOKEN_AUTH_UNSUPPORTED: "Token authentication not supported for "
                                      "this user.",
    ErrorCode.NOT_AUTHORIZED: "User is not authorized for the given operation.",
    ErrorCode.TRIAL_EXPIRED: "The trial period for the Subsonic server is over.",
    ErrorCode.NOT_FOUND: "The requested data was not found.",
}


class SubsonicError(Exception):
    """Ошибка, которую надо отдать клиенту в конверте subsonic-response."""

    def __init__(self, code: int | ErrorCode, message: str | None = None) -> None:
        self.code = int(code)
        self.message = message or DEFAULT_MESSAGES.get(
            self.code, DEFAULT_MESSAGES[ErrorCode.GENERIC]
        )
        super().__init__(f"[{self.code}] {self.message}")


def missing(name: str) -> SubsonicError:
    """Понятная ошибка «не хватает параметра» с именем параметра."""
    return SubsonicError(
        ErrorCode.MISSING_PARAMETER,
        f"Required parameter is missing: {name}",
    )
