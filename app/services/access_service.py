import uuid

from app.clients.core import CoreClient
from app.core.codes import ErrorCode
from app.core.exceptions import Conflict, NotFound, PermissionDenied, Unauthorized, UserInactive
from app.schemas.core import ChannelKind, ChannelRole, CoreChannel, Memberships, TeamRole


def _is_member(channel: CoreChannel, user_id: uuid.UUID) -> bool:
    return any(member.user_id == user_id for member in channel.members)


def _can_delete_others(user: Memberships, channel: CoreChannel) -> bool:
    """D-05: the channel owner or a team owner/admin. Never in a DM."""
    if channel.kind == ChannelKind.DM:
        return False

    if any(
        member.user_id == user.user_id and member.role == ChannelRole.OWNER
        for member in channel.members
    ):
        return True

    return any(
        team.team_id == channel.team_id and team.role in (TeamRole.OWNER, TeamRole.ADMIN)
        for team in user.teams
    )


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

    async def get_channel_for_delete(
        self, user: Memberships, channel_id: uuid.UUID, author_id: uuid.UUID
    ) -> CoreChannel:
        """The author, or someone who may delete others' messages (D-05), in an open channel."""
        channel = await self._core.get_channel(channel_id)
        if channel is None:
            raise NotFound(ErrorCode.MESSAGE_NOT_FOUND, "Message not found")
        if author_id != user.user_id and not _can_delete_others(user, channel):
            raise PermissionDenied(ErrorCode.FORBIDDEN, "You can't delete this message")
        if channel.archived or channel.team_deleted:
            raise Conflict(ErrorCode.CHANNEL_ARCHIVED)
        return channel
