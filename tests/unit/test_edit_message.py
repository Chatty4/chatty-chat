import uuid
from datetime import UTC, datetime

import pytest

from app.core.codes import ErrorCode
from app.core.exceptions import AppError, ValidationFailed
from app.models import Message
from app.schemas.core import CoreChannel, TeamMembership, TeamRole
from app.schemas.messages import EditMessageRequest
from tests.fakes import FakeCore, FakeMessages, MessageFakes, channel, memberships, saved_message

USER_ID, OTHER_ID, OUTSIDER_ID = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
USER = memberships(USER_ID)


def fakes_for(room: CoreChannel | None, message: Message, **messages) -> MessageFakes:
    return MessageFakes(FakeCore(channel=room), FakeMessages(stored=[message], **messages))


def message_in(room: CoreChannel, author_id: uuid.UUID = USER_ID) -> Message:
    return saved_message(room.channel_id, author_id, uuid.uuid4(), seq=3)


async def assert_error(fakes: MessageFakes, message_id: uuid.UUID, status: int, code: str) -> None:
    with pytest.raises(AppError) as error:
        await fakes.service.edit(USER, message_id, EditMessageRequest(body="new"))
    assert (error.value.status_code, error.value.code) == (status, code)
    assert fakes.counters.last_seq == 0
    assert fakes.events.rows == []
    assert fakes.bus.published == []


async def test_author_edits_body_and_mentions():
    room = channel(USER_ID, OTHER_ID)
    message = message_in(room)
    fakes = fakes_for(room, message)
    fakes.counters.last_seq = 7
    body = f"deploy at 6? <@{OTHER_ID}> <@{OUTSIDER_ID}>"

    response = await fakes.service.edit(USER, message.id, EditMessageRequest(body=body))

    assert (response.id, response.body, response.mentions) == (message.id, body, [OTHER_ID])
    assert response.seq == 3  # the message keeps its place; the event takes the new seq
    assert response.edited_at is not None
    payload = {
        "message_id": str(message.id),
        "body": body,
        "mentions": [str(OTHER_ID)],
        "edited_at": response.model_dump(mode="json")["edited_at"],
    }
    assert fakes.events.rows == [(room.channel_id, 8, "message.edited", payload)]
    assert fakes.log == ["commit", "commit", "publish"]  # the read, the write, then publish
    [(channel_id, event)] = fakes.bus.published
    assert channel_id == room.channel_id
    assert (event["type"], event["seq"], event["payload"]) == ("message.edited", 8, payload)
    assert event["created_at"] == payload["edited_at"]
    assert fakes.read_state.positions == {}  # editing doesn't move the read position


async def test_empty_body_is_refused_before_loading_the_message():
    fakes = MessageFakes(FakeCore())

    with pytest.raises(ValidationFailed) as error:
        await fakes.service.edit(USER, uuid.uuid4(), EditMessageRequest(body=""))

    assert error.value.fields == {"body": "required"}
    assert fakes.log == []


async def test_missing_message_is_404():
    room = channel(USER_ID)
    fakes = fakes_for(room, message_in(room))

    await assert_error(fakes, uuid.uuid4(), 404, ErrorCode.MESSAGE_NOT_FOUND)


@pytest.mark.parametrize("core_has_channel", [True, False], ids=["not a member", "no channel"])
async def test_message_in_channel_user_cannot_read_is_404(core_has_channel):
    room = channel(OTHER_ID)
    message = message_in(room, author_id=OTHER_ID)
    fakes = fakes_for(room if core_has_channel else None, message)

    await assert_error(fakes, message.id, 404, ErrorCode.MESSAGE_NOT_FOUND)


async def test_someone_elses_message_is_403():
    room = channel(USER_ID, OTHER_ID)
    message = message_in(room, author_id=OTHER_ID)

    await assert_error(fakes_for(room, message), message.id, 403, ErrorCode.NOT_AUTHOR)


async def test_deleted_message_is_409():
    room = channel(USER_ID)
    message = message_in(room)
    message.deleted_at = datetime.now(UTC)

    await assert_error(fakes_for(room, message), message.id, 409, ErrorCode.MESSAGE_DELETED)


async def test_message_deleted_during_the_edit_is_409_and_rolls_back():
    room = channel(USER_ID)
    message = message_in(room)
    fakes = fakes_for(room, message, deleted_meanwhile=True)

    with pytest.raises(AppError) as error:
        await fakes.service.edit(USER, message.id, EditMessageRequest(body="new"))

    assert (error.value.status_code, error.value.code) == (409, ErrorCode.MESSAGE_DELETED)
    assert fakes.log == ["commit", "rollback"]  # the seq taken in the write is given back
    assert fakes.events.rows == []
    assert fakes.bus.published == []


@pytest.mark.parametrize("flags", [{"archived": True}, {"team_deleted": True}])
async def test_archived_channel_is_409(flags):
    room = channel(USER_ID, **flags)
    message = message_in(room)

    await assert_error(fakes_for(room, message), message.id, 409, ErrorCode.CHANNEL_ARCHIVED)


async def test_author_who_left_a_public_channel_is_403_not_a_member():
    room = channel(OTHER_ID)  # the author is no longer a member
    message = message_in(room)
    team = TeamMembership(
        team_id=room.team_id, role=TeamRole.MEMBER, public_channel_ids=[room.channel_id]
    )
    user = memberships(USER_ID, teams=[team])  # can still read the public channel
    fakes = fakes_for(room, message)

    with pytest.raises(AppError) as error:
        await fakes.service.edit(user, message.id, EditMessageRequest(body="new"))

    assert (error.value.status_code, error.value.code) == (403, ErrorCode.NOT_A_MEMBER)
    assert fakes.bus.published == []
