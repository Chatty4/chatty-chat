import uuid
from datetime import UTC, datetime

import pytest

from app.core.codes import ErrorCode
from app.core.exceptions import AppError
from app.models import Message
from app.schemas.core import CoreChannel, Memberships, TeamMembership, TeamRole
from tests.fakes import FakeCore, FakeMessages, MessageFakes, channel, memberships, saved_message

USER_ID, OTHER_ID = uuid.uuid4(), uuid.uuid4()
USER = memberships(USER_ID)


def fakes_for(room: CoreChannel | None, message: Message, **messages) -> MessageFakes:
    return MessageFakes(FakeCore(channel=room), FakeMessages(stored=[message], **messages))


def message_in(room: CoreChannel, author_id: uuid.UUID = USER_ID) -> Message:
    message = saved_message(room.channel_id, author_id, uuid.uuid4(), seq=3)
    message.mentions = [OTHER_ID]
    return message


def team_member(room: CoreChannel, role: TeamRole, *, can_browse: bool = False) -> Memberships:
    """USER with `role` in the channel's team. `can_browse` lists the channel as public."""
    team = TeamMembership(
        team_id=room.team_id,
        role=role,
        public_channel_ids=[room.channel_id] if can_browse else [],
    )
    return memberships(USER_ID, teams=[team])


def assert_deleted(fakes: MessageFakes, message: Message, user: Memberships) -> None:
    """Soft-deleted with the next seq, and message.deleted published after the commit."""
    assert (message.body, message.mentions, message.seq) == ("", [], 3)
    assert message.deleted_at is not None
    [(channel_id, seq, kind, payload)] = fakes.events.rows
    assert (channel_id, kind) == (message.channel_id, "message.deleted")
    assert seq == fakes.counters.last_seq
    assert payload["message_id"] == str(message.id)
    assert payload["deleted_by"] == str(user.user_id)
    assert set(payload) == {"message_id", "deleted_by", "deleted_at"}
    assert fakes.log == ["commit", "commit", "publish"]  # the read, the write, then publish
    [(_, event)] = fakes.bus.published
    assert (event["type"], event["seq"], event["payload"]) == ("message.deleted", seq, payload)
    assert event["created_at"] == payload["deleted_at"]
    assert fakes.read_state.positions == {}  # deleting doesn't move the read position


async def assert_refused(
    fakes: MessageFakes, user: Memberships, message_id: uuid.UUID, status: int, code: str
) -> None:
    with pytest.raises(AppError) as error:
        await fakes.service.delete(user, message_id)
    assert (error.value.status_code, error.value.code) == (status, code)
    assert fakes.counters.last_seq == 0
    assert fakes.events.rows == []
    assert fakes.bus.published == []


async def test_author_deletes_their_message():
    room = channel(USER_ID, OTHER_ID)
    message = message_in(room)
    fakes = fakes_for(room, message)
    fakes.counters.last_seq = 7

    await fakes.service.delete(USER, message.id)

    assert fakes.counters.last_seq == 8
    assert_deleted(fakes, message, USER)


async def test_channel_owner_deletes_someone_elses_message():
    room = channel(OTHER_ID, owner=USER_ID)
    message = message_in(room, author_id=OTHER_ID)
    fakes = fakes_for(room, message)

    await fakes.service.delete(USER, message.id)

    assert_deleted(fakes, message, USER)


@pytest.mark.parametrize("role", [TeamRole.OWNER, TeamRole.ADMIN])
async def test_team_owner_or_admin_deletes_someone_elses_message(role):
    room = channel(USER_ID, OTHER_ID)
    message = message_in(room, author_id=OTHER_ID)
    user = team_member(room, role)
    fakes = fakes_for(room, message)

    await fakes.service.delete(user, message.id)

    assert_deleted(fakes, message, user)


async def test_team_admin_who_is_not_in_the_public_channel_can_delete():
    room = channel(OTHER_ID)
    message = message_in(room, author_id=OTHER_ID)
    user = team_member(room, TeamRole.ADMIN, can_browse=True)
    fakes = fakes_for(room, message)

    await fakes.service.delete(user, message.id)

    assert_deleted(fakes, message, user)


async def test_other_member_is_403():
    room = channel(USER_ID, OTHER_ID)
    message = message_in(room, author_id=OTHER_ID)
    user = team_member(room, TeamRole.MEMBER)

    await assert_refused(fakes_for(room, message), user, message.id, 403, ErrorCode.FORBIDDEN)


@pytest.mark.parametrize("who", ["channel owner", "team admin"])
async def test_only_the_author_can_delete_in_a_dm(who):
    if who == "channel owner":
        room = channel(OTHER_ID, owner=USER_ID, kind="dm")
        user = USER
    else:
        room = channel(USER_ID, OTHER_ID, kind="dm")
        user = team_member(room, TeamRole.ADMIN)
    message = message_in(room, author_id=OTHER_ID)

    await assert_refused(fakes_for(room, message), user, message.id, 403, ErrorCode.FORBIDDEN)


async def test_author_can_delete_in_a_dm():
    room = channel(USER_ID, OTHER_ID, kind="dm")
    message = message_in(room)
    fakes = fakes_for(room, message)

    await fakes.service.delete(USER, message.id)

    assert_deleted(fakes, message, USER)


async def test_missing_message_is_404():
    room = channel(USER_ID)
    fakes = fakes_for(room, message_in(room))

    await assert_refused(fakes, USER, uuid.uuid4(), 404, ErrorCode.MESSAGE_NOT_FOUND)


async def test_message_in_channel_user_cannot_read_is_404():
    room = channel(OTHER_ID)
    message = message_in(room, author_id=OTHER_ID)

    await assert_refused(
        fakes_for(room, message), USER, message.id, 404, ErrorCode.MESSAGE_NOT_FOUND
    )


@pytest.mark.parametrize("flags", [{"archived": True}, {"team_deleted": True}])
async def test_archived_channel_is_409(flags):
    room = channel(USER_ID, **flags)
    message = message_in(room)

    await assert_refused(
        fakes_for(room, message), USER, message.id, 409, ErrorCode.CHANNEL_ARCHIVED
    )


async def test_permission_is_checked_before_archived():
    room = channel(USER_ID, OTHER_ID, archived=True)
    message = message_in(room, author_id=OTHER_ID)

    await assert_refused(fakes_for(room, message), USER, message.id, 403, ErrorCode.FORBIDDEN)


async def test_already_deleted_message_is_204_without_an_event():
    room = channel(USER_ID)
    message = message_in(room)
    message.deleted_at = datetime.now(UTC)
    fakes = fakes_for(room, message)

    await fakes.service.delete(USER, message.id)

    assert fakes.counters.last_seq == 0
    assert fakes.events.rows == []
    assert fakes.log == ["commit"]  # only the read
    assert fakes.bus.published == []


async def test_message_deleted_during_the_delete_rolls_back_without_an_event():
    room = channel(USER_ID)
    message = message_in(room)
    fakes = fakes_for(room, message, deleted_meanwhile=True)

    await fakes.service.delete(USER, message.id)  # no error: it is deleted either way

    assert fakes.log == ["commit", "rollback"]  # the seq taken in the write is given back
    assert fakes.events.rows == []
    assert fakes.bus.published == []
