import uuid

import pytest

from app.api.deps import get_core_client, get_read_state_service
from app.schemas.core import ChannelMembership, CoreChannel, Memberships, TeamMembership, TeamRole
from tests.fakes import FakeCore, ReadStateFakes, channel, memberships

USER_ID, OTHER_ID = uuid.uuid4(), uuid.uuid4()


def url(channel_id: uuid.UUID) -> str:
    return f"/api/chat/v1/channels/{channel_id}/read"


@pytest.fixture
def auth(make_token) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(sub=str(USER_ID))}"}


def use(
    app, room: CoreChannel | None, user: Memberships | None = None, **fakes_args
) -> ReadStateFakes:
    core = FakeCore(memberships=user or memberships(USER_ID), channel=room)
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


# GET /unread

UNREAD_URL = "/api/chat/v1/unread"
TEAM_ID = uuid.uuid4()


def team_member(*channel_ids: uuid.UUID) -> Memberships:
    """USER in TEAM_ID, joined to the given channels of it."""
    user = memberships(
        USER_ID,
        teams=[TeamMembership(team_id=TEAM_ID, role=TeamRole.MEMBER, public_channel_ids=[])],
    )
    user.channels = [
        ChannelMembership.model_validate(
            {
                "channel_id": channel_id,
                "team_id": TEAM_ID,
                "kind": "public",
                "archived": False,
                "role": "member",
                "notify_level": "mentions",
                "muted": False,
                "hidden": False,
            }
        )
        for channel_id in channel_ids
    ]
    return user


async def test_unread_is_200_with_a_row_per_channel(app, client, auth):
    a, b = uuid.uuid4(), uuid.uuid4()
    use(app, None, team_member(a, b), counts={a: (100, 2)})

    response = await client.get(UNREAD_URL, params={"team_id": str(TEAM_ID)}, headers=auth)

    assert response.status_code == 200
    assert response.json() == {
        "channels": [
            {"channel_id": str(a), "unread": 100, "mentions": 2},
            {"channel_id": str(b), "unread": 0, "mentions": 0},
        ]
    }


@pytest.mark.parametrize(
    ("params", "fields"),
    [({}, {"team_id": "required"}), ({"team_id": "x"}, {"team_id": "invalid"})],
    ids=["missing", "not a uuid"],
)
async def test_invalid_unread_query_is_400(app, client, auth, params, fields):
    use(app, None, team_member())

    response = await client.get(UNREAD_URL, params=params, headers=auth)

    assert response.status_code == 400
    error = response.json()["error"]
    assert (error["code"], error["fields"]) == ("validation_error", fields)


async def test_unread_outside_the_team_is_403(app, client, auth):
    use(app, None, team_member())

    response = await client.get(UNREAD_URL, params={"team_id": str(uuid.uuid4())}, headers=auth)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "not_a_team_member"


async def test_unread_without_a_token_is_401(app, client):
    use(app, None)

    response = await client.get(UNREAD_URL, params={"team_id": str(TEAM_ID)})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
