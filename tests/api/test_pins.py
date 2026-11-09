import uuid

import pytest

from app.api.deps import get_core_client, get_pin_service
from app.models import Message
from tests.fakes import (
    FakeCore,
    FakeMessages,
    PinFakes,
    channel,
    memberships,
    saved_message,
    saved_pin,
)

USER_ID = uuid.uuid4()


def url(message: Message) -> str:
    return f"/api/chat/v1/channels/{message.channel_id}/pins/{message.id}"


@pytest.fixture
def auth(make_token) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(sub=str(USER_ID))}"}


ROOM = channel(USER_ID)


@pytest.fixture
def message() -> Message:
    return saved_message(ROOM.channel_id, USER_ID, uuid.uuid4())


@pytest.fixture(autouse=True)
def fakes(app, message) -> PinFakes:
    core = FakeCore(memberships=memberships(USER_ID), channel=ROOM)
    fakes = PinFakes(core, FakeMessages(stored=[message]))
    app.dependency_overrides[get_core_client] = lambda: core
    app.dependency_overrides[get_pin_service] = lambda: fakes.service
    return fakes


async def test_post_is_201_with_the_pin_then_200(client, auth, message):
    first = await client.post(url(message), headers=auth)
    again = await client.post(url(message), headers=auth)

    assert first.status_code == 201
    body = first.json()
    assert set(body) == {"channel_id", "message_id", "pinned_by", "pinned_at"}
    assert (body["channel_id"], body["message_id"], body["pinned_by"]) == (
        str(message.channel_id),
        str(message.id),
        str(USER_ID),
    )
    assert (again.status_code, again.json()) == (200, body)


async def test_post_over_the_limit_is_409_with_the_error_body(client, auth, fakes, message):
    for seq in range(2, 102):
        other = saved_message(ROOM.channel_id, USER_ID, uuid.uuid4(), seq=seq)
        fakes.pins.rows[other.id] = saved_pin(other, USER_ID)

    response = await client.post(url(message), headers=auth)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "pin_limit_reached"


async def test_delete_is_204_pinned_or_not(client, auth, message):
    await client.post(url(message), headers=auth)

    unpinned = await client.delete(url(message), headers=auth)
    again = await client.delete(url(message), headers=auth)

    assert (unpinned.status_code, unpinned.content) == (204, b"")
    assert (again.status_code, again.content) == (204, b"")


async def test_get_lists_pins_with_the_message_object(client, auth, message):
    await client.post(url(message), headers=auth)

    response = await client.get(f"/api/chat/v1/channels/{message.channel_id}/pins", headers=auth)

    assert response.status_code == 200
    [pin] = response.json()["pins"]
    assert set(pin) == {"pinned_by", "pinned_at", "message"}
    assert pin["pinned_by"] == str(USER_ID)
    assert set(pin["message"]) == {
        "id",
        "channel_id",
        "seq",
        "author_id",
        "client_msg_id",
        "body",
        "file_ids",
        "attachments",
        "mentions",
        "reactions",
        "pinned",
        "edited_at",
        "deleted_at",
        "created_at",
    }
    assert (pin["message"]["id"], pin["message"]["pinned"]) == (str(message.id), True)


@pytest.mark.parametrize("method", ["post", "delete"])
async def test_without_a_token_is_401(client, message, method):
    response = await client.request(method.upper(), url(message))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
