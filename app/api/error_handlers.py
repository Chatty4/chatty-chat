import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.core.exceptions import AppError

logger = logging.getLogger(__name__)

_PYDANTIC_CODES: dict[str, str] = {
    "missing": "required",
    "string_too_long": "too_long",
    "string_too_short": "too_short",
}
_PARAM_SOURCES = frozenset({"body", "query", "path", "header", "cookie"})
_HTTP_CODES: dict[int, str] = {404: "not_found", 405: "method_not_allowed"}
NON_FIELD = "non_field_errors"


def _body(
    code: str,
    message: str,
    fields: dict[str, str] | None = None,
    request_id: str | None = None,
) -> dict:
    error: dict = {"code": code, "message": message}
    if fields:
        error["fields"] = fields
    if request_id:
        error["request_id"] = request_id
    return {"error": error}


def _field_path(loc: tuple) -> str:
    parts = loc[1:] if loc and str(loc[0]) in _PARAM_SOURCES else loc
    return ".".join(str(p) for p in parts) if parts else NON_FIELD


def _field_code(pydantic_type: str) -> str:
    return _PYDANTIC_CODES.get(pydantic_type, "invalid")


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        assert exc.code is not None  # guaranteed by AppError.__init__
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after is not None else {}
        return JSONResponse(
            status_code=exc.status_code,
            content=_body(exc.code, exc.message, exc.fields or None),
            headers=headers or None,
        )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        code = _HTTP_CODES.get(exc.status_code, str(exc.status_code))
        message = exc.detail if isinstance(exc.detail, str) else "HTTP error."
        return JSONResponse(status_code=exc.status_code, content=_body(code, message))

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        fields: dict[str, str] = {}
        for error in exc.errors():
            field = _field_path(error["loc"])
            fields.setdefault(field, _field_code(error["type"]))
        return JSONResponse(
            status_code=400,
            content=_body("validation_error", "Invalid request.", fields or None),
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        request_id = uuid.uuid4().hex
        logger.exception(
            "Unhandled exception request_id=%s %s %s",
            request_id,
            request.method,
            request.url.path,
        )
        return JSONResponse(
            status_code=500,
            content=_body("internal_error", "An unexpected error occurred.", request_id=request_id),
        )
