import uuid
from urllib.parse import quote

import pytest

from app.api.deps import get_core_client, get_reaction_service
from app.models import Message
from tests.fakes import FakeCore, FakeMessages, ReactionFakes, channel, memberships, saved_message

USER_ID = uuid.uuid4()


def url(message_id: uuid.UUID, emoji: str) -> str:
    return f"/api/chat/v1/messages/{message_id}/reactions/{quote(emoji)}"


@pytest.fixture
def auth(make_token) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(sub=str(USER_ID))}"}


@pytest.fixture
def message(app) -> Message:
    room = channel(USER_ID)
    message = saved_message(room.channel_id, USER_ID, uuid.uuid4())
    core = FakeCore(memberships=memberships(USER_ID), channel=room)
    fakes = ReactionFakes(core, FakeMessages(stored=[message]))
    app.dependency_overrides[get_core_client] = lambda: core
    app.dependency_overrides[get_reaction_service] = lambda: fakes.service
    return message


@pytest.mark.parametrize(
    "emoji", ["👍", "👍🏽", "👨‍👩‍👧", "🇷🇴", "❤️"], ids=["plain", "skin tone", "zwj", "flag", "fe0f"]
)
async def test_put_is_200_with_the_reactions(client, auth, message, emoji):
    response = await client.put(url(message.id, emoji), headers=auth)

    assert response.status_code == 200
    assert response.json() == {
        "message_id": str(message.id),
        "reactions": [{"emoji": emoji, "count": 1, "me": True}],
    }


@pytest.mark.parametrize(
    "emoji",
    ["a", "👍👍", "👍 ", ":thumbsup:", "👨‍👩‍👧‍👦" * 2],
    ids=["letter", "two emoji", "trailing space", "shortcode", "over 32 bytes"],
)
async def test_not_one_emoji_is_400(client, auth, message, emoji):
    response = await client.put(url(message.id, emoji), headers=auth)

    assert response.status_code == 400
    error = response.json()["error"]
    assert (error["code"], error["fields"]) == ("validation_error", {"emoji": "invalid"})


async def test_without_a_token_is_401(client, message):
    response = await client.put(url(message.id, "👍"))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
