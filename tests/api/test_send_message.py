import uuid

import pytest

from app.api.deps import get_core_client, get_message_service
from app.schemas.core import CoreChannel
from tests.fakes import FakeCore, FakeMessages, MessageFakes, channel, memberships, saved_message

USER_ID, OTHER_ID = uuid.uuid4(), uuid.uuid4()


def url(channel_id: uuid.UUID) -> str:
    return f"/api/chat/v1/channels/{channel_id}/messages"


def use(app, room: CoreChannel | None, messages: FakeMessages | None = None) -> MessageFakes:
    core = FakeCore(memberships=memberships(USER_ID), channel=room)
    fakes = MessageFakes(core, messages)
    app.dependency_overrides[get_core_client] = lambda: core
    app.dependency_overrides[get_message_service] = lambda: fakes.service
    return fakes


@pytest.fixture
def auth(make_token) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(sub=str(USER_ID))}"}


async def test_new_message_is_201_with_the_message_object(app, client, auth):
    room = channel(USER_ID, OTHER_ID)
    use(app, room)
    client_msg_id = uuid.uuid4()
    body = f"deploy at 5? <@{OTHER_ID}>"

    response = await client.post(
        url(room.channel_id),
        json={"client_msg_id": str(client_msg_id), "body": body},
        headers=auth,
    )

    assert response.status_code == 201
    message = response.json()
    assert set(message) == {
        "id",
        "channel_id",
        "seq",
        "author_id",
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
    assert message["channel_id"] == str(room.channel_id)
    assert message["author_id"] == str(USER_ID)
    assert (message["seq"], message["body"], message["mentions"]) == (1, body, [str(OTHER_ID)])
    assert (message["pinned"], message["edited_at"], message["deleted_at"]) == (False, None, None)


async def test_retry_is_200_with_the_original(app, client, auth):
    room = channel(USER_ID)
    client_msg_id = uuid.uuid4()
    original = saved_message(room.channel_id, USER_ID, client_msg_id, seq=4)
    fakes = use(app, room, FakeMessages(existing=original))

    response = await client.post(
        url(room.channel_id),
        json={"client_msg_id": str(client_msg_id), "body": "hi"},
        headers=auth,
    )

    assert response.status_code == 200
    assert (response.json()["id"], response.json()["seq"]) == (str(original.id), 4)
    assert fakes.bus.published == []


async def test_retry_after_the_channel_was_archived_is_200(app, client, auth):
    room = channel(USER_ID, archived=True)
    client_msg_id = uuid.uuid4()
    original = saved_message(room.channel_id, USER_ID, client_msg_id)
    use(app, room, FakeMessages(existing=original))

    response = await client.post(
        url(room.channel_id),
        json={"client_msg_id": str(client_msg_id), "body": "hi"},
        headers=auth,
    )

    assert response.status_code == 200
    assert response.json()["id"] == str(original.id)


async def test_client_msg_id_used_in_another_channel_is_400(app, client, auth):
    room = channel(USER_ID)
    client_msg_id = uuid.uuid4()
    use(app, room, FakeMessages(existing=saved_message(uuid.uuid4(), USER_ID, client_msg_id)))

    response = await client.post(
        url(room.channel_id),
        json={"client_msg_id": str(client_msg_id), "body": "hi"},
        headers=auth,
    )

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["fields"] == {"client_msg_id": "already_used"}


@pytest.mark.parametrize(
    ("data", "fields"),
    [
        ({"body": "hi"}, {"client_msg_id": "required"}),
        ({"client_msg_id": "not-a-uuid", "body": "hi"}, {"client_msg_id": "invalid"}),
        ({"client_msg_id": str(uuid.uuid4())}, {"body": "required"}),
        ({"client_msg_id": str(uuid.uuid4()), "body": ""}, {"body": "required"}),
        ({"client_msg_id": str(uuid.uuid4()), "body": "x" * 4001}, {"body": "too_long"}),
        (
            {"client_msg_id": str(uuid.uuid4()), "file_ids": [str(uuid.uuid4())] * 11},
            {"file_ids": "too_long"},
        ),
    ],
)
async def test_invalid_request_is_400(app, client, auth, data, fields):
    room = channel(USER_ID)
    fakes = use(app, room)

    response = await client.post(url(room.channel_id), json=data, headers=auth)

    assert response.status_code == 400
    error = response.json()["error"]
    assert (error["code"], error["fields"]) == ("validation_error", fields)
    assert fakes.log == []


async def test_body_of_4000_chars_is_accepted(app, client, auth):
    room = channel(USER_ID)
    use(app, room)

    response = await client.post(
        url(room.channel_id),
        json={"client_msg_id": str(uuid.uuid4()), "body": "x" * 4000},
        headers=auth,
    )

    assert response.status_code == 201


async def test_files_are_400_invalid_file(app, client, auth):
    room = channel(USER_ID)
    use(app, room)

    response = await client.post(
        url(room.channel_id),
        json={"client_msg_id": str(uuid.uuid4()), "file_ids": [str(uuid.uuid4())]},
        headers=auth,
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_file"


@pytest.mark.parametrize(
    ("room", "status", "code"),
    [
        (None, 404, "channel_not_found"),
        (channel(OTHER_ID), 403, "not_a_member"),
        (channel(USER_ID, archived=True), 409, "channel_archived"),
        (channel(USER_ID, team_deleted=True), 409, "channel_archived"),
    ],
)
async def test_user_who_cannot_write_gets_the_contract_error(app, client, auth, room, status, code):
    use(app, room)

    response = await client.post(
        url(uuid.uuid4()), json={"client_msg_id": str(uuid.uuid4()), "body": "hi"}, headers=auth
    )

    assert response.status_code == status
    assert response.json()["error"]["code"] == code


async def test_no_token_is_401(app, client):
    room = channel(USER_ID)
    use(app, room)

    response = await client.post(
        url(room.channel_id), json={"client_msg_id": str(uuid.uuid4()), "body": "hi"}
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
