import re
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.codes import ErrorCode
from app.core.exceptions import BadRequest, MessageAlreadySent, ValidationFailed
from app.models import Message
from app.repositories import (
    ChannelCounterRepository,
    ChannelEventRepository,
    MessageRepository,
    ReadStateRepository,
)
from app.schemas.core import Memberships
from app.schemas.events import ChannelEvent, EventType
from app.schemas.messages import MessageResponse, SendMessageRequest
from app.services.access_service import AccessService
from app.services.event_bus import EventBus

MENTION = re.compile(r"<@([0-9a-fA-F-]{36})>")


def extract_mentions(body: str, member_ids: set[uuid.UUID]) -> list[uuid.UUID]:
    """
    The channel members mentioned in body, each once, in order.
    Mentions of other users stay in the text but not in the list.
    """
    mentions = []
    for match in MENTION.finditer(body):
        try:
            user_id = uuid.UUID(match.group(1))
        except ValueError:
            continue

        if user_id in member_ids and user_id not in mentions:
            mentions.append(user_id)

    return mentions


def to_message_response(message: Message) -> MessageResponse:
    """The message object. Files come with CHAT-212, reactions and pins with CHAT-127."""
    return MessageResponse(
        id=message.id,
        channel_id=message.channel_id,
        seq=message.seq,
        author_id=message.author_id,
        body=message.body,
        file_ids=[],
        attachments=[],
        mentions=list(message.mentions),
        reactions=[],
        pinned=False,
        edited_at=message.edited_at,
        deleted_at=message.deleted_at,
        created_at=message.created_at,
    )


class MessageService:
    def __init__(
        self,
        session: AsyncSession,
        messages: MessageRepository,
        counters: ChannelCounterRepository,
        events: ChannelEventRepository,
        read_state: ReadStateRepository,
        access: AccessService,
        bus: EventBus,
    ) -> None:
        self._session = session
        self._messages = messages
        self._counters = counters
        self._events = events
        self._read_state = read_state
        self._access = access
        self._bus = bus

    async def send(
        self, user: Memberships, channel_id: uuid.UUID, data: SendMessageRequest
    ) -> tuple[MessageResponse, bool]:
        """Send a message. Returns it and whether it is new (201) or a retry (200)."""
        if not data.body and not data.file_ids:
            raise ValidationFailed(fields={"body": "required"})

        channel = await self._access.get_writable_channel(user, channel_id)
        mentions = extract_mentions(data.body, {member.user_id for member in channel.members})

        try:
            async with self._session.begin():
                existing = await self._messages.get_by_client_msg_id(
                    user.user_id, data.client_msg_id
                )
                if existing is not None:
                    return to_message_response(existing), False

                # TODO(CHAT-212): attach the files through chatty-core before saving (D-03)
                if data.file_ids:
                    raise BadRequest(
                        ErrorCode.INVALID_FILE, "File attachments are not available yet"
                    )

                seq = await self._counters.next_seq(channel_id)
                message = await self._messages.insert_message(
                    channel_id=channel_id,
                    author_id=user.user_id,
                    seq=seq,
                    client_msg_id=data.client_msg_id,
                    body=data.body,
                    mentions=mentions,
                )
                response = to_message_response(message)
                payload = {"message": response.model_dump(mode="json")}

                await self._events.insert_channel_event(
                    channel_id, seq, EventType.MESSAGE_CREATED, payload
                )
                await self._read_state.advance_read_position(user.user_id, channel_id, seq)

        except MessageAlreadySent:
            # A concurrent retry committed first. The transaction above rolled back.
            original = await self._messages.get_by_client_msg_id(user.user_id, data.client_msg_id)
            if original is None:
                raise  # another constraint failed, not a retry
            return to_message_response(original), False

        # Only after the commit, so nobody gets an event for a message that isn't saved.
        event = ChannelEvent(
            type=EventType.MESSAGE_CREATED,
            channel_id=channel_id,
            seq=seq,
            created_at=message.created_at,
            payload=payload,
        )
        await self._bus.publish(channel_id, event.model_dump(mode="json"))
        return response, True
