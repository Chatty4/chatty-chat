import uuid
from datetime import UTC, datetime

import pytest

from app.core.codes import ErrorCode
from app.core.exceptions import AppError
from app.models import Message
from app.schemas.core import CoreChannel, Memberships, TeamMembership, TeamRole
from app.schemas.messages import ReactionResponse
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
USER = memberships(USER_ID)


def fakes_for(
    room: CoreChannel | None,
    message: Message,
    reactions: list[tuple[uuid.UUID, uuid.UUID, str]] | None = None,
) -> ReactionFakes:
    return ReactionFakes(
        FakeCore(channel=room), FakeMessages(stored=[message]), FakeReactions(reactions)
    )


def message_in(room: CoreChannel) -> Message:
    return saved_message(room.channel_id, OTHER_ID, uuid.uuid4(), seq=3)


async def assert_refused(
    fakes: ReactionFakes, message_id: uuid.UUID, status: int, code: str, user: Memberships = USER
) -> None:
    with pytest.raises(AppError) as error:
        await fakes.service.remove(user, message_id, "👍")
    assert (error.value.status_code, error.value.code) == (status, code)
    assert fakes.events.rows == []
    assert fakes.bus.published == []


async def test_my_reaction_is_removed_with_the_next_seq_and_reaction_removed():
    room = channel(USER_ID, OTHER_ID)
    message = message_in(room)
    fakes = fakes_for(room, message, [(message.id, USER_ID, "👍"), (message.id, OTHER_ID, "👍")])
    fakes.counters.last_seq = 7

    response = await fakes.service.remove(USER, message.id, "👍")

    assert response.reactions == [ReactionResponse(emoji="👍", count=1, me=False)]
    assert fakes.reactions.rows == [(message.id, OTHER_ID, "👍")]  # theirs stays
    [(channel_id, seq, kind, payload)] = fakes.events.rows
    assert (channel_id, seq, kind) == (room.channel_id, 8, "reaction.removed")
    assert payload == {"message_id": str(message.id), "user_id": str(USER_ID), "emoji": "👍"}
    assert fakes.log == ["commit", "commit", "publish", "commit"]  # read, write, publish, list
    [(published_to, event)] = fakes.bus.published
    assert published_to == room.channel_id
    assert (event["type"], event["seq"], event["payload"]) == ("reaction.removed", 8, payload)


async def test_the_last_reaction_leaves_an_empty_list():
    room = channel(USER_ID)
    message = message_in(room)
    fakes = fakes_for(room, message, [(message.id, USER_ID, "👍")])

    response = await fakes.service.remove(USER, message.id, "👍")

    assert response.reactions == []


async def test_a_reaction_i_dont_have_is_a_no_op():
    room = channel(USER_ID, OTHER_ID)
    message = message_in(room)
    fakes = fakes_for(room, message, [(message.id, OTHER_ID, "👍")])

    response = await fakes.service.remove(USER, message.id, "👍")

    assert response.reactions == [ReactionResponse(emoji="👍", count=1, me=False)]
    assert fakes.reactions.rows == [(message.id, OTHER_ID, "👍")]
    assert fakes.log == ["commit", "rollback", "commit"]  # the seq taken is given back
    assert fakes.events.rows == []
    assert fakes.bus.published == []


async def test_only_the_given_emoji_is_removed():
    room = channel(USER_ID)
    message = message_in(room)
    fakes = fakes_for(room, message, [(message.id, USER_ID, "👍"), (message.id, USER_ID, "🎉")])

    response = await fakes.service.remove(USER, message.id, "👍")

    assert response.reactions == [ReactionResponse(emoji="🎉", count=1, me=True)]


async def test_deleted_message_is_200_without_an_event():
    room = channel(USER_ID)
    message = message_in(room)
    message.deleted_at = datetime.now(UTC)
    fakes = fakes_for(room, message)

    response = await fakes.service.remove(USER, message.id, "👍")

    assert response.reactions == []
    assert fakes.events.rows == []
    assert fakes.bus.published == []


async def test_reader_who_is_not_a_member_is_403():
    room = channel(OTHER_ID)
    message = message_in(room)
    # the channel is public in the user's team, so they can read it, but they haven't joined
    team = TeamMembership(
        team_id=room.team_id, role=TeamRole.MEMBER, public_channel_ids=[room.channel_id]
    )
    reader = memberships(USER_ID, teams=[team])

    await assert_refused(
        fakes_for(room, message), message.id, 403, ErrorCode.NOT_A_MEMBER, user=reader
    )


@pytest.mark.parametrize("flags", [{"archived": True}, {"team_deleted": True}])
async def test_archived_channel_is_409_and_keeps_the_reaction(flags):
    room = channel(USER_ID, **flags)
    message = message_in(room)
    fakes = fakes_for(room, message, [(message.id, USER_ID, "👍")])

    await assert_refused(fakes, message.id, 409, ErrorCode.CHANNEL_ARCHIVED)
    assert fakes.reactions.rows == [(message.id, USER_ID, "👍")]


async def test_missing_message_is_404():
    room = channel(USER_ID)

    await assert_refused(
        fakes_for(room, message_in(room)), uuid.uuid4(), 404, ErrorCode.MESSAGE_NOT_FOUND
    )


async def test_message_in_channel_user_cannot_read_is_404():
    room = channel(OTHER_ID, kind="private")
    message = message_in(room)

    await assert_refused(fakes_for(room, message), message.id, 404, ErrorCode.MESSAGE_NOT_FOUND)
