import uuid

import pytest

from app.core.codes import ErrorCode
from app.core.exceptions import AppError
from app.schemas.core import ChannelMembership, CoreChannel, Memberships, TeamMembership, TeamRole
from tests.fakes import FakeCore, ReadStateFakes, channel, memberships

USER_ID, OTHER_ID = uuid.uuid4(), uuid.uuid4()
USER = memberships(USER_ID)


def joined(channel_id: uuid.UUID, team_id: uuid.UUID) -> ChannelMembership:
    """One channel of the memberships response."""
    return ChannelMembership.model_validate(
        {
            "channel_id": channel_id,
            "team_id": team_id,
            "kind": "public",
            "archived": False,
            "role": "member",
            "notify_level": "mentions",
            "muted": False,
            "hidden": False,
        }
    )


def member_of(room: CoreChannel) -> Memberships:
    """USER with the channel in its memberships, as chatty-core lists the channels it is in."""
    user = memberships(USER_ID)
    user.channels = [joined(room.channel_id, room.team_id)]
    return user


def in_team(team_id: uuid.UUID, *channel_ids: uuid.UUID, public: list[uuid.UUID] | None = None):
    """A team membership, and the user's joined channels in that team."""
    team = TeamMembership(team_id=team_id, role=TeamRole.MEMBER, public_channel_ids=public or [])
    return team, [joined(channel_id, team_id) for channel_id in channel_ids]


async def assert_refused(
    fakes: ReadStateFakes, user: Memberships, channel_id: uuid.UUID, status: int, code: str
) -> None:
    with pytest.raises(AppError) as error:
        await fakes.service.mark_read(user, channel_id, 5)
    assert (error.value.status_code, error.value.code) == (status, code)
    assert fakes.read_state.positions == {}
    assert fakes.log == []  # refused before the transaction, and nothing sent to other tabs


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
    # the other tabs get the counts, only after the commit
    assert fakes.log == ["commit", "send_to_user"]
    assert fakes.bus.sent == [
        (
            USER_ID,
            {"type": "unread", "channel_id": str(room.channel_id), "unread": 4, "mentions": 1},
        )
    ]


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


# list_unread

TEAM_ID, OTHER_TEAM_ID = uuid.uuid4(), uuid.uuid4()


def user_in(*teams_and_channels) -> Memberships:
    user = memberships(USER_ID, teams=[team for team, _ in teams_and_channels])
    user.channels = [c for _, channels in teams_and_channels for c in channels]
    return user


async def test_list_unread_has_every_joined_channel_of_the_team_only():
    a, b, elsewhere = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    user = user_in(in_team(TEAM_ID, a, b), in_team(OTHER_TEAM_ID, elsewhere))
    fakes = ReadStateFakes(FakeCore(), counts={a: (4, 1), elsewhere: (9, 9)})

    result = await fakes.service.list_unread(user, TEAM_ID)

    assert [c.model_dump() for c in result.channels] == [
        {"channel_id": a, "unread": 4, "mentions": 1},
        {"channel_id": b, "unread": 0, "mentions": 0},  # nothing unread still gets a row
    ]
    assert fakes.read_state.counted == [(USER_ID, [a, b])]
    assert fakes.log == []  # read only, no transaction


async def test_public_channels_not_joined_get_no_row():
    a, public = uuid.uuid4(), uuid.uuid4()
    user = user_in(in_team(TEAM_ID, a, public=[public]))
    fakes = ReadStateFakes(FakeCore())

    result = await fakes.service.list_unread(user, TEAM_ID)

    assert [c.channel_id for c in result.channels] == [a]


async def test_a_team_without_joined_channels_is_an_empty_list():
    fakes = ReadStateFakes(FakeCore())

    result = await fakes.service.list_unread(user_in(in_team(TEAM_ID)), TEAM_ID)

    assert result.channels == []


async def test_list_unread_outside_the_team_is_403():
    user = user_in(in_team(OTHER_TEAM_ID, uuid.uuid4()))
    fakes = ReadStateFakes(FakeCore())

    with pytest.raises(AppError) as error:
        await fakes.service.list_unread(user, TEAM_ID)

    assert (error.value.status_code, error.value.code) == (403, ErrorCode.NOT_A_TEAM_MEMBER)
    assert fakes.read_state.counted == []
