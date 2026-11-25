import uuid
from datetime import datetime

from sqlalchemy import Row, func, select, tuple_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import MessageAlreadySent
from app.models import Attachment, Message
from app.models.message import SEARCH_CONFIG
from app.schemas.core import CoreAttachment

HIGHLIGHT_START = "\x02"
HIGHLIGHT_STOP = "\x03"


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

    async def insert_attachments(
        self, message_id: uuid.UUID, attachments: list[CoreAttachment]
    ) -> list[Attachment]:
        """One row per file, in the order of the list: position is the index (D-03)."""
        rows = [
            Attachment(
                file_id=attachment.file_id,
                message_id=message_id,
                position=position,
                object_key=attachment.object_key,
                filename=attachment.filename,
                mime=attachment.mime,
                size=attachment.size,
            )
            for position, attachment in enumerate(attachments)
        ]
        self._session.add_all(rows)
        await self._session.flush()
        return rows

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

    async def soft_delete_message(self, message_id: uuid.UUID) -> Message | None:
        """
        Clear the body and mentions and set deleted_at (database time).
        The UPDATE locks the row, and returns None if the message was already deleted.
        """
        stmt = (
            update(Message)
            .where(Message.id == message_id, Message.deleted_at.is_(None))
            .values(body="", mentions=[], deleted_at=func.now())
            .returning(Message)
            .execution_options(populate_existing=True)
        )
        return (await self._session.scalars(stmt)).one_or_none()

    async def lock_undeleted_message(self, message_id: uuid.UUID) -> bool:
        """
        Lock the message row until the transaction ends, so it can't be deleted
        meanwhile. False if it is already deleted.
        """
        stmt = (
            select(Message.id)
            .where(Message.id == message_id, Message.deleted_at.is_(None))
            .with_for_update(read=True)
        )
        return await self._session.scalar(stmt) is not None

    async def list_attachments(
        self, message_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, list[Attachment]]:
        """
        The files of the messages, each message's in the order they were sent.
        One query for a whole page; messages without files are missing from the dict.
        """
        if not message_ids:
            return {}

        stmt = (
            select(Attachment)
            .where(Attachment.message_id.in_(message_ids))
            .order_by(Attachment.message_id, Attachment.position)
        )
        result: dict[uuid.UUID, list[Attachment]] = {}
        for attachment in await self._session.scalars(stmt):
            result.setdefault(attachment.message_id, []).append(attachment)
        return result

    async def list_messages_before(
        self, channel_id: uuid.UUID, before_seq: int | None, limit: int
    ) -> list[Message]:
        """Newest first: the messages with seq < before_seq, or the newest ones without it."""
        stmt = select(Message).where(Message.channel_id == channel_id)
        if before_seq is not None:
            stmt = stmt.where(Message.seq < before_seq)
        stmt = stmt.order_by(Message.seq.desc()).limit(limit)
        return list(await self._session.scalars(stmt))

    async def list_messages_after(
        self, channel_id: uuid.UUID, after_seq: int, limit: int
    ) -> list[Message]:
        """Oldest first: the next messages with seq > after_seq."""
        stmt = (
            select(Message)
            .where(Message.channel_id == channel_id, Message.seq > after_seq)
            .order_by(Message.seq)
            .limit(limit)
        )
        return list(await self._session.scalars(stmt))

    async def search_messages(
        self,
        *,
        channel_ids: list[uuid.UUID],
        query: str,
        author_id: uuid.UUID | None,
        before: datetime | None,
        after: datetime | None,
        cursor: tuple[float, datetime, uuid.UUID] | None,
        limit: int,
    ) -> list[Row[Message, float, str]]:
        """Best match first, then newest. Rows of (Message, rank, highlight)."""

        if not channel_ids:
            return []

        tsquery = func.plainto_tsquery(SEARCH_CONFIG, query)
        rank = func.ts_rank(Message.tsv, tsquery)
        highlight = func.ts_headline(
            SEARCH_CONFIG,
            Message.body,
            tsquery,
            f"StartSel={HIGHLIGHT_START}, StopSel={HIGHLIGHT_STOP}",
        )

        stmt = (
            select(Message, rank.label("rank"), highlight.label("highlight"))
            .where(
                Message.tsv.bool_op("@@")(tsquery),
                Message.channel_id.in_(channel_ids),
                Message.deleted_at.is_(None),
            )
            .order_by(rank.desc(), Message.created_at.desc(), Message.id.desc())
            .limit(limit)
        )

        if author_id is not None:
            stmt = stmt.where(Message.author_id == author_id)
        if before is not None:
            stmt = stmt.where(Message.created_at < before)
        if after is not None:
            stmt = stmt.where(Message.created_at > after)

        if cursor is not None:
            stmt = stmt.where(tuple_(rank, Message.created_at, Message.id) < tuple_(*cursor))

        return list((await self._session.execute(stmt)).all())

    async def search_attachments(
        self,
        *,
        channel_ids: list[uuid.UUID],
        query: str,
        author_id: uuid.UUID | None,
        before: datetime | None,
        after: datetime | None,
        cursor: tuple[float, datetime, uuid.UUID] | None,
        limit: int,
    ) -> list[Row[Attachment, Message, float, str]]:
        """Files by name, labels and text; best match first, then newest message.
        Rows of (Attachment, Message, rank, highlight). Filters apply to the message."""
        if not channel_ids:
            return []

        tsquery = func.plainto_tsquery(SEARCH_CONFIG, query)
        rank = func.ts_rank(Attachment.tsv, tsquery)
        # the name split as in the tsv, so a match in "report_q3.pdf" gets its <mark>
        name = func.regexp_replace(Attachment.filename, "[._-]+", " ", "g")
        highlight = func.ts_headline(
            SEARCH_CONFIG,
            func.concat_ws(", ", name, Attachment.labels, Attachment.content),
            tsquery,
            f"StartSel={HIGHLIGHT_START}, StopSel={HIGHLIGHT_STOP}",
        )

        stmt = (
            select(Attachment, Message, rank.label("rank"), highlight.label("highlight"))
            .join(Message, Message.id == Attachment.message_id)
            .where(
                Attachment.tsv.bool_op("@@")(tsquery),
                Message.channel_id.in_(channel_ids),
                Message.deleted_at.is_(None),
            )
            # file_id last: one message can have several matching files
            .order_by(rank.desc(), Message.created_at.desc(), Attachment.file_id.desc())
            .limit(limit)
        )

        if author_id is not None:
            stmt = stmt.where(Message.author_id == author_id)
        if before is not None:
            stmt = stmt.where(Message.created_at < before)
        if after is not None:
            stmt = stmt.where(Message.created_at > after)

        if cursor is not None:
            stmt = stmt.where(
                tuple_(rank, Message.created_at, Attachment.file_id) < tuple_(*cursor)
            )

        return list((await self._session.execute(stmt)).all())

    async def is_deleted(self, message_id: uuid.UUID) -> bool:
        """True if the message was deleted, or isn't there at all."""
        row = (
            await self._session.execute(select(Message.deleted_at).where(Message.id == message_id))
        ).one_or_none()
        return row is None or row.deleted_at is not None
