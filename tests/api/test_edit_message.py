import uuid
from datetime import UTC, datetime

import pytest

from app.api.deps import get_core_client, get_message_service
from app.models import Message
from app.schemas.core import CoreChannel
from tests.fakes import FakeCore, FakeMessages, MessageFakes, channel, memberships, saved_message

USER_ID, OTHER_ID = uuid.uuid4(), uuid.uuid4()


def url(message_id: uuid.UUID) -> str:
    return f"/api/chat/v1/messages/{message_id}"


def use(app, room: CoreChannel | None, message: Message) -> MessageFakes:
    core = FakeCore(memberships=memberships(USER_ID), channel=room)
    fakes = MessageFakes(core, FakeMessages(stored=[message]))
    app.dependency_overrides[get_core_client] = lambda: core
    app.dependency_overrides[get_message_service] = lambda: fakes.service
    return fakes


def message_in(room: CoreChannel, author_id: uuid.UUID = USER_ID) -> Message:
    return saved_message(room.channel_id, author_id, uuid.uuid4(), seq=3)


@pytest.fixture
def auth(make_token) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(sub=str(USER_ID))}"}


async def test_edit_is_200_with_the_message_object(app, client, auth):
    room = channel(USER_ID, OTHER_ID)
    message = message_in(room)
    use(app, room, message)
    body = f"deploy at 6? <@{OTHER_ID}>"

    response = await client.put(url(message.id), json={"body": body}, headers=auth)

    assert response.status_code == 200
    data = response.json()
    assert (data["id"], data["seq"], data["body"]) == (str(message.id), 3, body)
    assert data["mentions"] == [str(OTHER_ID)]
    assert data["edited_at"] is not None
    assert data["deleted_at"] is None


@pytest.mark.parametrize(
    ("data", "fields"),
    [
        ({}, {"body": "required"}),
        ({"body": ""}, {"body": "required"}),
        ({"body": "x" * 4001}, {"body": "too_long"}),
    ],
)
async def test_invalid_body_is_400(app, client, auth, data, fields):
    room = channel(USER_ID)
    message = message_in(room)
    fakes = use(app, room, message)

    response = await client.put(url(message.id), json=data, headers=auth)

    assert response.status_code == 400
    error = response.json()["error"]
    assert (error["code"], error["fields"]) == ("validation_error", fields)
    assert fakes.log == []


async def test_someone_elses_message_is_403_not_author(app, client, auth):
    room = channel(USER_ID, OTHER_ID)
    message = message_in(room, author_id=OTHER_ID)
    use(app, room, message)

    response = await client.put(url(message.id), json={"body": "new"}, headers=auth)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "not_author"


async def test_missing_message_is_404(app, client, auth):
    room = channel(USER_ID)
    use(app, room, message_in(room))

    response = await client.put(url(uuid.uuid4()), json={"body": "new"}, headers=auth)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "message_not_found"


async def test_message_in_channel_user_cannot_read_is_404(app, client, auth):
    room = channel(OTHER_ID)
    message = message_in(room, author_id=OTHER_ID)
    use(app, room, message)

    response = await client.put(url(message.id), json={"body": "new"}, headers=auth)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "message_not_found"


async def test_deleted_message_is_409(app, client, auth):
    room = channel(USER_ID)
    message = message_in(room)
    message.deleted_at = datetime.now(UTC)
    use(app, room, message)

    response = await client.put(url(message.id), json={"body": "new"}, headers=auth)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "message_deleted"


@pytest.mark.parametrize("flags", [{"archived": True}, {"team_deleted": True}])
async def test_archived_channel_is_409(app, client, auth, flags):
    room = channel(USER_ID, **flags)
    message = message_in(room)
    use(app, room, message)

    response = await client.put(url(message.id), json={"body": "new"}, headers=auth)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "channel_archived"


async def test_no_token_is_401(app, client):
    room = channel(USER_ID)
    message = message_in(room)
    use(app, room, message)

    response = await client.put(url(message.id), json={"body": "new"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
