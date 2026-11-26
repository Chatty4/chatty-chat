import uuid
from typing import NamedTuple

from sqlalchemy import (
    ColumnElement,
    ScalarSelect,
    Uuid,
    and_,
    any_,
    exists,
    func,
    literal,
    or_,
    select,
)
from sqlalchemy.dialects.postgresql import ARRAY, insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.selectable import TableValuedAlias

from app.models import Message, ReadState

UNREAD_CAP = 100  # api-chat.md, GET /unread: the UI shows 99+


class UnreadCounts(NamedTuple):
    channel_id: uuid.UUID
    unread: int
    mentions: int


class ReadPosition(NamedTuple):
    last_read_seq: int
    moved: bool  # True only when the stored position went up


class ReadStateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def advance_read_position(
        self, user_id: uuid.UUID, channel_id: uuid.UUID, seq: int
    ) -> ReadPosition:
        """
        Move the user's read position to seq. It only moves forward.
        Returns the stored position and whether it moved.
        """
        stmt = insert(ReadState).values(user_id=user_id, channel_id=channel_id, last_read_seq=seq)
        stmt = stmt.on_conflict_do_update(
            index_elements=[ReadState.user_id, ReadState.channel_id],
            set_={"last_read_seq": stmt.excluded.last_read_seq, "updated_at": func.now()},
            # no row comes back when the position wouldn't go up
            where=ReadState.last_read_seq < stmt.excluded.last_read_seq,
        ).returning(ReadState.last_read_seq)

        moved_to = (await self._session.execute(stmt)).scalar_one_or_none()
        if moved_to is not None:
            return ReadPosition(moved_to, moved=moved_to > 0)  # a new row at 0 moved nothing

        # The upsert locked the conflicting row, so this reads its current value.
        stmt = select(ReadState.last_read_seq).where(
            ReadState.user_id == user_id, ReadState.channel_id == channel_id
        )
        return ReadPosition((await self._session.execute(stmt)).scalar_one(), moved=False)

    async def count_unread(
        self, user_id: uuid.UUID, channel_ids: list[uuid.UUID]
    ) -> list[UnreadCounts]:
        """
        Unread messages, and unread mentions or replies to my messages, per channel, each capped
        at UNREAD_CAP. Every channel in channel_ids gets a row; one without a read position counts
        from seq 0.
        """
        if not channel_ids:
            return []

        channels = (
            func.unnest(literal(channel_ids, ARRAY(Uuid)))
            .table_valued("channel_id")
            .render_derived(name="c")
        )

        stmt = (
            select(
                channels.c.channel_id,
                self._capped_count(channels, user_id).label("unread"),
                self._capped_count(
                    channels,
                    user_id,
                    or_(
                        literal(user_id, Uuid) == any_(Message.mentions),
                        self._replies_to(user_id),
                    ),
                ).label("mentions"),
            )
            .select_from(channels)
            .outerjoin(
                ReadState,
                and_(
                    ReadState.channel_id == channels.c.channel_id,
                    ReadState.user_id == user_id,
                ),
            )
        )

        rows = (await self._session.execute(stmt)).all()
        return [UnreadCounts(row.channel_id, row.unread, row.mentions) for row in rows]

    async def list_positions(
        self, channel_id: uuid.UUID, user_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, int]:
        """The read positions of these users in one channel. Users without one are left out."""
        if not user_ids:
            return {}
        stmt = select(ReadState.user_id, ReadState.last_read_seq).where(
            ReadState.channel_id == channel_id, ReadState.user_id.in_(user_ids)
        )
        rows = await self._session.execute(stmt)
        return {row.user_id: row.last_read_seq for row in rows}

    @staticmethod
    def _capped_count(
        channels: TableValuedAlias, user_id: uuid.UUID, *extra: ColumnElement[bool]
    ) -> ScalarSelect[int]:
        """
        count(*) over at most UNREAD_CAP rows, so a channel with 50,000 unread messages
        stops after 100 instead of counting all of them.
        """
        rows = (
            select(Message.id)
            .where(
                Message.channel_id == channels.c.channel_id,
                Message.seq > func.coalesce(ReadState.last_read_seq, 0),
                Message.author_id != user_id,
                Message.deleted_at.is_(None),
                *extra,
            )
            .limit(UNREAD_CAP)
            # channels and read_state come from the outer query; don't add them here
            .correlate(channels, ReadState)
            .subquery()
        )
        return select(func.count()).select_from(rows).scalar_subquery()

    @staticmethod
    def _replies_to(user_id: uuid.UUID) -> ColumnElement[bool]:
        """
        The message replies to one of the user's messages. `replied` is a second name for the
        messages table: one row is the reply, the other the message it quotes.
        """
        replied = aliased(Message)
        return exists().where(replied.id == Message.reply_to_id, replied.author_id == user_id)
