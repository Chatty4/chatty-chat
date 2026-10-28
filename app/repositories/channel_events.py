import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ChannelEvent


class ChannelEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert_channel_event(
        self, channel_id: uuid.UUID, seq: int, event_type: str, payload: dict[str, Any]
    ) -> None:
        """One row of the event history, also the outbox for the relay (published_at is null)."""
        self._session.add(
            ChannelEvent(channel_id=channel_id, seq=seq, type=event_type, payload=payload)
        )
        await self._session.flush()

    async def list_events_after(
        self, channel_id: uuid.UUID, after_seq: int, limit: int
    ) -> list[ChannelEvent]:
        """Oldest first: the events with seq > after_seq"""
        stmt = (
            select(ChannelEvent)
            .where(ChannelEvent.channel_id == channel_id, ChannelEvent.seq > after_seq)
            .order_by(ChannelEvent.seq)
            .limit(limit)
        )
        return list(await self._session.scalars(stmt))
