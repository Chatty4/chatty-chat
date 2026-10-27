import uuid

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import MessageAlreadySent
from app.models import Message


class MessageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_message(self, message_id: uuid.UUID) -> Message | None:
        return await self._session.get(Message, message_id)

    async def get_by_client_msg_id(
        self, author_id: uuid.UUID, client_msg_id: uuid.UUID
    ) -> Message | None:
        stmt = select(Message).where(
            Message.author_id == author_id, Message.client_msg_id == client_msg_id
        )
        return await self._session.scalar(stmt)

    async def insert_message(
        self,
        *,
        channel_id: uuid.UUID,
        author_id: uuid.UUID,
        seq: int,
        client_msg_id: uuid.UUID,
        body: str,
        mentions: list[uuid.UUID],
    ) -> Message:
        """Add the message and load what the database fills in (created_at). Raises
        MessageAlreadySent when another request with the same client_msg_id saved it first."""
        message = Message(
            channel_id=channel_id,
            author_id=author_id,
            seq=seq,
            client_msg_id=client_msg_id,
            body=body,
            mentions=mentions,
        )
        self._session.add(message)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise MessageAlreadySent() from exc
        await self._session.refresh(message)
        return message

    async def update_message_body(
        self, message_id: uuid.UUID, body: str, mentions: list[uuid.UUID]
    ) -> Message | None:
        """
        Set the new body and mentions; edited_at is the database time. The UPDATE locks the row,
        and returns None if the message was deleted meanwhile.
        """
        stmt = (
            update(Message)
            .where(Message.id == message_id, Message.deleted_at.is_(None))
            .values(body=body, mentions=mentions, edited_at=func.now())
            .returning(Message)
            .execution_options(populate_existing=True)
        )
        return (await self._session.scalars(stmt)).one_or_none()
