import uuid

import pytest

from app.api.deps import get_core_client, get_message_service
from app.models import Message
from app.schemas.core import CoreChannel
from tests.fakes import FakeCore, FakeMessages, MessageFakes, channel, memberships, saved_message

USER_ID, OTHER_ID = uuid.uuid4(), uuid.uuid4()


def url(channel_id: uuid.UUID) -> str:
    return f"/api/chat/v1/channels/{channel_id}/messages"


def use(app, room: CoreChannel | None, stored: list[Message], last_seq: int = 0) -> None:
    core = FakeCore(memberships=memberships(USER_ID), channel=room)
    fakes = MessageFakes(core, FakeMessages(stored=stored))
    fakes.counters.last_seq = last_seq
    app.dependency_overrides[get_core_client] = lambda: core
    app.dependency_overrides[get_message_service] = lambda: fakes.service


@pytest.fixture
def auth(make_token) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(sub=str(USER_ID))}"}


async def test_history_is_200_with_messages_has_more_and_last_seq(app, client, auth):
    room = channel(USER_ID)
    stored = [saved_message(room.channel_id, USER_ID, uuid.uuid4(), seq=s) for s in (1, 2, 3)]
    use(app, room, stored, last_seq=5)

    response = await client.get(url(room.channel_id), params={"limit": 2}, headers=auth)

    assert response.status_code == 200
    data = response.json()
    assert [message["seq"] for message in data["messages"]] == [3, 2]
    assert (data["has_more"], data["last_seq"]) == (True, 5)


@pytest.mark.parametrize(
    "params", [{"limit": 0}, {"limit": 101}, {"limit": "x"}, {"before_seq": 0}]
)
async def test_invalid_query_is_400(app, client, auth, params):
    room = channel(USER_ID)
    use(app, room, [])

    response = await client.get(url(room.channel_id), params=params, headers=auth)

    assert response.status_code == 400
    error = response.json()["error"]
    assert (error["code"], error["fields"]) == ("validation_error", {next(iter(params)): "invalid"})


async def test_private_channel_user_is_not_in_is_403(app, client, auth):
    room = channel(OTHER_ID, kind="private")
    use(app, room, [])

    response = await client.get(url(room.channel_id), headers=auth)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "cannot_read_channel"


async def test_missing_channel_is_404(app, client, auth):
    use(app, None, [])

    response = await client.get(url(uuid.uuid4()), headers=auth)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "channel_not_found"


async def test_no_token_is_401(app, client):
    use(app, channel(USER_ID), [])

    response = await client.get(url(uuid.uuid4()))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


async def test_history_items_have_reactions_with_count_and_me(app, client, auth):
    room = channel(USER_ID, OTHER_ID)
    message = saved_message(room.channel_id, USER_ID, uuid.uuid4())
    core = FakeCore(memberships=memberships(USER_ID), channel=room)
    fakes = MessageFakes(core, FakeMessages(stored=[message]))
    fakes.reactions.rows = [(message.id, OTHER_ID, "👍"), (message.id, USER_ID, "👍")]
    app.dependency_overrides[get_core_client] = lambda: core
    app.dependency_overrides[get_message_service] = lambda: fakes.service

    response = await client.get(url(room.channel_id), headers=auth)

    assert response.status_code == 200
    [item] = response.json()["messages"]
    assert item["reactions"] == [{"emoji": "👍", "count": 2, "me": True}]
