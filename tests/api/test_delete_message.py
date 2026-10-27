import uuid
from datetime import UTC, datetime

import pytest

from app.api.deps import get_core_client, get_message_service
from app.models import Message
from app.schemas.core import CoreChannel, Memberships, TeamMembership, TeamRole
from tests.fakes import FakeCore, FakeMessages, MessageFakes, channel, memberships, saved_message

USER_ID, OTHER_ID = uuid.uuid4(), uuid.uuid4()


def url(message_id: uuid.UUID) -> str:
    return f"/api/chat/v1/messages/{message_id}"


def use(
    app, room: CoreChannel | None, message: Message, user: Memberships | None = None
) -> MessageFakes:
    core = FakeCore(memberships=user or memberships(USER_ID), channel=room)
    fakes = MessageFakes(core, FakeMessages(stored=[message]))
    app.dependency_overrides[get_core_client] = lambda: core
    app.dependency_overrides[get_message_service] = lambda: fakes.service
    return fakes


def message_in(room: CoreChannel, author_id: uuid.UUID = USER_ID) -> Message:
    return saved_message(room.channel_id, author_id, uuid.uuid4(), seq=3)


@pytest.fixture
def auth(make_token) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(sub=str(USER_ID))}"}


async def test_author_delete_is_204_without_a_body(app, client, auth):
    room = channel(USER_ID)
    message = message_in(room)
    fakes = use(app, room, message)

    response = await client.delete(url(message.id), headers=auth)

    assert response.status_code == 204
    assert response.content == b""
    assert len(fakes.bus.published) == 1


async def test_team_admin_delete_is_204(app, client, auth):
    room = channel(USER_ID, OTHER_ID)
    message = message_in(room, author_id=OTHER_ID)
    team = TeamMembership(team_id=room.team_id, role=TeamRole.ADMIN, public_channel_ids=[])
    use(app, room, message, memberships(USER_ID, teams=[team]))

    response = await client.delete(url(message.id), headers=auth)

    assert response.status_code == 204


async def test_already_deleted_is_204(app, client, auth):
    room = channel(USER_ID)
    message = message_in(room)
    message.deleted_at = datetime.now(UTC)
    fakes = use(app, room, message)

    response = await client.delete(url(message.id), headers=auth)

    assert response.status_code == 204
    assert fakes.bus.published == []


@pytest.mark.parametrize("kind", ["public", "dm"])
async def test_someone_elses_message_is_403_forbidden(app, client, auth, kind):
    room = channel(USER_ID, OTHER_ID, kind=kind)
    message = message_in(room, author_id=OTHER_ID)
    use(app, room, message)

    response = await client.delete(url(message.id), headers=auth)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


async def test_missing_message_is_404(app, client, auth):
    room = channel(USER_ID)
    use(app, room, message_in(room))

    response = await client.delete(url(uuid.uuid4()), headers=auth)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "message_not_found"


async def test_message_in_channel_user_cannot_read_is_404(app, client, auth):
    room = channel(OTHER_ID)
    message = message_in(room, author_id=OTHER_ID)
    use(app, room, message)

    response = await client.delete(url(message.id), headers=auth)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "message_not_found"


@pytest.mark.parametrize("flags", [{"archived": True}, {"team_deleted": True}])
async def test_archived_channel_is_409(app, client, auth, flags):
    room = channel(USER_ID, **flags)
    message = message_in(room)
    use(app, room, message)

    response = await client.delete(url(message.id), headers=auth)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "channel_archived"


async def test_no_token_is_401(app, client):
    room = channel(USER_ID)
    message = message_in(room)
    use(app, room, message)

    response = await client.delete(url(message.id))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
