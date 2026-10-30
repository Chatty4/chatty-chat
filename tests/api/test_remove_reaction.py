import uuid
from urllib.parse import quote

import pytest

from app.api.deps import get_core_client, get_reaction_service
from app.models import Message
from tests.fakes import (
    FakeCore,
    FakeMessages,
    FakeReactions,
    ReactionFakes,
    channel,
    memberships,
    saved_message,
)

USER_ID, OTHER_ID = uuid.uuid4(), uuid.uuid4()


def url(message_id: uuid.UUID, emoji: str) -> str:
    return f"/api/chat/v1/messages/{message_id}/reactions/{quote(emoji)}"


@pytest.fixture
def auth(make_token) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(sub=str(USER_ID))}"}


@pytest.fixture
def message(app) -> Message:
    """A message with my 👍🏽 and someone else's 🎉."""
    room = channel(USER_ID, OTHER_ID)
    message = saved_message(room.channel_id, OTHER_ID, uuid.uuid4())
    core = FakeCore(memberships=memberships(USER_ID), channel=room)
    reactions = FakeReactions([(message.id, USER_ID, "👍🏽"), (message.id, OTHER_ID, "🎉")])
    fakes = ReactionFakes(core, FakeMessages(stored=[message]), reactions)
    app.dependency_overrides[get_core_client] = lambda: core
    app.dependency_overrides[get_reaction_service] = lambda: fakes.service
    return message


async def test_delete_is_200_with_the_reactions_left(client, auth, message):
    response = await client.delete(url(message.id, "👍🏽"), headers=auth)

    assert response.status_code == 200
    assert response.json() == {
        "message_id": str(message.id),
        "reactions": [{"emoji": "🎉", "count": 1, "me": False}],
    }


async def test_delete_of_someone_elses_reaction_changes_nothing(client, auth, message):
    response = await client.delete(url(message.id, "🎉"), headers=auth)

    assert response.status_code == 200
    assert response.json()["reactions"] == [
        {"emoji": "👍🏽", "count": 1, "me": True},
        {"emoji": "🎉", "count": 1, "me": False},
    ]


@pytest.mark.parametrize(
    "emoji", ["a", "👍👍", ":thumbsup:"], ids=["letter", "two emoji", "shortcode"]
)
async def test_not_one_emoji_is_400(client, auth, message, emoji):
    response = await client.delete(url(message.id, emoji), headers=auth)

    assert response.status_code == 400
    error = response.json()["error"]
    assert (error["code"], error["fields"]) == ("validation_error", {"emoji": "invalid"})


async def test_without_a_token_is_401(client, message):
    response = await client.delete(url(message.id, "👍🏽"))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
