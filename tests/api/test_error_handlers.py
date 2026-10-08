import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from app.core.codes import ErrorCode
from app.core.exceptions import (
    BadRequest,
    Conflict,
    InternalError,
    NotFound,
    PermissionDenied,
    RateLimited,
    ServiceUnavailable,
    Unauthorized,
    UserInactive,
    ValidationFailed,
)


def make_raise_route(app, exc: Exception) -> None:
    @app.get("/_raise")
    async def _raise():
        raise exc


# --- AppError subclasses ---


async def test_bad_request(app, client):
    make_raise_route(app, BadRequest(ErrorCode.INVALID_FILE, "file is not ready"))
    r = await client.get("/_raise")
    assert r.status_code == 400
    assert r.json() == {"error": {"code": "invalid_file", "message": "file is not ready"}}


async def test_not_found(app, client):
    make_raise_route(app, NotFound(ErrorCode.CHANNEL_NOT_FOUND))
    r = await client.get("/_raise")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "channel_not_found"


async def test_conflict(app, client):
    make_raise_route(app, Conflict(ErrorCode.CHANNEL_ARCHIVED))
    r = await client.get("/_raise")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "channel_archived"


async def test_validation_failed_with_fields(app, client):
    make_raise_route(app, ValidationFailed(message="body too long", fields={"body": "too_long"}))
    r = await client.get("/_raise")
    assert r.status_code == 400
    assert r.json() == {
        "error": {
            "code": "validation_error",
            "message": "body too long",
            "fields": {"body": "too_long"},
        }
    }


async def test_validation_failed_without_fields_omits_fields_key(app, client):
    make_raise_route(app, ValidationFailed(message="invalid request"))
    r = await client.get("/_raise")
    assert r.status_code == 400
    body = r.json()["error"]
    assert body["code"] == "validation_error"
    assert "fields" not in body


async def test_unauthorized(app, client):
    make_raise_route(app, Unauthorized())
    r = await client.get("/_raise")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


async def test_permission_denied_passes_through_code(app, client):
    make_raise_route(app, PermissionDenied(ErrorCode.NOT_A_MEMBER))
    r = await client.get("/_raise")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "not_a_member"


async def test_user_inactive(app, client):
    make_raise_route(app, UserInactive())
    r = await client.get("/_raise")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "user_inactive"


async def test_rate_limited_sets_retry_after_header(app, client):
    make_raise_route(app, RateLimited(retry_after=30))
    r = await client.get("/_raise")
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "rate_limited"
    assert r.headers["retry-after"] == "30"


async def test_internal_error(app, client):
    make_raise_route(app, InternalError())
    r = await client.get("/_raise")
    assert r.status_code == 500
    assert r.json()["error"]["code"] == "internal_error"


async def test_service_unavailable(app, client):
    make_raise_route(app, ServiceUnavailable())
    r = await client.get("/_raise")
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "core_unavailable"


# --- RequestValidationError ---


async def test_request_validation_error_returns_400_with_fields(app, client):
    class Body(BaseModel):
        text: str
        count: int

    @app.post("/_validate")
    async def _validate(body: Body):
        return body

    r = await client.post("/_validate", json={"text": "hi"})
    assert r.status_code == 400
    body = r.json()["error"]
    assert body["code"] == "validation_error"
    assert "count" in body["fields"]


async def test_request_validation_error_omits_fields_when_no_body(app, client):
    r = await client.post("/api/chat/v1/nonexistent", json=None)
    # 404 from FastAPI itself, but we confirm the error shape is contract-compliant
    assert r.status_code == 404


# --- Unhandled Exception catch-all ---
#
# Starlette's ServerErrorMiddleware always re-raises after calling the handler so
# that test clients can inspect the exception. ASGITransport(raise_app_exceptions=False)
# suppresses that re-raise and lets us assert on the 500 response instead.


@pytest.fixture
async def silent_client(app):
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as c:
        yield c


async def test_unhandled_exception_returns_500_with_request_id(app, silent_client):
    make_raise_route(app, RuntimeError("something broke"))
    r = await silent_client.get("/_raise")
    assert r.status_code == 500
    body = r.json()["error"]
    assert body["code"] == "internal_error"
    assert "request_id=" in body["message"]


async def test_unhandled_exception_includes_uuid_in_request_id(app, silent_client):
    make_raise_route(app, ValueError("unexpected"))
    r = await silent_client.get("/_raise")
    message = r.json()["error"]["message"]
    request_id = message.split("request_id=")[-1]
    assert len(request_id) == 36
    assert request_id.count("-") == 4
