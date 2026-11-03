import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.codes import ErrorCode
from app.core.exceptions import Conflict, NotFound
from app.models import Message
from app.repositories import (
    ChannelCounterRepository,
    ChannelEventRepository,
    MessageRepository,
    PinRepository,
    ReactionRepository,
)
from app.schemas.core import Memberships
from app.schemas.events import ChannelEvent, EventType
from app.schemas.pins import PinnedMessage, PinResponse, PinsResponse
from app.services.access_service import AccessService
from app.services.event_bus import EventBus
from app.services.message_service import to_message_response

MAX_PINS = 100  # api-chat.md


class _AlreadyPinned(Exception):
    """Raised inside the transaction to roll back the seq it took. Carries the existing pin."""

    def __init__(self, pin: PinResponse) -> None:
        self.pin = pin


class _NotPinned(Exception):
    """Raised inside the transaction to roll back the seq it took."""


class PinService:
    def __init__(
        self,
        session: AsyncSession,
        messages: MessageRepository,
        pins: PinRepository,
        reactions: ReactionRepository,
        counters: ChannelCounterRepository,
        events: ChannelEventRepository,
        access: AccessService,
        bus: EventBus,
    ) -> None:
        self._session = session
        self._messages = messages
        self._pins = pins
        self._reactions = reactions
        self._counters = counters
        self._events = events
        self._access = access
        self._bus = bus

    async def pin(
        self, user: Memberships, channel_id: uuid.UUID, message_id: uuid.UUID
    ) -> tuple[PinResponse, bool]:
        """Pin a message. Returns the pin and whether it is new (201) or was already there (200)."""
        await self._access.get_writable_channel(user, channel_id)
        message = await self._get_channel_message(channel_id, message_id)
        if message.deleted_at is not None:
            raise Conflict(ErrorCode.MESSAGE_DELETED, "The message was deleted")

        try:
            async with self._session.begin():
                # the counter lock makes two pins in this channel wait for each other,
                # so both can't pass the limit check at 99
                seq = await self._counters.next_seq(channel_id)
                if not await self._messages.lock_undeleted_message(message_id):
                    raise Conflict(ErrorCode.MESSAGE_DELETED, "The message was deleted")

                existing = await self._pins.get_pin(message_id)
                if existing is not None:
                    raise _AlreadyPinned(
                        PinResponse(
                            channel_id=channel_id,
                            message_id=message_id,
                            pinned_by=existing.pinned_by,
                            pinned_at=existing.pinned_at,
                        )
                    )
                if await self._pins.count_pins(channel_id) >= MAX_PINS:
                    raise Conflict(ErrorCode.PIN_LIMIT_REACHED, "The channel already has 100 pins")

                pinned_at = await self._pins.add_pin(channel_id, message_id, user.user_id)
                response = PinResponse(
                    channel_id=channel_id,
                    message_id=message_id,
                    pinned_by=user.user_id,
                    pinned_at=pinned_at,
                )
                payload = response.model_dump(mode="json", exclude={"channel_id"})
                await self._events.insert_channel_event(
                    channel_id, seq, EventType.PIN_ADDED, payload
                )

        except _AlreadyPinned as exc:
            return exc.pin, False  # nothing changed: 200 without an event

        await self._bus.publish(
            ChannelEvent(
                type=EventType.PIN_ADDED,
                channel_id=channel_id,
                seq=seq,
                created_at=pinned_at,
                payload=payload,
            )
        )
        return response, True

    async def unpin(self, user: Memberships, channel_id: uuid.UUID, message_id: uuid.UUID) -> None:
        """Unpin a message. Any member who can write may do it. 204 even if it wasn't pinned."""
        await self._access.get_writable_channel(user, channel_id)
        await self._get_channel_message(channel_id, message_id)

        try:
            async with self._session.begin():
                seq = await self._counters.next_seq(channel_id)
                removed_at = await self._pins.remove_pin(message_id)
                if removed_at is None:
                    raise _NotPinned

                payload = {"message_id": str(message_id), "unpinned_by": str(user.user_id)}
                await self._events.insert_channel_event(
                    channel_id, seq, EventType.PIN_REMOVED, payload
                )

        except _NotPinned:
            return  # nothing changed: 204 without an event

        await self._bus.publish(
            ChannelEvent(
                type=EventType.PIN_REMOVED,
                channel_id=channel_id,
                seq=seq,
                created_at=removed_at,
                payload=payload,
            )
        )

    async def list_pins(self, user: Memberships, channel_id: uuid.UUID) -> PinsResponse:
        """Every pin of the channel, newest first, with the full message objects."""
        await self._access.check_can_read(user, channel_id)

        async with self._session.begin():
            rows = await self._pins.list_pins(channel_id)
            reactions = await self._reactions.list_reactions(
                [message.id for _, message in rows], user.user_id
            )

        return PinsResponse(
            pins=[
                PinnedMessage(
                    pinned_by=pin.pinned_by,
                    pinned_at=pin.pinned_at,
                    message=to_message_response(message, reactions.get(message.id), pinned=True),
                )
                for pin, message in rows
            ]
        )

    async def _get_channel_message(self, channel_id: uuid.UUID, message_id: uuid.UUID) -> Message:
        """The message, if it is in this channel. Otherwise 404 message_not_found."""
        async with self._session.begin():
            message = await self._messages.get_message(message_id)

        if message is None or message.channel_id != channel_id:
            raise NotFound(ErrorCode.MESSAGE_NOT_FOUND, "Message not found")
        return message
