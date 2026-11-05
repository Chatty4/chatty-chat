import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.codes import ErrorCode
from app.core.exceptions import PermissionDenied
from app.repositories import ChannelCounterRepository, ReadStateRepository
from app.schemas.core import Memberships
from app.schemas.read_state import ChannelUnread, MarkReadResponse, UnreadResponse
from app.services.access_service import AccessService


class ReadStateService:
    """Read positions and unread counts (api-chat.md, "Read state and search")."""

    def __init__(
        self,
        session: AsyncSession,
        read_state: ReadStateRepository,
        counters: ChannelCounterRepository,
        access: AccessService,
    ) -> None:
        self._session = session
        self._read_state = read_state
        self._counters = counters
        self._access = access

    async def mark_read(
        self, user: Memberships, channel_id: uuid.UUID, seq: int
    ) -> MarkReadResponse:
        """Move the read position forward to seq and return the channel's new counts."""
        # chatty-core may be called here: do it before the transaction, not while holding it
        await self._access.check_member(user, channel_id)

        async with self._session.begin():
            # A seq past the channel's last message is lowered to it, so a buggy client
            # can't mark messages that don't exist yet as read.
            last_seq = await self._counters.get_last_seq(channel_id)
            position = await self._read_state.advance_read_position(
                user.user_id, channel_id, min(seq, last_seq)
            )
            # inside the transaction, so the counts see the position written above
            [counts] = await self._read_state.count_unread(user.user_id, [channel_id])

        # Phase 4: publish the unread message to the user's other tabs here, after the commit

        return MarkReadResponse(
            channel_id=channel_id,
            last_read_seq=position,
            unread=counts.unread,
            mentions=counts.mentions,
        )

    async def list_unread(self, user: Memberships, team_id: uuid.UUID) -> UnreadResponse:
        """Unread counts of every channel the user is in, in one team (the sidebar badges)."""
        if not any(team.team_id == team_id for team in user.teams):
            raise PermissionDenied(ErrorCode.NOT_A_TEAM_MEMBER)

        channel_ids = [c.channel_id for c in user.channels if c.team_id == team_id]
        counts = await self._read_state.count_unread(user.user_id, channel_ids)

        return UnreadResponse(
            channels=[
                ChannelUnread(channel_id=c.channel_id, unread=c.unread, mentions=c.mentions)
                for c in counts
            ]
        )
