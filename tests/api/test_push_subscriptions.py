import base64
import uuid

import pytest

from app.api.deps import get_core_client, get_push_subscription_service
from app.schemas.push import DeletePushSubscriptionRequest, SavePushSubscriptionRequest
from tests.fakes import FakeCore, memberships

USER_ID = uuid.uuid4()
URL = "/api/chat/v1/push/subscriptions"
ENDPOINT = "https://fcm.googleapis.com/fcm/send/abc123"


def b64url(size: int, padded: bool = False) -> str:
    """base64url of `size` bytes, the way browsers send it (no padding) unless `padded`."""
    value = base64.urlsafe_b64encode(bytes(range(size))).decode()
    return value if padded else value.rstrip("=")


P256DH, AUTH = b64url(65), b64url(16)


class FakePushSubscriptionService:
    def __init__(self) -> None:
        self.saved: list[tuple[uuid.UUID, SavePushSubscriptionRequest]] = []
        self.deleted: list[tuple[uuid.UUID, DeletePushSubscriptionRequest]] = []

    async def save_subscription(
        self, user_id: uuid.UUID, request: SavePushSubscriptionRequest
    ) -> None:
        self.saved.append((user_id, request))

    async def delete_subscription(
        self, user_id: uuid.UUID, request: DeletePushSubscriptionRequest
    ) -> None:
        self.deleted.append((user_id, request))


@pytest.fixture
def auth(make_token) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(sub=str(USER_ID))}"}


@pytest.fixture
def service(app) -> FakePushSubscriptionService:
    fake = FakePushSubscriptionService()
    app.dependency_overrides[get_core_client] = lambda: FakeCore(memberships=memberships(USER_ID))
    app.dependency_overrides[get_push_subscription_service] = lambda: fake
    return fake


def body(**changes: str | None) -> dict:
    """A valid save body; a field set to None is left out."""
    data = {"endpoint": ENDPOINT, "p256dh": P256DH, "auth": AUTH} | changes
    return {name: value for name, value in data.items() if value is not None}


async def delete(client, data: dict, headers: dict | None = None):
    return await client.request("DELETE", URL, json=data, headers=headers)


@pytest.mark.parametrize("padded", [False, True], ids=["no padding", "padding"])
async def test_save_is_204_and_saves_for_the_current_user(client, auth, service, padded):
    data = body(p256dh=b64url(65, padded), auth=b64url(16, padded))

    response = await client.post(URL, json=data, headers=auth)

    assert response.status_code == 204
    assert response.content == b""
    [(user_id, request)] = service.saved
    assert user_id == USER_ID
    assert request.model_dump() == data


async def test_save_ignores_extra_browser_fields(client, auth, service):
    data = body() | {"expirationTime": None}

    response = await client.post(URL, json=data, headers=auth)

    assert response.status_code == 204


@pytest.mark.parametrize(
    ("changes", "fields"),
    [
        ({"endpoint": "http://fcm.googleapis.com/x"}, {"endpoint": "invalid"}),
        ({"endpoint": "https://"}, {"endpoint": "invalid"}),
        ({"endpoint": "fcm.googleapis.com/x"}, {"endpoint": "invalid"}),
        ({"endpoint": "https://a.test/" + "a" * 2034}, {"endpoint": "too_long"}),
        ({"p256dh": b64url(64)}, {"p256dh": "invalid"}),
        ({"p256dh": "+" + P256DH[1:]}, {"p256dh": "invalid"}),
        ({"p256dh": "x" * 101}, {"p256dh": "too_long"}),
        ({"auth": b64url(15)}, {"auth": "invalid"}),
        ({"auth": AUTH[:10] + "=" + AUTH[11:]}, {"auth": "invalid"}),
        ({"auth": ""}, {"auth": "invalid"}),
        ({"auth": None}, {"auth": "required"}),
        ({"endpoint": "https://example.com/x"}, {"endpoint": "invalid"}),
        ({"endpoint": "https://fcm.googleapis.com@evil.example/x"}, {"endpoint": "invalid"}),
    ],
    ids=[
        "http endpoint",
        "no host",
        "no scheme",
        "endpoint too long",
        "p256dh 64 bytes",
        "p256dh plain base64",
        "p256dh too long",
        "auth 15 bytes",
        "auth padding in the middle",
        "auth empty",
        "auth missing",
        "unknown push host",
        "push host as user info",
    ],
)
async def test_invalid_save_is_400(client, auth, service, changes, fields):
    response = await client.post(URL, json=body(**changes), headers=auth)

    assert response.status_code == 400
    error = response.json()["error"]
    assert (error["code"], error["fields"]) == ("validation_error", fields)
    assert service.saved == []


async def test_delete_is_204_and_deletes_for_the_current_user(client, auth, service):
    response = await delete(client, {"endpoint": ENDPOINT}, auth)

    assert response.status_code == 204
    assert response.content == b""
    [(user_id, request)] = service.deleted
    assert (user_id, request.endpoint) == (USER_ID, ENDPOINT)


@pytest.mark.parametrize(
    ("data", "fields"),
    [({}, {"endpoint": "required"}), ({"endpoint": "http://a.test/x"}, {"endpoint": "invalid"})],
    ids=["missing", "not https"],
)
async def test_invalid_delete_is_400(client, auth, service, data, fields):
    response = await delete(client, data, auth)

    assert response.status_code == 400
    error = response.json()["error"]
    assert (error["code"], error["fields"]) == ("validation_error", fields)
    assert service.deleted == []


async def test_without_a_token_is_401(client, service):
    saved = await client.post(URL, json=body())
    deleted = await delete(client, {"endpoint": ENDPOINT})

    assert (saved.status_code, deleted.status_code) == (401, 401)
    assert (service.saved, service.deleted) == ([], [])


async def test_inactive_user_is_403(app, client, auth, service):
    core = FakeCore(memberships=memberships(USER_ID, is_active=False))
    app.dependency_overrides[get_core_client] = lambda: core

    response = await client.post(URL, json=body(), headers=auth)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "user_inactive"
    assert service.saved == []
