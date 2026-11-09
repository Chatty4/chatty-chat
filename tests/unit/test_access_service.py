import uuid

import pytest

from app.core.codes import ErrorCode
from app.core.exceptions import AppError, Unauthorized, UserInactive
from app.schemas.core import CoreChannel, TeamMembership, TeamRole
from app.services.access_service import AccessService
from tests.fakes import FakeCore, channel, memberships

USER_ID = uuid.uuid4()


async def test_inactive_user_is_refused():
    core = FakeCore(memberships=memberships(USER_ID, is_active=False))

    with pytest.raises(UserInactive):
        await AccessService(core).get_user(USER_ID)


async def test_unknown_user_is_unauthorized():
    with pytest.raises(Unauthorized):
        await AccessService(FakeCore()).get_user(USER_ID)


async def test_public_channel_not_joined_is_readable_but_not_writable():
    public = channel(uuid.uuid4())  # someone else is the only member
    team = TeamMembership(
        team_id=public.team_id, role=TeamRole.MEMBER, public_channel_ids=[public.channel_id]
    )
    user = memberships(USER_ID, teams=[team])
    access = AccessService(FakeCore(channel=public))

    await access.check_can_read(user, public.channel_id)
    with pytest.raises(AppError) as error:
        await access.get_writable_channel(user, public.channel_id)
    assert (error.value.status_code, error.value.code) == (403, ErrorCode.NOT_A_MEMBER)


@pytest.mark.parametrize("flags", [{"archived": True}, {"team_deleted": True}])
async def test_archived_channel_is_readable_but_write_is_409(flags):
    closed = channel(USER_ID, **flags)
    access = AccessService(FakeCore(channel=closed))
    user = memberships(USER_ID)

    await access.check_can_read(user, closed.channel_id)
    with pytest.raises(AppError) as error:
        await access.get_writable_channel(user, closed.channel_id)
    assert (error.value.status_code, error.value.code) == (409, ErrorCode.CHANNEL_ARCHIVED)


async def test_channel_of_other_people_is_403():
    private = channel(uuid.uuid4())

    with pytest.raises(AppError) as error:
        await AccessService(FakeCore(channel=private)).check_can_read(
            memberships(USER_ID), private.channel_id
        )
    assert (error.value.status_code, error.value.code) == (403, ErrorCode.CANNOT_READ_CHANNEL)


def team_of(core_channel: CoreChannel) -> TeamMembership:
    """A membership in the channel's team, from a cache that doesn't list the channel yet."""
    return TeamMembership(team_id=core_channel.team_id, role=TeamRole.MEMBER, public_channel_ids=[])


async def test_public_channel_newer_than_the_cache_is_readable():
    new_public = channel(uuid.uuid4())  # created after members:{user_id} was cached
    user = memberships(USER_ID, teams=[team_of(new_public)])

    await AccessService(FakeCore(channel=new_public)).check_can_read(user, new_public.channel_id)


@pytest.mark.parametrize(
    "flags",
    [
        pytest.param({"kind": "private"}, id="private channel of my team"),
        pytest.param({"kind": "dm"}, id="dm of my team"),
        pytest.param({"team_deleted": True}, id="public channel of a deleted team"),
    ],
)
async def test_fallback_still_refuses_non_members(flags):
    other = channel(uuid.uuid4(), **flags)
    user = memberships(USER_ID, teams=[team_of(other)])

    with pytest.raises(AppError) as error:
        await AccessService(FakeCore(channel=other)).check_can_read(user, other.channel_id)
    assert (error.value.status_code, error.value.code) == (403, ErrorCode.CANNOT_READ_CHANNEL)


async def test_public_channel_of_another_team_is_403():
    elsewhere = channel(uuid.uuid4())
    my_team = TeamMembership(team_id=uuid.uuid4(), role=TeamRole.MEMBER, public_channel_ids=[])
    user = memberships(USER_ID, teams=[my_team])

    with pytest.raises(AppError) as error:
        await AccessService(FakeCore(channel=elsewhere)).check_can_read(user, elsewhere.channel_id)
    assert (error.value.status_code, error.value.code) == (403, ErrorCode.CANNOT_READ_CHANNEL)


async def test_watch_memberships_list_a_public_channel_newer_than_the_cache():
    new_public = channel(uuid.uuid4())
    cached = memberships(USER_ID, teams=[team_of(new_public)])
    access = AccessService(FakeCore(memberships=cached, channel=new_public))

    user = await access.get_user_for_watch(USER_ID, new_public.channel_id)

    assert user.teams[0].public_channel_ids == [new_public.channel_id]
    assert cached.teams[0].public_channel_ids == []  # the cached answer isn't changed


@pytest.mark.parametrize(
    "flags",
    [
        pytest.param({"kind": "private"}, id="private channel of my team"),
        pytest.param({"team_deleted": True}, id="public channel of a deleted team"),
    ],
)
async def test_watch_memberships_dont_list_a_channel_the_user_cant_watch(flags):
    other = channel(uuid.uuid4(), **flags)
    cached = memberships(USER_ID, teams=[team_of(other)])
    access = AccessService(FakeCore(memberships=cached, channel=other))

    user = await access.get_user_for_watch(USER_ID, other.channel_id)

    assert user.teams[0].public_channel_ids == []


async def test_watch_memberships_dont_list_a_public_channel_of_another_team():
    elsewhere = channel(uuid.uuid4())
    my_team = TeamMembership(team_id=uuid.uuid4(), role=TeamRole.MEMBER, public_channel_ids=[])
    access = AccessService(
        FakeCore(memberships=memberships(USER_ID, teams=[my_team]), channel=elsewhere)
    )

    user = await access.get_user_for_watch(USER_ID, elsewhere.channel_id)

    assert user.teams[0].public_channel_ids == []


class NoChannelLookups(FakeCore):
    async def get_channel(self, channel_id: uuid.UUID) -> CoreChannel | None:
        raise AssertionError("asked chatty-core for the channel")


async def test_watch_of_a_listed_channel_doesnt_ask_chatty_core_for_it():
    channel_id = uuid.uuid4()
    team = TeamMembership(
        team_id=uuid.uuid4(), role=TeamRole.MEMBER, public_channel_ids=[channel_id]
    )
    cached = memberships(USER_ID, teams=[team])

    user = await AccessService(NoChannelLookups(memberships=cached)).get_user_for_watch(
        USER_ID, channel_id
    )

    assert user == cached


async def test_unknown_channel_is_404():
    with pytest.raises(AppError) as error:
        await AccessService(FakeCore()).check_can_read(memberships(USER_ID), uuid.uuid4())
    assert (error.value.status_code, error.value.code) == (404, ErrorCode.CHANNEL_NOT_FOUND)


async def test_member_of_open_channel_can_write():
    open_channel = channel(USER_ID)

    result = await AccessService(FakeCore(channel=open_channel)).get_writable_channel(
        memberships(USER_ID), open_channel.channel_id
    )

    assert result == open_channel


def test_a_team_of_the_user_passes_the_team_check():
    team = TeamMembership(team_id=uuid.uuid4(), role=TeamRole.MEMBER, public_channel_ids=[])

    AccessService(FakeCore()).check_in_team(memberships(USER_ID, teams=[team]), team.team_id)


def test_another_team_is_403():
    team = TeamMembership(team_id=uuid.uuid4(), role=TeamRole.MEMBER, public_channel_ids=[])

    with pytest.raises(AppError) as error:
        AccessService(FakeCore()).check_in_team(memberships(USER_ID, teams=[team]), uuid.uuid4())
    assert (error.value.status_code, error.value.code) == (403, ErrorCode.NOT_A_TEAM_MEMBER)
