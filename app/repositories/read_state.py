import uuid
from typing import NamedTuple

from sqlalchemy import ColumnElement, ScalarSelect, Uuid, and_, any_, func, literal, select
from sqlalchemy.dialects.postgresql import ARRAY, insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.selectable import TableValuedAlias

from app.models import Message, ReadState

UNREAD_CAP = 100  # api-chat.md, GET /unread: the UI shows 99+


class UnreadCounts(NamedTuple):
    channel_id: uuid.UUID
    unread: int
    mentions: int


class ReadStateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def advance_read_position(
        self, user_id: uuid.UUID, channel_id: uuid.UUID, seq: int
    ) -> int:
        """
        Move the user's read position to seq. It only moves forward.
        Returns the stored position.
        """
        stmt = insert(ReadState).values(user_id=user_id, channel_id=channel_id, last_read_seq=seq)
        stmt = stmt.on_conflict_do_update(
            index_elements=[ReadState.user_id, ReadState.channel_id],
            set_={
                "last_read_seq": func.greatest(
                    ReadState.last_read_seq, stmt.excluded.last_read_seq
                ),
                "updated_at": func.now(),
            },
        ).returning(ReadState.last_read_seq)

        return (await self._session.execute(stmt)).scalar_one()

    async def count_unread(
        self, user_id: uuid.UUID, channel_ids: list[uuid.UUID]
    ) -> list[UnreadCounts]:
        """
        Unread messages and unread mentions per channel, each capped at UNREAD_CAP.
        Every channel in channel_ids gets a row; one without a read position counts from seq 0.
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
                    channels, user_id, literal(user_id, Uuid) == any_(Message.mentions)
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
