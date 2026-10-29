import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.codes import ErrorCode
from app.core.exceptions import Conflict
from app.repositories import (
    ChannelCounterRepository,
    ChannelEventRepository,
    MessageRepository,
    ReactionRepository,
)
from app.schemas.core import Memberships
from app.schemas.events import ChannelEvent, EventType
from app.schemas.messages import ReactionResponse, ReactionsResponse
from app.services.access_service import AccessService
from app.services.event_bus import EventBus
from app.services.message_service import get_readable_message


class _AlreadyReacted(Exception):
    """Raised inside the transaction to roll back the seq it took."""


class ReactionService:
    def __init__(
        self,
        session: AsyncSession,
        messages: MessageRepository,
        reactions: ReactionRepository,
        counters: ChannelCounterRepository,
        events: ChannelEventRepository,
        access: AccessService,
        bus: EventBus,
    ) -> None:
        self._session = session
        self._messages = messages
        self._reactions = reactions
        self._counters = counters
        self._events = events
        self._access = access
        self._bus = bus

    async def add(self, user: Memberships, message_id: uuid.UUID, emoji: str) -> ReactionsResponse:
        """Add my reaction. Takes the next seq and sends reaction.added, only if it is new."""
        message = await get_readable_message(
            self._session, self._messages, self._access, user, message_id
        )
        if message.deleted_at is not None:
            raise Conflict(ErrorCode.MESSAGE_DELETED, "The message was deleted")
        await self._access.get_writable_channel(user, message.channel_id)

        try:
            async with self._session.begin():
                seq = await self._counters.next_seq(message.channel_id)
                if not await self._messages.lock_undeleted_message(message_id):
                    raise Conflict(ErrorCode.MESSAGE_DELETED, "The message was deleted")

                created_at = await self._reactions.add_reaction(message_id, user.user_id, emoji)
                if created_at is None:
                    raise _AlreadyReacted

                payload = {
                    "message_id": str(message_id),
                    "user_id": str(user.user_id),
                    "emoji": emoji,
                }
                await self._events.insert_channel_event(
                    message.channel_id, seq, EventType.REACTION_ADDED, payload
                )

        except _AlreadyReacted:
            pass  # nothing changed: 200 without an event

        else:
            await self._bus.publish(
                ChannelEvent(
                    type=EventType.REACTION_ADDED,
                    channel_id=message.channel_id,
                    seq=seq,
                    created_at=created_at,
                    payload=payload,
                )
            )

        return await self._list_reactions(message_id, user.user_id)

    async def _list_reactions(self, message_id: uuid.UUID, user_id: uuid.UUID) -> ReactionsResponse:
        async with self._session.begin():
            rows = await self._reactions.list_reactions(message_id, user_id)

        return ReactionsResponse(
            message_id=message_id,
            reactions=[ReactionResponse(emoji=e, count=c, me=me) for e, c, me in rows],
        )
