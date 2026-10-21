import uuid

from app.clients.core import CoreClient
from app.core.codes import ErrorCode
from app.core.exceptions import Conflict, NotFound, PermissionDenied, Unauthorized, UserInactive
from app.schemas.core import CoreChannel, Memberships


def _is_member(channel: CoreChannel, user_id: uuid.UUID) -> bool:
    return any(member.user_id == user_id for member in channel.members)


class AccessService:
    """(api-chat.md, "Who can read and write a channel")."""

    def __init__(self, core: CoreClient) -> None:
        self._core = core

    async def get_user(self, user_id: uuid.UUID) -> Memberships:
        memberships = await self._core.get_memberships(user_id)
        if memberships is None:  # the token is valid but the user no longer exists
            raise Unauthorized()
        if not memberships.is_active:
            raise UserInactive()
        return memberships

    async def check_can_read(self, user: Memberships, channel_id: uuid.UUID) -> None:
        if any(c.channel_id == channel_id for c in user.channels):
            return
        if any(channel_id in team.public_channel_ids for team in user.teams):
            return
        channel = await self._core.get_channel(channel_id)
        if channel is None:
            raise NotFound(ErrorCode.CHANNEL_NOT_FOUND)
        # memberships leaves out deleted teams, but their members can still read
        if not _is_member(channel, user.user_id):
            raise PermissionDenied(ErrorCode.CANNOT_READ_CHANNEL)

    async def get_writable_channel(self, user: Memberships, channel_id: uuid.UUID) -> CoreChannel:
        channel = await self._core.get_channel(channel_id)
        if channel is None:
            raise NotFound(ErrorCode.CHANNEL_NOT_FOUND)
        if not _is_member(channel, user.user_id):
            raise PermissionDenied(ErrorCode.NOT_A_MEMBER)
        if channel.archived or channel.team_deleted:
            raise Conflict(ErrorCode.CHANNEL_ARCHIVED)
        return channel
