"""Ошибки API в едином виде `{error: {code, message}}` (docs/api.md, «Общее»)."""

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

_CODES = {
    400: "bad_request",
    404: "not_found",
    409: "conflict",
    413: "too_large",
    415: "unsupported_media_type",
    422: "validation_error",
    503: "unavailable",
}


class ApiError(HTTPException):
    """Ошибка с кодом для интерфейса: `raise ApiError(404, "run_not_found", "прогон 7 не найден")`."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(status_code=status_code, detail=message)
        self.code = code


def _body(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def install(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = getattr(exc, "code", None) or _CODES.get(exc.status_code, "error")
        return JSONResponse(_body(code, str(exc.detail)), status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", ()))
        return JSONResponse(_body("validation_error", f"{where}: {first.get('msg', 'неверный запрос')}"), 422)
