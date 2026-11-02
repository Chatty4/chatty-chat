import uuid
from datetime import datetime

from sqlalchemy import delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Message, Pin


class PinRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_pin(self, message_id: uuid.UUID) -> Pin | None:
        return await self._session.get(Pin, message_id)

    async def count_pins(self, channel_id: uuid.UUID) -> int:
        stmt = select(func.count()).select_from(Pin).where(Pin.channel_id == channel_id)
        return (await self._session.execute(stmt)).scalar_one()

    async def add_pin(
        self, channel_id: uuid.UUID, message_id: uuid.UUID, user_id: uuid.UUID
    ) -> datetime:
        """Insert the pin. Return its pinned_at (the database time)."""
        stmt = (
            insert(Pin)
            .values(channel_id=channel_id, message_id=message_id, pinned_by=user_id)
            .returning(Pin.pinned_at)
        )
        return (await self._session.execute(stmt)).scalar_one()

    async def remove_pin(self, message_id: uuid.UUID) -> datetime | None:
        """
        Delete the pin. Returns the database time (the event's created_at),
        or None if it wasn't pinned
        """
        stmt = delete(Pin).where(Pin.message_id == message_id).returning(func.now())
        return await self._session.scalar(stmt)

    async def delete_message_pin(self, message_id: uuid.UUID) -> None:
        """Remove the pin of a deleted message. message.deleted covers it: no event."""
        await self._session.execute(delete(Pin).where(Pin.message_id == message_id))

    async def pinned_message_ids(self, message_ids: list[uuid.UUID]) -> set[uuid.UUID]:
        """Which of these messages are pinned. One query for a whole history page."""
        if not message_ids:
            return set()

        stmt = select(Pin.message_id).where(Pin.message_id.in_(message_ids))
        return set(await self._session.scalars(stmt))

    async def list_pins(self, channel_id: uuid.UUID) -> list[tuple[Pin, Message]]:
        """Every pin of the channel with its message, newest pin first."""
        stmt = (
            select(Pin, Message)
            .join(Message, Message.id == Pin.message_id)
            .where(Pin.channel_id == channel_id)
            .order_by(Pin.pinned_at.desc())
        )
        return [(pin, message) for pin, message in await self._session.execute(stmt)]
