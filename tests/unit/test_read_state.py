import uuid

import pytest

from app.core.codes import ErrorCode
from app.core.exceptions import AppError
from app.schemas.core import ChannelMembership, CoreChannel, Memberships, TeamMembership, TeamRole
from tests.fakes import FakeCore, ReadStateFakes, channel, memberships

USER_ID, OTHER_ID = uuid.uuid4(), uuid.uuid4()
USER = memberships(USER_ID)


def member_of(room: CoreChannel) -> Memberships:
    """USER with the channel in its memberships, as chatty-core lists the channels it is in."""
    user = memberships(USER_ID)
    user.channels = [
        ChannelMembership.model_validate(
            {
                "channel_id": room.channel_id,
                "team_id": room.team_id,
                "kind": room.kind,
                "archived": room.archived,
                "role": "member",
                "notify_level": "mentions",
                "muted": False,
                "hidden": False,
            }
        )
    ]
    return user


async def assert_refused(
    fakes: ReadStateFakes, user: Memberships, channel_id: uuid.UUID, status: int, code: str
) -> None:
    with pytest.raises(AppError) as error:
        await fakes.service.mark_read(user, channel_id, 5)
    assert (error.value.status_code, error.value.code) == (status, code)
    assert fakes.read_state.positions == {}
    assert fakes.log == []  # refused before the transaction


async def test_mark_read_moves_the_position_and_returns_the_counts():
    room = channel(USER_ID)
    fakes = ReadStateFakes(FakeCore(channel=room), last_seq=120, counts={room.channel_id: (4, 1)})

    result = await fakes.service.mark_read(USER, room.channel_id, 112)

    assert result.model_dump() == {
        "channel_id": room.channel_id,
        "last_read_seq": 112,
        "unread": 4,
        "mentions": 1,
    }
    assert fakes.read_state.positions == {(USER_ID, room.channel_id): 112}
    assert fakes.read_state.counted == [(USER_ID, [room.channel_id])]
    assert fakes.log == ["commit"]


async def test_a_seq_past_the_last_message_is_lowered_to_it():
    room = channel(USER_ID)
    fakes = ReadStateFakes(FakeCore(channel=room), last_seq=50)

    result = await fakes.service.mark_read(USER, room.channel_id, 999)

    assert result.last_read_seq == 50
    assert fakes.read_state.positions == {(USER_ID, room.channel_id): 50}


async def test_an_empty_channel_stays_at_0():
    room = channel(USER_ID)
    fakes = ReadStateFakes(FakeCore(channel=room), last_seq=0)

    result = await fakes.service.mark_read(USER, room.channel_id, 5)

    assert result.last_read_seq == 0


async def test_an_older_seq_does_not_move_the_position_back():
    room = channel(USER_ID)
    fakes = ReadStateFakes(FakeCore(channel=room), last_seq=200)

    await fakes.service.mark_read(USER, room.channel_id, 150)
    result = await fakes.service.mark_read(USER, room.channel_id, 112)  # an old tab

    assert result.last_read_seq == 150


async def test_a_member_from_memberships_needs_no_core_channel_call():
    room = channel(USER_ID)
    # chatty-core would answer 404: the channel must come from memberships alone
    fakes = ReadStateFakes(FakeCore(channel=None), last_seq=10)

    result = await fakes.service.mark_read(member_of(room), room.channel_id, 10)

    assert result.last_read_seq == 10


async def test_an_archived_channel_can_be_marked_read():
    room = channel(USER_ID, archived=True)
    fakes = ReadStateFakes(FakeCore(channel=room), last_seq=10)

    result = await fakes.service.mark_read(USER, room.channel_id, 10)

    assert result.last_read_seq == 10


async def test_not_a_member_is_403():
    room = channel(OTHER_ID)
    fakes = ReadStateFakes(FakeCore(channel=room), last_seq=10)

    await assert_refused(fakes, USER, room.channel_id, 403, ErrorCode.NOT_A_MEMBER)


async def test_reading_a_public_channel_without_joining_is_403():
    room = channel(OTHER_ID)
    team = TeamMembership(
        team_id=room.team_id, role=TeamRole.MEMBER, public_channel_ids=[room.channel_id]
    )
    fakes = ReadStateFakes(FakeCore(channel=room), last_seq=10)

    await assert_refused(
        fakes, memberships(USER_ID, teams=[team]), room.channel_id, 403, ErrorCode.NOT_A_MEMBER
    )


async def test_an_unknown_channel_is_404():
    fakes = ReadStateFakes(FakeCore(channel=None), last_seq=10)

    await assert_refused(fakes, USER, uuid.uuid4(), 404, ErrorCode.CHANNEL_NOT_FOUND)
