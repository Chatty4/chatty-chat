import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.codes import ErrorCode
from app.core.exceptions import BadRequest
from app.repositories import ChannelCounterRepository, ReadStateRepository
from app.schemas.core import Memberships
from app.schemas.read_state import (
    ChannelReadsResponse,
    ChannelUnread,
    MarkReadResponse,
    MemberRead,
    UnreadResponse,
)
from app.schemas.ws import UnreadMessage
from app.services.access_service import AccessService, has_read_receipts
from app.services.event_bus import EventBus


class ReadStateService:
    """Read positions and unread counts (api-chat.md, "Read state and search")."""

    def __init__(
        self,
        session: AsyncSession,
        read_state: ReadStateRepository,
        counters: ChannelCounterRepository,
        access: AccessService,
        bus: EventBus,
    ) -> None:
        self._session = session
        self._read_state = read_state
        self._counters = counters
        self._access = access
        self._bus = bus

    async def mark_read(
        self, user: Memberships, channel_id: uuid.UUID, seq: int
    ) -> MarkReadResponse:
        """Move the read position forward to seq and return the channel's new counts."""
        # chatty-core may be called here: do it before the transaction, not while holding it
        kind = await self._access.check_member(user, channel_id)

        async with self._session.begin():
            # A seq past the channel's last message is lowered to it, so a buggy client
            # can't mark messages that don't exist yet as read.
            last_seq = await self._counters.get_last_seq(channel_id)
            position = await self._read_state.advance_read_position(
                user.user_id, channel_id, min(seq, last_seq)
            )
            # inside the transaction, so the counts see the position written above
            [counts] = await self._read_state.count_unread(user.user_id, [channel_id])

        # After the commit, so no tab shows counts that weren't saved. The tab that sent this
        # request gets it too: a REST request can't tell which socket is its own.
        await self._bus.send_to_user(
            user.user_id,
            UnreadMessage(channel_id=channel_id, unread=counts.unread, mentions=counts.mentions),
        )
        if position.moved and has_read_receipts(kind):
            await self._bus.send_read(channel_id, user.user_id, position.last_read_seq)

        return MarkReadResponse(
            channel_id=channel_id,
            last_read_seq=position.last_read_seq,
            unread=counts.unread,
            mentions=counts.mentions,
        )

    async def list_unread(self, user: Memberships, team_id: uuid.UUID) -> UnreadResponse:
        """Unread counts of every channel the user is in, in one team (the sidebar badges)."""
        self._access.check_in_team(user, team_id)

        channel_ids = [c.channel_id for c in user.channels if c.team_id == team_id]
        counts = await self._read_state.count_unread(user.user_id, channel_ids)

        return UnreadResponse(
            channels=[
                ChannelUnread(channel_id=c.channel_id, unread=c.unread, mentions=c.mentions)
                for c in counts
            ]
        )

    async def list_reads(self, user: Memberships, channel_id: uuid.UUID) -> ChannelReadsResponse:
        """Every member's read position in a private channel or DM (D-19)."""
        channel = await self._access.get_member_channel(user, channel_id)
        if not has_read_receipts(channel.kind):
            raise BadRequest(
                ErrorCode.READ_RECEIPTS_UNAVAILABLE,
                "Read receipts are only in private channels and DMs",
            )

        member_ids = [member.user_id for member in channel.members]
        async with self._session.begin():
            last_seq = await self._counters.get_last_seq(channel_id)
            positions = await self._read_state.list_positions(channel_id, member_ids)

        return ChannelReadsResponse(
            channel_id=channel_id,
            last_seq=last_seq,
            reads=[
                MemberRead(user_id=member_id, last_read_seq=positions.get(member_id, 0))
                for member_id in member_ids
            ],
        )
