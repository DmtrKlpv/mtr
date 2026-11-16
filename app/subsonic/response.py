"""Формирование конверта `subsonic-response`.

Все ответы протокола — один JSON-объект с единственным ключом
`subsonic-response`. Внутри: status, version, служебные поля сервера
и полезная нагрузка конкретного метода.

`openSubsonic: true` сообщает клиентам (Feishin, Supersonic, Amperfy), что мы
поддерживаем расширения OpenSubsonic. Мы заявляем только те, что реально
работают, иначе клиент начнёт использовать отсутствующие возможности.
"""

from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse

from app import __version__
from app.subsonic.errors import SubsonicError

# Имя сервера в поле type — клиенты показывают его в «О сервере».
SERVER_TYPE = "mtr"


def _base(api_version: str) -> dict[str, Any]:
    """Служебная часть конверта, общая для успеха и ошибки."""
    return {
        "status": "ok",
        "version": api_version,
        "type": SERVER_TYPE,
        "serverVersion": __version__,
        "openSubsonic": True,
    }


def ok_response(
    payload: dict[str, Any] | None = None, *, api_version: str
) -> JSONResponse:
    """Успешный ответ. None-значения в payload отбрасываются.

    Клиенты Subsonic не ожидают `null` в полях и часть из них падает
    на разборе, поэтому пустые значения не отправляем вовсе.
    """
    body = _base(api_version)
    if payload:
        body.update({key: value for key, value in payload.items() if value is not None})
    return JSONResponse({"subsonic-response": body})


def error_response(
    error: SubsonicError,
    *,
    api_version: str,
    status_code: int = 200,
) -> JSONResponse:
    """Ответ с ошибкой. По умолчанию HTTP 200 — как требует протокол."""
    body = _base(api_version)
    body["status"] = "failed"
    body["error"] = {"code": error.code, "message": error.message}
    return JSONResponse({"subsonic-response": body}, status_code=status_code)
