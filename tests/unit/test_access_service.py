import uuid

import pytest

from app.core.codes import ErrorCode
from app.core.exceptions import AppError, Unauthorized, UserInactive
from app.schemas.core import TeamMembership
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
        team_id=public.team_id, role="member", public_channel_ids=[public.channel_id]
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
