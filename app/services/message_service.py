import asyncio
import logging
import re
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.clients import storage
from app.clients.core import CoreClient
from app.core.codes import ErrorCode
from app.core.exceptions import (
    AppError,
    Conflict,
    InvalidFile,
    MessageAlreadySent,
    NotFound,
    PermissionDenied,
    ValidationFailed,
)
from app.models import Attachment, Message
from app.repositories import (
    ChannelCounterRepository,
    ChannelEventRepository,
    MessageRepository,
    PinRepository,
    ReactionRepository,
    ReadStateRepository,
)
from app.schemas.core import CoreAttachment, Memberships
from app.schemas.events import ChannelEvent, EventType
from app.schemas.messages import (
    AttachmentResponse,
    AttachmentUrlResponse,
    AttachmentUrlsResponse,
    EditMessageRequest,
    MessageHistoryResponse,
    MessageResponse,
    ReactionResponse,
    SendMessageRequest,
)
from app.services.access_service import AccessService
from app.services.event_bus import EventBus

logger = logging.getLogger(__name__)

MENTION = re.compile(r"<@([0-9a-fA-F-]{36})>")

DETACH_ATTEMPTS = 3  # after a message is deleted: the first call and two retries
DETACH_RETRY_SECONDS = 0.2  # times the attempt number: 0.2 s, then 0.4 s


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


async def get_readable_message(
    session: AsyncSession,
    messages: MessageRepository,
    access: AccessService,
    user: Memberships,
    message_id: uuid.UUID,
) -> Message:
    """
    The message, if the user can read the channel. Otherwise 404, so nobody learns it exists.
    """
    async with session.begin():
        message = await messages.get_message(message_id)

    if message is None:
        raise NotFound(ErrorCode.MESSAGE_NOT_FOUND, "Message not found")

    try:
        await access.check_can_read(user, message.channel_id)
    except (NotFound, PermissionDenied) as exc:
        raise NotFound(ErrorCode.MESSAGE_NOT_FOUND, "Message not found") from exc
    return message


def to_reaction_responses(rows: list[tuple[str, int, bool]]) -> list[ReactionResponse]:
    return [ReactionResponse(emoji=emoji, count=count, me=me) for emoji, count, me in rows]


def to_message_response(
    message: Message,
    reactions: list[tuple[str, int, bool]] | None = None,
    pinned: bool = False,
    attachments: list[Attachment] | None = None,
) -> MessageResponse:
    """
    The message object. A deleted message has no files and no reactions (api-chat.md), even if
    rows from before the delete are still there. Attachments come ordered by position.
    """
    files = [] if message.deleted_at else attachments or []
    return MessageResponse(
        id=message.id,
        channel_id=message.channel_id,
        seq=message.seq,
        author_id=message.author_id,
        client_msg_id=message.client_msg_id,
        body=message.body,
        file_ids=[attachment.file_id for attachment in files],
        attachments=[
            AttachmentResponse(
                file_id=attachment.file_id,
                filename=attachment.filename,
                mime=attachment.mime,
                size=attachment.size,
            )
            for attachment in files
        ],
        mentions=list(message.mentions),
        reactions=[] if message.deleted_at else to_reaction_responses(reactions or []),
        pinned=pinned,
        edited_at=message.edited_at,
        deleted_at=message.deleted_at,
        created_at=message.created_at,
    )


def to_attachment_url(attachment: Attachment) -> AttachmentUrlResponse:
    """The file with a download URL signed now, valid 5 minutes (D-02, D-16)."""
    url, expires_at = storage.presign_attachment(
        attachment.object_key, attachment.filename, attachment.mime
    )
    return AttachmentUrlResponse(
        file_id=attachment.file_id,
        filename=attachment.filename,
        mime=attachment.mime,
        size=attachment.size,
        url=url,
        expires_at=expires_at,
    )


class MessageService:
    def __init__(
        self,
        session: AsyncSession,
        messages: MessageRepository,
        counters: ChannelCounterRepository,
        events: ChannelEventRepository,
        read_state: ReadStateRepository,
        reactions: ReactionRepository,
        pins: PinRepository,
        access: AccessService,
        core: CoreClient,
        bus: EventBus,
    ) -> None:
        self._session = session
        self._messages = messages
        self._counters = counters
        self._events = events
        self._read_state = read_state
        self._reactions = reactions
        self._pins = pins
        self._access = access
        self._core = core
        self._bus = bus

    async def send(
        self, user: Memberships, channel_id: uuid.UUID, data: SendMessageRequest
    ) -> tuple[MessageResponse, bool]:
        """Send a message. Returns it and whether it is new (201) or a retry (200)."""
        if not data.body and not data.file_ids:
            raise ValidationFailed(fields={"body": "required"})

        if len(set(data.file_ids)) != len(data.file_ids):
            raise ValidationFailed(
                message="a file is in file_ids twice", fields={"file_ids": "invalid"}
            )

        # Before the access check: a retry returns the saved message even if the channel was
        # archived since. It is the sender's own message, so nothing new is revealed.
        async with self._session.begin():  # ends the read, so the write can begin() its own
            existing = await self._messages.get_by_client_msg_id(user.user_id, data.client_msg_id)
            if existing is not None:
                return await self._retry(existing, user.user_id, channel_id), False

        channel = await self._access.get_writable_channel(user, channel_id)
        mentions = extract_mentions(data.body, {member.user_id for member in channel.members})

        # Before the transaction, so none is held during the call to chatty-core (D-03).
        attached: list[CoreAttachment] = []
        if data.file_ids:
            try:
                attached = await self._core.attach(user.user_id, channel_id, data.file_ids)
            except InvalidFile as exc:
                if exc.reason != "file_already_attached":
                    raise
                # A concurent request with this client_msg_id may have attached them and saved.
                async with self._session.begin():
                    saved = await self._messages.get_by_client_msg_id(
                        user.user_id, data.client_msg_id
                    )
                    if saved is None:
                        raise
                    return await self._retry(saved, user.user_id, channel_id), False

            order = {file_id: index for index, file_id in enumerate(data.file_ids)}
            attached.sort(key=lambda attachment: order[attachment.file_id])

        try:
            async with self._session.begin():
                seq = await self._counters.next_seq(channel_id)
                message = await self._messages.insert_message(
                    channel_id=channel_id,
                    author_id=user.user_id,
                    seq=seq,
                    client_msg_id=data.client_msg_id,
                    body=data.body,
                    mentions=mentions,
                )
                files = await self._messages.insert_attachments(message.id, attached)
                response = to_message_response(message, attachments=files)
                payload = {"message": response.model_dump(mode="json")}

                await self._events.insert_channel_event(
                    channel_id, seq, EventType.MESSAGE_CREATED, payload
                )
                await self._read_state.advance_read_position(user.user_id, channel_id, seq)

        except MessageAlreadySent:
            # A concurrent request with this client_msg_id committed first; this one rolled back.
            try:
                async with self._session.begin():
                    original = await self._messages.get_by_client_msg_id(
                        user.user_id, data.client_msg_id
                    )
                    if original is None:
                        raise  # another constraint failed, not a retry
                    response = await self._retry(original, user.user_id, channel_id)
            finally:
                # the files this request attached aren't in the message that won
                await self._release_unsaved(user.user_id, data.client_msg_id, attached)
            return response, False

        except Exception:
            await self._release_unsaved(user.user_id, data.client_msg_id, attached)
            raise

        await self._bus.publish(
            ChannelEvent(
                type=EventType.MESSAGE_CREATED,
                channel_id=channel_id,
                seq=seq,
                created_at=message.created_at,
                payload=payload,
            )
        )
        return response, True

    async def _release_unsaved(
        self, user_id: uuid.UUID, client_msg_id: uuid.UUID, attached: list[CoreAttachment]
    ) -> None:
        """
        After a failed save: detach the attached files that aren't in a saved message with this
        client_msg_id (D-03). If that can't be read, they stay attached: the outcome is unknown,
        and a file of a sent message must never be detached.
        """
        if not attached:
            return
        try:
            async with self._session.begin():
                saved = await self._messages.get_by_client_msg_id(user_id, client_msg_id)
                files = await self._messages.list_attachments([saved.id] if saved else [])
        except Exception:
            logger.warning("Kept files attached, the save is unknown: %s", client_msg_id)
            return

        kept = {row.file_id for row in files.get(saved.id, [])} if saved else set()
        unused = [file.file_id for file in attached if file.file_id not in kept]
        if unused:
            await self._detach(unused)

    async def _detach(self, file_ids: list[uuid.UUID], attempts: int = 1) -> None:
        """Best effort, tried `attempts` times: if it keeps failing, the files stay attached
        to no message."""
        for attempt in range(1, attempts + 1):
            try:
                await self._core.detach(file_ids)
                return
            except AppError:
                if attempt < attempts:
                    await asyncio.sleep(DETACH_RETRY_SECONDS * attempt)
        logger.warning("Failed to detach files %s", [str(file_id) for file_id in file_ids])

    async def list_history(
        self,
        user: Memberships,
        channel_id: uuid.UUID,
        before_seq: int | None,
        limit: int,
        around_seq: int | None = None,
        after_seq: int | None = None,
    ) -> MessageHistoryResponse:
        """A page of messages, newest first. Deleted ones are included so the order is kept."""
        if sum(seq is not None for seq in (before_seq, around_seq, after_seq)) > 1:
            raise ValidationFailed(
                message="send only one of before_seq, around_seq, after_seq",
                fields={"around_seq": "conflict"},
            )
        await self._access.check_can_read(user, channel_id)

        async with self._session.begin():
            # One snapshot for every read below. Under the default READ COMMITTED, a reaction
            # saved after last_seq was read would be counted in this page and again by its event
            # (seq > last_seq), which the web app then applies on top.
            await self._session.connection(execution_options={"isolation_level": "REPEATABLE READ"})

            last_seq = await self._counters.get_last_seq(channel_id)

            # one extra row on a side tells whether more messages exist there.
            has_more_after = False
            if around_seq is not None:
                older_count = limit // 2
                newer_count = limit - older_count
                newer = await self._messages.list_messages_after(
                    channel_id, around_seq - 1, newer_count + 1
                )
                older = await self._messages.list_messages_before(
                    channel_id, around_seq, older_count + 1
                )
                has_more_after = len(newer) > newer_count
                has_more = len(older) > older_count
                page = newer[:newer_count][::-1] + older[:older_count]
            elif after_seq is not None:
                rows = await self._messages.list_messages_after(channel_id, after_seq, limit + 1)
                has_more_after = len(rows) > limit
                page = rows[:limit][::-1]
                has_more = bool(
                    await self._messages.list_messages_before(channel_id, after_seq + 1, 1)
                )
            else:
                rows = await self._messages.list_messages_before(channel_id, before_seq, limit + 1)
                has_more = len(rows) > limit
                page = rows[:limit]

            message_ids = [message.id for message in page]
            reactions = await self._reactions.list_reactions(message_ids, user.user_id)
            pinned = await self._pins.pinned_message_ids(message_ids)

            attachments = await self._messages.list_attachments(message_ids)
        return MessageHistoryResponse(
            messages=[
                to_message_response(
                    message,
                    reactions.get(message.id),
                    message.id in pinned,
                    attachments.get(message.id),
                )
                for message in page
            ],
            has_more=has_more,
            has_more_after=has_more_after,
            last_seq=last_seq,
        )

    async def _retry(
        self, existing: Message, user_id: uuid.UUID, channel_id: uuid.UUID
    ) -> MessageResponse:
        """
        The saved message for a retry, as it is now: others may have reacted or pinned it since.
        Its client_msg_id in another channel is a client bug. Call inside a transaction.
        """
        if existing.channel_id != channel_id:
            raise ValidationFailed(
                message="client_msg_id was already used for a message in another channel",
                fields={"client_msg_id": "already_used"},
            )
        return await self._full_response(existing, user_id)

    async def _full_response(self, message: Message, user_id: uuid.UUID) -> MessageResponse:
        """The message object with its reactions, pin and files. Call inside a transaction."""
        reactions = await self._reactions.list_reactions([message.id], user_id)
        pinned = await self._pins.pinned_message_ids([message.id])
        attachments = await self._messages.list_attachments([message.id])
        return to_message_response(
            message, reactions.get(message.id), message.id in pinned, attachments.get(message.id)
        )

    async def edit(
        self, user: Memberships, message_id: uuid.UUID, data: EditMessageRequest
    ) -> MessageResponse:
        """Edit the body of my message. Takes the next seq and sends message.edited."""
        message = await self._get_readable_message(user, message_id)
        if message.author_id != user.user_id:
            raise PermissionDenied(ErrorCode.NOT_AUTHOR, "You can only edit your own messages")
        if message.deleted_at is not None:
            raise Conflict(ErrorCode.MESSAGE_DELETED, "The message was deleted")

        if not data.body:  # empty only when the message has files (api-chat.md)
            async with self._session.begin():
                files = await self._messages.list_attachments([message.id])
            if not files:
                raise ValidationFailed(fields={"body": "required"})

        channel = await self._access.get_writable_channel(user, message.channel_id)
        mentions = extract_mentions(data.body, {member.user_id for member in channel.members})

        async with self._session.begin():
            seq = await self._counters.next_seq(message.channel_id)
            updated = await self._messages.update_message_body(message_id, data.body, mentions)
            if updated is None:  # deleted after the check above
                raise Conflict(ErrorCode.MESSAGE_DELETED, "The message was deleted")

            # Inside the transaction: it holds the channel's counter lock, which every reaction
            # and pin change also takes, so these are exactly as of this edit.
            response = await self._full_response(updated, user.user_id)
            payload = {
                "message_id": str(updated.id),
                **response.model_dump(mode="json", include={"body", "mentions", "edited_at"}),
            }
            await self._events.insert_channel_event(
                updated.channel_id, seq, EventType.MESSAGE_EDITED, payload
            )

        assert updated.edited_at is not None  # set by update_message_body
        await self._bus.publish(
            ChannelEvent(
                type=EventType.MESSAGE_EDITED,
                channel_id=updated.channel_id,
                seq=seq,
                created_at=updated.edited_at,
                payload=payload,
            )
        )
        return response

    async def _get_readable_message(self, user: Memberships, message_id: uuid.UUID) -> Message:
        return await get_readable_message(
            self._session, self._messages, self._access, user, message_id
        )

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
                await self._events.clear_message_text(
                    deleted.channel_id,
                    deleted.seq,
                    deleted.id,
                    was_edited=deleted.edited_at is not None,
                )
                await self._reactions.delete_message_reactions(deleted.id)
                await self._pins.delete_message_pin(deleted.id)
                attachments = await self._messages.list_attachments([deleted.id])
                file_ids = [row.file_id for row in attachments.get(deleted.id, [])]

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
        await self._bus.publish(
            ChannelEvent(
                type=EventType.MESSAGE_DELETED,
                channel_id=deleted.channel_id,
                seq=seq,
                created_at=deleted.deleted_at,
                payload=payload,
            )
        )

        if file_ids:  # after the commit (D-03); the delete answers 204 even if this fails
            await self._detach(file_ids, attempts=DETACH_ATTEMPTS)

    async def attachment_urls(
        self, user: Memberships, message_id: uuid.UUID
    ) -> AttachmentUrlsResponse:
        """
        Download URLs for the files of a message, in the order they were sent (D-02).
        404 if it doesn't exist, the user can't read it's channel, or it was deleted.
        """
        message = await self._get_readable_message(user, message_id)
        if message.deleted_at is not None:
            raise NotFound(ErrorCode.MESSAGE_NOT_FOUND, "Message not found")

        async with self._session.begin():
            attachments = await self._messages.list_attachments([message_id])
        files = attachments.get(message.id, [])
        return AttachmentUrlsResponse(attachments=[to_attachment_url(file) for file in files])
