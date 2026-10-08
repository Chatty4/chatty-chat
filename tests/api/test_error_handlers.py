import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, Field

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


# --- HTTPException (routing 404 / 405) ---


async def test_unknown_route_returns_not_found_in_contract_shape(app, client):
    r = await client.get("/api/chat/v1/does-not-exist")
    assert r.status_code == 404
    assert r.json() == {"error": {"code": "not_found", "message": "Not Found"}}


async def test_wrong_method_returns_method_not_allowed_in_contract_shape(app, client):
    # health is GET-only; POST should get 405
    r = await client.post("/health")
    assert r.status_code == 405
    assert r.json()["error"]["code"] == "method_not_allowed"


# --- RequestValidationError ---


async def test_request_validation_error_returns_400_with_short_codes(app, client):
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
    assert body["fields"]["count"] == "required"


async def test_request_validation_too_long_maps_to_too_long(app, client):
    class Body(BaseModel):
        name: str = Field(max_length=3)

    @app.post("/_validate_len")
    async def _validate_len(body: Body):
        return body

    r = await client.post("/_validate_len", json={"name": "toolong"})
    assert r.status_code == 400
    assert r.json()["error"]["fields"]["name"] == "too_long"


async def test_request_validation_nested_path_is_preserved(app, client):
    class Address(BaseModel):
        city: str

    class Body(BaseModel):
        address: Address

    @app.post("/_validate_nested")
    async def _validate_nested(body: Body):
        return body

    r = await client.post("/_validate_nested", json={"address": {}})
    assert r.status_code == 400
    assert "address.city" in r.json()["error"]["fields"]


async def test_request_validation_message_is_always_invalid_request(app, client):
    class Body(BaseModel):
        x: int

    @app.post("/_validate_msg")
    async def _validate_msg(body: Body):
        return body

    r = await client.post("/_validate_msg", json={})
    assert r.json()["error"]["message"] == "Invalid request."


@pytest.fixture
async def silent_client(app):
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as c:
        yield c


async def test_unhandled_exception_returns_500_with_request_id_field(app, silent_client):
    make_raise_route(app, RuntimeError("something broke"))
    r = await silent_client.get("/_raise")
    assert r.status_code == 500
    body = r.json()["error"]
    assert body["code"] == "internal_error"
    assert "request_id" in body
    assert "something broke" not in r.text


async def test_unhandled_exception_request_id_is_hex(app, silent_client):
    make_raise_route(app, ValueError("unexpected"))
    r = await silent_client.get("/_raise")
    request_id = r.json()["error"]["request_id"]
    assert len(request_id) == 32
    assert all(c in "0123456789abcdef" for c in request_id)


async def test_unhandled_exception_does_not_leak_details(app, silent_client):
    make_raise_route(app, RuntimeError("secret detail"))
    r = await silent_client.get("/_raise")
    assert "secret detail" not in r.text
