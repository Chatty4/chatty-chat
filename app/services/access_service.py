import uuid

from app.clients.core import CoreClient
from app.core.codes import ErrorCode
from app.core.exceptions import Conflict, NotFound, PermissionDenied, Unauthorized, UserInactive
from app.schemas.core import ChannelKind, ChannelRole, CoreChannel, Memberships, TeamRole


def _is_member(channel: CoreChannel, user_id: uuid.UUID) -> bool:
    return any(member.user_id == user_id for member in channel.members)


def _can_read(user: Memberships, channel: CoreChannel) -> bool:
    """A channel member, or any team member for a public channel (api-chat.md).

    The channel is a fresh answer from chatty-core. The user's teams come from the same
    members cache as public_channel_ids, so this adds no new staleness.
    """
    if _is_member(channel, user.user_id):
        return True  # members of a deleted team's channels can still read them
    return (
        channel.kind == ChannelKind.PUBLIC
        and not channel.team_deleted
        and any(team.team_id == channel.team_id for team in user.teams)
    )


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
        # Not in the cached lists: the channel may be newer than them (a public channel created
        # after the cache was filled), or of a deleted team, which memberships leave out.
        channel = await self._core.get_channel(channel_id)
        if channel is None:
            raise NotFound(ErrorCode.CHANNEL_NOT_FOUND)
        if not _can_read(user, channel):
            raise PermissionDenied(ErrorCode.CANNOT_READ_CHANNEL)

    def check_in_team(self, user: Memberships, team_id: uuid.UUID) -> None:
        if not any(team.team_id == team_id for team in user.teams):
            raise PermissionDenied(ErrorCode.NOT_A_TEAM_MEMBER)

    async def check_member(self, user: Memberships, channel_id: uuid.UUID) -> None:
        if any(c.channel_id == channel_id for c in user.channels):
            return
        channel = await self._core.get_channel(channel_id)
        if channel is None:
            raise NotFound(ErrorCode.CHANNEL_NOT_FOUND)
        if not _is_member(channel, user.user_id):
            raise PermissionDenied(ErrorCode.NOT_A_MEMBER)

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
