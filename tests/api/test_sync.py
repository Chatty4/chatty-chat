import uuid

import pytest

from app.api.deps import get_core_client, get_sync_service
from app.schemas.core import CoreChannel
from tests.fakes import FakeCore, SyncFakes, channel, memberships, saved_event

USER_ID, OTHER_ID = uuid.uuid4(), uuid.uuid4()


def use(app, room: CoreChannel | None, last_seq: int = 0, *seqs: int) -> None:
    core = FakeCore(memberships=memberships(USER_ID), channel=room)
    stored = [saved_event(room.channel_id, seq) for seq in seqs] if room else []
    fakes = SyncFakes(core, stored, {room.channel_id: last_seq} if room else {})
    app.dependency_overrides[get_core_client] = lambda: core
    app.dependency_overrides[get_sync_service] = lambda: fakes.service


def events_url(channel_id: uuid.UUID) -> str:
    return f"/api/chat/v1/channels/{channel_id}/events"


@pytest.fixture
def auth(make_token) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(sub=str(USER_ID))}"}


async def test_sync_is_200_with_events_per_channel(app, client, auth):
    room = channel(USER_ID)
    use(app, room, 3, 1, 2, 3)

    response = await client.post(
        "/api/chat/v1/sync", json={"cursors": {str(room.channel_id): 1}}, headers=auth
    )

    assert response.status_code == 200
    found = response.json()["channels"][str(room.channel_id)]
    assert [event["seq"] for event in found["events"]] == [2, 3]
    assert set(found["events"][0]) == {"type", "channel_id", "seq", "created_at", "payload"}
    assert (found["last_seq"], found["gap_too_large"]) == (3, False)


async def test_sync_answers_a_channel_the_user_cannot_read_with_an_error(app, client, auth):
    room = channel(OTHER_ID, kind="private")
    use(app, room)

    response = await client.post(
        "/api/chat/v1/sync", json={"cursors": {str(room.channel_id): 0}}, headers=auth
    )

    assert response.status_code == 200
    assert response.json()["channels"] == {str(room.channel_id): {"error": "cannot_read_channel"}}


@pytest.mark.parametrize(
    ("data", "fields"),
    [
        ({}, {"cursors": "required"}),
        ({"cursors": {str(uuid.UUID(int=1)): -1}}, {f"cursors.{uuid.UUID(int=1)}": "invalid"}),
        ({"cursors": {str(uuid.uuid4()): 0 for _ in range(501)}}, {"cursors": "too_long"}),
    ],
    ids=["missing", "negative seq", "501 channels"],
)
async def test_invalid_sync_is_400(app, client, auth, data, fields):
    use(app, channel(USER_ID))

    response = await client.post("/api/chat/v1/sync", json=data, headers=auth)

    assert response.status_code == 400
    error = response.json()["error"]
    assert (error["code"], error["fields"]) == ("validation_error", fields)


async def test_events_is_200_with_has_more(app, client, auth):
    room = channel(USER_ID)
    use(app, room, 3, 1, 2, 3)

    response = await client.get(
        events_url(room.channel_id), params={"after_seq": 1, "limit": 1}, headers=auth
    )

    assert response.status_code == 200
    data = response.json()
    assert [event["seq"] for event in data["events"]] == [2]
    assert (data["last_seq"], data["has_more"]) == (3, True)


async def test_cleaned_up_events_are_409(app, client, auth):
    room = channel(USER_ID)
    use(app, room, 5, 4, 5)

    response = await client.get(events_url(room.channel_id), params={"after_seq": 1}, headers=auth)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "gap_too_large"


@pytest.mark.parametrize(
    ("params", "fields"),
    [({}, {"after_seq": "required"}), ({"after_seq": 0, "limit": 501}, {"limit": "invalid"})],
)
async def test_invalid_events_query_is_400(app, client, auth, params, fields):
    room = channel(USER_ID)
    use(app, room)

    response = await client.get(events_url(room.channel_id), params=params, headers=auth)

    assert response.status_code == 400
    assert response.json()["error"]["fields"] == fields


async def test_no_token_is_401(app, client):
    use(app, channel(USER_ID))

    response = await client.post("/api/chat/v1/sync", json={"cursors": {}})

    assert response.status_code == 401
