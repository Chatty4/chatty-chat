import uuid

import pytest

from app.api.deps import get_core_client, get_read_state_service
from app.schemas.core import CoreChannel
from tests.fakes import FakeCore, ReadStateFakes, channel, memberships

USER_ID, OTHER_ID = uuid.uuid4(), uuid.uuid4()


def url(channel_id: uuid.UUID) -> str:
    return f"/api/chat/v1/channels/{channel_id}/read"


@pytest.fixture
def auth(make_token) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(sub=str(USER_ID))}"}


def use(app, room: CoreChannel | None, **fakes_args) -> ReadStateFakes:
    core = FakeCore(memberships=memberships(USER_ID), channel=room)
    fakes = ReadStateFakes(core, **fakes_args)
    app.dependency_overrides[get_core_client] = lambda: core
    app.dependency_overrides[get_read_state_service] = lambda: fakes.service
    return fakes


async def test_read_is_200_with_the_new_position_and_counts(app, client, auth):
    room = channel(USER_ID)
    use(app, room, last_seq=120, counts={room.channel_id: (4, 1)})

    response = await client.post(url(room.channel_id), json={"seq": 112}, headers=auth)

    assert response.status_code == 200
    assert response.json() == {
        "channel_id": str(room.channel_id),
        "last_read_seq": 112,
        "unread": 4,
        "mentions": 1,
    }


@pytest.mark.parametrize(
    ("data", "fields"),
    [({}, {"seq": "required"}), ({"seq": -1}, {"seq": "invalid"})],
    ids=["missing", "negative"],
)
async def test_invalid_read_is_400(app, client, auth, data, fields):
    room = channel(USER_ID)
    use(app, room)

    response = await client.post(url(room.channel_id), json=data, headers=auth)

    assert response.status_code == 400
    error = response.json()["error"]
    assert (error["code"], error["fields"]) == ("validation_error", fields)


async def test_not_a_member_is_403(app, client, auth):
    room = channel(OTHER_ID)
    use(app, room)

    response = await client.post(url(room.channel_id), json={"seq": 1}, headers=auth)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "not_a_member"


async def test_unknown_channel_is_404(app, client, auth):
    use(app, None)

    response = await client.post(url(uuid.uuid4()), json={"seq": 1}, headers=auth)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "channel_not_found"


async def test_without_a_token_is_401(app, client):
    use(app, None)  # no real Redis or chatty-core in API tests

    response = await client.post(url(uuid.uuid4()), json={"seq": 1})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
