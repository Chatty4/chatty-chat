import re
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.codes import ErrorCode
from app.core.exceptions import (
    BadRequest,
    Conflict,
    MessageAlreadySent,
    NotFound,
    PermissionDenied,
    ValidationFailed,
)
from app.models import Message
from app.repositories import (
    ChannelCounterRepository,
    ChannelEventRepository,
    MessageRepository,
    ReadStateRepository,
)
from app.schemas.core import Memberships
from app.schemas.events import ChannelEvent, EventType
from app.schemas.messages import (
    EditMessageRequest,
    MessageHistoryResponse,
    MessageResponse,
    SendMessageRequest,
)
from app.services.access_service import AccessService
from app.services.event_bus import EventBus

MENTION = re.compile(r"<@([0-9a-fA-F-]{36})>")


class _DeletedMeanwhile(Exception):
    """Raised inside the delete transaction to roll back the seq it took."""


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

    async def _publish(
        self,
        event_type: EventType,
        channel_id: uuid.UUID,
        seq: int,
        created_at: datetime,
        payload: dict[str, Any],
    ) -> None:
        """Only after the commit, so nobody gets an event for a change that isn't saved."""
        event = ChannelEvent(
            type=event_type, channel_id=channel_id, seq=seq, created_at=created_at, payload=payload
        )
        await self._bus.publish(channel_id, event.model_dump(mode="json"))

    async def send(
        self, user: Memberships, channel_id: uuid.UUID, data: SendMessageRequest
    ) -> tuple[MessageResponse, bool]:
        """Send a message. Returns it and whether it is new (201) or a retry (200)."""
        if not data.body and not data.file_ids:
            raise ValidationFailed(fields={"body": "required"})

        # Before the access check: a retry returns the saved message even if the channel was
        # archived since. It is the sender's own message, so nothing new is revealed.
        async with self._session.begin():  # ends the read, so the write can begin() its own
            existing = await self._messages.get_by_client_msg_id(user.user_id, data.client_msg_id)
        if existing is not None:
            return self._retry(existing, channel_id), False

        channel = await self._access.get_writable_channel(user, channel_id)
        mentions = extract_mentions(data.body, {member.user_id for member in channel.members})

        try:
            async with self._session.begin():
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
            # A concurrent request with this client_msg_id committed first; this one rolled back.
            original = await self._messages.get_by_client_msg_id(user.user_id, data.client_msg_id)
            if original is None:
                raise  # another constraint failed, not a retry
            return self._retry(original, channel_id), False

        await self._publish(EventType.MESSAGE_CREATED, channel_id, seq, message.created_at, payload)
        return response, True

    async def list_history(
        self, user: Memberships, channel_id: uuid.UUID, before_seq: int | None, limit: int
    ) -> MessageHistoryResponse:
        """A page of messages, newest first. Deleted ones are included so the order is kept."""
        await self._access.check_can_read(user, channel_id)

        async with self._session.begin():
            # last_seq first: a message saved between the two reads then comes in this page and
            # again from /sync (dropped by seq), instead of being missed by both

            last_seq = await self._counters.get_last_seq(channel_id)

            # one extra row tells whether an older page exists.
            rows = await self._messages.list_messages_before(channel_id, before_seq, limit + 1)

        return MessageHistoryResponse(
            messages=[to_message_response(message) for message in rows[:limit]],
            has_more=len(rows) > limit,
            last_seq=last_seq,
        )

    def _retry(self, existing: Message, channel_id: uuid.UUID) -> MessageResponse:
        """The saved message for a retry. Its client_msg_id in another channel is a client bug."""
        if existing.channel_id != channel_id:
            raise ValidationFailed(
                message="client_msg_id was already used for a message in another channel",
                fields={"client_msg_id": "already_used"},
            )
        return to_message_response(existing)

    async def edit(
        self, user: Memberships, message_id: uuid.UUID, data: EditMessageRequest
    ) -> MessageResponse:
        """Edit the body of my message. Takes the next seq and sends message.edited."""
        # TODO(CHAT-212): empty is allowed when the message has files
        if not data.body:
            raise ValidationFailed(fields={"body": "required"})

        message = await self._get_readable_message(user, message_id)
        if message.author_id != user.user_id:
            raise PermissionDenied(ErrorCode.NOT_AUTHOR, "You can only edit your own messages")
        if message.deleted_at is not None:
            raise Conflict(ErrorCode.MESSAGE_DELETED, "The message was deleted")

        channel = await self._access.get_writable_channel(user, message.channel_id)
        mentions = extract_mentions(data.body, {member.user_id for member in channel.members})

        async with self._session.begin():
            seq = await self._counters.next_seq(message.channel_id)
            updated = await self._messages.update_message_body(message_id, data.body, mentions)
            if updated is None:  # deleted after the check above
                raise Conflict(ErrorCode.MESSAGE_DELETED, "The message was deleted")

            response = to_message_response(updated)
            payload = {
                "message_id": str(updated.id),
                **response.model_dump(mode="json", include={"body", "mentions", "edited_at"}),
            }
            await self._events.insert_channel_event(
                updated.channel_id, seq, EventType.MESSAGE_EDITED, payload
            )

        assert updated.edited_at is not None  # set by update_message_body
        await self._publish(
            EventType.MESSAGE_EDITED, updated.channel_id, seq, updated.edited_at, payload
        )
        return response

    async def _get_readable_message(self, user: Memberships, message_id: uuid.UUID) -> Message:
        """
        The message, if the user can read the channel. Otherwise 404, so nobody learns it exists.
        """
        async with self._session.begin():
            message = await self._messages.get_message(message_id)

        if message is None:
            raise NotFound(ErrorCode.MESSAGE_NOT_FOUND, "Message not found")

        try:
            await self._access.check_can_read(user, message.channel_id)
        except (NotFound, PermissionDenied) as exc:
            raise NotFound(ErrorCode.MESSAGE_NOT_FOUND, "Message not found") from exc
        return message

    async def delete(self, user: Memberships, message_id: uuid.UUID) -> None:
        """Soft-delete a message (D-05). Takes the next seq and sends message.deleted."""
        message = await self._get_readable_message(user, message_id)
        await self._access.get_channel_for_delete(user, message.channel_id, message.author_id)
        if message.deleted_at is not None:
            return  # already deleted: 204, no event

        try:
            async with self._session.begin():
                seq = await self._counters.next_seq(message.channel_id)
                deleted = await self._messages.soft_delete_message(message_id)

                if deleted is None:  # deleted by another request after the check above
                    raise _DeletedMeanwhile
                # TODO(CHAT-127): remove the message's pins

                payload = {
                    "message_id": str(deleted.id),
                    "deleted_by": str(user.user_id),
                    **to_message_response(deleted).model_dump(mode="json", include={"deleted_at"}),
                }
                await self._events.insert_channel_event(
                    deleted.channel_id, seq, EventType.MESSAGE_DELETED, payload
                )

        except _DeletedMeanwhile:
            return

        assert deleted.deleted_at is not None  # set by soft_delete_message
        await self._publish(
            EventType.MESSAGE_DELETED, deleted.channel_id, seq, deleted.deleted_at, payload
        )
        # TODO(CHAT-212): detach the message's files through chatty-core (D-03)
